"""REAL FreeSWITCH + REAL loaded mod_audio_stream, MOCK model servers.

The "caller" is a loopback leg playing a tone WAV (stand-in for speech; it is NOT human speech). This proves the media
transport end to end through FreeSWITCH: caller audio -> module -> WebSocket -> VAD -> (mock STT/Brain/TTS) -> reply ->
module temp file -> `uuid_broadcast` -> audible on the caller leg (recorded with record_session). It does NOT prove
anything about the real models, real speech, codecs on a phone line, or carrier latency.
"""
import asyncio
import glob
import os
import struct
import sys
import time
import wave
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from app.main import create_app
from tests.conftest import auth, call_body, enable_tenant, idem, make_settings
from tests.helpers import MockModels, live_server, pcm_tone

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from esl_check import esl_password  # noqa: E402

pytestmark = pytest.mark.freeswitch
ADMIN = lambda: auth("t1", ("telephony:admin",))  # noqa: E731
CFG = {"stt_url": "http://stt.test:9000", "brain_url": "http://brain.test:9000", "tts_url": "http://tts.test:9000", "enabled": True}


def _have_module() -> bool:
    import socket
    try:
        socket.create_connection(("127.0.0.1", 8021), timeout=1).close()
    except OSError:
        return False
    return bool(esl_password())


def write_caller_wav(path="/tmp/mme_ai_caller.wav", ms=1200, rate=8000):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm_tone(ms, rate, amp=9000))
    os.chmod(path, 0o644)


def heard_stats(uuid_glob_after: float):
    """Largest recent 'what the caller heard' recording -> (seconds of audible signal, peak)."""
    files = [f for f in glob.glob("/tmp/mme_ai_heard_*.wav") if os.path.getmtime(f) >= uuid_glob_after]
    best = (0.0, 0)
    for f in files:
        try:
            with wave.open(f, "rb") as w:
                rate, n = w.getframerate(), w.getnframes()
                data = w.readframes(n)
        except Exception:
            continue
        xs = struct.unpack("<%dh" % (len(data) // 2), data)
        win = rate // 50
        loud = sum(1 for i in range(0, len(xs) - win, win) if (sum(x * x for x in xs[i:i + win]) / win) ** 0.5 > 800)
        best = max(best, (loud * 0.02, max((abs(x) for x in xs), default=0)))
    return best


@pytest_asyncio.fixture
async def rig(db):
    if not _have_module():
        pytest.skip("local FreeSWITCH not reachable")
    write_caller_wav()
    models = MockModels()
    s = make_settings(mongo_db=db.s.mongo_db, fs_enabled=True, esl_password=esl_password(), provider="freeswitch_gateway",
                      fs_dial_template="loopback/9010/mme_test", ai_enabled=True, ai_media_bridge="ws_fork",
                      ai_allowed_hosts="stt.test,brain.test,tts.test", esl_reconnect_initial_s=0.2, esl_reconnect_max_s=1.0,
                      reconcile_interval_s=2.0, ring_timeout_s=20)
    app = create_app(s, db=db, ai_transport=models.transport())
    async with live_server(app) as host:
        ctrl, ai = app.state.controller, app.state.ai
        for _ in range(100):
            if ctrl.esl_events.connected:
                break
            await asyncio.sleep(0.05)
        ready, reason = await ai.bridge.ready()
        if not ready:
            pytest.skip(f"media module not loaded: {reason}")
        s.ai_media_ws_url = f"ws://{host}/v1/ai/media/fs"
        async with httpx.AsyncClient(base_url=f"http://{host}", timeout=15) as c:
            e = type("E", (), {})()
            e.app, e.c, e.host, e.models, e.db, e.s, e.ctrl, e.ai, e.repos = app, c, host, models, db, s, ctrl, ai, app.state.repos
            await enable_tenant(e)
            assert (await c.put("/v1/ai/config", json=CFG, headers=ADMIN())).status_code == 200
            yield e
            with __import__("contextlib").suppress(Exception):
                await ctrl.esl_cmd.api("hupall NORMAL_CLEARING mme_leg a")


async def start_ai_call(e, dial="loopback/9010/mme_test"):
    e.s.fs_dial_template = dial
    r = await e.c.post("/v1/calls", json=call_body(ai_enabled=True, language="hinglish"), headers={**auth(), **idem()})
    assert r.status_code == 202, r.text
    call = r.json()
    for _ in range(100):
        got = await e.repos.get_call(call["id"])
        if got["state"] == "answered":
            break
        await asyncio.sleep(0.1)
    assert got["state"] == "answered", got["state"]
    t0 = time.time()
    r = await e.c.post(f"/v1/ai/sessions/{call['session_id']}/start", json={}, headers=auth())
    assert r.status_code == 202, r.text
    return call, got, t0


async def wait_ai(e, sid, states, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        a = await e.db["ai_sessions"].find_one({"session_id": sid})
        if a and a["state"] in states:
            return a
        await asyncio.sleep(0.1)
    raise AssertionError(f"AI session never reached {states}: {a and a['state']} {a and a.get('error')}")


async def test_bridge_reports_ready_only_because_module_is_really_loaded(rig):
    e = rig
    h = (await e.c.get("/v1/ai/health", headers=auth())).json()
    assert h["media_bridge"] == {"name": "ws_fork", "ready": True, "reason": "mod_audio_stream loaded"}
    assert (await e.ctrl.esl_cmd.api("module_exists mod_audio_stream")).strip() == "true"


async def test_real_audio_roundtrip_through_freeswitch_module(rig):
    e = rig
    since = time.time() - 1
    call, fs, t0 = await start_ai_call(e)
    sid = call["session_id"]
    ai = await wait_ai(e, sid, {"active"})  # active only when the module's socket really connected
    t_active = time.time()
    assert ai["media"]["connected"] is True and ai["media"]["hello"]["flavor"] == "mod_audio_stream"
    # caller audio -> VAD -> mock STT/RAG/Brain/TTS -> reply
    for _ in range(100):
        a = await e.db["ai_sessions"].find_one({"session_id": sid})
        if a["turns"] >= 1:
            break
        await asyncio.sleep(0.1)
    assert a["turns"] == 1 and set(a["timings_ms"]) >= {"stt", "brain", "tts"}
    kinds = [k for k, _ in e.models.calls]
    assert kinds == ["stt", "brain", "tts"]  # no RAG configured in this rig
    # the module wrote the reply file and we played it back to the caller: event trail + audible audio on the caller leg
    for _ in range(60):
        evs = await e.db["call_events"].find({"call_id": call["id"], "type": "media.play"}).to_list(5)
        if evs:
            break
        await asyncio.sleep(0.1)
    assert evs, "mod_audio_stream::play event never reached the controller"
    await asyncio.sleep(1.2)  # let FreeSWITCH finish playing the 300 ms reply and flush the recording
    await e.ctrl.esl_cmd.api(f"uuid_kill {fs['fs_uuid']} NORMAL_CLEARING")
    await asyncio.sleep(1.0)
    audible_s, peak = heard_stats(since)
    if not (audible_s >= 0.2 and peak > 2000):
        files = [(f, os.path.getsize(f), round(time.time() - os.path.getmtime(f), 1)) for f in sorted(glob.glob("/tmp/mme_ai_heard_*.wav"))[-4:]]
        evs = [(x["type"], x["source"]) for x in await e.db["call_events"].find({"call_id": call["id"]}).sort("ts", 1).to_list(40)]
        raise AssertionError(f"caller heard {audible_s:.2f}s peak {peak}; files={files}; events={evs}; timings={a['timings_ms']}")
    a = await e.db["ai_sessions"].find_one({"session_id": sid})
    print(f"\nMEASURED active_after_start={t_active - t0:.2f}s timings_ms={a['timings_ms']} audible_s={audible_s:.2f}")
    assert "module_return_ms" in a["timings_ms"] and "broadcast_cmd_ms" in a["timings_ms"]


async def test_barge_in_really_interrupts_playback_on_the_call(rig):
    e = rig
    e.models.tts_ms = 6000  # long reply; the caller talks again at ~+2.2 s after the first utterance
    since = time.time() - 1
    call, fs, _ = await start_ai_call(e, "loopback/9011/mme_test")
    sid = call["session_id"]
    await wait_ai(e, sid, {"active"})
    for _ in range(150):
        if await e.db["call_events"].count_documents({"call_id": call["id"], "type": "media.play"}):
            break
        await asyncio.sleep(0.1)
    await asyncio.sleep(4.5)  # caller's second utterance happens during the reply; wait for it and for the break
    await e.ctrl.esl_cmd.api(f"uuid_kill {fs['fs_uuid']} NORMAL_CLEARING")
    await asyncio.sleep(1.0)
    audible_s, _ = heard_stats(since)
    assert 0.2 <= audible_s < 4.5, f"reply should have been cut well short of 6 s, caller heard {audible_s:.2f}s"
    print(f"\nMEASURED barge-in: reply 6.0s synthesized, caller heard {audible_s:.2f}s")


async def test_hangup_stops_ai_and_media_stream(rig):
    e = rig
    call, fs, _ = await start_ai_call(e)
    sid = call["session_id"]
    await wait_ai(e, sid, {"active"})
    await e.ctrl.esl_cmd.api(f"uuid_kill {fs['fs_uuid']} NORMAL_CLEARING")
    ai = await wait_ai(e, sid, {"stopped", "failed"})
    assert ai["state"] == "stopped" and ai["stop_reason"] == "call_ended"
    assert (await e.ctrl.esl_cmd.api(f"uuid_exists {fs['fs_uuid']}")).strip() == "false"  # the call itself is gone
    assert sid not in e.ai.runtime


async def test_media_socket_refuses_missing_wrong_or_viewer_tokens(rig):
    import websockets
    e = rig
    url = f"ws://{e.host}/v1/ai/media/fs"
    for hdr in ({}, {"Authorization": "Bearer garbage"}):
        with pytest.raises(Exception):
            async with websockets.connect(url, additional_headers=hdr) as ws:
                await ws.recv()
    from tests.conftest import token
    with pytest.raises(Exception):  # viewer role may not open a media socket
        async with websockets.connect(url, additional_headers={"Authorization": "Bearer " + token(roles=("telephony:viewer",))}) as ws:
            await ws.recv()
