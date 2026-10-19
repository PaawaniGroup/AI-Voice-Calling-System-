import uuid

import httpx
import pytest  # noqa: F401
import pytest_asyncio
from plivo.utils.signature_v3 import construct_post_url, get_signature_v3

from app.core.ids import utcnow
from app.main import create_app
from app.providers.plivo import map_status
from tests.conftest import enable_tenant, make_settings

BASE = "https://tel.example.test"
TOKEN = "plivo-test-token-not-real"


@pytest_asyncio.fixture
async def penv(db):
    s = make_settings(mongo_db=db.s.mongo_db, provider="plivo_api", plivo_auth_id="MAXXXXXXXXXXXXXXXXXX",
                      plivo_auth_token=TOKEN, webhook_public_base_url=BASE, real_calls_enabled=True)
    app = create_app(s, db=db)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://internal") as c:
            e = type("E", (), {})()
            e.client, e.repos, e.db, e.ctrl = c, app.state.repos, db, app.state.controller
            yield e


def signed_post(path, params, nonce=None, token=TOKEN, tamper=False):
    nonce = nonce or uuid.uuid4().hex
    url = BASE + path
    sig = get_signature_v3(token.encode(), construct_post_url(url, params), nonce.encode())
    if isinstance(sig, bytes):
        sig = sig.decode()
    if tamper:
        params = {**params, "CallStatus": "completed"}
    return dict(url=path, data=params, headers={"X-Plivo-Signature-V3": sig, "X-Plivo-Signature-V3-Nonce": nonce})


async def seed_call(env, answered=False, **kw):
    now = utcnow()
    doc = {"_id": "call_p1", "tenant_id": "t1", "direction": "outbound", "provider": "plivo_api", "provider_call_id": "REQ-1",
           "from_number": "+911140000000", "to_number": "+919876543210", "state": "initiating", "state_version": 1,
           "initiated_at": now, **kw}
    await env.repos.insert_call(doc)
    return doc


async def test_invalid_or_missing_signature_rejected(penv):
    await seed_call(penv)
    p = {"CallUUID": "U1", "RequestUUID": "REQ-1", "CallStatus": "ringing"}
    req = signed_post("/v1/providers/plivo/status", p)
    assert (await penv.client.post(**{**req, "headers": {}})).status_code == 401
    assert (await penv.client.post(**signed_post("/v1/providers/plivo/status", p, token="wrong-token"))).status_code == 401
    assert (await penv.client.post(**signed_post("/v1/providers/plivo/status", p, tamper=True))).status_code == 401
    assert (await penv.repos.get_call("call_p1"))["state"] == "initiating"


async def test_valid_callbacks_progress_dedupe_and_replay(penv):
    await seed_call(penv)
    path = "/v1/providers/plivo/status"
    ring = {"CallUUID": "U1", "RequestUUID": "REQ-1", "CallStatus": "ringing", "Event": "Ring"}
    r = await penv.client.post(**signed_post(path, ring))
    assert r.status_code == 200 and r.json()["status"] == "applied"
    c = await penv.repos.get_call("call_p1")
    assert c["state"] == "ringing" and c["provider_call_id"] == "U1"  # request uuid mapped to call uuid
    # same nonce replayed => rejected as duplicate, no state change
    same = signed_post(path, ring, nonce="fixed-nonce")
    assert (await penv.client.post(**same)).json()["status"] in ("applied", "duplicate")
    assert (await penv.client.post(**same)).json()["status"] == "duplicate"
    # a provider retry with a NEW nonce but the same event => deduplicated by event key
    assert (await penv.client.post(**signed_post(path, ring))).json()["status"] == "duplicate"
    ans = {"CallUUID": "U1", "CallStatus": "in-progress"}
    assert (await penv.client.post(**signed_post(path, ans))).json()["status"] == "applied"
    # out-of-order late ring after answer
    late = {"CallUUID": "U1", "CallStatus": "ringing", "Event": "Ring2"}
    assert (await penv.client.post(**signed_post(path, late))).json()["status"] == "out_of_order"
    done = {"CallUUID": "U1", "CallStatus": "completed", "HangupCause": "NORMAL_CLEARING", "Duration": "12"}
    assert (await penv.client.post(**signed_post(path, done))).json()["status"] == "applied"
    c = await penv.repos.get_call("call_p1")
    assert c["state"] == "completed" and c["connected_duration_s"] is not None


async def test_completed_without_answer_is_failed_or_mapped(penv):
    assert map_status({"CallStatus": "busy"}, False)[0].value == "busy"
    assert map_status({"CallStatus": "no-answer"}, False)[0].value == "no_answer"
    assert map_status({"CallStatus": "completed", "HangupCause": "NORMAL_CLEARING"}, False)[0].value == "failed"
    assert map_status({"CallStatus": "cancel"}, False)[0].value == "cancelled"
    assert map_status({"CallStatus": "weird"}, False) is None


async def test_unknown_call_acknowledged_not_retried(penv):
    r = await penv.client.post(**signed_post("/v1/providers/plivo/status", {"CallUUID": "nope", "CallStatus": "ringing"}))
    assert r.status_code == 200 and r.json()["status"] == "ignored"


async def test_inbound_answer_routing(penv):
    path = "/v1/providers/plivo/answer"
    inb = {"CallUUID": "IN1", "Direction": "inbound", "To": "911140001111", "From": "919876543210"}
    # unrouted number -> rejected
    r = await penv.client.post(**signed_post(path, inb))
    assert "Hangup" in r.text and r.headers["content-type"].startswith("application/xml")
    # route exists but tenant disabled -> rejected
    await penv.db["inbound_routes"].insert_one({"number": "+911140001111", "tenant_id": "t1", "action": "bridge",
                                                "target": "+919999999999", "enabled": True})
    r = await penv.client.post(**signed_post(path, {**inb, "CallUUID": "IN2"}))
    assert "Hangup" in r.text
    await enable_tenant(penv)
    r = await penv.client.post(**signed_post(path, {**inb, "CallUUID": "IN3"}))
    assert "<Dial" in r.text and "919999999999" in r.text
    c = await penv.db["calls"].find_one({"provider_call_id": "IN3"})
    assert c["direction"] == "inbound" and c["tenant_id"] == "t1" and c["state"] == "answered"
    # invalid signature on inbound -> 401 (no call created)
    bad = signed_post(path, {**inb, "CallUUID": "IN4"}, token="x")
    assert (await penv.client.post(**bad)).status_code == 401
    assert await penv.db["calls"].count_documents({"provider_call_id": "IN4"}) == 0


async def test_inbound_capacity_rejected_busy(penv):
    await enable_tenant(penv, max_active_calls=1)
    await penv.db["inbound_routes"].insert_one({"number": "+911140001111", "tenant_id": "t1", "action": "park", "enabled": True})
    await seed_call(penv, state="answered", answered_at=utcnow())
    r = await penv.client.post(**signed_post("/v1/providers/plivo/answer", {"CallUUID": "IN9", "Direction": "inbound", "To": "911140001111"}))
    assert 'reason="busy"' in r.text
