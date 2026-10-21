"""Production-hardening behaviour: malformed model replies, startup recovery, graceful drain, new metrics.
MOCKED: models (httpx mock transport), Plivo (signed test requests, fake REST client). Real: service, MongoDB, HTTP/WS server."""
import asyncio
import io
import json
import re
import time
import wave

import httpx
import pytest

from app.ai.clients import AiError, ModelClients, wav_to_pcm16
from app.ai.pipeline import Conversation
from app.core.ids import utcnow
from app.main import create_app
from app.providers.base import MockProvider
from tests.conftest import make_settings
from tests.helpers import MockModels, pcm_tone
from tests.integration.test_plivo_voice import (BASE, DID, TOKEN, FakePlivo, _active, ai_doc, answered_stream, connect, form,
                                                post_answer, pv, recv_until, send_media, until, utterance)

__all__ = ["pv"]  # the shared fixture lives in test_plivo_voice.py; re-exported so pytest finds it here

GOOD = {"stt_url": "http://stt.test", "brain_url": "http://brain.test", "tts_url": "http://tts.test", "rag_url": ""}


def wav(rate=16000, ch=1, width=2, frames=1600, raw=None):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(ch), w.setsampwidth(width), w.setframerate(rate)
        w.writeframes(raw if raw is not None else b"\x01\x00" * frames * ch if width == 2 else b"\x01" * frames * ch)
    return buf.getvalue()


# =================================================================================== TTS output validation
@pytest.mark.parametrize("blob,why", [
    (b"", "bad_audio"), (b"definitely not a wav file at all", "bad_audio"), (wav(ch=2), "bad_audio"), (wav(width=1), "bad_audio"),
    (wav(rate=11000), "bad_audio"), (wav(frames=0, raw=b""), "bad_audio"), (wav(rate=8000, frames=8000 * 121), "bad_audio"),
    (wav()[:60], "bad_audio")])
def test_tts_audio_is_validated_before_use(blob, why):
    with pytest.raises(AiError) as e:
        wav_to_pcm16(blob)
    assert (e.value.service, e.value.kind) == ("tts", why)


def test_valid_tts_audio_passes_including_24khz_output():
    pcm, rate = wav_to_pcm16(wav(rate=24000, frames=2400))  # Qwen3-TTS-class output rates must be accepted
    assert rate == 24000 and len(pcm) == 4800


# =================================================================================== malformed model replies
def _clients(models: MockModels):
    s = make_settings(ai_allowed_hosts="stt.test,brain.test,tts.test")
    return ModelClients(s, {**GOOD, "models": {}}, models.transport())


@pytest.mark.parametrize("service,body,code", [
    ("stt", b"this is not json", "stt_bad_response"), ("stt", b"[1, 2, 3]", "stt_bad_response"),
    ("brain", b"{}", "brain_bad_response"), ("brain", b'{"choices": []}', "brain_bad_response"), ("brain", b'{"choices": [{"x": 1}]}', "brain_bad_response"),
    ("brain", b'{"choices": [{"message": {"content": null}}]}', "empty_reply"),
    ("tts", b"garbage that is not a wav", "tts_bad_audio")])
async def test_malformed_replies_become_error_codes_not_exceptions_and_leave_clean_history(service, body, code):
    m = MockModels()
    m.bad[service] = body
    conv = Conversation(_clients(m), "auto")
    r = await conv.turn(pcm_tone(400, 8000), 8000)  # must RETURN, never raise
    assert r.error == code and r.audio is None
    assert len(conv.messages) == 1, conv.messages  # only the system prompt: no dangling user/assistant message
    m.bad.clear()
    ok = await conv.turn(pcm_tone(400, 8000), 8000)  # the same conversation works afterwards
    assert ok.error is None and ok.audio and len(conv.messages) == 3


async def test_unexpected_exception_in_a_turn_is_contained(monkeypatch):
    m = MockModels()
    clients = _clients(m)

    async def boom(*a, **k):
        raise RuntimeError("surprise")
    monkeypatch.setattr(clients, "chat", boom)
    conv = Conversation(clients, "auto")
    r = await conv.turn(pcm_tone(400, 8000), 8000)
    assert r.error == "internal_RuntimeError" and len(conv.messages) == 1


async def test_malformed_replies_do_not_kill_the_live_call_worker(pv):
    a = await answered_stream(pv, call_uuid="BADMODEL-1")
    ws = await connect(pv, a)
    await until(lambda: _active(pv, a.sid))
    n = 1
    plan = [("stt", b"<html>502 Bad Gateway</html>"), ("brain", b'{"choices": []}'), ("tts", b"not audio")]
    for svc, body in plan:                                   # three consecutive failing turns, each failing at a different stage
        pv.models.bad[svc] = body
        before = len([k for k, _ in pv.models.calls if k == svc])
        n = await send_media(ws, utterance(), first_chunk=n)

        async def seen(svc=svc, before=before):
            return len([k for k, _ in pv.models.calls if k == svc]) > before
        assert await until(seen)
        pv.models.bad.clear()
    assert (await ai_doc(pv, a.sid))["state"] == "active"
    n = await send_media(ws, utterance(), first_chunk=n)     # the worker is still alive: a healthy turn is answered
    msgs = await recv_until(ws, lambda m: m.get("event") == "checkpoint")
    assert any(m["event"] == "playAudio" for m in msgs)
    rt = pv.ai.runtime[a.sid]
    assert len(rt.conversation.messages) == 3                # system + the one successful exchange
    metrics = (await pv.c.get("/metrics")).text
    assert "mme_tel_ai_turn_errors_total" in metrics
    assert any(ln.startswith("mme_tel_ai_turn_seconds_count") and not ln.endswith(" 0.0") for ln in metrics.splitlines())
    assert any(ln.startswith("mme_tel_ai_runtimes") and ln.endswith(" 1.0") for ln in metrics.splitlines())
    await ws.close()


# =================================================================================== startup recovery
async def _settings_app(db, **kw):
    s = make_settings(mongo_db=db.s.mongo_db, provider="mock", plivo_inbound_enabled=True, plivo_auth_token=TOKEN,
                      webhook_public_base_url=BASE, **kw)
    return s


async def test_startup_fails_stale_ai_sessions_once_and_restores_the_session(db):
    now = utcnow()
    await db["sessions"].insert_one({"_id": "sess-stale", "tenant_id": "t1", "call_id": "c1", "status": "active", "state_version": 0,
                                     "history": [], "created_at": now, "updated_at": now, "answered_at": now})
    await db["ai_sessions"].insert_one({"_id": "ai-stale", "session_id": "sess-stale", "tenant_id": "t1", "call_id": "c1", "state": "active",
                                        "media": {"connected": True}, "turns": 2, "created_at": now})
    await db["ai_sessions"].insert_one({"_id": "ai-done", "session_id": "sess-done", "tenant_id": "t1", "state": "stopped", "stop_reason": "x",
                                        "media": {"connected": False}, "created_at": now})
    s = await _settings_app(db)
    app = create_app(s, db=db, provider=MockProvider())
    async with app.router.lifespan_context(app):
        stale = await db["ai_sessions"].find_one({"_id": "ai-stale"})
        assert stale["state"] == "failed" and stale["stop_reason"] == "service_restarted" and stale["media"]["connected"] is False
        assert (await db["ai_sessions"].find_one({"_id": "ai-done"}))["stop_reason"] == "x"   # terminal ones untouched
        assert (await db["sessions"].find_one({"_id": "sess-stale"}))["status"] == "answered"  # the call itself continues
        assert await app.state.ai.recover_on_startup() == {"ai_sessions_failed": 0}            # idempotent
        m = re.search(r'mme_tel_recovery_total\{action="ai_session_failed"\} ([0-9.]+)', await _metrics(app))
        assert m and float(m.group(1)) >= 1.0


async def _metrics(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        return (await c.get("/metrics")).text


async def test_startup_hangs_up_plivo_calls_whose_stream_died_with_the_old_process(db):
    now = utcnow()
    base = {"tenant_id": "t1", "direction": "inbound", "provider": "plivo_api", "from_number": "1", "to_number": DID, "state": "answered",
            "state_version": 1, "initiated_at": now, "answered_at": now, "ai_enabled": True}
    await db["calls"].insert_many([{**base, "_id": "call_lost", "provider_call_id": "LOST-1", "stream_connected": True},
                                   {**base, "_id": "call_never", "provider_call_id": "NEVER-1", "stream_connected": False},
                                   {**base, "_id": "call_done", "provider_call_id": "DONE-1", "stream_connected": True, "state": "completed"}])
    fake = FakePlivo()
    app = create_app(await _settings_app(db), db=db, provider=MockProvider(), providers={"plivo_api": fake})
    async with app.router.lifespan_context(app):
        assert fake.hangups == [("call_lost", "NORMAL_CLEARING")]
        c = await db["calls"].find_one({"_id": "call_lost"})
        assert c["stream_connected"] is False and c["stream_lost_at"]
        types = [e["type"] for e in await db["call_events"].find({"call_id": "call_lost"}).to_list(10)]
        assert "stream.lost_on_restart" in types
    app2 = create_app(await _settings_app(db), db=db, provider=MockProvider(), providers={"plivo_api": fake})
    async with app2.router.lifespan_context(app2):
        assert fake.hangups == [("call_lost", "NORMAL_CLEARING")]  # a second restart does not hang up again


async def test_startup_recovery_failure_does_not_prevent_startup(db, monkeypatch):
    from app.ai.service import AiService

    async def boom(self):
        raise RuntimeError("db hiccup")
    monkeypatch.setattr(AiService, "recover_on_startup", boom)
    app = create_app(await _settings_app(db), db=db, provider=MockProvider())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            assert (await c.get("/health/ready")).status_code == 200


# =================================================================================== graceful drain
async def test_drain_stops_new_calls_closes_live_streams_and_hangs_the_callers_up(pv):
    a = await answered_stream(pv, call_uuid="DRAIN-1")
    ws = await connect(pv, a)
    await until(lambda: _active(pv, a.sid))
    assert (await pv.c.get("/health/ready")).status_code == 200
    t0 = time.monotonic()
    out = await pv.app.state.drain(timeout=0.3)
    assert out == {"stopped": 1, "hangups_requested": 1} and time.monotonic() - t0 < 3
    ai = await ai_doc(pv, a.sid)
    assert ai["state"] == "stopped" and ai["stop_reason"] == "service_shutdown" and a.sid not in pv.ai.runtime
    assert pv.fake.hangups == [(a.call["_id"], "NORMAL_CLEARING")]       # the caller is not left in silence
    with pytest.raises(Exception):
        await asyncio.wait_for(ws.recv(), 3)                             # socket closed
    r = await pv.c.get("/health/ready")
    assert r.status_code == 503 and r.json()["checks"]["draining"] == "yes"
    assert (await pv.c.get("/health/live")).status_code == 200            # still alive: only taken out of rotation
    busy = await post_answer(pv, form("DRAIN-2"))
    assert 'reason="busy"' in busy.text and await pv.db["calls"].count_documents({"provider_call_id": "DRAIN-2"}) == 0
    assert await pv.app.state.drain(timeout=0) == {"stopped": 0, "hangups_requested": 0}   # idempotent
    assert "mme_tel_draining 1.0" in (await pv.c.get("/metrics")).text


async def test_drain_waits_for_conversations_that_end_on_their_own(pv):
    a = await answered_stream(pv, call_uuid="DRAIN-3")
    ws = await connect(pv, a)
    await until(lambda: _active(pv, a.sid))

    async def caller_hangs_up_soon():
        await asyncio.sleep(0.5)
        await ws.send(json.dumps({"event": "stop", "sequenceNumber": 9, "streamId": a.stream_id}))
    task = asyncio.create_task(caller_hangs_up_soon())
    t0 = time.monotonic()
    out = await pv.app.state.drain(timeout=10)
    await task
    assert out["stopped"] == 0 and 0.4 < time.monotonic() - t0 < 5   # finished naturally: nothing was force-closed
    assert (await ai_doc(pv, a.sid))["stop_reason"] == "stream_stopped" and pv.fake.hangups == []


# =================================================================================== misc
def test_config_has_drain_timeout_and_it_is_bounded():
    from pydantic import ValidationError
    assert make_settings().drain_timeout_s == 0 and make_settings(drain_timeout_s=15).drain_timeout_s == 15
    with pytest.raises(ValidationError):
        make_settings(drain_timeout_s=-1)
    with pytest.raises(ValidationError):
        make_settings(drain_timeout_s=301)
