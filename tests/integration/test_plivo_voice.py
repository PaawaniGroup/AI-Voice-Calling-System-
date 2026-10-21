"""Plivo Voice Application -> existing MME_Telephony lifecycle -> existing AI pipeline.

MOCKED: Plivo (requests are signed with the SDK's own signature primitives and a fake token; the media client is a test
WebSocket speaking Plivo's documented stream messages) and the STT/Brain/TTS servers (httpx mock transport). The service
itself (routes, controller, MongoDB, AiService, conversation loop) is real. Nothing here proves behaviour with real
Plivo, real models or a real phone line.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import struct
from datetime import timedelta
import time
import uuid
import xml.etree.ElementTree as ET

import httpx
import pytest
import pytest_asyncio
import websockets
from plivo.utils.signature_v3 import construct_post_url, get_signature_v3

from app.ai import codec
from app.core.ids import utcnow
from app.main import create_app
from app.providers.base import MockProvider, ProviderAdapter, ProviderError
from tests.conftest import auth, make_settings
from tests.helpers import MockModels, live_server, pcm_silence, pcm_tone

BASE = "https://tel.example.test"
WSS = "wss://tel.example.test/v1/providers/plivo/stream"
TOKEN = "plivo-test-token-not-real"
DID = "+911140001111"
CFG = {"stt_url": "http://stt.test:9000", "brain_url": "http://brain.test:9000", "tts_url": "http://tts.test:9000",
       "rag_url": "http://rag.test:9000", "enabled": True}
ADMIN = lambda: auth("t1", ("telephony:admin",))  # noqa: E731
RATE = 8000


class Models(MockModels):
    """Mock model servers that also remember how big the STT upload was and which WAV rate it carried."""

    def __init__(self):
        super().__init__()
        self.stt_uploads: list[tuple[int, int]] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/audio/transcriptions":
            body = request.content
            i = body.find(b"RIFF")
            self.stt_uploads.append((len(body), struct.unpack_from("<I", body, i + 24)[0] if i >= 0 else -1))
        return await super().handler(request)


class FakePlivo(ProviderAdapter):
    """Stands in for the Plivo REST client (never called for real in tests)."""
    name = "plivo_api"

    def __init__(self):
        self.hangups: list[tuple[str, str]] = []
        self.fail = False

    async def originate(self, req):  # pragma: no cover - never used here
        raise ProviderError("not used")

    async def hangup(self, call, cause):
        if self.fail:
            raise ProviderError("plivo_hangup_failed")
        self.hangups.append((call["_id"], cause))


def sign(path, params, *, nonce=None, token=TOKEN, query="", base=BASE):
    nonce = nonce or uuid.uuid4().hex
    url = base + path + (f"?{query}" if query else "")
    sig = get_signature_v3(token.encode(), construct_post_url(url, params), nonce.encode())
    sig = sig.decode() if isinstance(sig, bytes) else sig
    return {"X-Plivo-Signature-V3": sig, "X-Plivo-Signature-V3-Nonce": nonce}


def ws_headers(*, nonce=None, token=TOKEN, uri=WSS):
    nonce = nonce or uuid.uuid4().hex
    sig = get_signature_v3(token.encode(), uri.encode(), nonce.encode())
    return {"X-Plivo-Signature-V3": sig.decode() if isinstance(sig, bytes) else sig, "X-Plivo-Signature-V3-Nonce": nonce}


async def until(cond, timeout=6.0, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        r = await cond()
        if r:
            return r
        await asyncio.sleep(step)
    return await cond()


@pytest_asyncio.fixture
async def pv(db):
    s = make_settings(mongo_db=db.s.mongo_db, provider="mock", plivo_inbound_enabled=True, plivo_auth_token=TOKEN,
                      webhook_public_base_url=BASE, ai_enabled=True, ai_allowed_hosts="stt.test,brain.test,tts.test,rag.test",
                      ai_api_key="test-model-key-123456", plivo_orphan_grace_s=30, plivo_stream_connect_s=20)
    models, fake, prov = Models(), FakePlivo(), MockProvider()
    app = create_app(s, db=db, provider=prov, ai_transport=models.transport())
    async with live_server(app) as host:
        app.state.controller.providers["plivo_api"] = fake  # the real REST client must never run in tests
        async with httpx.AsyncClient(base_url=f"http://{host}", timeout=15) as c:
            e = type("PV", (), {})()
            e.s, e.app, e.c, e.host, e.models, e.fake, e.prov, e.db = s, app, c, host, models, fake, prov, db
            e.repos, e.ctrl, e.ai = app.state.repos, app.state.controller, app.state.ai
            await e.repos.put_policy("t1", {"enabled": True, "caller_ids": ["+911140000000"], "max_active_calls": 5,
                                            "transfer_destinations": ["+919999999999"]})
            await db["inbound_routes"].insert_one({"number": DID, "tenant_id": "t1", "action": "ai", "enabled": True})
            assert (await c.put("/v1/ai/config", json=CFG, headers=ADMIN())).status_code == 200
            yield e


def form(call_uuid=None, **kw):
    return {"CallUUID": call_uuid or str(uuid.uuid4()), "Direction": "inbound", "To": DID.lstrip("+"), "From": "919876543210", **kw}


async def post_answer(pv, params, **sk):
    return await pv.c.post("/v1/providers/plivo/answer", data=params, headers=sign("/v1/providers/plivo/answer", params, **sk))


async def post_cb(pv, name, params, **sk):
    path = f"/v1/providers/plivo/{name}"
    return await pv.c.post(path, data=params, headers=sign(path, params, **sk))


def stream_of(resp_text):
    root = ET.fromstring(resp_text)
    st = root.find("Stream")
    return root, st


async def answered_stream(pv, **kw):
    """Run the real Answer step and return what Plivo would need to open the stream."""
    params = form(**kw)
    r = await post_answer(pv, params)
    assert r.status_code == 200, r.text
    _, st = stream_of(r.text)
    assert st is not None, r.text
    call = await pv.db["calls"].find_one({"provider_call_id": params["CallUUID"]})
    return type("A", (), {"params": params, "extra": st.get("extraHeaders"), "call": call, "uuid": params["CallUUID"],
                          "sid": call["session_id"], "stream_id": str(uuid.uuid4())})()


def start_msg(a, **over):
    m = {"event": "start", "sequenceNumber": 1, "start": {"callId": a.uuid, "streamId": a.stream_id, "accountId": "MAXXXXXXXX",
                                                          "tracks": ["inbound"], "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000}},
         "extra_headers": a.extra}
    for k, v in over.items():
        if k in ("start",):
            m["start"].update(v)
        else:
            m[k] = v
    return m


def media_frames(pcm, first_chunk=1, track="inbound"):
    mu = codec.pcm16_to_mulaw(pcm)
    out = []
    for i, off in enumerate(range(0, len(mu), 160)):
        out.append({"event": "media", "sequenceNumber": first_chunk + i, "streamId": "x", "extra_headers": "",
                    "media": {"track": track, "timestamp": str(20 * (first_chunk + i)), "chunk": first_chunk + i,
                              "payload": base64.b64encode(mu[off:off + 160]).decode()}})
    return out


def utterance(speech_ms=600, silence_ms=800):
    return pcm_tone(speech_ms, RATE, amp=6000) + pcm_silence(silence_ms, RATE)


async def connect(pv, a, *, headers=None, send_start=True):
    ws = await websockets.connect(f"ws://{pv.host}/v1/providers/plivo/stream", additional_headers=headers or ws_headers())
    if send_start:
        await ws.send(json.dumps(start_msg(a)))
    return ws


async def send_media(ws, pcm, first_chunk=1):
    for f in media_frames(pcm, first_chunk):
        await ws.send(json.dumps(f))
        await asyncio.sleep(0.001)
    return first_chunk + len(media_frames(pcm, first_chunk))


async def recv_until(ws, pred, timeout=8):
    out = []
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        m = json.loads(await asyncio.wait_for(ws.recv(), max(0.1, end - time.monotonic())))
        out.append(m)
        if pred(m):
            return out
    raise AssertionError(f"condition not met; got {[x.get('event') for x in out]}")


async def ai_doc(pv, sid):
    return await pv.db["ai_sessions"].find_one({"session_id": sid})


# =================================================================================== Answer callback
async def test_valid_answer_returns_stream_xml_and_creates_durable_call_and_session(pv):
    params = form("CALL-AAA-1")
    r = await post_answer(pv, params)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    root, st = stream_of(r.text)
    assert root.tag == "Response" and st is not None
    assert st.get("bidirectional") == "true" and st.get("keepCallAlive") == "true" and st.get("contentType") == "audio/x-mulaw;rate=8000"
    assert st.get("extraHeaders").startswith("mme_ticket=") and ";" not in st.get("extraHeaders") and st.text == WSS
    assert st.get("statusCallbackUrl") == BASE + "/v1/providers/plivo/stream-status" and st.get("statusCallbackMethod") == "POST"
    call = await pv.db["calls"].find_one({"provider_call_id": "CALL-AAA-1"})
    assert call["direction"] == "inbound" and call["provider"] == "plivo_api" and call["tenant_id"] == "t1"
    assert call["state"] == "answered" and call["answered_at"] and call["ai_enabled"] is True and call["to_number"] == DID
    assert call["route_action"] == "ai" and call["stream_expected_by"] and "9876543210" in call["from_number"]
    sess = await pv.db["sessions"].find_one({"_id": call["session_id"]})
    assert sess["call_id"] == call["_id"] and sess["status"] == "answered" and sess["tenant_id"] == "t1"
    evs = [e["type"] for e in await pv.db["call_events"].find({"call_id": call["_id"]}).to_list(20)]
    assert "state.answered" in evs
    assert "token" not in r.text.lower() and TOKEN not in r.text  # no secret in the response
    # existing management API sees the call, with its session
    got = (await pv.c.get(f"/v1/calls/{call['_id']}", headers=auth())).json()
    assert got["session_id"] == call["session_id"] and got["state"] == "answered" and got["direction"] == "inbound"


async def test_missing_or_invalid_signature_rejected_and_nothing_created(pv):
    params = form("CALL-SIG-1")
    path = "/v1/providers/plivo/answer"
    assert (await pv.c.post(path, data=params)).status_code == 401  # no headers
    assert (await pv.c.post(path, data=params, headers=sign(path, params, token="wrong-token"))).status_code == 401
    tampered = sign(path, params)
    assert (await pv.c.post(path, data={**params, "To": "919999999999"}, headers=tampered)).status_code == 401  # body changed
    assert (await pv.c.post(path, data=params, headers=sign("/v1/providers/plivo/status", params))).status_code == 401  # signed for another URL
    assert (await pv.c.post(path, data=params, headers=sign(path, params, base="https://evil.example"))).status_code == 401  # other host
    assert (await pv.c.post(path, data=params, headers={"X-Plivo-Signature-V3": "AAAA", "X-Plivo-Signature-V3-Nonce": "n"})).status_code == 401
    assert await pv.db["calls"].count_documents({}) == 0
    r = await pv.c.post(path, data=params, headers=sign(path, params, token="x"))
    assert r.json()["error"]["code"] == "invalid_signature" and TOKEN not in r.text


async def test_forwarded_headers_cannot_change_the_url_that_is_validated(pv):
    params = form("CALL-FWD-1")
    path = "/v1/providers/plivo/answer"
    hdr = {**sign(path, params), "X-Forwarded-Host": "evil.example", "X-Forwarded-Proto": "http", "Host": "evil.example"}
    r = await pv.c.post(path, data=params, headers=hdr)
    assert r.status_code == 200  # signature was made over the configured public URL; spoofed forwarding headers are ignored
    bad = {**sign(path, params, base="https://evil.example"), "X-Forwarded-Host": "tel.example.test"}
    assert (await pv.c.post(path, data=params, headers=bad)).status_code == 401  # claiming the right host does not help


async def test_malformed_payloads(pv):
    path = "/v1/providers/plivo/answer"
    for bad in ({"Direction": "inbound", "To": DID.lstrip("+")}, {**form(), "CallUUID": ""}, {**form(), "CallUUID": "bad id; DROP"},
                {**form(), "CallUUID": "x" * 80}):
        r = await pv.c.post(path, data=bad, headers=sign(path, bad))
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_payload", bad
    for p in ({**form("MAL-1"), "To": "abc"}, {**form("MAL-2"), "To": ""}):
        assert "Hangup" in (await post_answer(pv, p)).text
    huge = {"CallUUID": "BIG-1", "Junk": "x" * 200000}
    assert (await pv.c.post(path, data=huge, headers=sign(path, huge))).status_code == 413
    assert (await pv.c.post(path, content=b"\xff\xfe\x00 not a form", headers={**sign(path, {}), "Content-Type": "application/x-www-form-urlencoded"})).status_code in (400, 401, 422)
    assert await pv.db["calls"].count_documents({}) == 0


async def test_duplicate_answers_same_call_same_decision_no_second_call_or_session(pv):
    params = form("CALL-DUP-1")
    first = await post_answer(pv, params, nonce="n-fixed")
    replay = await post_answer(pv, params, nonce="n-fixed")           # identical signed request (replayed nonce)
    retry = await post_answer(pv, params)                              # provider retry with a fresh nonce
    for r in (first, replay, retry):
        assert r.status_code == 200 and stream_of(r.text)[1] is not None, r.text  # never a Hangup for a live call
    assert await pv.db["calls"].count_documents({"provider_call_id": "CALL-DUP-1"}) == 1
    assert await pv.db["sessions"].count_documents({}) == 1
    # a replayed request for a call we do NOT know creates nothing
    ghost = form("CALL-GHOST-1")
    await post_answer(pv, ghost, nonce="n-ghost")
    await pv.db["calls"].delete_one({"provider_call_id": "CALL-GHOST-1"})
    assert "Hangup" in (await post_answer(pv, ghost, nonce="n-ghost")).text
    assert await pv.db["calls"].count_documents({"provider_call_id": "CALL-GHOST-1"}) == 0


async def test_concurrent_duplicate_answers_create_exactly_one_call(pv):
    params = form("CALL-RACE-1")
    rs = await asyncio.gather(*[post_answer(pv, params) for _ in range(10)])
    assert all(r.status_code == 200 and stream_of(r.text)[1] is not None for r in rs), [r.text[:80] for r in rs]
    assert await pv.db["calls"].count_documents({"provider_call_id": "CALL-RACE-1"}) == 1
    assert await pv.db["sessions"].count_documents({}) == 1
    assert await pv.db["call_events"].count_documents({"type": "state.answered"}) == 1


async def test_uuid_mapping_call_session_stream(pv):
    a = await answered_stream(pv, call_uuid="MAP-CALL-1")
    ws = await connect(pv, a)
    doc = await until(lambda: _active(pv, a.sid))
    assert doc["state"] == "active"
    call = await pv.db["calls"].find_one({"_id": a.call["_id"]})
    assert call["provider_call_id"] == "MAP-CALL-1" and call["stream_id"] == a.stream_id and call["stream_connected"] is True
    sess = await pv.db["sessions"].find_one({"_id": a.sid})
    assert sess["call_id"] == call["_id"] and sess["status"] == "active"
    assert (await pv.c.get(f"/v1/calls/{call['_id']}/session", headers=auth())).json()["session_id"] == a.sid
    assert (await pv.c.get(f"/v1/calls/{call['_id']}", headers=auth("t2"))).status_code == 404  # tenant isolation
    await ws.close()


async def _active(pv, sid):
    d = await ai_doc(pv, sid)
    return d if d and d["state"] == "active" else None


# =================================================================================== routing and policy
async def test_unrouted_disabled_and_busy_inbound_are_rejected_safely(pv):
    r = await post_answer(pv, {**form("RT-1"), "To": "919000000000"})  # no route
    assert "Hangup" in r.text and "Stream" not in r.text
    await pv.repos.put_policy("t1", {"enabled": False})
    assert "Hangup" in (await post_answer(pv, form("RT-2"))).text  # tenant disabled
    assert await pv.db["calls"].count_documents({}) == 0
    await pv.repos.put_policy("t1", {"enabled": True, "max_active_calls": 1})
    assert "Stream" in (await post_answer(pv, form("RT-3"))).text
    busy = await post_answer(pv, form("RT-4"))
    assert 'reason="busy"' in busy.text and await pv.db["calls"].count_documents({"provider_call_id": "RT-4"}) == 0


async def test_disabled_route_is_not_served(pv):
    await pv.db["inbound_routes"].update_one({"number": DID}, {"$set": {"enabled": False}})
    assert "Hangup" in (await post_answer(pv, form("RT-5"))).text


@pytest.mark.parametrize("break_it,why", [("global", "ai_disabled"), ("tenant", "ai_disabled_for_tenant"), ("urls", "ai_not_configured")])
async def test_ai_not_ready_records_a_failed_call_and_rejects_instead_of_streaming(pv, break_it, why):
    if break_it == "global":
        pv.ai.s = pv.s.model_copy(update={"ai_enabled": False})
    elif break_it == "tenant":
        await pv.c.put("/v1/ai/config", json={"enabled": False}, headers=ADMIN())
    else:
        await pv.db["ai_config"].update_one({"tenant_id": "t1"}, {"$set": {"stt_url": ""}})
    r = await post_answer(pv, form("NOAI-1"))
    assert "Hangup" in r.text and "Stream" not in r.text
    call = await pv.db["calls"].find_one({"provider_call_id": "NOAI-1"})
    assert call["state"] == "failed" and call["failure_reason"] == f"ai_unavailable:{why}"
    assert await pv.db["ai_sessions"].count_documents({}) == 0


async def test_other_route_actions_keep_their_behaviour(pv):
    await pv.db["inbound_routes"].update_one({"number": DID}, {"$set": {"action": "park"}})
    assert "<Wait" in (await post_answer(pv, form("ACT-1"))).text
    await pv.db["inbound_routes"].update_one({"number": DID}, {"$set": {"action": "bridge", "target": "+919999999999"}})
    r = await post_answer(pv, form("ACT-2"))
    assert "<Dial" in r.text and "919999999999" in r.text
    await pv.db["inbound_routes"].update_one({"number": DID}, {"$set": {"action": "hangup"}})
    assert "<Hangup/>" in (await post_answer(pv, form("ACT-3"))).text
    assert (await pv.db["calls"].find_one({"provider_call_id": "ACT-1"}))["ai_enabled"] is False


async def test_admin_can_configure_ai_route_through_the_existing_api(pv):
    plat = auth("platform", ("platform:admin",))
    r = await pv.c.put("/v1/admin/tenants/t2/routes", json={"number": "+911140002222", "action": "ai"}, headers=plat)
    assert r.status_code == 200 and r.json()["action"] == "ai"
    assert (await pv.c.put("/v1/admin/tenants/t2/routes", json={"number": "+911140003333", "action": "teleport"}, headers=plat)).status_code == 422


# =================================================================================== status / hangup callbacks
async def test_status_and_hangup_callbacks_drive_lifecycle_dedupe_and_end_everything(pv):
    a = await answered_stream(pv, call_uuid="LIFE-1")
    ws = await connect(pv, a)
    await until(lambda: _active(pv, a.sid))
    done = {"CallUUID": "LIFE-1", "CallStatus": "completed", "HangupCause": "NORMAL_CLEARING", "Duration": "7"}
    r = await post_cb(pv, "hangup", done)
    assert r.status_code == 200 and r.json()["status"] == "applied"
    again = await post_cb(pv, "status", done)               # provider retry through the other alias, fresh nonce
    assert again.json()["status"] == "duplicate"
    assert (await post_cb(pv, "hangup", done, nonce="same")).json()["status"] == "duplicate"
    call = await pv.db["calls"].find_one({"_id": a.call["_id"]})
    assert call["state"] == "completed" and call["connected_duration_s"] is not None and call["hangup_cause"] == "NORMAL_CLEARING"
    sess = await pv.db["sessions"].find_one({"_id": a.sid})
    assert sess["status"] == "ended"
    ai = await until(lambda: _stopped(pv, a.sid))
    assert ai["stop_reason"] == "call_ended" and a.sid not in pv.ai.runtime
    with pytest.raises(websockets.ConnectionClosed):          # the media socket was closed by the cleanup
        await asyncio.wait_for(ws.recv(), 3)
    assert (await post_cb(pv, "status", {"CallUUID": "NOPE-1", "CallStatus": "completed"})).json()["status"] == "ignored"
    bad = {"CallUUID": "LIFE-1", "CallStatus": "completed"}
    assert (await pv.c.post("/v1/providers/plivo/hangup", data=bad)).status_code == 401


async def _stopped(pv, sid):
    d = await ai_doc(pv, sid)
    return d if d and d["state"] in ("stopped", "failed") else None


async def test_events_webhooks_and_audit_survive_for_plivo_calls(pv):
    ep = await pv.c.post("/v1/webhooks/endpoints", json={"url": "http://receiver.test/h", "events": ["*"]}, headers=ADMIN())
    assert ep.status_code == 201
    a = await answered_stream(pv, call_uuid="WH-1")
    await post_cb(pv, "hangup", {"CallUUID": "WH-1", "CallStatus": "completed", "HangupCause": "NORMAL_CLEARING"})
    types = {d["event_type"] for d in await pv.db["webhook_deliveries"].find({}).to_list(100)}
    assert {"call.answered", "call.ended", "call.completed", "session.created", "session.answered", "session.ended"} <= types
    ev = [e["type"] for e in (await pv.c.get(f"/v1/calls/{a.call['_id']}/events", headers=auth())).json()["items"]]
    assert "state.answered" in ev and "state.completed" in ev
    assert await pv.db["recordings"].count_documents({}) == 0  # recording is FreeSWITCH-only: nothing is faked for Plivo calls
    metrics = (await pv.c.get("/metrics")).text
    assert any(ln.startswith("mme_tel_plivo_callbacks_total") and 'route="answer"' in ln and 'outcome="accepted"' in ln
               for ln in metrics.splitlines())


# =================================================================================== stream-status
async def test_stream_status_started_stopped_dedupe_and_failed_hangs_up(pv):
    a = await answered_stream(pv, call_uuid="SS-1")
    st = {"CallUUID": "SS-1", "StreamID": a.stream_id, "Event": "started", "StatusReason": ""}
    assert (await post_cb(pv, "stream-status", st)).json()["status"] == "recorded"
    assert (await post_cb(pv, "stream-status", st)).json()["status"] == "duplicate"
    assert (await post_cb(pv, "stream-status", {**st, "Event": "weird"})).json()["status"] == "ignored"
    assert (await post_cb(pv, "stream-status", {**st, "CallUUID": "UNKNOWN-1"})).json()["status"] == "ignored"
    assert (await pv.c.post("/v1/providers/plivo/stream-status", data=st)).status_code == 401
    failed = await post_cb(pv, "stream-status", {**st, "Event": "failed", "StatusReason": "connection_failed"})
    assert failed.json()["status"] == "recorded"
    assert pv.fake.hangups and pv.fake.hangups[-1][0] == a.call["_id"]  # asked Plivo (not FreeSWITCH) to hang up
    assert pv.prov.hangups == []
    call = await pv.db["calls"].find_one({"_id": a.call["_id"]})
    assert call["state"] == "answered"  # the final state still comes from the Hangup callback
    types = [e["type"] for e in await pv.db["call_events"].find({"call_id": call["_id"]}).to_list(50)]
    assert {"stream.started", "stream.failed"} <= set(types)


# =================================================================================== media WebSocket: authentication
async def test_ws_handshake_requires_valid_signature_and_unique_nonce(pv):
    a = await answered_stream(pv)
    url = f"ws://{pv.host}/v1/providers/plivo/stream"
    for hdrs in ({}, ws_headers(token="wrong"), ws_headers(uri="wss://evil.example/v1/providers/plivo/stream"),
                 {"X-Plivo-Signature-V3": "AAAA", "X-Plivo-Signature-V3-Nonce": "n"}):
        with pytest.raises(websockets.exceptions.InvalidStatus):
            await websockets.connect(url, additional_headers=hdrs)
    h = ws_headers(nonce="ws-nonce-1")
    ws = await websockets.connect(url, additional_headers=h)
    await ws.close()
    with pytest.raises(websockets.exceptions.InvalidStatus):  # same signed handshake replayed
        await websockets.connect(url, additional_headers=h)
    # the https twin of the URL is also accepted (the SDK helper only knows http(s)); nothing else is
    ws = await websockets.connect(url, additional_headers=ws_headers(uri=WSS.replace("wss://", "https://")))
    await ws.send(json.dumps(start_msg(a)))
    await until(lambda: _active(pv, a.sid))
    await ws.close()


@pytest.mark.parametrize("case", ["no_ticket", "garbage", "tampered", "other_call", "wrong_format", "not_start_first", "expired"])
async def test_ws_start_rejected_without_a_valid_ticket_for_this_call(pv, case):
    a = await answered_stream(pv, call_uuid="WS-AUTH-1")
    await answered_stream(pv, call_uuid="WS-AUTH-2")
    ws = await connect(pv, a, send_start=False)
    msg = start_msg(a)
    if case == "no_ticket":
        msg["extra_headers"] = ""
    elif case == "garbage":
        msg["extra_headers"] = "mme_ticket=not-a-ticket"
    elif case == "tampered":
        body, sig = a.extra.split("=", 1)[1].split(".")
        msg["extra_headers"] = f"mme_ticket={body}.{'A' * len(sig)}"
    elif case == "other_call":
        msg = start_msg(a, start={"callId": "WS-AUTH-2"})  # ticket is for call 1, claims call 2
        msg["extra_headers"] = a.extra
    elif case == "wrong_format":
        msg["start"]["mediaFormat"] = {"encoding": "audio/x-l16", "sampleRate": 16000}
    elif case == "expired":  # correctly signed, for this very call, but already past its expiry
        from app.security import tickets as T
        body = T._b64(json.dumps({"s": a.sid, "t": "t1", "c": a.uuid, "e": int(time.time()) - 5}, separators=(",", ":")).encode())
        msg["extra_headers"] = f"mme_ticket={body}.{T._b64(hmac.new(T._key(pv.s), body.encode(), hashlib.sha256).digest())}"
    if case == "not_start_first":
        await ws.send(json.dumps(media_frames(pcm_tone(20, RATE))[0]))
    else:
        await ws.send(json.dumps(msg))
    with pytest.raises(websockets.ConnectionClosed) as ei:
        await asyncio.wait_for(ws.recv(), 4)
    assert ei.value.rcvd.code in (1003, 1008)
    assert await pv.db["ai_sessions"].count_documents({}) == 0
    assert (await pv.db["calls"].find_one({"_id": a.call["_id"]}))["state"] == "answered"


async def test_ticket_cannot_open_two_streams_and_cannot_cross_tenants(pv):
    a = await answered_stream(pv, call_uuid="TWO-1")
    ws1 = await connect(pv, a)
    await until(lambda: _active(pv, a.sid))
    ws2 = await connect(pv, a)
    with pytest.raises(websockets.ConnectionClosed) as ei:
        await asyncio.wait_for(ws2.recv(), 4)
    assert ei.value.rcvd.code == 1008
    assert (await ai_doc(pv, a.sid))["state"] == "active"  # first stream unaffected
    await ws1.close()
    # a ticket naming another tenant's session is useless: tenant is part of the signed ticket and re-checked against the call
    from app.security import tickets
    forged = tickets.mint(pv.s, session_id=a.sid, tenant_id="t2", call_id="TWO-1")
    b = await answered_stream(pv, call_uuid="TWO-2")
    ws3 = await connect(pv, b, send_start=False)
    await ws3.send(json.dumps({**start_msg(b, start={"callId": "TWO-1"}), "extra_headers": f"mme_ticket={forged}"}))
    with pytest.raises(websockets.ConnectionClosed):
        await asyncio.wait_for(ws3.recv(), 4)


# =================================================================================== media WebSocket: audio and AI
async def test_full_turn_caller_audio_to_stt_brain_tts_and_playaudio_back(pv):
    a = await answered_stream(pv, call_uuid="TURN-1")
    ws = await connect(pv, a)
    doc = await until(lambda: _active(pv, a.sid))
    assert doc["media"]["hello"]["flavor"] == "plivo_stream" and doc["media"]["hello"]["sample_rate"] == 8000
    await send_media(ws, utterance())
    msgs = await recv_until(ws, lambda m: m.get("event") == "checkpoint")
    plays = [m for m in msgs if m["event"] == "playAudio"]
    assert plays, [m["event"] for m in msgs]
    for p in plays:
        assert p["media"]["contentType"] == "audio/x-mulaw" and p["media"]["sampleRate"] == 8000
        assert len(p["media"]["payload"]) <= 16 * 1024  # under Plivo's per-message guidance
    mu = b"".join(base64.b64decode(p["media"]["payload"], validate=True) for p in plays)
    assert abs(len(mu) - 2400) <= 4  # mock TTS = 300 ms @16 kHz -> 2400 mu-law bytes at 8 kHz
    pcm = codec.mulaw_to_pcm16(mu)
    assert max(abs(x) for x in struct.unpack("<%dh" % (len(pcm) // 2), pcm)) > 1000  # real audio, not silence
    cp = msgs[-1]
    assert cp["event"] == "checkpoint" and cp["streamId"] == a.stream_id and cp["name"] == "utt-1"
    assert [k for k, _ in pv.models.calls] == ["stt", "rag", "brain", "tts"]
    size, wav_rate = pv.models.stt_uploads[0]
    # buffered from the 200 ms speech gate: 21 speech frames + 35 trailing-silence frames = 56 x 320 B (+ WAV header + multipart)
    assert wav_rate == 8000 and 17900 < size < 19500, size
    brain = next(b for k, b in pv.models.calls if k == "brain")
    assert "CONTEXT" in brain["messages"][-1]["content"]
    ai = await ai_doc(pv, a.sid)
    assert ai["turns"] == 1 and {"stt", "brain", "tts", "turn_ms"} <= set(ai["timings_ms"])
    await ws.send(json.dumps({"event": "playedStream", "sequenceNumber": 9, "streamId": a.stream_id, "name": "utt-1"}))
    await ws.close()


async def test_second_turn_keeps_conversation_context(pv):
    a = await answered_stream(pv, call_uuid="TURN-2")
    ws = await connect(pv, a)
    await until(lambda: _active(pv, a.sid))
    n = 1
    for _ in range(2):
        n = await send_media(ws, utterance(), first_chunk=n)
        await recv_until(ws, lambda m: m.get("event") == "checkpoint")
    brains = [b for k, b in pv.models.calls if k == "brain"]
    assert len(brains) == 2 and any(m["role"] == "assistant" for m in brains[1]["messages"])
    await ws.close()


async def test_inbound_media_robustness_duplicates_bad_payloads_wrong_track_dtmf(pv):
    a = await answered_stream(pv, call_uuid="ROB-1")
    ws = await connect(pv, a)
    await until(lambda: _active(pv, a.sid))
    frames = media_frames(utterance())
    bad = [{"event": "media", "media": {"track": "inbound", "chunk": 9000, "payload": "***not base64***"}},
           {"event": "media", "media": {"track": "inbound", "chunk": 9001, "payload": ""}},
           {"event": "media", "media": {"track": "inbound", "chunk": 9002, "payload": base64.b64encode(b"\xff" * 5000).decode()}},
           {"event": "media", "media": {"track": "outbound", "chunk": 9003, "payload": base64.b64encode(b"\xff" * 160).decode()}},
           {"event": "dtmf", "dtmf": {"digit": "5", "track": "inbound"}}, {"event": "nonsense"}, ["not", "an", "object"]]
    await ws.send("this is not json")
    for f in bad:
        await ws.send(json.dumps(f))
    for f in frames:
        await ws.send(json.dumps(f))
        await ws.send(json.dumps(f))  # every frame delivered twice: the duplicate must be dropped, not fed twice
    await recv_until(ws, lambda m: m.get("event") == "checkpoint")
    assert [k for k, _ in pv.models.calls].count("stt") == 1
    size, _ = pv.models.stt_uploads[0]
    assert 17900 < size < 19500, size  # a stream fed twice would be ~2x larger
    assert (await ai_doc(pv, a.sid))["state"] == "active"  # none of that killed the stream
    await ws.close()


async def test_barge_in_sends_clearaudio_and_stops_the_reply(pv):
    pv.models.tts_ms = 6000
    a = await answered_stream(pv, call_uuid="BARGE-1")
    ws = await connect(pv, a)
    await until(lambda: _active(pv, a.sid))
    n = await send_media(ws, utterance())
    first = await recv_until(ws, lambda m: m.get("event") == "playAudio")
    assert first[-1]["event"] == "playAudio"
    await send_media(ws, pcm_tone(400, RATE, amp=6000), first_chunk=n)  # caller talks over the assistant
    rest = await recv_until(ws, lambda m: m.get("event") == "clearAudio")
    assert rest[-1] == {"event": "clearAudio", "streamId": a.stream_id}
    await ws.send(json.dumps({"event": "clearedAudio", "sequenceNumber": 5, "streamId": a.stream_id}))
    assert (await ai_doc(pv, a.sid))["state"] == "active"  # the call continues after an interruption
    await ws.close()


async def test_oversize_reply_is_truncated_to_plivos_queue_limit(pv):
    pv.models.tts_ms = 55000  # a 55 s reply must not exceed the ~60 s Plivo queue
    a = await answered_stream(pv, call_uuid="BIG-REPLY-1")
    ws = await connect(pv, a)
    await until(lambda: _active(pv, a.sid))
    await send_media(ws, utterance())
    msgs = await recv_until(ws, lambda m: m.get("event") == "checkpoint", timeout=15)
    total = sum(len(base64.b64decode(m["media"]["payload"])) for m in msgs if m["event"] == "playAudio")
    assert total == 50 * 8000
    await ws.close()


# =================================================================================== cleanup and failures
async def test_stop_event_ends_ai_session_and_releases_resources_but_not_the_call(pv):
    a = await answered_stream(pv, call_uuid="STOP-1")
    ws = await connect(pv, a)
    await until(lambda: _active(pv, a.sid))
    await ws.send(json.dumps({"event": "stop", "sequenceNumber": 99, "streamId": a.stream_id}))
    ai = await until(lambda: _stopped(pv, a.sid))
    assert ai["state"] == "stopped" and ai["stop_reason"] == "stream_stopped" and a.sid not in pv.ai.runtime
    from app.ai import ws as ws_mod

    async def released():
        return a.sid not in ws_mod._plivo_streaming
    assert await until(released)
    call = await pv.db["calls"].find_one({"_id": a.call["_id"]})
    assert call["state"] == "answered"  # Plivo's Hangup callback, not the stream, ends the call
    assert (await pv.db["sessions"].find_one({"_id": a.sid}))["status"] == "answered"
    types = [e["type"] for e in await pv.db["call_events"].find({"call_id": call["_id"]}).to_list(50)]
    assert "stream.connected" in types and "stream.ended" in types
    assert pv.fake.hangups == []  # a normal stop must not hang up the caller


async def test_abrupt_disconnect_cleans_up(pv):
    a = await answered_stream(pv, call_uuid="DROP-1")
    ws = await connect(pv, a)
    await until(lambda: _active(pv, a.sid))
    await ws.close(code=1011)
    ai = await until(lambda: _stopped(pv, a.sid))
    assert ai["state"] == "stopped" and ai["stop_reason"] in ("ws_disconnected", "socket_closed") and a.sid not in pv.ai.runtime


async def test_hangup_during_inference_cancels_it_and_no_audio_arrives_afterwards(pv):
    pv.models.stt_delay = 1.5
    a = await answered_stream(pv, call_uuid="MID-1")
    ws = await connect(pv, a)
    await until(lambda: _active(pv, a.sid))
    await send_media(ws, utterance())
    await asyncio.sleep(0.4)                                  # STT request is in flight
    await post_cb(pv, "hangup", {"CallUUID": "MID-1", "CallStatus": "completed", "HangupCause": "NORMAL_CLEARING"})
    ai = await until(lambda: _stopped(pv, a.sid))
    assert ai["stop_reason"] == "call_ended"
    heard = []
    try:
        while True:
            heard.append(json.loads(await asyncio.wait_for(ws.recv(), 3)))
    except (websockets.ConnectionClosed, asyncio.TimeoutError):
        pass
    assert not [m for m in heard if m.get("event") == "playAudio"]
    await asyncio.sleep(1.6)                                  # the slow STT would have finished by now
    assert [k for k, _ in pv.models.calls].count("brain") == 0 and a.sid not in pv.ai.runtime


async def test_model_failure_is_nonfatal_and_the_stream_recovers(pv):
    pv.models.fail["brain"] = 500
    a = await answered_stream(pv, call_uuid="FAIL-1")
    ws = await connect(pv, a)
    await until(lambda: _active(pv, a.sid))
    n = await send_media(ws, utterance())
    await asyncio.sleep(0.8)
    assert (await ai_doc(pv, a.sid))["state"] == "active"     # no audio, no crash
    pv.models.fail.clear()
    await send_media(ws, utterance(), first_chunk=n)
    msgs = await recv_until(ws, lambda m: m.get("event") == "checkpoint")
    assert any(m["event"] == "playAudio" for m in msgs)
    await ws.close()


async def test_ai_start_failure_during_stream_closes_it_hangs_up_and_records(pv):
    a = await answered_stream(pv, call_uuid="STARTFAIL-1")
    await pv.c.put("/v1/ai/config", json={"enabled": False}, headers=ADMIN())  # changed between Answer and stream start
    ws = await connect(pv, a)
    with pytest.raises(websockets.ConnectionClosed) as ei:
        await asyncio.wait_for(ws.recv(), 5)
    assert ei.value.rcvd.code == 1011
    assert await pv.db["ai_sessions"].count_documents({}) == 0
    assert pv.fake.hangups and pv.fake.hangups[-1][0] == a.call["_id"]
    types = [e["type"] for e in await pv.db["call_events"].find({"call_id": a.call["_id"]}).to_list(50)]
    assert "ai.start_failed" in types
    from app.ai import ws as ws_mod

    async def released():
        return a.sid not in ws_mod._plivo_streaming  # cleanup runs just after the client sees the close
    assert await until(released)


async def test_stream_capacity_limit(pv):
    pv.app.state.settings.__dict__["plivo_stream_max_concurrent"] = 1
    a = await answered_stream(pv, call_uuid="CAP-1")
    b = await answered_stream(pv, call_uuid="CAP-2")
    ws1 = await connect(pv, a)
    await until(lambda: _active(pv, a.sid))
    ws2 = await websockets.connect(f"ws://{pv.host}/v1/providers/plivo/stream", additional_headers=ws_headers())
    with pytest.raises(websockets.ConnectionClosed) as ei:
        await asyncio.wait_for(ws2.recv(), 4)
    assert ei.value.rcvd.code == 1013
    assert (await pv.db["calls"].find_one({"_id": b.call["_id"]}))["state"] == "answered"
    await ws1.close()


# =================================================================================== controller backstops
async def test_stream_that_never_connects_gets_the_call_hung_up_once(pv):
    a = await answered_stream(pv, call_uuid="NEVER-1")
    await pv.db["calls"].update_one({"_id": a.call["_id"]}, {"$set": {"stream_expected_by": utcnow().replace(year=2020)}})
    await pv.ctrl.sweep_timeouts()
    await pv.ctrl.sweep_timeouts()
    assert pv.fake.hangups == [(a.call["_id"], "NORMAL_CLEARING")]
    types = [e["type"] for e in await pv.db["call_events"].find({"call_id": a.call["_id"]}).to_list(50)]
    assert types.count("stream.not_connected") == 1


async def test_connected_stream_is_not_hung_up_by_the_watchdog(pv):
    a = await answered_stream(pv, call_uuid="OKSTREAM-1")
    ws = await connect(pv, a)
    await until(lambda: _active(pv, a.sid))
    await pv.db["calls"].update_one({"_id": a.call["_id"]}, {"$set": {"stream_expected_by": utcnow().replace(year=2020)}})
    await pv.ctrl.sweep_timeouts()
    assert pv.fake.hangups == []
    await ws.close()


async def test_plivo_calls_are_hung_up_through_plivo_not_freeswitch(pv):
    a = await answered_stream(pv, call_uuid="PROV-1")
    r = await pv.c.post(f"/v1/calls/{a.call['_id']}/hangup", headers=auth())
    assert r.status_code == 200
    assert pv.fake.hangups == [(a.call["_id"], "NORMAL_CLEARING")] and pv.prov.hangups == []
    assert pv.ctrl.provider_for({"provider": "freeswitch_gateway"}) is pv.prov and pv.ctrl.provider_for({}) is pv.prov
    pv.fake.fail = True
    b = await answered_stream(pv, call_uuid="PROV-2")
    r = await pv.c.post(f"/v1/calls/{b.call['_id']}/hangup", headers=auth())
    assert r.status_code == 502 and r.json()["error"]["code"] == "hangup_failed"  # honest failure, no invented success
    t = await pv.c.post(f"/v1/calls/{b.call['_id']}/transfer", json={"destination": "+919999999999"}, headers=auth())
    assert t.status_code == 502 and t.json()["error"]["code"] == "transfer_failed"  # unsupported for Plivo calls; the call survives
    assert (await pv.db["calls"].find_one({"_id": b.call["_id"]}))["state"] == "answered"


async def test_orphaned_plivo_call_is_closed_locally_after_the_grace_period(pv):
    a = await answered_stream(pv, call_uuid="ORPH-1")
    ws = await connect(pv, a)
    await until(lambda: _active(pv, a.sid))
    pv.fake.fail = True  # Plivo REST unreachable: the hangup can never be confirmed
    old = utcnow() - timedelta(seconds=25)
    await pv.db["calls"].update_one({"_id": a.call["_id"]}, {"$set": {"max_duration_s": 10, "answered_at": old}})
    await pv.ctrl.sweep_timeouts()                              # attempt 1: tries the REST hangup (fails), throttled afterwards
    assert (await pv.db["calls"].find_one({"_id": a.call["_id"]}))["state"] == "answered"
    old = utcnow() - timedelta(seconds=120)
    await pv.db["calls"].update_one({"_id": a.call["_id"]}, {"$set": {"answered_at": old}})
    await pv.ctrl.sweep_timeouts()                              # past max duration + grace: closed locally
    call = await pv.db["calls"].find_one({"_id": a.call["_id"]})
    assert call["state"] == "completed" and call["hangup_cause"] == "TIMEOUT_UNCONFIRMED" and call["end_reason"] == "max_duration_unconfirmed"
    ai = await until(lambda: _stopped(pv, a.sid))
    assert ai["stop_reason"] == "call_ended" and (await pv.db["sessions"].find_one({"_id": a.sid}))["status"] == "ended"
    with pytest.raises(websockets.ConnectionClosed):
        await asyncio.wait_for(ws.recv(), 3)


async def test_incomplete_answer_is_failed_by_the_sweeper(pv):
    now = utcnow()
    await pv.repos.insert_call({"_id": "call_stuck1", "tenant_id": "t1", "direction": "inbound", "provider": "plivo_api",
                                "provider_call_id": "STUCK-1", "from_number": "1", "to_number": DID, "state": "initiating",
                                "state_version": 0, "initiated_at": now.replace(year=2020)})
    await pv.ctrl.sweep_timeouts()
    c = await pv.db["calls"].find_one({"_id": "call_stuck1"})
    assert c["state"] == "failed" and c["failure_reason"] == "answer_not_completed"


# =================================================================================== regression: nothing else changed
async def test_existing_outbound_callbacks_and_api_still_work_with_inbound_enabled(pv):
    await pv.repos.insert_call({"_id": "call_out1", "tenant_id": "t1", "direction": "outbound", "provider": "plivo_api",
                                "provider_call_id": "REQ-OUT-1", "from_number": "+911140000000", "to_number": "+919876543210",
                                "state": "initiating", "state_version": 1, "initiated_at": utcnow(), "bridge_to": "+919999999999"})
    r = await post_answer(pv, {"CallUUID": "OUT-UUID-1", "RequestUUID": "REQ-OUT-1", "Direction": "outbound", "To": "919876543210"})
    assert "<Dial" in r.text and "919999999999" in r.text and "Stream" not in r.text   # outbound keeps its old behaviour
    c = await pv.db["calls"].find_one({"_id": "call_out1"})
    assert c["state"] == "answered" and c["provider_call_id"] == "OUT-UUID-1"
    assert (await post_answer(pv, {"CallUUID": "UNKNOWN-OUT-9", "Direction": "outbound", "To": "919876543210"})).text.count("Hangup") == 1
    assert (await pv.c.get("/health/live")).status_code == 200 and (await pv.c.get("/health/ready")).status_code == 200
    h = (await pv.c.get("/v1/ai/health", headers=auth())).json()
    assert h["plivo_stream"]["ready"] is True and h["media_bridge"]["ready"] is False  # FreeSWITCH bridge status is unchanged
    # the FreeSWITCH-flavoured AI endpoints still refuse a Plivo-delivered call (different protocol, never interchangeable)
    a = await answered_stream(pv, call_uuid="XPROTO-1")
    r = await pv.c.post(f"/v1/ai/sessions/{a.sid}/start", json={}, headers=auth())
    assert r.status_code == 409 and r.json()["error"]["code"] == "media_bridge_unavailable"


async def test_plivo_callbacks_are_404_when_the_feature_is_off(db):
    s = make_settings(mongo_db=db.s.mongo_db)  # provider=mock, plivo_inbound_enabled=False
    app = create_app(s, db=db, provider=MockProvider())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            for p in ("answer", "status", "hangup", "stream-status"):
                assert (await c.post(f"/v1/providers/plivo/{p}", data={"CallUUID": "X1"})).status_code == 404
