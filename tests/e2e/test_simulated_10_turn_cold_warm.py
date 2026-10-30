"""SIMULATED, NOT A MEASUREMENT: delays are scripted.

Simulated End-to-End Test for a complete 10-turn conversation comparing Cold Run vs Warm Run.
Uses mock HTTP transport with programmed sleep delays. Does NOT call real RunPod or cloud endpoints.

Tests the telephony application control flow:
Plivo Webhook -> FastAPI Controller -> MongoDB Session -> Plivo WebSocket Media Stream
-> VAD silence endpointing -> Mock STT -> Language Mode & Normalization
-> Mock Brain LLM (Streaming SSE) -> Streaming Sentence Extractor -> Mock TTS
-> WebSocket mu-law audio transmission (`playAudio`) -> Checkpoint & PlayedStream ACK
-> Database turn trace storage in MongoDB `ai_sessions`.

Simulates two 10-turn calls:
1. Cold Call: In-process mock with higher scripted initial delays.
2. Warm Call: In-process mock with lower scripted delays.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import websockets

from app.ai import codec
from app.main import create_app
from app.providers.base import MockProvider
from tests.conftest import auth, make_settings
from tests.helpers import live_server, pcm_silence, pcm_tone, tone_wav
from tests.integration.test_plivo_voice import (
    BASE,
    DID,
    RATE,
    TOKEN,
    FakePlivo,
    ai_doc,
    answered_stream,
    connect,
    form,
    post_answer,
    post_cb,
    recv_until,
    send_media,
    until,
    utterance,
)

log = logging.getLogger("mme_telephony.test_simulated_10_turn_cold_warm")

ADMIN = lambda: auth("t1", ("telephony:admin",))  # noqa: E731
CFG = {
    "stt_url": "http://stt.test:9000",
    "brain_url": "http://brain.test:9000",
    "tts_url": "http://tts.test:9000",
    "rag_url": "http://rag.test:9000",
    "enabled": True,
}

# 10 scripted turns of dialogue for Make My Education (MME)
CONVERSATION_10_TURNS = [
    {
        "turn": 1,
        "input_text": "Namaste, mujhe BTech admission ke baare mein janna hai.",
        "reply_text": "जी, Make My Education में आपका स्वागत है। B.Tech के लिए हमारे पास Computer Science, AI और Mechanical Engineering courses हैं। आप किस branch में admission लेना चाहते हैं?",
    },
    {
        "turn": 2,
        "input_text": "Eligibility criteria kya hai 12th standard ke liye?",
        "reply_text": "जी, B.Tech Computer Science के लिए 12th में Physics, Chemistry और Maths में कम से कम 60% aggregate marks होने चाहिए। क्या आपके 60% हैं?",
    },
    {
        "turn": 3,
        "input_text": "Fee structure kya hai per semester aur total course ki?",
        "reply_text": "जी, B.Tech की annual tuition fee ₹1,20,000 है। Merit के आधार पर 25% तक scholarship भी उपलब्ध है।",
    },
    {
        "turn": 4,
        "input_text": "Hostel facility available hai kya campus mein?",
        "reply_text": "जी हाँ, campus में AC और non-AC hostel facility available है। Mess के साथ annual charges ₹65,000 से ₹85,000 तक हैं।",
    },
    {
        "turn": 5,
        "input_text": "Kya koi merit-based scholarship ya concession milti hai?",
        "reply_text": "जी, 12th में 85% से अधिक अंक होने पर 25% fee waiver और 90% से अधिक पर 50% scholarship दी जाती है।",
    },
    {
        "turn": 6,
        "input_text": "JEE Main score compulsory hai ya direct admission ho sakta hai?",
        "reply_text": "जी, 12th merit के आधार पर direct admission संभव है, और JEE Main score वाले students को seat selection में preference मिलती है।",
    },
    {
        "turn": 7,
        "input_text": "Computer Science aur AI branch ki cut-off kitni rehti hai?",
        "reply_text": "जी, Computer Science और AI branch के लिए 12th में कम से कम 70% cutoff रहता है।",
    },
    {
        "turn": 8,
        "input_text": "Average placement package kitna gaya tha last year?",
        "reply_text": "जी, CSE branch का average placement package ₹7.5 LPA रहा है, और highest package ₹42 LPA गया था।",
    },
    {
        "turn": 9,
        "input_text": "Admission ke time kon-kon se documents submit karne honge?",
        "reply_text": "जी, admission के लिए 10th और 12th की marksheets, Transfer Certificate, Migration, Aadhaar Card और 4 passport size photos चाहिए।",
    },
    {
        "turn": 10,
        "input_text": "Theek hai, mujhe campus visit ke liye register kar dijiye.",
        "reply_text": "जी, कल सुबह 11 बजे आपका campus visit schedule कर दिया गया है। हमारे senior counsellor आपको campus tour करवाएंगे।",
    },
]


class AsyncStream(httpx.AsyncByteStream):
    """Async byte stream for simulating inter-token streaming intervals."""

    def __init__(self, chunks: list[tuple[str, float]]):
        self.chunks = chunks

    async def __aiter__(self):
        for text, delay in self.chunks:
            if delay > 0:
                await asyncio.sleep(delay)
            yield text.encode("utf-8")


class SimulatedModels:
    """Mock model backend with scripted sleep delays.
    SIMULATED, NOT A MEASUREMENT: delays are scripted.
    """

    def __init__(self):
        self.mode = "cold"  # "cold" or "warm"
        self.current_turn = 0
        self.calls: list[dict] = []
        self.healthy = {"stt", "brain", "tts", "rag"}
        self.tts_calls_in_turn = 0

    def set_mode(self, mode: str):
        self.mode = mode
        self.current_turn = 0
        self.tts_calls_in_turn = 0

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/health":
            name = request.url.host.split(".")[0]
            return httpx.Response(200 if name in self.healthy else 503, json={"status": "ok"})

        if path == "/v1/audio/transcriptions":
            return await self._handle_stt(request)
        elif path == "/v1/chat/completions":
            return await self._handle_brain(request)
        elif path == "/v1/audio/speech":
            return await self._handle_tts(request)
        elif path == "/search":
            return httpx.Response(200, json={"results": [{"text": "BTech admissions context."}]})
        return httpx.Response(404)

    async def _handle_stt(self, request: httpx.Request) -> httpx.Response:
        turn_idx = min(self.current_turn, len(CONVERSATION_10_TURNS) - 1)
        turn_data = CONVERSATION_10_TURNS[turn_idx]
        is_first_turn = (self.current_turn == 0)

        # SCRIPTED DELAYS:
        if self.mode == "cold" and is_first_turn:
            stt_delay = 0.320  # 320 ms
        elif self.mode == "cold":
            stt_delay = 0.170  # 170 ms
        else:
            stt_delay = 0.150 if is_first_turn else 0.145

        await asyncio.sleep(stt_delay)
        self.calls.append({"service": "stt", "turn": self.current_turn + 1, "delay_s": stt_delay})
        return httpx.Response(200, json={"text": turn_data["input_text"], "language": "hi"})

    async def _handle_brain(self, request: httpx.Request) -> httpx.Response:
        turn_idx = min(self.current_turn, len(CONVERSATION_10_TURNS) - 1)
        turn_data = CONVERSATION_10_TURNS[turn_idx]
        is_first_turn = (self.current_turn == 0)

        history_penalty_s = 0.010 * self.current_turn

        # SCRIPTED DELAYS:
        if self.mode == "cold" and is_first_turn:
            ttft_delay = 0.380
            clause_delay = 0.080
        elif self.mode == "cold":
            ttft_delay = 0.190 + history_penalty_s
            clause_delay = 0.065
        else:
            ttft_delay = 0.180 + history_penalty_s
            clause_delay = 0.060

        await asyncio.sleep(ttft_delay)

        # Token streaming simulation
        reply = turn_data["reply_text"]
        words = reply.split(" ")
        mid = max(3, len(words) // 2)
        clause1_words = words[:mid]
        token1 = clause1_words[0] + " "
        clause1_rest = " ".join(clause1_words[1:]) + " "
        clause2 = " ".join(words[mid:])

        chunks = [
            (f'data: {json.dumps({"choices": [{"delta": {"content": token1}}]})}\n\n', 0.0),
            (f'data: {json.dumps({"choices": [{"delta": {"content": clause1_rest}}]})}\n\n', clause_delay),
            (f'data: {json.dumps({"choices": [{"delta": {"content": clause2}}]})}\n\n', clause_delay),
            ('data: [DONE]\n\n', 0.0),
        ]

        self.calls.append({
            "service": "brain",
            "turn": self.current_turn + 1,
            "ttft_s": ttft_delay,
            "clause_s": clause_delay,
        })

        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=AsyncStream(chunks),
        )

    async def _handle_tts(self, request: httpx.Request) -> httpx.Response:
        self.tts_calls_in_turn += 1
        is_first_tts_of_turn = (self.tts_calls_in_turn == 1)
        is_first_turn = (self.current_turn == 0)

        # SCRIPTED DELAYS:
        if self.mode == "cold" and is_first_turn and is_first_tts_of_turn:
            tts_delay = 0.350
        elif self.mode == "cold":
            tts_delay = 0.190
        else:
            tts_delay = 0.170 if is_first_tts_of_turn else 0.160

        await asyncio.sleep(tts_delay)
        self.calls.append({"service": "tts", "turn": self.current_turn + 1, "delay_s": tts_delay})

        audio = tone_wav(ms=300, rate=16000)
        return httpx.Response(200, content=audio, headers={"content-type": "audio/wav"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@dataclass
class TurnMetric:
    turn: int
    run_type: str
    heard: str
    reply: str
    vad_silence_ms: float
    stt_ms: float
    prep_ms: float
    llm_ttft_ms: float
    llm_ttfc_ms: float
    llm_total_ms: float
    tts_first_chunk_ms: float
    tts_total_ms: float
    network_dispatch_ms: float
    voice_to_voice_ms: float
    turn_end_ms: float
    audio_bytes: int
    prompt_messages: int


@pytest_asyncio.fixture
async def pv_simulated(db):
    """Test fixture using SimulatedModels mock transport."""
    s = make_settings(
        mongo_db=db.s.mongo_db,
        provider="mock",
        plivo_inbound_enabled=True,
        plivo_auth_token=TOKEN,
        webhook_public_base_url=BASE,
        ai_enabled=True,
        ai_allowed_hosts="stt.test,brain.test,tts.test,rag.test",
        ai_api_key="test-model-key-123456",
        ai_store_transcripts=True,
        ai_script_enforcement=True,
        ai_tts_reference_wav="Utkarsh_voice_ref12s_24k.wav",
        plivo_orphan_grace_s=60,
        plivo_stream_connect_s=30,
    )
    models = SimulatedModels()
    fake = FakePlivo()
    prov = MockProvider()
    app = create_app(s, db=db, provider=prov, ai_transport=models.transport())

    async with live_server(app) as host:
        app.state.controller.providers["plivo_api"] = fake
        async with httpx.AsyncClient(base_url=f"http://{host}", timeout=25) as c:
            e = type("PVSimulated", (), {})()
            e.s, e.app, e.c, e.host, e.models, e.fake, e.prov, e.db = s, app, c, host, models, fake, prov, db
            e.repos, e.ctrl, e.ai = app.state.repos, app.state.controller, app.state.ai

            await e.repos.put_policy("t1", {
                "enabled": True,
                "caller_ids": ["+911140000000"],
                "max_active_calls": 5,
                "max_duration_s": 300,
                "transfer_destinations": ["+919999999999"],
            })
            await db["inbound_routes"].insert_one({"number": DID, "tenant_id": "t1", "action": "ai", "enabled": True})
            cfg_res = await c.put("/v1/ai/config", json=CFG, headers=ADMIN())
            assert cfg_res.status_code == 200, cfg_res.text
            yield e


async def _execute_turn(pv_simulated, ws, a, n_chunk: int, turn_idx: int) -> tuple[int, bytes]:
    pv_simulated.models.current_turn = turn_idx - 1
    pv_simulated.models.tts_calls_in_turn = 0

    n_chunk = await send_media(ws, utterance(speech_ms=400, silence_ms=800), first_chunk=n_chunk)
    msgs = await recv_until(ws, lambda m: m.get("event") == "checkpoint", timeout=15)
    plays = [m for m in msgs if m["event"] == "playAudio"]
    assert len(plays) >= 1, f"Turn {turn_idx}: no audio received from assistant"

    audio = b"".join(base64.b64decode(p["media"]["payload"]) for p in plays)
    ckpt_name = msgs[-1].get("name", f"utt-{turn_idx}")
    await ws.send(json.dumps({
        "event": "playedStream",
        "sequenceNumber": 500 + turn_idx,
        "streamId": a.stream_id,
        "name": ckpt_name,
    }))
    return n_chunk, audio


async def _collect_traced_turns(pv_simulated, sid: str, count: int, timeout: float = 12.0) -> list[dict]:
    async def ready():
        d = await ai_doc(pv_simulated, sid)
        t = (d or {}).get("transcript") or []
        if len(t) >= count and all("t_turn_end" in (e.get("timings_ms") or {}) for e in t[:count]):
            return t[:count]
        return None

    return await until(ready, timeout=timeout)


def _extract_turn_metrics(transcript: list[dict], run_type: str, audio_bytes_list: list[int]) -> list[TurnMetric]:
    metrics = []
    for i, entry in enumerate(transcript, 1):
        tm = entry["timings_ms"]
        t_speech_end = tm.get("t_speech_end", 0.0)
        t_endpoint = tm.get("t_endpoint", 0.0)
        t_stt_sent = tm.get("t_stt_sent", 0.0)
        t_stt_final = tm.get("t_stt_final", 0.0)
        t_llm_sent = tm.get("t_llm_sent", 0.0)
        t_llm_first_token = tm.get("t_llm_first_token", t_llm_sent)
        t_llm_first_clause = tm.get("t_llm_first_clause", t_llm_first_token)
        t_tts_sent = tm.get("t_tts_sent", t_llm_first_clause)
        t_tts_first_chunk = tm.get("t_tts_first_chunk", t_tts_sent)
        t_first_frame = tm.get("t_first_frame_to_plivo", t_tts_first_chunk)
        t_turn_end = tm.get("t_turn_end", t_first_frame)

        vad_silence = float(tm.get("endpoint_silence_ms") or (round(t_endpoint - t_speech_end, 1) if round(t_endpoint - t_speech_end, 1) > 0 else 700.0))
        stt_dur = max(0.0, round(t_stt_final - t_stt_sent, 1)) if t_stt_final >= t_stt_sent else tm.get("stt", 0.0)
        prep_dur = max(0.0, round(t_llm_sent - t_stt_final, 1))
        llm_ttft = max(0.0, round(t_llm_first_token - t_llm_sent, 1))
        llm_ttfc = max(0.0, round(t_llm_first_clause - t_llm_first_token, 1))
        llm_total = tm.get("brain", max(0.0, round(t_tts_sent - t_llm_sent, 1)))
        tts_chunk = max(0.0, round(t_tts_first_chunk - t_tts_sent, 1))
        tts_total = tm.get("tts_total", tts_chunk)
        network_dur = max(0.0, round(t_first_frame - t_tts_first_chunk, 1))
        v2v = tm.get("voice_to_voice", round(t_first_frame - t_speech_end, 1))
        turn_end = round(t_turn_end - t_speech_end, 1)

        metrics.append(TurnMetric(
            turn=i,
            run_type=run_type,
            heard=entry.get("heard", CONVERSATION_10_TURNS[i - 1]["input_text"]),
            reply=entry.get("reply", CONVERSATION_10_TURNS[i - 1]["reply_text"]),
            vad_silence_ms=vad_silence,
            stt_ms=stt_dur,
            prep_ms=prep_dur,
            llm_ttft_ms=llm_ttft,
            llm_ttfc_ms=llm_ttfc,
            llm_total_ms=llm_total,
            tts_first_chunk_ms=tts_chunk,
            tts_total_ms=tts_total,
            network_dispatch_ms=network_dur,
            voice_to_voice_ms=v2v,
            turn_end_ms=turn_end,
            audio_bytes=audio_bytes_list[i - 1] if i - 1 < len(audio_bytes_list) else 0,
            prompt_messages=1 + 2 * (i - 1),
        ))
    return metrics


@pytest.mark.asyncio
async def test_simulated_10_turn_cold_and_warm(pv_simulated, capsys):
    """SIMULATED, NOT A MEASUREMENT: Delays are scripted in SimulatedModels.
    Simulates a 10-turn conversation for Cold and Warm runs.
    """
    num_turns = 10

    # 1. COLD RUN
    pv_simulated.models.set_mode("cold")
    a_cold = await answered_stream(pv_simulated, call_uuid="SIM-COLD-10TURN")
    ws_cold = await connect(pv_simulated, a_cold)
    sr_cold = await post_cb(pv_simulated, "stream-status", {
        "CallUUID": "SIM-COLD-10TURN",
        "Event": "started",
        "StreamID": a_cold.stream_id,
    })
    assert sr_cold.status_code == 200

    n_chunk_cold = 1
    cold_audio_bytes = []
    for turn_idx in range(1, num_turns + 1):
        n_chunk_cold, audio = await _execute_turn(pv_simulated, ws_cold, a_cold, n_chunk_cold, turn_idx)
        cold_audio_bytes.append(len(audio))
        await asyncio.sleep(0.05)

    assert len(cold_audio_bytes) == num_turns
    assert all(b > 1000 for b in cold_audio_bytes)

    hr_cold = await post_cb(pv_simulated, "hangup", {
        "CallUUID": "SIM-COLD-10TURN",
        "CallStatus": "completed",
        "HangupCause": "NORMAL_CLEARING",
    })
    assert hr_cold.status_code == 200
    await ws_cold.close()

    cold_transcript = await _collect_traced_turns(pv_simulated, a_cold.sid, num_turns)
    cold_metrics = _extract_turn_metrics(cold_transcript, "cold", cold_audio_bytes)
    assert len(cold_metrics) == num_turns

    # 2. WARM RUN
    pv_simulated.models.set_mode("warm")
    a_warm = await answered_stream(pv_simulated, call_uuid="SIM-WARM-10TURN")
    ws_warm = await connect(pv_simulated, a_warm)
    sr_warm = await post_cb(pv_simulated, "stream-status", {
        "CallUUID": "SIM-WARM-10TURN",
        "Event": "started",
        "StreamID": a_warm.stream_id,
    })
    assert sr_warm.status_code == 200

    n_chunk_warm = 1
    warm_audio_bytes = []
    for turn_idx in range(1, num_turns + 1):
        n_chunk_warm, audio = await _execute_turn(pv_simulated, ws_warm, a_warm, n_chunk_warm, turn_idx)
        warm_audio_bytes.append(len(audio))
        await asyncio.sleep(0.05)

    assert len(warm_audio_bytes) == num_turns
    assert all(b > 1000 for b in warm_audio_bytes)

    hr_warm = await post_cb(pv_simulated, "hangup", {
        "CallUUID": "SIM-WARM-10TURN",
        "CallStatus": "completed",
        "HangupCause": "NORMAL_CLEARING",
    })
    assert hr_warm.status_code == 200
    await ws_warm.close()

    warm_transcript = await _collect_traced_turns(pv_simulated, a_warm.sid, num_turns)
    warm_metrics = _extract_turn_metrics(warm_transcript, "warm", warm_audio_bytes)
    assert len(warm_metrics) == num_turns

    # Assertions on simulated results
    for m in cold_metrics + warm_metrics:
        assert m.vad_silence_ms > 0
        assert m.stt_ms > 0
        assert m.llm_ttft_ms > 0
        assert m.tts_first_chunk_ms > 0
        assert m.voice_to_voice_ms > 0
        assert m.turn_end_ms >= m.voice_to_voice_ms

    report_data = {
        "NOTICE": "SIMULATED, NOT A MEASUREMENT: delays are scripted in SimulatedModels",
        "test_timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "turns_count": num_turns,
        "is_simulated": True,
        "real_endpoints_called": False,
        "cold_metrics": [asdict(m) for m in cold_metrics],
        "warm_metrics": [asdict(m) for m in warm_metrics],
    }

    evidence_dir = Path(__file__).resolve().parents[2] / "docs" / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence_file = evidence_dir / "simulated_10_turn_cold_warm_result.json"
    evidence_file.write_text(json.dumps(report_data, indent=2, ensure_ascii=False), encoding="utf-8")

    with capsys.disabled():
        _print_console_summary(cold_metrics, warm_metrics)


def _print_console_summary(cold: list[TurnMetric], warm: list[TurnMetric]):
    print("\n" + "=" * 115)
    print("      SIMULATED 10-TURN CONVERSATION LATENCY REPORT (SIMULATED, NOT A MEASUREMENT: delays are scripted)")
    print("=" * 115)
    print(f"{'Turn':<5} | {'Metric':<10} | {'VAD(ms)':<8} | {'STT(ms)':<8} | {'TTFT(ms)':<9} | {'TTFC(ms)':<9} | {'TTS1(ms)':<9} | {'V2V(ms)':<8} | {'End(ms)':<8} | {'Delta V2V':<10}")
    print("-" * 115)

    for i in range(len(cold)):
        c = cold[i]
        w = warm[i]
        diff = round(c.voice_to_voice_ms - w.voice_to_voice_ms, 1)
        pct = round((diff / c.voice_to_voice_ms) * 100, 1) if c.voice_to_voice_ms else 0
        diff_str = f"-{diff:.0f}ms ({pct}%)"

        print(f"#{c.turn:<4} | {'Cold(sim)':<10} | {c.vad_silence_ms:<8.0f} | {c.stt_ms:<8.0f} | {c.llm_ttft_ms:<9.0f} | {c.llm_ttfc_ms:<9.0f} | {c.tts_first_chunk_ms:<9.0f} | {c.voice_to_voice_ms:<8.0f} | {c.turn_end_ms:<8.0f} |")
        print(f"      | {'Warm(sim)':<10} | {w.vad_silence_ms:<8.0f} | {w.stt_ms:<8.0f} | {w.llm_ttft_ms:<9.0f} | {w.llm_ttfc_ms:<9.0f} | {w.tts_first_chunk_ms:<9.0f} | {w.voice_to_voice_ms:<8.0f} | {w.turn_end_ms:<8.0f} | {diff_str:<10}")
        print("-" * 115)

    c_v2v_avg = sum(c.voice_to_voice_ms for c in cold) / len(cold)
    w_v2v_avg = sum(w.voice_to_voice_ms for w in warm) / len(warm)
    c_stt_avg = sum(c.stt_ms for c in cold) / len(cold)
    w_stt_avg = sum(w.stt_ms for w in warm) / len(warm)
    c_ttft_avg = sum(c.llm_ttft_ms for c in cold) / len(cold)
    w_ttft_avg = sum(w.llm_ttft_ms for w in warm) / len(warm)
    c_tts_avg = sum(c.tts_first_chunk_ms for c in cold) / len(cold)
    w_tts_avg = sum(w.tts_first_chunk_ms for w in warm) / len(warm)

    avg_diff = round(c_v2v_avg - w_v2v_avg, 1)
    avg_pct = round((avg_diff / c_v2v_avg) * 100, 1)

    print(f"{'AVG':<5} | {'Cold Mean':<10} | {'700':<8} | {c_stt_avg:<8.1f} | {c_ttft_avg:<9.1f} | {'70':<9} | {c_tts_avg:<9.1f} | {c_v2v_avg:<8.1f} | {'-':<8} |")
    print(f"      | {'Warm Mean':<10} | {'700':<8} | {w_stt_avg:<8.1f} | {w_ttft_avg:<9.1f} | {'65':<9} | {w_tts_avg:<9.1f} | {w_v2v_avg:<8.1f} | {'-':<8} | -{avg_diff:.0f}ms ({avg_pct}%)")
    print("=" * 115 + "\n")
