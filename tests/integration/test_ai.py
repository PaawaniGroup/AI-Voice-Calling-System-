"""AI contracts. Model services are MOCKS (tests/helpers.MockModels); the media client is SYNTHETIC.
Nothing here proves real-model behaviour, FreeSWITCH audio, or latency on a real call."""
import asyncio
import json

import httpx
import pytest
import pytest_asyncio
import websockets

from app.ai.clients import AiTimeout, AiUnavailable, ModelClients, validate_service_url
from app.core.errors import Unprocessable
from app.main import create_app
from app.providers.base import MockProvider
from tests.conftest import JWT_SECRET, auth, call_body, enable_tenant, idem, make_settings, token
from tests.helpers import MockModels, live_server, pcm_silence, pcm_tone
from tests.integration.test_api_calls import esl, make_call, wait_state

ADMIN = lambda: auth("t1", ("telephony:admin",))  # noqa: E731
CFG = {"stt_url": "http://stt.test:9000", "brain_url": "http://brain.test:9000", "tts_url": "http://tts.test:9000",
       "rag_url": "http://rag.test:9000", "enabled": True}


class ReadyBridge:
    name = "synthetic-test-bridge"

    async def ready(self):
        return True, "synthetic test bridge"


@pytest_asyncio.fixture
async def aienv(db):
    s = make_settings(mongo_db=db.s.mongo_db, ai_enabled=True, ai_allowed_hosts="stt.test,brain.test,tts.test,rag.test",
                      ai_api_key="test-model-key-123456")
    models = MockModels()
    app = create_app(s, db=db, provider=MockProvider(), ai_transport=models.transport())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            e = type("E", (), {})()
            e.app, e.client, e.models, e.db, e.s = app, c, models, db, s
            e.repos, e.ctrl, e.ai = app.state.repos, app.state.controller, app.state.ai
            yield e


async def answered_ai_call(e, ai_enabled=True):
    await enable_tenant(e)
    c = await make_call(e, ai_enabled=ai_enabled)
    c2 = await wait_state(e, c["id"], "initiating")
    await e.ctrl.on_esl_event(esl("CHANNEL_ANSWER", c2["fs_uuid"]))
    return c, c2


# ------------------------------------------------------------------ configuration
def test_service_url_validation():
    s = make_settings(ai_allowed_hosts="stt.test")
    assert validate_service_url("http://stt.test:8000/", s) == "http://stt.test:8000"
    for bad in ["http://evil.test", "ftp://stt.test", "http://u:p@stt.test", "http://stt.test/?x=1", "http://169.254.169.254", "javascript:alert(1)", "http://"]:
        with pytest.raises(Unprocessable):
            validate_service_url(bad, s)


async def test_config_admin_only_masked_for_viewers_and_audited(aienv):
    e = aienv
    assert (await e.client.put("/v1/ai/config", json=CFG, headers=auth())).status_code == 403
    r = await e.client.put("/v1/ai/config", json=CFG, headers=ADMIN())
    assert r.status_code == 200 and r.json()["stt_url"] == CFG["stt_url"] and r.json()["models"]["brain"] == "Qwen3-30B-A3B-Instruct-2507"
    assert "test-model-key" not in r.text
    v = (await e.client.get("/v1/ai/config", headers=auth("t1", ("telephony:viewer",)))).json()
    assert v["stt_url"] == "configured" and "stt.test" not in json.dumps(v)  # viewers never see endpoints
    bad = await e.client.put("/v1/ai/config", json={"stt_url": "http://evil.example"}, headers=ADMIN())
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "service_host_not_allowed"
    assert (await e.client.put("/v1/ai/config", json={"timeouts": {"stt": 9999}}, headers=ADMIN())).status_code == 422
    assert (await e.client.put("/v1/ai/config", json={"paths": {"stt": "/../etc"}}, headers=ADMIN())).status_code == 422
    assert (await e.client.put("/v1/ai/config", json={"bogus": 1}, headers=ADMIN())).status_code == 422
    assert await e.db["audit"].count_documents({"action": "ai.config.put"}) == 1
    assert (await e.client.get("/v1/ai/config", headers=auth("t2", ("telephony:admin",)))).json()["stt_url"] == ""  # tenant isolation


async def test_health_distinguishes_healthy_unreachable_not_configured(aienv):
    e = aienv
    await e.client.put("/v1/ai/config", json={**CFG, "rag_url": ""}, headers=ADMIN())
    e.models.healthy = {"stt", "brain"}  # tts returns 503
    h = (await e.client.get("/v1/ai/health", headers=auth())).json()
    assert h["services"]["stt"]["status"] == "healthy" and h["services"]["tts"]["status"] == "unhealthy" and h["services"]["rag"]["status"] == "not_configured"
    assert h["media_bridge"]["ready"] is False and h["checked_at"]


async def test_adapter_timeout_and_unreachable_are_classified(db):
    s = make_settings(mongo_db=db.s.mongo_db, ai_allowed_hosts="stt.test")
    m = MockModels()
    m.stt_delay = 2.0
    c = ModelClients(s, {"stt_url": "http://stt.test:1", "timeouts": {"stt": 0.2}}, m.transport())
    with pytest.raises(AiTimeout):
        await c.transcribe(pcm_tone(200), 16000, "auto")
    await c.aclose()

    def boom(request):
        raise httpx.ConnectError("refused")
    c = ModelClients(s, {"stt_url": "http://stt.test:1"}, httpx.MockTransport(boom))
    with pytest.raises(AiUnavailable):
        await c.transcribe(pcm_tone(200), 16000, "auto")
    await c.aclose()
    with pytest.raises(AiUnavailable):
        await ModelClients(s, {}, m.transport()).chat([{"role": "user", "content": "x"}])  # not configured


# ------------------------------------------------------------------ start preconditions (never fake readiness)
async def test_start_refusals_in_order(aienv):
    e = aienv
    c, _ = await answered_ai_call(e)
    sid = c["session_id"]
    start = lambda: e.client.post(f"/v1/ai/sessions/{sid}/start", json={}, headers=auth())  # noqa: E731
    assert (await start()).json()["error"]["code"] == "ai_disabled_for_tenant"
    await e.client.put("/v1/ai/config", json=CFG, headers=ADMIN())
    r = await start()
    assert r.status_code == 409 and r.json()["error"]["code"] == "media_bridge_unavailable"  # default bridge: honest refusal
    assert "ESL" in r.json()["error"]["details"]["reason"] or "media" in r.json()["error"]["details"]["reason"]
    g = (await e.client.get(f"/v1/ai/sessions/{sid}", headers=auth())).json()
    assert g["state"] == "not_started" and g["media"]["connected"] is False and g["media"]["bridge_ready"] is False
    assert await e.db["ai_sessions"].count_documents({}) == 0  # nothing pretended to start


async def test_start_requires_answered_call_flag_and_global_switch(aienv, db):
    e = aienv
    e.ai.bridge = ReadyBridge()
    await e.client.put("/v1/ai/config", json=CFG, headers=ADMIN())
    await enable_tenant(e)
    c = await make_call(e, ai_enabled=True)
    r = await e.client.post(f"/v1/ai/sessions/{c['session_id']}/start", json={}, headers=auth())
    assert r.json()["error"]["code"] == "session_not_answered"
    c2 = await wait_state(e, c["id"], "initiating")
    await e.ctrl.on_esl_event(esl("CHANNEL_ANSWER", c2["fs_uuid"]))
    n = await make_call(e, ai_enabled=False)
    n2 = await wait_state(e, n["id"], "initiating")
    await e.ctrl.on_esl_event(esl("CHANNEL_ANSWER", n2["fs_uuid"]))
    assert (await e.client.post(f"/v1/ai/sessions/{n['session_id']}/start", json={}, headers=auth())).json()["error"]["code"] == "call_not_ai_enabled"
    assert (await e.client.post(f"/v1/ai/sessions/{c['session_id']}/start", json={}, headers=auth("t2"))).status_code == 404
    e.ai.s = e.s.model_copy(update={"ai_enabled": False})
    assert (await e.client.post(f"/v1/ai/sessions/{c['session_id']}/start", json={}, headers=auth())).json()["error"]["code"] == "ai_disabled"


# ------------------------------------------------------------------ synthetic media session end to end
@pytest_asyncio.fixture
async def media(db):
    s = make_settings(mongo_db=db.s.mongo_db, ai_enabled=True, ai_allowed_hosts="stt.test,brain.test,tts.test,rag.test", ai_api_key="test-model-key-123456")
    models = MockModels()
    app = create_app(s, db=db, provider=MockProvider(), ai_transport=models.transport())
    async with live_server(app) as host:
        app.state.ai.bridge = ReadyBridge()
        async with httpx.AsyncClient(base_url=f"http://{host}", timeout=10) as c:
            e = type("E", (), {})()
            e.app, e.c, e.host, e.models, e.db = app, c, host, models, db
            e.repos, e.ctrl, e.ai = app.state.repos, app.state.controller, app.state.ai
            await enable_tenant(e)
            assert (await c.put("/v1/ai/config", json=CFG, headers=ADMIN())).status_code == 200
            call = (await c.post("/v1/calls", json=call_body(ai_enabled=True, language="hinglish"), headers={**auth(), **idem()})).json()
            c2 = await wait_state(e, call["id"], "initiating")
            await e.ctrl.on_esl_event(esl("CHANNEL_ANSWER", c2["fs_uuid"]))
            e.call, e.fs_uuid, e.sid = call, c2["fs_uuid"], call["session_id"]
            r = await c.post(f"/v1/ai/sessions/{e.sid}/start", json={}, headers=auth())
            assert r.status_code == 202 and r.json()["state"] == "starting"
            yield e


async def connect(e, tok=None):
    ws = await websockets.connect(f"ws://{e.host}/v1/ai/sessions/{e.sid}/media")
    await ws.send(json.dumps({"type": "auth", "token": tok or token()}))
    return ws


async def hello(ws, rate=16000):
    assert json.loads(await ws.recv())["type"] == "auth_ok"
    await ws.send(json.dumps({"type": "hello", "encoding": "pcm_s16le", "sample_rate": rate, "channels": 1, "frame_ms": 20}))
    assert json.loads(await ws.recv())["type"] == "ready"


async def speak(ws, ms=600, rate=16000, silence=900):
    for chunk in (pcm_tone(ms, rate), pcm_silence(silence, rate)):
        fb = rate * 20 // 1000 * 2
        for i in range(0, len(chunk), fb):
            await ws.send(chunk[i:i + fb])
            await asyncio.sleep(0.002)


async def collect(ws, until, timeout=8):
    msgs, audio = [], 0
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        m = await asyncio.wait_for(ws.recv(), timeout)
        if isinstance(m, bytes):
            audio += len(m)
            continue
        msgs.append(json.loads(m))
        if msgs[-1]["type"] == until:
            break
    return msgs, audio


async def test_full_turn_stt_brain_tts_with_state_and_metrics(media):
    e = media
    ws = await connect(e)
    await hello(ws)
    st = (await e.c.get(f"/v1/ai/sessions/{e.sid}", headers=auth())).json()
    assert st["state"] == "active" and st["media"]["connected"] is True
    assert (await e.c.get(f"/v1/sessions/{e.sid}", headers=auth())).json()["status"] == "active"
    await speak(ws)
    msgs, audio = await collect(ws, "playback_end")
    assert [m["type"] for m in msgs][:1] == ["playback_start"] and audio > 4000 and msgs[0]["sample_rate"] == 16000
    kinds = [k for k, _ in e.models.calls]
    assert kinds == ["stt", "rag", "brain", "tts"]  # pipeline order
    assert all(b["auth"] == "Bearer test-model-key-123456" for k, b in e.models.calls if k in ("brain", "tts"))
    brain = next(b for k, b in e.models.calls if k == "brain")
    assert brain["messages"][0]["role"] == "system" and "CONTEXT" in brain["messages"][-1]["content"]  # RAG context reached the brain
    ai = (await e.c.get(f"/v1/ai/sessions/{e.sid}", headers=auth())).json()
    assert ai["turns"] == 1 and set(ai["timings_ms"]) >= {"stt", "brain", "tts"}
    await ws.close()


async def test_second_turn_keeps_conversation_context(media):
    e = media
    ws = await connect(e)
    await hello(ws)
    for _ in range(2):
        await speak(ws)
        await collect(ws, "playback_end")
    brains = [b for k, b in e.models.calls if k == "brain"]
    assert len(brains) == 2 and len(brains[1]["messages"]) > len(brains[0]["messages"])
    assert any(m["role"] == "assistant" for m in brains[1]["messages"])
    await ws.close()


async def test_barge_in_interrupts_playback(media):
    e = media
    e.models.tts_ms = 3000  # long reply so the caller can interrupt
    ws = await connect(e)
    await hello(ws)
    await speak(ws)
    msgs, _ = await collect(ws, "playback_start")
    for _ in range(15):  # caller talks over the assistant
        await ws.send(pcm_tone(20))
        await asyncio.sleep(0.01)
    got = []
    for _ in range(100):
        m = await asyncio.wait_for(ws.recv(), 5)
        if isinstance(m, str):
            got.append(json.loads(m)["type"])
            if "interrupt" in got:
                break
    assert "interrupt" in got and "playback_end" not in got
    await ws.close()


async def test_hangup_cancels_ai_and_closes_media_no_stale_audio(media):
    e = media
    e.models.stt_delay = 1.0
    ws = await connect(e)
    await hello(ws)
    await speak(ws, silence=800)  # inference now in flight
    await asyncio.sleep(0.3)
    await e.ctrl.on_esl_event(esl("CHANNEL_HANGUP", e.fs_uuid, **{"Hangup-Cause": "NORMAL_CLEARING"}))
    ai = await e.db["ai_sessions"].find_one({"session_id": e.sid})
    assert ai["state"] == "stopped" and ai["stop_reason"] == "call_ended"
    assert e.sid not in e.ai.runtime
    audio_after = 0
    try:
        while True:
            m = await asyncio.wait_for(ws.recv(), 2.5)
            if isinstance(m, bytes):
                audio_after += len(m)
    except (websockets.ConnectionClosed, asyncio.TimeoutError):
        pass
    assert audio_after == 0  # nothing synthesised after hangup reaches the caller
    assert (await e.c.get(f"/v1/sessions/{e.sid}", headers=auth())).json()["status"] == "ended"


async def test_model_timeout_is_nonfatal_error_event(media):
    e = media
    cfg = {"timeouts": {"stt": 0.5}}
    assert (await e.c.put("/v1/ai/config", json=cfg, headers=ADMIN())).status_code == 200
    # runtime captured the config at start; restart AI with the shorter timeout
    await e.c.post(f"/v1/ai/sessions/{e.sid}/stop", headers=auth())
    assert (await e.c.post(f"/v1/ai/sessions/{e.sid}/start", json={}, headers=auth())).status_code == 202
    e.models.stt_delay = 1.5
    ws = await connect(e)
    await hello(ws)
    await speak(ws)
    msgs, _ = await collect(ws, "error")
    assert msgs[-1]["code"] == "stt_timeout" and msgs[-1]["fatal"] is False
    e.models.stt_delay = 0
    await speak(ws)  # session survives the failure and recovers
    msgs, audio = await collect(ws, "playback_end")
    assert audio > 0
    await ws.close()


async def test_media_ws_auth_and_format_enforced(media):
    e = media
    ws = await websockets.connect(f"ws://{e.host}/v1/ai/sessions/{e.sid}/media")
    await ws.send(json.dumps({"type": "auth", "token": "garbage"}))
    assert json.loads(await ws.recv())["code"] == "unauthorized"
    ws = await connect(e, token("t2", roles=("telephony:user",)))  # other tenant cannot attach
    assert json.loads(await ws.recv())["code"] == "no_ai_session"
    ws = await connect(e, token(roles=("telephony:viewer",)))  # viewer role may not stream
    assert json.loads(await ws.recv())["code"] == "forbidden"
    ws = await connect(e)
    assert json.loads(await ws.recv())["type"] == "auth_ok"
    await ws.send(json.dumps({"type": "hello", "encoding": "mulaw", "sample_rate": 8000, "channels": 1, "frame_ms": 20}))
    assert json.loads(await ws.recv())["code"] == "unsupported_format"


async def test_media_reconnect_resumes_same_conversation(media):
    e = media
    ws = await connect(e)
    await hello(ws)
    await speak(ws)
    await collect(ws, "playback_end")
    await ws.close()
    for _ in range(40):
        if (await e.db["ai_sessions"].find_one({"session_id": e.sid}))["state"] == "media_disconnected":
            break
        await asyncio.sleep(0.1)
    assert (await e.db["ai_sessions"].find_one({"session_id": e.sid}))["state"] == "media_disconnected"
    ws = await connect(e)
    await hello(ws)
    assert (await e.db["ai_sessions"].find_one({"session_id": e.sid}))["state"] == "active"
    await speak(ws)
    await collect(ws, "playback_end")
    brains = [b for k, b in e.models.calls if k == "brain"]
    assert len(brains) == 2 and any(m["role"] == "assistant" for m in brains[1]["messages"])  # context survived the reconnect
    await ws.close()
    await e.c.post(f"/v1/ai/sessions/{e.sid}/stop", headers=auth())
    again = await e.c.post(f"/v1/ai/sessions/{e.sid}/stop", headers=auth())
    assert again.status_code == 200 and again.json()["state"] == "stopped"  # idempotent


async def test_media_never_connecting_fails_the_ai_session(media):
    e = media
    await e.c.post(f"/v1/ai/sessions/{e.sid}/stop", headers=auth())
    e.ai.connect_s = 0.3
    assert (await e.c.post(f"/v1/ai/sessions/{e.sid}/start", json={}, headers=auth())).status_code == 202
    for _ in range(50):
        ai = await e.db["ai_sessions"].find_one({"session_id": e.sid})
        if ai["state"] == "failed":
            break
        await asyncio.sleep(0.1)
    assert ai["state"] == "failed" and ai["error"] == "media_connect_timeout"
    assert (await e.c.get(f"/v1/sessions/{e.sid}", headers=auth())).json()["status"] == "answered"  # call continues without AI
