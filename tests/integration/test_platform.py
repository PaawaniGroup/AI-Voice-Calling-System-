import asyncio
import json
import uuid

import httpx
import pytest
import pytest_asyncio

from app.main import create_app
from app.providers.base import MockProvider
from app.security import users as userlib
from tests.conftest import auth, call_body, enable_tenant, idem, make_settings
from tests.helpers import live_server
from tests.integration.test_api_calls import esl, make_call, wait_state

PW = "correct horse battery"


# ------------------------------------------------------------------ login, roles, masking
async def test_login_roles_and_failures(env):
    for name, role in [("vera", "viewer"), ("olga", "operator"), ("adam", "admin")]:
        await userlib.create_user(env.db, name, PW, "t1", role)
    bad = await env.client.post("/v1/auth/login", json={"username": "vera", "password": "wrong-password-1"})
    unk = await env.client.post("/v1/auth/login", json={"username": "nobody", "password": "wrong-password-1"})
    assert bad.status_code == unk.status_code == 401 and bad.json()["error"]["code"] == unk.json()["error"]["code"] == "invalid_credentials"
    toks = {}
    for name in ("vera", "olga", "adam"):
        r = await env.client.post("/v1/auth/login", json={"username": name, "password": PW})
        assert r.status_code == 200 and "password" not in r.text
        toks[name] = {"Authorization": "Bearer " + r.json()["access_token"]}
    await enable_tenant(env)
    # viewer: reads yes, control no
    assert (await env.client.get("/v1/calls", headers=toks["vera"])).status_code == 200
    assert (await env.client.get("/v1/dashboard/overview", headers=toks["vera"])).status_code == 200
    r = await env.client.post("/v1/calls", json=call_body(), headers={**toks["vera"], **idem()})
    assert r.status_code == 403
    # operator: can call, cannot administer
    r = await env.client.post("/v1/calls", json=call_body(), headers={**toks["olga"], **idem()})
    assert r.status_code == 202
    assert (await env.client.get("/v1/admin/users", headers=toks["olga"])).status_code == 403
    assert (await env.client.get("/v1/admin/users", headers=toks["adam"])).status_code == 200
    # viewer sees masked numbers, operator sees full numbers
    cid = r.json()["id"]
    v = (await env.client.get(f"/v1/calls/{cid}", headers=toks["vera"])).json()
    o = (await env.client.get(f"/v1/calls/{cid}", headers=toks["olga"])).json()
    assert "9876543210" not in v["to_number"] and "*" in v["to_number"] and o["to_number"] == "+919876543210"
    # audit trail of failed login recorded, password never stored in audit
    audit = await env.db["audit"].find({"action": {"$in": ["auth.login_failed", "auth.login"]}}).to_list(20)
    assert any(a["action"] == "auth.login_failed" for a in audit) and "wrong-password" not in json.dumps(audit, default=str)


async def test_login_is_rate_limited_and_hashes_are_scrypt(env):
    u = await userlib.create_user(env.db, "rate", PW, "t1", "viewer")
    assert u["password_hash"].startswith("scrypt$") and PW not in u["password_hash"]
    env.app.state.settings = env.s.model_copy(update={"login_rate_limit_per_min": 3})
    codes = [(await env.client.post("/v1/auth/login", json={"username": "rate", "password": "nope-nope-nope"})).status_code for _ in range(5)]
    assert codes[:3] == [401] * 3 and 429 in codes[3:]
    with pytest.raises(Exception):
        userlib.hash_password("short")


async def test_platform_role_only_from_cli_not_http(env):
    r = await env.client.post("/v1/admin/users", json={"username": "root1", "password": PW, "role": "platform"}, headers=auth("t1", ("telephony:admin",)))
    assert r.status_code == 422  # not an allowed value over HTTP
    await userlib.create_user(env.db, "root1", PW, "t1", "platform", allow_platform=True)
    tok = (await env.client.post("/v1/auth/login", json={"username": "root1", "password": PW})).json()["access_token"]
    h = {"Authorization": "Bearer " + tok}
    r = await env.client.put("/v1/admin/tenants/t9/policy", json={"enabled": True, "caller_ids": ["+911140000000"]}, headers=h)
    assert r.status_code == 200 and r.json()["tenant_id"] == "t9"


async def test_disabled_user_cannot_login_and_users_are_tenant_scoped(env):
    await userlib.create_user(env.db, "carl", PW, "t1", "operator")
    await userlib.create_user(env.db, "dina", PW, "t2", "admin")
    adm = auth("t1", ("telephony:admin",))
    r = await env.client.post("/v1/admin/users/carl/disable", headers=auth("t2", ("telephony:admin",)))
    assert r.status_code == 404  # other tenant's admin cannot touch t1 users
    assert (await env.client.post("/v1/admin/users/carl/disable", headers=adm)).status_code == 200
    assert (await env.client.post("/v1/auth/login", json={"username": "carl", "password": PW})).status_code == 401
    r = await env.client.post("/v1/admin/users", json={"username": "evil", "password": PW + "x", "role": "admin", "tenant_id": "t2"}, headers=adm)
    assert r.status_code == 403


# ------------------------------------------------------------------ sessions
async def test_session_created_with_call_answered_and_ended_by_events(env):
    await enable_tenant(env)
    c = await make_call(env, language="hinglish", ai_enabled=True)
    assert c["session_id"] and c["language"] == "hinglish" and c["ai_enabled"] is True
    s = (await env.client.get(f"/v1/sessions/{c['session_id']}", headers=auth())).json()
    assert s["status"] == "created" and s["call_id"] == c["id"]
    c2 = await wait_state(env, c["id"], "initiating")
    await env.ctrl.on_esl_event(esl("CHANNEL_ANSWER", c2["fs_uuid"]))
    s = (await env.client.get(f"/v1/sessions/{c['session_id']}", headers=auth())).json()
    assert s["status"] == "answered" and s["answered_at"]
    await env.ctrl.on_esl_event(esl("CHANNEL_HANGUP", c2["fs_uuid"], **{"Hangup-Cause": "NORMAL_CLEARING"}))
    s = (await env.client.get(f"/v1/sessions/{c['session_id']}", headers=auth())).json()
    assert s["status"] == "ended" and s["end_reason"] and [h["state"] for h in s["history"]] == ["created", "answered", "ended"]
    assert (await env.client.get(f"/v1/calls/{c['id']}/session", headers=auth())).json()["session_id"] == c["session_id"]


async def test_failed_call_marks_session_failed_and_duplicates_do_not_duplicate(env):
    await enable_tenant(env)
    c = await make_call(env)
    c2 = await wait_state(env, c["id"], "initiating")
    h = esl("CHANNEL_HANGUP", c2["fs_uuid"], **{"Hangup-Cause": "CALL_REJECTED"})
    await env.ctrl.on_esl_event(h)
    await env.ctrl.on_esl_event(h)
    assert (await env.client.get(f"/v1/sessions/{c['session_id']}", headers=auth())).json()["status"] == "failed"
    assert await env.db["sessions"].count_documents({"call_id": c["id"]}) == 1


async def test_session_api_idempotent_validated_and_tenant_isolated(env):
    h = auth("t1")
    r1 = await env.client.post("/v1/sessions", json={"tenant_id": "t1", "caller_id": "+919876543210", "call_id": "fs-uuid-1", "language": "hi"}, headers=h)
    r2 = await env.client.post("/v1/sessions", json={"tenant_id": "t1", "call_id": "fs-uuid-1"}, headers=h)
    assert r1.status_code == 201 and r2.json()["session_id"] == r1.json()["session_id"]  # one session per call reference
    assert (await env.client.post("/v1/sessions", json={"tenant_id": "t2"}, headers=h)).status_code == 403  # tenant from token only
    assert (await env.client.post("/v1/sessions", json={"tenant_id": "t1", "language": "klingon"}, headers=h)).status_code == 422
    sid = r1.json()["session_id"]
    assert (await env.client.get(f"/v1/sessions/{sid}", headers=auth("t2"))).status_code == 404
    assert (await env.client.post(f"/v1/sessions/{sid}/answer", headers=auth("t2"))).status_code == 404
    assert (await env.client.post(f"/v1/sessions/{sid}/answer", headers=h)).json()["status"] == "answered"
    assert (await env.client.post(f"/v1/sessions/{sid}/answer", headers=h)).json()["status"] == "answered"  # idempotent
    e = await env.client.post(f"/v1/sessions/{sid}/end", json={"reason": "done"}, headers=h)
    assert e.json()["status"] == "ended" and e.json()["end_reason"] == "done"
    e2 = await env.client.post(f"/v1/sessions/{sid}/end", json={"reason": "again"}, headers=h)
    assert e2.json()["end_reason"] == "done"  # terminal state is immutable
    lst = (await env.client.get("/v1/sessions?q=fs-uuid-1", headers=h)).json()["items"]
    assert [x["session_id"] for x in lst] == [sid]
    assert (await env.client.get("/v1/sessions", headers=auth("t2"))).json()["items"] == []


async def test_session_and_call_webhooks_emitted(env):
    await enable_tenant(env)
    await env.client.post("/v1/webhooks/endpoints", json={"url": "http://receiver.test/h", "events": ["*"]}, headers=auth("t1", ("telephony:admin",)))
    c = await make_call(env)
    c2 = await wait_state(env, c["id"], "initiating")
    await env.ctrl.on_esl_event(esl("CHANNEL_ANSWER", c2["fs_uuid"]))
    await env.ctrl.on_esl_event(esl("CHANNEL_HANGUP", c2["fs_uuid"], **{"Hangup-Cause": "NORMAL_CLEARING"}))
    types = {d["event_type"] for d in await env.db["webhook_deliveries"].find({}).to_list(100)}
    assert {"call.created", "call.initiated", "call.answered", "call.completed", "call.ended", "session.created", "session.answered", "session.ended"} <= types


# ------------------------------------------------------------------ observe-only inbound (public context)
async def test_observed_inbound_creates_records_and_never_commands_freeswitch(db):
    s = make_settings(mongo_db=db.s.mongo_db, observe_contexts="public")
    app = create_app(s, db=db, provider=MockProvider())
    async with app.router.lifespan_context(app):
        ctrl = app.state.controller
        assert ctrl.esl_cmd is None  # any ESL command would raise -> proves record-only behaviour
        await db["inbound_routes"].insert_one({"number": "+918065530418", "tenant_id": "mme", "action": "park", "enabled": True})
        u = str(uuid.uuid4())
        base = {"Unique-ID": u, "Call-Direction": "inbound", "Caller-Context": "public", "Caller-Destination-Number": "918065530418",
                "Caller-Caller-ID-Number": "+919876543210", "variable_mme_leg": "x"}
        await ctrl.on_esl_event({"Event-Name": "CHANNEL_CREATE", "Event-Sequence": "1", **base})
        await ctrl.on_esl_event({"Event-Name": "CHANNEL_PARK", "Event-Sequence": "2", **base})  # duplicate trigger
        await ctrl.on_esl_event({"Event-Name": "CHANNEL_ANSWER", "Event-Sequence": "3", **base})
        await ctrl.on_esl_event({"Event-Name": "CHANNEL_HANGUP", "Event-Sequence": "4", "Hangup-Cause": "NORMAL_CLEARING", **base})
        calls = await db["calls"].find({}).to_list(10)
        assert len(calls) == 1 and calls[0]["direction"] == "inbound" and calls[0]["observed"] and calls[0]["state"] == "completed"
        assert calls[0]["tenant_id"] == "mme" and calls[0]["session_id"]
        sess = await db["sessions"].find_one({"_id": calls[0]["session_id"]})
        assert sess["status"] == "ended" and sess["source"] == "call"
        # unrouted number and other contexts are ignored
        await ctrl.on_esl_event({"Event-Name": "CHANNEL_CREATE", "Event-Sequence": "5", **{**base, "Unique-ID": str(uuid.uuid4()), "Caller-Destination-Number": "910000000001"}})
        await ctrl.on_esl_event({"Event-Name": "CHANNEL_CREATE", "Event-Sequence": "6", **{**base, "Unique-ID": str(uuid.uuid4()), "Caller-Context": "default"}})
        assert await db["calls"].count_documents({}) == 1


# ------------------------------------------------------------------ dashboard data + system
async def test_overview_system_status_queues_logs(env):
    await enable_tenant(env)
    c = await make_call(env)
    c2 = await wait_state(env, c["id"], "initiating")
    await env.ctrl.on_esl_event(esl("CHANNEL_ANSWER", c2["fs_uuid"]))
    await env.ctrl.on_esl_event(esl("CHANNEL_HANGUP", c2["fs_uuid"], **{"Hangup-Cause": "NORMAL_CLEARING"}))
    import logging
    logging.getLogger("mme_telephony.test").error("failed with password=hunter2 for +919876543210")
    o = (await env.client.get("/v1/dashboard/overview", headers=auth("t1", ("telephony:admin",)))).json()
    assert o["calls_today"] == 1 and o["completed_today"] == 1 and o["health"]["database"]["status"] == "up"
    assert o["health"]["last_ok_at"]["database"] and o["health"]["ai"]["status"] == "disabled" and o["health"]["ai"]["media_bridge"]["ready"] is False
    assert (await env.client.get("/v1/dashboard/overview", headers=auth("t2"))).json()["calls_today"] == 0  # tenant scoped
    ss = (await env.client.get("/v1/system/status", headers=auth())).json()
    assert ss["version"] and ss["components"]["workers"]["webhook_worker"] is True
    assert (await env.client.get("/v1/system/queues", headers=auth())).status_code == 200
    # logs: admin only, redacted
    assert (await env.client.get("/v1/logs/errors", headers=auth())).status_code == 403
    logs = (await env.client.get("/v1/logs/errors?q=failed", headers=auth("t1", ("telephony:admin",)))).json()["items"]
    assert logs and "hunter2" not in json.dumps(logs) and "9876543210" not in json.dumps(logs)
    # transcripts/summary honestly unavailable; recording 404 without data
    for p in ("transcript", "summary", "recording"):
        r = await env.client.get(f"/v1/calls/{c['id']}/{p}", headers=auth())
        assert r.status_code == 404 and r.json()["error"]["code"].endswith("not_available")
        assert (await env.client.get(f"/v1/calls/{c['id']}/{p}", headers=auth("t2"))).status_code == 404


@pytest_asyncio.fixture
async def server(db):
    s = make_settings(mongo_db=db.s.mongo_db)
    app = create_app(s, db=db, provider=MockProvider())
    async with live_server(app) as host:
        yield type("S", (), {"app": app, "host": host, "url": f"http://{host}"})()


async def test_dashboard_static_served_with_csp_and_sse_streams_live_calls(server):
    async with httpx.AsyncClient(base_url=server.url, timeout=10) as c:
        r = await c.get("/dashboard/")
        assert r.status_code == 200 and "MME Voice" in r.text
        assert "script-src 'self'" in r.headers["content-security-policy"] and r.headers["x-frame-options"] == "DENY"
        import re
        assert not re.search(r"\.innerHTML|insertAdjacentHTML|document\.write|eval\(", (await c.get("/dashboard/app.js")).text)  # XSS-safe rendering rule
        await enable_tenant(type("E", (), {"repos": server.app.state.repos}))
        seen = []
        async with c.stream("GET", "/v1/stream/events", headers=auth()) as resp:
            assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/event-stream")
            await c.post("/v1/calls", json=call_body(), headers={**auth(), **idem()})
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    seen = json.loads(line[6:])
                    if seen:
                        break
        assert len(seen) == 1 and seen[0]["state"] in ("queued", "initiating")
        # unauthenticated streams refused
        assert (await c.get("/v1/stream/events")).status_code == 401
