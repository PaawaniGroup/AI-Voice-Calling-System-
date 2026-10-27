"""Cached acknowledgement on the REAL service stack (routes, Plivo stream handler, MongoDB, AiService, serve_loop) with MOCK Plivo, MOCK models and SYNTHETIC clips.
SIMULATED: nothing here measures real latency, uses a real voice or places a call. Per-tenant flag `ack_enabled`: OFF by default."""
import base64
import json
import logging
import time

import pytest

from tests.conftest import auth
from tests.helpers import pcm_tone
from tests.integration.test_plivo_voice import (RATE, ai_doc, answered_stream, connect, pv, recv_until, send_media, until, utterance)  # noqa: F401
from tests.integration.test_streaming_live import REPLY, _call, _conv  # noqa: F401
from tests.unit.test_ack import GOOD, make

ADMIN = lambda: auth("t1", ("telephony:admin",))  # noqa: E731
PHRASE_WORDS = ("सेकंड", "क्षण", "moment", "second")        # the clips' text: it must never reach a log or the stored record


async def _enable(pv, tmp_path, monkeypatch, *, ack=True, after=200, **extra):
    monkeypatch.setattr(pv.ai.s, "ai_ack_dir", str(make(tmp_path)))
    await pv.ai.put_config("t1", {"streaming_pipeline": True, "ack_enabled": ack, "ack_after_ms": after, **extra}, "test")   # service layer: independent of the API model


async def test_the_flag_is_off_by_default_and_files_alone_change_nothing(pv, tmp_path, monkeypatch):
    cfg = (await pv.c.get("/v1/ai/config", headers=ADMIN())).json()
    assert cfg["ack_enabled"] is False and cfg["keepalive_enabled"] is False and cfg["ack_after_ms"] is None
    await _enable(pv, tmp_path, monkeypatch, ack=False)                               # files present, flag OFF
    pv.models.brain_sse, pv.models.reply, pv.models.tts_delay = True, REPLY, 0.6
    a, ws = await _call(pv, "ACK-OFF")
    assert _conv(pv, a.sid).ack is None
    await send_media(ws, utterance())
    await recv_until(ws, lambda m: m.get("event") == "checkpoint", timeout=15)
    assert _conv(pv, a.sid).last_stream.stream["ack_ms"] is None
    await ws.close()


async def test_missing_files_with_the_flag_on_never_break_the_call(pv, tmp_path, monkeypatch):
    monkeypatch.setattr(pv.ai.s, "ai_ack_dir", str(tmp_path / "nothing-here"))
    await pv.ai.put_config("t1", {"streaming_pipeline": True, "ack_enabled": True}, "test")
    pv.models.brain_sse, pv.models.reply, pv.models.tts_delay = True, REPLY, 0.6
    a, ws = await _call(pv, "ACK-MISSING")
    assert _conv(pv, a.sid).ack is None
    await send_media(ws, utterance())
    await recv_until(ws, lambda m: m.get("event") == "checkpoint", timeout=15)         # the turn is answered normally
    assert _conv(pv, a.sid).last_stream.stream["ack_ms"] is None
    await ws.close()


async def test_a_slow_voice_gets_the_phrase_first_and_it_is_not_in_the_history_or_the_record(pv, tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    await _enable(pv, tmp_path, monkeypatch)
    pv.models.brain_sse, pv.models.reply, pv.models.tts_delay = True, REPLY, 1.5          # the first real clause takes 1.5 s
    a, ws = await _call(pv, "ACK-ON")
    assert _conv(pv, a.sid).ack is not None
    await send_media(ws, utterance())
    t0 = time.monotonic()
    msgs = await recv_until(ws, lambda m: m.get("event") == "playAudio", timeout=10)
    assert time.monotonic() - t0 < 1.2                                                    # sound arrived long before the real clause could
    msgs += await recv_until(ws, lambda m: m.get("event") == "checkpoint", timeout=20)
    mu = b"".join(base64.b64decode(m["media"]["payload"]) for m in msgs if m["event"] == "playAudio")
    conv = _conv(pv, a.sid)
    out = conv.last_stream
    assert out.stream["ack_ms"] == 1000 and len(mu) >= 8000 + 2 * 2400                    # the 1 s phrase + the answer (the mock voice makes 300 ms = 2400 bytes per clause)
    assert [m["role"] for m in conv.messages] == ["system", "user", "assistant"]          # nothing of the phrase in the history
    assert conv.messages[-1]["content"] == " ".join(out.clause_texts[i] for i in sorted(out.clause_texts))

    async def stored():
        d = await ai_doc(pv, a.sid)
        e = ((d or {}).get("transcript") or [None])[0]
        return e if e and "t_turn_end" in e["timings_ms"] else None
    e = await until(stored)
    tm = e["timings_ms"]
    assert tm["t_ack_first_frame"] < 1000 < tm["t_first_frame_to_plivo"] and tm["voice_to_voice"] == tm["t_first_frame_to_plivo"]   # v2v = the ANSWER, not the phrase
    assert e["reply_chars"] == len(conv.messages[-1]["content"])
    blob = json.dumps(await ai_doc(pv, a.sid), default=str, ensure_ascii=False)
    for w in PHRASE_WORDS:
        assert w not in blob and w not in caplog.text, w
    await ws.close()


async def test_a_fast_voice_gets_no_phrase(pv, tmp_path, monkeypatch):
    await _enable(pv, tmp_path, monkeypatch, after=2500)
    pv.models.brain_sse, pv.models.reply, pv.models.tts_delay = True, REPLY, 0.0
    a, ws = await _call(pv, "ACK-FAST")
    await send_media(ws, utterance())
    await recv_until(ws, lambda m: m.get("event") == "checkpoint", timeout=15)
    assert _conv(pv, a.sid).last_stream.stream["ack_ms"] is None and _conv(pv, a.sid).ack.played == 0
    await ws.close()


async def test_talking_over_the_phrase_stops_it_and_the_question_is_merged_not_lost(pv, tmp_path, monkeypatch):
    await _enable(pv, tmp_path, monkeypatch)
    pv.models.brain_sse, pv.models.reply, pv.models.tts_delay = True, REPLY, 3.0
    a, ws = await _call(pv, "ACK-BARGE")
    n = await send_media(ws, utterance())
    await recv_until(ws, lambda m: m.get("event") == "playAudio", timeout=10)             # the phrase is playing
    n = await send_media(ws, pcm_tone(500, RATE, amp=6000), first_chunk=n)                # the caller talks over it
    await recv_until(ws, lambda m: m.get("event") == "clearAudio", timeout=10)
    pv.models.tts_delay = 0.0
    n = await send_media(ws, utterance(speech_ms=400, silence_ms=800), first_chunk=n)     # and finishes the question
    await recv_until(ws, lambda m: m.get("event") == "checkpoint", timeout=20)
    conv = _conv(pv, a.sid)
    assert [m["role"] for m in conv.messages] == ["system", "user", "assistant"]          # ONE question in the history: the first attempt was merged
    sizes = [s for s, _ in pv.models.stt_uploads]
    assert len(sizes) >= 2 and sizes[-1] > sizes[0] * 1.5                                 # the last STT upload carries the first part of the speech too
    await ws.close()
