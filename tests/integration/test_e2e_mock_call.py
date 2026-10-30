"""END-TO-END MOCK CALL: the whole supervised-test-call story against the REAL service stack (routes, signature check, controller,
MongoDB, AiService, media WebSocket, conversation loop, kill switch), with Plivo and the three model services MOCKED.

Story: arm (allow only my number, 1 call, 180 s cap) -> a stranger is rejected without a trace -> I call, the AI answers -> 3 normal
turns -> a barge-in -> one more turn -> hangup -> everything is cleaned up -> the kill switch closes the route -> nothing the caller or the
assistant said is in the feed, the flow, the stored session or any log.

The per-turn timings printed at the end come from MOCK models: they show the pipeline's own overhead and where each stage is
recorded, NOT real latency. Real numbers need the live call."""
import asyncio
import base64
import importlib.util
import json
import logging
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import websockets

from app.core.safe_output import safe_json
from tests.conftest import auth
from tests.integration.test_ai_teardown import HANDLERS, WORKER, _live
from tests.integration.test_plivo_voice import (DID, RATE, TOKEN, answered_stream, ai_doc, connect, form, post_answer, post_cb,  # noqa: F401
                                                pv, recv_until, send_media, until, utterance)
from tests.helpers import pcm_tone

_spec = importlib.util.spec_from_file_location("plivo_testcall", Path(__file__).resolve().parents[2] / "scripts" / "plivo_testcall.py")
tc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tc)

ME = "+919876543210"                 # the default caller of test_plivo_voice.form()
STRANGER = "919000000009"
CALLER_WORDS = ("baare", "batao")    # the mock STT's transcript: "mujhe BTech ke baare mein batao"
ASSISTANT_WORDS = ("namaste", "eligibility")   # the mock Brain's reply: "Namaste! BTech ke liye eligibility 12th mein 50% hai."
N_NORMAL_TURNS = 3


def _xml(text):
    root = ET.fromstring(text)
    return root.find("Hangup") is not None, root.find("Stream") is not None


async def _turn(pv, ws, a, n_chunk, idx):
    """One caller utterance -> assistant audio -> checkpoint. Returns (next chunk number, assistant mu-law bytes)."""
    n_chunk = await send_media(ws, utterance(), first_chunk=n_chunk)
    msgs = await recv_until(ws, lambda m: m.get("event") == "checkpoint", timeout=10)
    plays = [m for m in msgs if m["event"] == "playAudio"]
    assert plays, f"turn {idx}: no audio came back"
    audio = b"".join(base64.b64decode(p["media"]["payload"]) for p in plays)
    await ws.send(json.dumps({"event": "playedStream", "sequenceNumber": 100 + idx, "streamId": a.stream_id, "name": msgs[-1]["name"]}))
    return n_chunk, audio


async def _traced_turns(pv, sid, n, timeout=8):
    async def ready():
        d = await ai_doc(pv, sid)
        t = (d or {}).get("transcript") or []
        return t if len(t) >= n and all("t_turn_end" in e["timings_ms"] for e in t[:n]) else None
    return await until(ready, timeout=timeout)


async def test_end_to_end_mock_call(pv, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    pv.models.stt_delay = 0.15                      # a visible STT stage in the mock timings

    # ------------------------------------------------------------------ 1. arm: only my number, one call, 180 s
    armed = await tc.arm(pv.db, "t1", ME, DID)
    st = await tc.status(pv.db, "t1", DID, pv.s)
    assert st["policy"]["restricted_to_allowlist"] and st["policy"]["max_duration_s"] == 180 and st["policy"]["max_active_calls"] == 1
    assert st["open_to_anyone"] is False and st["live_calls"] == []
    assert armed["policy_before"]["enabled"] is True

    # ------------------------------------------------------------------ 2. a stranger is rejected without a trace
    r = await post_answer(pv, form("STRANGER-1", **{"From": STRANGER}))
    assert r.status_code == 200 and _xml(r.text) == (True, False)           # <Hangup>, no <Stream>
    assert await pv.db["calls"].count_documents({}) == 0 and await pv.db["ai_sessions"].count_documents({}) == 0
    assert pv.models.calls == []                                            # no STT/Brain/TTS request: no spend for strangers

    # ------------------------------------------------------------------ 3. I call: the AI answers
    a = await answered_stream(pv, call_uuid="E2E-1")
    call = a.call
    assert call["state"] == "answered" and call["ai_enabled"] is True and call["max_duration_s"] == 180 and call["provider"] == "plivo_api"
    ws = await connect(pv, a)
    doc = await until(lambda: _active_doc(pv, a.sid))
    assert doc["media"]["hello"]["flavor"] == "plivo_stream"
    sr = await post_cb(pv, "stream-status", {"CallUUID": "E2E-1", "Event": "started", "StreamID": a.stream_id})
    assert sr.status_code == 200
    live = await tc.status(pv.db, "t1", DID, pv.s)
    assert [c["state"] for c in live["live_calls"]] == ["answered"] and live["live_ai_sessions"] == 1

    # ------------------------------------------------------------------ 4. a conversation: 3 normal turns
    n_chunk, audio_bytes = 1, []
    for i in range(1, N_NORMAL_TURNS + 1):
        n_chunk, audio = await _turn(pv, ws, a, n_chunk, i)
        audio_bytes.append(len(audio))
        await asyncio.sleep(0.2)                                            # the caller pauses
    assert all(b > 1000 for b in audio_bytes)
    brains = [b for k, b in pv.models.calls if k == "brain"]
    assert len(brains) == N_NORMAL_TURNS and len(brains[-1]["messages"]) > len(brains[0]["messages"])   # history grows turn by turn

    # ------------------------------------------------------------------ 5. barge-in: the caller talks over a long reply
    pv.models.tts_ms = 6000
    n_chunk = await send_media(ws, utterance(), first_chunk=n_chunk)
    await recv_until(ws, lambda m: m.get("event") == "playAudio", timeout=10)
    n_chunk = await send_media(ws, pcm_tone(400, RATE, amp=6000), first_chunk=n_chunk)
    await recv_until(ws, lambda m: m.get("event") == "clearAudio", timeout=10)          # playback is cut
    await ws.send(json.dumps({"event": "clearedAudio", "sequenceNumber": 200, "streamId": a.stream_id}))
    assert (await ai_doc(pv, a.sid))["state"] == "active"                               # the call goes on
    pv.models.tts_ms = 300
    n_chunk, audio = await _turn(pv, ws, a, n_chunk, N_NORMAL_TURNS + 1)                # and the next turn still works
    assert len(audio) > 1000

    # ------------------------------------------------------------------ 6. per-turn stage timings (MOCK numbers)
    turns = await _traced_turns(pv, a.sid, N_NORMAL_TURNS)
    rows = []
    for i, e in enumerate(turns[:N_NORMAL_TURNS], 1):
        tm = e["timings_ms"]
        order = ["t_speech_end", "t_endpoint", "t_stt_sent", "t_stt_final", "t_llm_sent", "t_llm_first_token", "t_llm_first_clause",
                 "t_tts_sent", "t_tts_first_chunk", "t_first_frame_to_plivo", "t_turn_end"]
        assert [tm[k] for k in order] == sorted(tm[k] for k in order), f"turn {i}: stage marks out of order"
        assert tm["voice_to_voice"] == tm["t_first_frame_to_plivo"] < 2500, f"turn {i}: the pipeline's own overhead is far too high"
        rows.append((i, tm["stt"], tm["brain"], tm["tts"], tm["voice_to_voice"], tm["t_turn_end"], e["heard_chars"], e["reply_chars"]))
    with capsys.disabled():
        print("\nMOCK-MODEL stage timings (pipeline overhead only, NOT real latency):")
        print("turn  stt_ms  brain_ms  tts_ms  voice_to_voice_ms  turn_end_ms  heard_chars  reply_chars")
        for r in rows:
            print("{:>4}  {:>6.0f}  {:>8.0f}  {:>6.0f}  {:>17.0f}  {:>11.0f}  {:>11}  {:>11}".format(*r))

    # ------------------------------------------------------------------ 7. hangup: clean end state
    pre = await tc.status(pv.db, "t1", DID, pv.s)
    assert pre["live_ai_sessions"] == 1
    hr = await post_cb(pv, "hangup", {"CallUUID": "E2E-1", "CallStatus": "completed", "HangupCause": "NORMAL_CLEARING"})
    assert hr.status_code == 200
    ai = await until(lambda: _stopped_doc(pv, a.sid))
    assert ai["state"] == "stopped" and ai["stop_reason"] == "call_ended" and ai["turns"] >= N_NORMAL_TURNS + 1
    final_call = await pv.db["calls"].find_one({"_id": call["_id"]})
    assert final_call["state"] == "completed"
    assert (await pv.db["sessions"].find_one({"_id": a.sid}))["status"] == "ended"
    assert a.sid not in pv.ai.runtime
    with pytest.raises(websockets.ConnectionClosed):                                    # the media socket is closed
        for _ in range(50):
            await asyncio.wait_for(ws.recv(), 4)
    end = time.monotonic() + 4
    while time.monotonic() < end and _live((WORKER,) + HANDLERS):
        await asyncio.sleep(0.05)
    assert _live((WORKER,) + HANDLERS) == [], "a turn worker or a media handler is still alive after hangup"
    after = await tc.status(pv.db, "t1", DID, pv.s)
    assert after["live_calls"] == [] and after["live_ai_sessions"] == 0

    # ------------------------------------------------------------------ 8. kill switch: the route is closed
    asked = []

    async def fake_hangup(c):
        asked.append(c["_id"])
    out = await tc.disarm(pv.db, "t1", fake_hangup)
    assert out["policy_enabled"] is False and out["hangups"] == [] and asked == []      # nothing live to hang up
    before = await pv.db["calls"].count_documents({})
    r = await post_answer(pv, form("AFTER-KILL-1"))                                     # even MY number is refused now
    assert _xml(r.text) == (True, False) and await pv.db["calls"].count_documents({}) == before

    # ------------------------------------------------------------------ 9. privacy: no words anywhere
    stored = json.dumps(await ai_doc(pv, a.sid), default=str).lower()
    feed = json.dumps(pv.app.state.activity.recent("t1", 2000)).lower()
    flow = (await pv.c.get(f"/v1/live/calls/{call['_id']}/flow", headers=auth("t1", ("telephony:viewer",)))).text.lower()
    session_view = (await pv.c.get(f"/v1/ai/sessions/{a.sid}", headers=auth("t1", ("telephony:viewer",)))).text.lower()
    for where, text in (("stored session", stored), ("live feed", feed), ("call flow", flow), ("AI session view", session_view), ("logs", caplog.text.lower())):
        for w in CALLER_WORDS + ASSISTANT_WORDS:
            assert w not in text, f"{w!r} found in the {where}"
    tool_out = safe_json({"status": after, "armed": armed, "kill": out}).lower()
    assert "9876543210" not in tool_out and "1140001111" not in tool_out and TOKEN.lower() not in tool_out


async def _active_doc(pv, sid):
    d = await ai_doc(pv, sid)
    return d if d and d["state"] == "active" else None


async def _stopped_doc(pv, sid):
    d = await ai_doc(pv, sid)
    return d if d and d["state"] in ("stopped", "failed") else None
