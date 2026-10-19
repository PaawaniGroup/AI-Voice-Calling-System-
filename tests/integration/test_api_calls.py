import asyncio
import uuid

from app.core.ids import utcnow

import httpx
import pytest

from app.providers.base import ProviderError, ProviderUncertain
from tests.conftest import auth, call_body, enable_tenant, idem, token

HDR_ADMIN = lambda t="t1": auth(t, ("telephony:admin",))  # noqa: E731
PLATFORM = lambda: auth("platform", ("platform:admin",))  # noqa: E731
_seq = iter(range(1, 10**9))


def esl(name, fs_uuid, **kw):
    return {"Event-Name": name, "Unique-ID": fs_uuid, "Event-Sequence": str(next(_seq)), "variable_mme_leg": "a", **kw}


async def wait_state(env, call_id, states, timeout=3.0):
    if isinstance(states, str):
        states = {states}
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        c = await env.repos.get_call(call_id)
        if c and c["state"] in states and (c["state"] != "initiating" or c.get("fs_uuid")):
            return c
        await asyncio.sleep(0.03)
    c = await env.repos.get_call(call_id)
    raise AssertionError(f"call {call_id} stuck in {c['state']} (wanted {states})")


async def make_call(env, tenant="t1", **kw):
    r = await env.client.post("/v1/calls", json=call_body(**kw), headers={**auth(tenant), **idem()})
    assert r.status_code == 202, r.text
    return r.json()


# ------------------------------------------------------------------ auth
async def test_auth_required_and_validated(env):
    r = await env.client.get("/v1/calls")
    assert r.status_code == 401
    for h in [{"Authorization": "Bearer garbage"}, {"Authorization": f"Bearer {token(secret='z'*40)}"},
              {"Authorization": f"Bearer {token(exp=-10)}"}, {"Authorization": "Basic abc"}]:
        assert (await env.client.get("/v1/calls", headers=h)).status_code == 401
    # a valid token without a tenant claim is rejected
    import jwt, time
    t = jwt.encode({"sub": "u", "exp": int(time.time()) + 60, "roles": ["telephony:user"]}, env.s.jwt_secret.get_secret_value(), "HS256")
    assert (await env.client.get("/v1/calls", headers={"Authorization": f"Bearer {t}"})).status_code == 401
    # no role => forbidden
    assert (await env.client.get("/v1/calls", headers=auth(roles=()))).status_code == 403
    # alg=none must not be accepted
    import base64, json
    b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    none_tok = f"{b({'alg': 'none', 'typ': 'JWT'})}.{b({'sub': 'u', 'tenant_id': 't1', 'roles': ['telephony:user'], 'exp': int(time.time()) + 60})}."
    assert (await env.client.get("/v1/calls", headers={"Authorization": f"Bearer {none_tok}"})).status_code == 401


async def test_health(env):
    assert (await env.client.get("/health/live")).json() == {"status": "ok"}
    r = await env.client.get("/health/ready")
    assert r.status_code == 200 and r.json()["checks"]["database"] == "ok"
    body = r.text
    assert "mongodb" not in body and "password" not in body.lower()
    assert (await env.client.get("/v1/telephony/status")).status_code == 401
    st = (await env.client.get("/v1/telephony/status", headers=auth())).json()
    assert st["ai_enabled"] is False


# ------------------------------------------------------------------ create / policy / validation
async def test_tenant_disabled_by_default(env):
    r = await env.client.post("/v1/calls", json=call_body(), headers={**auth(), **idem()})
    assert r.status_code == 403 and r.json()["error"]["code"] == "tenant_calling_disabled"


async def test_create_and_dispatch(env):
    await enable_tenant(env)
    c = await make_call(env)
    assert c["state"] == "queued" or c["state"] == "initiating"
    c2 = await wait_state(env, c["id"], "initiating")
    assert c2["fs_uuid"] and len(env.prov.originated) == 1
    assert env.prov.originated[0].to_number == "+919876543210"


@pytest.mark.parametrize("patch,code", [
    ({"to": "+442071838750"}, "destination_country_denied"),
    ({"to": "+911860123456"}, "destination_denied"),
    ({"from_number": "+919000000000"}, "caller_id_not_approved"),
    ({"to": "abcdefgh"}, "invalid_number"),
    ({"bridge_to": "+919000000001"}, "transfer_not_permitted"),
])
async def test_policy_rejections(env, patch, code):
    await enable_tenant(env)
    r = await env.client.post("/v1/calls", json=call_body(**patch), headers={**auth(), **idem()})
    assert r.status_code == 422 and r.json()["error"]["code"] == code
    assert env.prov.originated == []


async def test_invalid_bodies(env):
    await enable_tenant(env)
    h = {**auth(), **idem()}
    assert (await env.client.post("/v1/calls", json={"to": "+919876543210"}, headers=h)).status_code == 422
    assert (await env.client.post("/v1/calls", json=call_body(extra="x"), headers=h)).status_code == 422
    assert (await env.client.post("/v1/calls", content=b"{bad", headers={**h, "Content-Type": "application/json"})).status_code == 422
    assert (await env.client.post("/v1/calls", json=call_body(), headers=auth())).status_code == 422  # no idempotency key
    big = await env.client.post("/v1/calls", content=b"x" * 200000, headers={**h, "Content-Type": "application/json"})
    assert big.status_code == 413


# ------------------------------------------------------------------ idempotency
async def test_idempotency(env):
    await enable_tenant(env)
    h = {**auth(), **idem()}
    r1 = await env.client.post("/v1/calls", json=call_body(), headers=h)
    r2 = await env.client.post("/v1/calls", json=call_body(), headers=h)
    assert r1.status_code == 202 and r2.status_code == 200 and r2.headers["idempotent-replay"] == "true"
    assert r1.json()["id"] == r2.json()["id"]
    r3 = await env.client.post("/v1/calls", json=call_body(to="+919000000009"), headers=h)
    assert r3.status_code == 409 and r3.json()["error"]["code"] == "idempotency_key_reuse"
    assert await env.db["calls"].count_documents({}) == 1
    # same key, different tenant => independent namespace
    await enable_tenant(env, "t2")
    r4 = await env.client.post("/v1/calls", json=call_body(), headers={**auth("t2"), "Idempotency-Key": h["Idempotency-Key"]})
    assert r4.status_code == 202 and r4.json()["id"] != r1.json()["id"]


async def test_concurrent_duplicate_requests_create_one_call(env):
    await enable_tenant(env)
    h = {**auth(), **idem()}
    rs = await asyncio.gather(*[env.client.post("/v1/calls", json=call_body(), headers=h) for _ in range(10)])
    assert {r.json()["id"] for r in rs if r.status_code in (200, 202)} and len({r.json()["id"] for r in rs}) == 1
    assert await env.db["calls"].count_documents({}) == 1


# ------------------------------------------------------------------ tenant isolation
async def test_cross_tenant_access_denied(env):
    await enable_tenant(env, "t1")
    await enable_tenant(env, "t2")
    c = await make_call(env, "t1")
    cid = c["id"]
    other = auth("t2")
    for method, path, body in [("GET", f"/v1/calls/{cid}", None), ("POST", f"/v1/calls/{cid}/hangup", None),
                               ("POST", f"/v1/calls/{cid}/cancel", None), ("GET", f"/v1/calls/{cid}/events", None),
                               ("POST", f"/v1/calls/{cid}/transfer", {"destination": "+919999999999"}),
                               ("GET", f"/v1/calls/{cid}/recordings", None)]:
        r = await env.client.request(method, path, headers=other, json=body)
        assert r.status_code == 404, (method, path, r.status_code)
    assert (await env.client.get("/v1/calls", headers=other)).json()["items"] == []
    # client-supplied tenant in token claims of another user cannot be forged without the secret; query param ignored
    assert (await env.client.get(f"/v1/calls?tenant_id=t1", headers=other)).json()["items"] == []
    # usage cross-tenant
    assert (await env.client.get("/v1/usage?tenant_id=t1", headers=other)).status_code == 403
    assert (await env.client.get("/v1/usage?tenant_id=t1", headers=PLATFORM())).status_code == 200
    # admin ops: tenant admin cannot set policy / routes
    r = await env.client.put("/v1/admin/tenants/t1/policy", json={"enabled": True}, headers=HDR_ADMIN("t2"))
    assert r.status_code == 403
    # webhook endpoints are tenant scoped
    r = await env.client.post("/v1/webhooks/endpoints", json={"url": "http://127.0.0.1:9/x"}, headers=HDR_ADMIN("t1"))
    assert r.status_code == 201
    eid = r.json()["id"]
    assert (await env.client.get("/v1/webhooks/endpoints", headers=HDR_ADMIN("t2"))).json()["items"] == []
    assert (await env.client.delete(f"/v1/webhooks/endpoints/{eid}", headers=HDR_ADMIN("t2"))).status_code == 404
    # plain user cannot administer
    assert (await env.client.post("/v1/webhooks/endpoints", json={"url": "http://127.0.0.1:9/x"}, headers=auth("t1"))).status_code == 403


# ------------------------------------------------------------------ lifecycle with simulated ESL events
async def test_full_lifecycle_duplicates_and_out_of_order(env):
    await enable_tenant(env)
    c = await make_call(env)
    c = await wait_state(env, c["id"], "initiating")
    u = c["fs_uuid"]
    ring = esl("CHANNEL_PROGRESS", u)
    await env.ctrl.on_esl_event(ring)
    await env.ctrl.on_esl_event(ring)  # duplicate
    assert (await env.repos.get_call(c["_id"]))["state"] == "ringing"
    await env.ctrl.on_esl_event(esl("CHANNEL_ANSWER", u))
    await env.ctrl.on_esl_event(esl("CHANNEL_PROGRESS", u))  # late/out-of-order ring after answer
    got = await env.repos.get_call(c["_id"])
    assert got["state"] == "answered" and got["answered_at"]
    await env.ctrl.on_esl_event(esl("CHANNEL_BRIDGE", u, **{"Other-Leg-Unique-ID": str(uuid.uuid4())}))
    assert (await env.repos.get_call(c["_id"]))["state"] == "bridged"
    h = esl("CHANNEL_HANGUP", u, **{"Hangup-Cause": "NORMAL_CLEARING"})
    await env.ctrl.on_esl_event(h)
    await env.ctrl.on_esl_event(h)
    await env.ctrl.on_esl_event(esl("CHANNEL_HANGUP_COMPLETE", u, **{"Hangup-Cause": "NORMAL_CLEARING"}))
    await env.ctrl.on_esl_event(esl("CHANNEL_ANSWER", u))  # after terminal
    final = await env.repos.get_call(c["_id"])
    assert final["state"] == "completed" and final["connected_duration_s"] is not None
    assert final["state_version"] == 5  # claim(initiating), ringing, answered, bridged, completed
    evs = (await env.client.get(f"/v1/calls/{c['_id']}/events", headers=auth())).json()["items"]
    types = [e["type"] for e in evs]
    assert types.count("state.ringing") == 2  # duplicate collapsed by dedup id => only distinct recorded
    assert (await env.client.get(f"/v1/calls/{c['_id']}", headers=auth())).json()["state"] == "completed"


async def test_failure_classification(env):
    await enable_tenant(env, max_active_calls=30)
    for cause, state in [("USER_BUSY", "busy"), ("NO_ANSWER", "no_answer"), ("CALL_REJECTED", "failed")]:
        c = await make_call(env)
        c = await wait_state(env, c["id"], "initiating")
        await env.ctrl.on_esl_event(esl("CHANNEL_HANGUP", c["fs_uuid"], **{"Hangup-Cause": cause}))
        got = await env.repos.get_call(c["_id"])
        assert got["state"] == state and got["ended_at"] and got.get("connected_duration_s") is None


async def test_background_job_failure_finalizes_call(env):
    await enable_tenant(env)
    c = await make_call(env)
    c = await wait_state(env, c["id"], "initiating")
    await env.ctrl.on_esl_event({"Event-Name": "BACKGROUND_JOB", "Job-UUID": c["job_uuid"], "_body": "-ERR NO_ROUTE_DESTINATION"})
    got = await env.repos.get_call(c["_id"])
    assert got["state"] == "failed" and got["failure_reason"] == "invalid_destination"


# ------------------------------------------------------------------ hangup / cancel / transfer
async def test_cancel_queued_call_and_idempotent(env):
    await enable_tenant(env)
    env.ctrl.max_active = 0  # nothing dispatches => stays queued
    c = await make_call(env)
    r = await env.client.post(f"/v1/calls/{c['id']}/cancel", headers=auth())
    assert r.status_code == 200 and r.json()["state"] == "cancelled"
    assert (await env.client.post(f"/v1/calls/{c['id']}/cancel", headers=auth())).json()["state"] == "cancelled"
    assert (await env.client.post(f"/v1/calls/{c['id']}/hangup", headers=auth())).json()["state"] == "cancelled"
    assert env.prov.originated == []


async def test_cancel_rejected_when_connected_and_hangup_flow(env):
    await enable_tenant(env)
    c = await make_call(env)
    c = await wait_state(env, c["id"], "initiating")
    u = c["fs_uuid"]
    await env.ctrl.on_esl_event(esl("CHANNEL_ANSWER", u))
    r = await env.client.post(f"/v1/calls/{c['_id']}/cancel", headers=auth())
    assert r.status_code == 409 and r.json()["error"]["code"] == "already_connected"
    r = await env.client.post(f"/v1/calls/{c['_id']}/hangup", headers=auth())
    assert r.status_code == 200 and env.prov.hangups[-1][1] == "NORMAL_CLEARING"
    # state is NOT invented: it only becomes completed when the hangup event arrives
    assert r.json()["state"] == "answered"
    await env.ctrl.on_esl_event(esl("CHANNEL_HANGUP", u, **{"Hangup-Cause": "NORMAL_CLEARING"}))
    assert (await env.repos.get_call(c["_id"]))["state"] == "completed"


async def test_cancel_before_connect_ends_cancelled(env):
    await enable_tenant(env)
    c = await make_call(env)
    c = await wait_state(env, c["id"], "initiating")
    r = await env.client.post(f"/v1/calls/{c['_id']}/cancel", headers=auth())
    assert r.status_code == 200 and env.prov.hangups[-1][1] == "ORIGINATOR_CANCEL"
    await env.ctrl.on_esl_event(esl("CHANNEL_HANGUP", c["fs_uuid"], **{"Hangup-Cause": "ORIGINATOR_CANCEL"}))
    assert (await env.repos.get_call(c["_id"]))["state"] == "cancelled"


async def test_transfer_authorization_and_flow(env):
    await enable_tenant(env)
    c = await make_call(env)
    c = await wait_state(env, c["id"], "initiating")
    u = c["fs_uuid"]
    r = await env.client.post(f"/v1/calls/{c['_id']}/transfer", json={"destination": "+919999999999"}, headers=auth())
    assert r.status_code == 409  # not connected yet
    await env.ctrl.on_esl_event(esl("CHANNEL_ANSWER", u))
    r = await env.client.post(f"/v1/calls/{c['_id']}/transfer", json={"destination": "+919876543210"}, headers=auth())
    assert r.status_code == 422 and r.json()["error"]["code"] == "transfer_not_permitted"
    r = await env.client.post(f"/v1/calls/{c['_id']}/transfer", json={"destination": "+919999999999"}, headers=auth())
    assert r.status_code == 202 and r.json()["state"] == "transferring"
    await env.ctrl.on_esl_event(esl("CHANNEL_BRIDGE", u, **{"Other-Leg-Unique-ID": str(uuid.uuid4())}))
    got = await env.repos.get_call(c["_id"])
    assert got["state"] == "bridged" and got["transfer"]["attempts"][0]["outcome"] == "completed"


async def test_transfer_timeout_reverts(env):
    await enable_tenant(env)
    c = await make_call(env)
    c = await wait_state(env, c["id"], "initiating")
    await env.ctrl.on_esl_event(esl("CHANNEL_ANSWER", c["fs_uuid"]))
    await env.client.post(f"/v1/calls/{c['_id']}/transfer", json={"destination": "+919999999999"}, headers=auth())
    env.ctrl.s = env.s.model_copy(update={"transfer_timeout_s": 0})
    await asyncio.sleep(0.01)
    await env.ctrl.sweep_timeouts()
    got = await env.repos.get_call(c["_id"])
    assert got["state"] == "answered" and got["transfer"]["attempts"][0]["outcome"] == "timeout"


# ------------------------------------------------------------------ provider failures
async def test_provider_rejection_fails_call(env):
    await enable_tenant(env)
    env.prov.fail_with = ProviderError("plivo_rejected:ValidationError")
    c = await make_call(env)
    got = await wait_state(env, c["id"], "failed")
    assert got["failure_reason"].startswith("plivo_rejected")


async def test_uncertain_origination_is_not_retried(env):
    await enable_tenant(env)
    env.prov.fail_with = ProviderUncertain("esl_timeout")
    c = await make_call(env)
    got = await wait_state(env, c["id"], "initiating")
    await asyncio.sleep(0.5)
    got = await env.repos.get_call(c["id"])
    assert got["state"] == "initiating" and got["origination_uncertain"] is True
    assert env.prov.originated == []  # never retried


# ------------------------------------------------------------------ capacity / queue
async def test_capacity_queue_and_reject(env):
    await enable_tenant(env, max_active_calls=2)
    env.ctrl.max_active = 0
    for _ in range(3):  # per-tenant cap 2, queue headroom lets a few more wait
        r = await env.client.post("/v1/calls", json=call_body(), headers={**auth(), **idem()})
        assert r.status_code == 202
    assert (await env.client.get("/v1/telephony/status", headers=auth())).json()["queued_calls"] == 3
    env.ctrl.s = env.s.model_copy(update={"queue_policy": "reject"})
    rr = await env.client.post("/v1/calls", json=call_body(), headers={**auth(), **idem()})
    s2 = env.s.model_copy(update={"queue_policy": "reject"})
    # with reject policy through the service
    from app.services import calls as svc
    from app.security.auth import Principal
    from app.core.errors import CapacityExceeded
    with pytest.raises(CapacityExceeded):
        await svc.create_call(s2, env.repos, Principal("u", "t1", frozenset(["telephony:user"])), call_body(), "k" * 10)
    assert rr.status_code in (202, 429)


async def test_per_tenant_concurrency_limit_enforced_by_dispatcher(env):
    await enable_tenant(env, "ta", max_active_calls=2)
    await enable_tenant(env, "tb", max_active_calls=2)
    ids = [(await make_call(env, "ta"))["id"] for _ in range(4)]
    await asyncio.sleep(0.6)
    states = [(await env.repos.get_call(i))["state"] for i in ids]
    assert states.count("initiating") == 2 and states.count("queued") == 2
    b = await make_call(env, "tb")  # other tenant is not starved
    await wait_state(env, b["id"], "initiating")


async def test_rate_limit(env):
    s = env.s.model_copy(update={"rate_limit_per_min": 5})
    env.app.state.settings = s
    codes = [(await env.client.get("/v1/calls", headers=auth())).status_code for _ in range(8)]
    assert codes[:5] == [200] * 5 and 429 in codes[5:]


# ------------------------------------------------------------------ history, usage, analytics
async def test_pagination_filters_usage(env):
    await enable_tenant(env, max_active_calls=30)
    ids = []
    for _ in range(5):
        c = await make_call(env)
        c = await wait_state(env, c["id"], "initiating")
        await env.ctrl.on_esl_event(esl("CHANNEL_ANSWER", c["fs_uuid"]))
        await asyncio.sleep(0.05)
        await env.ctrl.on_esl_event(esl("CHANNEL_HANGUP", c["fs_uuid"], **{"Hangup-Cause": "NORMAL_CLEARING"}))
        ids.append(c["_id"])
    seen, cur = [], None
    while True:
        r = (await env.client.get("/v1/calls", params={"limit": 2, **({"cursor": cur} if cur else {})}, headers=auth())).json()
        seen += [i["id"] for i in r["items"]]
        cur = r["next_cursor"]
        if not cur:
            break
    assert sorted(seen) == sorted(ids) and len(set(seen)) == 5
    assert len((await env.client.get("/v1/calls?state=completed", headers=auth())).json()["items"]) == 5
    assert (await env.client.get("/v1/calls?state=bogus", headers=auth())).status_code == 422
    u = (await env.client.get("/v1/usage", headers=auth())).json()
    assert u["measured"]["total_calls"] == 5 and u["estimated_charges"] is None
    assert u["measured"]["by_provider"]["mock"]["billable_minutes_rounded_up"] == 5
    a = (await env.client.get("/v1/analytics/calls", headers=auth())).json()
    assert a["completed"] == 5 and a["outcomes"]["completed"] == 5
    assert "metric,value" in (await env.client.get("/v1/analytics/calls?format=csv", headers=auth())).text


# ------------------------------------------------------------------ webhooks
async def test_webhook_delivery_signing_retry_and_replay(env):
    from app.events import webhooks as wh
    await enable_tenant(env)
    r = await env.client.post("/v1/webhooks/endpoints", json={"url": "http://receiver.test/hook", "events": ["call.answered", "call.completed"]},
                              headers=HDR_ADMIN())
    eid, secret = r.json()["id"], r.json()["signing_secret"]
    c = await make_call(env)
    c = await wait_state(env, c["id"], "initiating")
    await env.ctrl.on_esl_event(esl("CHANNEL_ANSWER", c["fs_uuid"]))
    assert await env.db["webhook_deliveries"].count_documents({}) == 1  # initiating not subscribed
    received, fail = [], {"n": 1}

    def handler(request: httpx.Request):
        received.append(request)
        if fail["n"] > 0:
            fail["n"] -= 1
            return httpx.Response(500)
        return httpx.Response(200)

    w = env.app.state.webhook_worker
    w._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await w.run_once() == 1  # attempt 1 -> 500 -> retrying
    d = await env.db["webhook_deliveries"].find_one({})
    assert d["status"] == "retrying" and d["attempts"][0]["status_code"] == 500
    await env.db["webhook_deliveries"].update_one({"_id": d["_id"]}, {"$set": {"next_attempt_at": d["created_at"]}})
    assert await w.run_once() == 1
    d = await env.db["webhook_deliveries"].find_one({})
    assert d["status"] == "delivered" and d["attempt_count"] == 2
    req = received[-1]
    ts = req.headers["x-mme-timestamp"]
    assert req.headers["x-mme-signature"] == wh.sign(secret, int(ts), req.content)
    assert req.headers["idempotency-key"] == d["event_id"]
    body = req.content.decode()
    assert "+919876543210" not in body and "recording" not in body.lower()
    # replay
    assert (await env.client.post(f"/v1/webhooks/deliveries/{d['_id']}/replay", headers=HDR_ADMIN("t2"))).status_code == 404
    assert (await env.client.post(f"/v1/webhooks/deliveries/{d['_id']}/replay", headers=HDR_ADMIN())).status_code == 202
    # emitting the same event again does not create a second delivery
    call = await env.repos.get_call(c["_id"])
    await env.ctrl.emitter.emit_for_call(call, "call.answered", d["event_id"])
    assert await env.db["webhook_deliveries"].count_documents({}) == 1


async def test_webhook_dead_letter_after_max_attempts(env):
    await enable_tenant(env)
    await env.client.post("/v1/webhooks/endpoints", json={"url": "http://receiver.test/h", "events": ["*"]}, headers=HDR_ADMIN())
    env.ctrl.s = env.s
    object.__setattr__(env.app.state.webhook_worker, "s", env.s.model_copy(update={"webhook_max_attempts": 2, "webhook_backoff_base_s": 0.001}))
    c = await make_call(env)
    await wait_state(env, c["id"], "initiating")
    w = env.app.state.webhook_worker
    w._http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    for _ in range(3):
        await env.db["webhook_deliveries"].update_many({"status": "retrying"}, {"$set": {"next_attempt_at": utcnow()}})
        await w.run_once()
        await asyncio.sleep(0.01)
    d = await env.db["webhook_deliveries"].find_one({})
    assert d["status"] == "dead" and d["attempt_count"] == 2
