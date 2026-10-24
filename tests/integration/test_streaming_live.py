"""Token-streaming pipeline on the REAL service stack (routes, Plivo stream handler, MongoDB, AiService, serve_loop) with MOCK Plivo and MOCK models.
Per-tenant flag `streaming_pipeline`: OFF by default (the old path is untouched), ON for a tenant that sets it. Nothing here measures real latency."""
import asyncio
import base64
import json
import logging

import pytest

from app.ai import codec
from tests.conftest import auth
from tests.helpers import pcm_tone
from tests.integration.test_plivo_voice import (RATE, ai_doc, answered_stream, connect, pv, recv_until, send_media, until, utterance)  # noqa: F401

ADMIN = lambda: auth("t1", ("telephony:admin",))  # noqa: E731
CALLER_WORDS = ("baare", "batao")
REPLY = ("जी, मैं आपको admission की पूरी जानकारी देता हूँ। B.Tech चार साल का course है, और admission entrance exam से होता है। "
         "आप online form भर दीजिए, फिर हमारी team आपको call करेगी।")
REPLY_WORDS = ("admission", "जानकारी", "entrance", "team")


async def _call(pv, uuid):
    a = await answered_stream(pv, call_uuid=uuid)
    ws = await connect(pv, a)
    await until(lambda: _active(pv, a.sid))
    return a, ws


async def _active(pv, sid):
    d = await ai_doc(pv, sid)
    return d if d and d["state"] == "active" else None


def _conv(pv, sid):
    return pv.ai.runtime[sid].conversation


async def _enable(pv, **extra):
    r = await pv.c.put("/v1/ai/config", json={"streaming_pipeline": True, **extra}, headers=ADMIN())
    assert r.status_code == 200, r.text


# ------------------------------------------------------------------ the flag and its settings
async def test_default_is_off_and_the_old_path_is_used(pv):
    cfg = (await pv.c.get("/v1/ai/config", headers=ADMIN())).json()
    assert cfg["streaming_pipeline"] is False
    a, ws = await _call(pv, "STREAM-OFF")
    assert _conv(pv, a.sid).streaming is None
    await send_media(ws, utterance())
    await recv_until(ws, lambda m: m.get("event") == "checkpoint", timeout=10)
    assert any(k == "brain" for k, _ in pv.models.calls)
    assert _conv(pv, a.sid).last_stream is None                                    # the streaming pipeline never ran: the old path served the turn
    await ws.close()


async def test_settings_are_validated_and_stored_per_tenant(pv):
    assert (await pv.c.put("/v1/ai/config", json={"stream_max_words": 3}, headers=ADMIN())).status_code == 422
    assert (await pv.c.put("/v1/ai/config", json={"stream_prefetch_depth": 9}, headers=ADMIN())).status_code == 422
    assert (await pv.c.put("/v1/ai/config", json={"runpod_poll_cap_s": 5}, headers=ADMIN())).status_code == 422
    await _enable(pv, stream_max_words=12, stream_prefetch_depth=3, stream_prebuffer_ms=100, runpod_poll_cap_s=0.15)
    cfg = (await pv.c.get("/v1/ai/config", headers=ADMIN())).json()
    assert cfg["streaming_pipeline"] is True and cfg["stream_max_words"] == 12 and cfg["stream_prefetch_depth"] == 3 and cfg["stream_first_min_words"] is None
    a, ws = await _call(pv, "STREAM-CFG")
    s = _conv(pv, a.sid).streaming
    assert s.segmenter.max_words == 12 and s.prefetch_depth == 3 and s.playout.prebuffer_ms == 100
    await ws.close()


# ------------------------------------------------------------------ a streamed turn on a Plivo stream
async def test_a_streamed_turn_speaks_clause_by_clause_and_records_the_same_stored_fields(pv, caplog):
    caplog.set_level(logging.DEBUG)
    pv.models.brain_sse, pv.models.reply, pv.models.tts_ms = True, REPLY, 400
    await _enable(pv)
    a, ws = await _call(pv, "STREAM-ON")
    await send_media(ws, utterance())
    msgs = await recv_until(ws, lambda m: m.get("event") == "checkpoint", timeout=15)
    plays = [m for m in msgs if m["event"] == "playAudio"]
    assert plays and msgs[-1]["event"] == "checkpoint" and msgs[-1]["name"].startswith("utt-")
    mu = b"".join(base64.b64decode(p["media"]["payload"]) for p in plays)
    assert len(mu) > 8000 and all(p["media"]["contentType"] == "audio/x-mulaw" and p["media"]["sampleRate"] == 8000 for p in plays)
    tts_calls = [b for k, b in pv.models.calls if k == "tts"]
    brain = next(b for k, b in pv.models.calls if k == "brain")
    assert brain["stream"] is True and len(tts_calls) >= 3                         # the Brain streamed; the reply was spoken in clauses
    assert all(isinstance(c.get("input"), str) for c in tts_calls)
    conv = _conv(pv, a.sid)
    assert conv.messages[-1] == {"role": "assistant", "content": " ".join(conv.last_stream.clause_texts[i] for i in sorted(conv.last_stream.clause_texts))}
    assert conv.messages[-2]["role"] == "user" and len(conv.turns) == 1

    async def stored():
        d = await ai_doc(pv, a.sid)
        t = (d or {}).get("transcript") or []
        return t if t and "t_turn_end" in t[0]["timings_ms"] else None
    t = await until(stored)
    e = t[0]
    assert set(e) >= {"turn", "heard_chars", "reply_chars", "timings_ms", "trace_id"} and e["heard_chars"] > 0
    assert e["reply_chars"] == len(conv.messages[-1]["content"])                   # the FULL reply length (not the first clause only)
    tm = e["timings_ms"]
    order = ["t_speech_end", "t_endpoint", "t_stt_sent", "t_stt_final", "t_llm_sent", "t_llm_first_token", "t_llm_first_clause",
             "t_tts_sent", "t_tts_first_chunk", "t_first_frame_to_plivo", "t_turn_end"]
    assert [tm[k] for k in order] == sorted(tm[k] for k in order)
    assert {"stt", "brain", "tts", "turn_ms", "voice_to_voice"} <= set(tm)
    blob = json.dumps(await ai_doc(pv, a.sid), default=str, ensure_ascii=False)
    for w in (*CALLER_WORDS, *REPLY_WORDS):                                        # privacy: no caller or reply words in the stored record or any log
        assert w not in blob and w not in caplog.text, w
    await ws.close()


async def test_barge_in_clears_the_audio_and_keeps_only_the_heard_part_in_the_history(pv):
    pv.models.brain_sse, pv.models.reply, pv.models.tts_ms = True, REPLY, 4000
    await _enable(pv)
    a, ws = await _call(pv, "STREAM-BARGE")
    await send_media(ws, utterance())
    await recv_until(ws, lambda m: m.get("event") == "playAudio", timeout=15)
    await asyncio.sleep(0.3)
    await send_media(ws, pcm_tone(500, RATE, amp=6000), first_chunk=500)
    await recv_until(ws, lambda m: m.get("event") == "clearAudio", timeout=10)
    await ws.send(json.dumps({"event": "clearedAudio", "sequenceNumber": 900, "streamId": a.stream_id}))
    conv = _conv(pv, a.sid)

    async def settled():
        return conv.last_stream if conv.last_stream is not None and conv.last_stream.status == "cancelled" else None
    out = await until(settled)
    assert out is not None and out.status == "cancelled"
    full = " ".join(out.clause_texts[i] for i in sorted(out.clause_texts))
    assistant = [m["content"] for m in conv.messages if m["role"] == "assistant"]
    if assistant:                                                                  # what stays is a strict prefix of what was produced, never the rest
        assert len(assistant[-1]) < len(full) and full.startswith(assistant[-1].rstrip())
    assert out.stream["interrupt"]["interrupt_to_last_frame_ms"] <= 25
    assert (await ai_doc(pv, a.sid))["state"] == "active"                          # the call goes on
    pv.models.tts_ms = 300
    await send_media(ws, utterance(), first_chunk=1500)
    await recv_until(ws, lambda m: m.get("event") == "checkpoint", timeout=15)     # and the next turn works
    await ws.close()


async def test_a_second_utterance_before_any_audio_cancels_the_first_without_leaving_history(pv):
    pv.models.brain_sse, pv.models.reply, pv.models.tts_delay = True, REPLY, 3.0   # TTS is slow: no audio yet when the caller speaks again
    await _enable(pv)
    a, ws = await _call(pv, "STREAM-MERGE")
    n = await send_media(ws, utterance(speech_ms=600, silence_ms=800))
    await asyncio.sleep(0.5)
    pv.models.tts_delay = 0.0
    n = await send_media(ws, utterance(speech_ms=600, silence_ms=800), first_chunk=n)
    await recv_until(ws, lambda m: m.get("event") == "checkpoint", timeout=20)
    conv = _conv(pv, a.sid)
    users = [m for m in conv.messages if m["role"] == "user"]
    assert len(users) == 1                                                         # the cancelled first attempt left no user message behind
    await ws.close()
