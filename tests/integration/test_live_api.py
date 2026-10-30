"""Live operations API: auth, tenant isolation, masking, what is (and is not) published, and the SSE stream over a real server.
Provider is the test MOCK and models are mocks: this proves the dashboard feed, not real calls or real models."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from app.main import create_app
from app.providers.base import MockProvider
from tests.conftest import auth, call_body, enable_tenant, idem, make_settings
from tests.helpers import live_server
from tests.integration.test_api_calls import esl, make_call, wait_state

ADMIN = lambda t="t1": auth(t, ("telephony:admin",))  # noqa: E731
VIEWER = lambda t="t1": auth(t, ("telephony:viewer",))  # noqa: E731


async def seed_call(e, tenant="t1", cid="call_x1", state="answered", **kw):
    doc = {"_id": cid, "tenant_id": tenant, "direction": "inbound", "provider": "mock", "from_number": "+919876543210",
           "to_number": "+911140000000", "state": state, "state_version": 2, "answered_at": None, **kw}
    await e.repos.insert_call(doc)
    return await e.repos.get_call(cid)


# ------------------------------------------------------------------ auth
async def test_every_live_endpoint_requires_a_token(env):
    for path in ("/v1/live/snapshot", "/v1/live/activity", "/v1/live/stream", "/v1/live/calls/x/flow"):
        assert (await env.client.get(path)).status_code == 401, path
        assert (await env.client.get(path, headers={"Authorization": "Bearer junk"})).status_code == 401, path


# ------------------------------------------------------------------ snapshot
async def test_snapshot_shape_scope_and_masking(env):
    await seed_call(env, "t1", "call_a", answered_at=None)
    await seed_call(env, "t2", "call_b")
    snap = (await env.client.get("/v1/live/snapshot", headers=ADMIN())).json()
    assert {"as_of", "service", "components", "gauges", "flows", "last_seq", "process", "recent_errors"} <= set(snap)
    assert [f["call"]["id"] for f in snap["flows"]] == ["call_a"]  # only this tenant's calls
    assert snap["flows"][0]["call"]["from_number"] == "+919876543210"  # operators/admins see full numbers
    stages = [s["key"] for s in snap["flows"][0]["flow"]["stages"]]
    assert stages == ["created", "ringing", "answered", "session", "media", "ai", "ended"]
    keys = {c["key"] for c in snap["components"]}
    assert {"plivo", "fs_events", "media_bridge", "controller", "database", "webhooks", "stt", "brain", "tts", "rag"} <= keys
    assert next(c for c in snap["components"] if c["key"] == "database")["status"] == "up"
    assert next(c for c in snap["components"] if c["key"] == "stt")["status"] == "disabled"  # AI is off: stated, not guessed
    v = (await env.client.get("/v1/live/snapshot", headers=VIEWER())).json()
    assert "9876543210" not in json.dumps(v) and "*" in v["flows"][0]["call"]["from_number"]  # read-only viewers get masked numbers
    assert "process" not in v and "recent_errors" not in v  # process-wide numbers and error logs are admin-only


async def test_gauges_reflect_stored_state(env):
    await seed_call(env, "t1", "call_g1", state="answered", answered_at=None)
    await seed_call(env, "t1", "call_g2", state="completed")
    g = (await env.client.get("/v1/live/snapshot", headers=ADMIN())).json()["gauges"]
    assert g["active_calls"] == 1 and g["calls_today"] == 2 and g["completed_today"] == 1


# ------------------------------------------------------------------ activity feed
async def test_state_changes_reach_the_feed_for_the_owning_tenant_only(env):
    await enable_tenant(env)
    c = await make_call(env)
    c2 = await wait_state(env, c["id"], "initiating")
    await env.ctrl.on_esl_event(esl("CHANNEL_ANSWER", c2["fs_uuid"]))
    mine = (await env.client.get("/v1/live/activity", headers=VIEWER())).json()
    kinds = [i["kind"] for i in mine["items"] if i["call_id"] == c["id"]]
    assert "state.initiating" in kinds and "state.answered" in kinds
    assert [i["seq"] for i in mine["items"]] == sorted(i["seq"] for i in mine["items"])
    other = (await env.client.get("/v1/live/activity", headers=VIEWER("t2"))).json()
    assert other["items"] == []  # tenant isolation
    after = mine["items"][-1]["seq"]
    assert (await env.client.get(f"/v1/live/activity?after={after}", headers=VIEWER())).json()["items"] == []


async def test_numbers_and_secrets_inside_events_are_masked_in_the_feed(env):
    call = await seed_call(env)
    await env.repos.add_event(call, "transfer.requested", "api", "dedup-1", {"destination": "+919999999999", "auth_token": "tok-123456"})
    txt = (await env.client.get("/v1/live/activity", headers=VIEWER())).text
    assert "9999999999" not in txt and "tok-123456" not in txt and "transfer.requested" in txt


async def test_duplicate_events_are_not_announced_twice(env):
    call = await seed_call(env)
    assert await env.repos.add_event(call, "state.ringing", "esl", "same-key") is True
    assert await env.repos.add_event(call, "state.ringing", "esl", "same-key") is False
    items = (await env.client.get("/v1/live/activity", headers=VIEWER())).json()["items"]
    assert [i["kind"] for i in items].count("state.ringing") == 1


async def test_ai_turn_entries_carry_timings_never_words(env):
    await env.db["ai_sessions"].insert_one({"session_id": "s1", "tenant_id": "t1", "state": "active", "turns": 0, "timings_ms": {}})
    rt = SimpleNamespace(session_id="s1", tenant_id="t1", call_id="call_ai1", conversation=SimpleNamespace(turns=[{"heard": "SECRET WORDS", "reply": "MORE SECRET"}]))
    await env.app.state.ai.record_turn(rt, {"stt": 310.5, "brain": 800.0, "tts": 400.2, "turn_ms": 1510.7, "heard": "SECRET WORDS"})
    env.app.state.ai.note_error(rt, "tts_unreachable")
    items = (await env.client.get("/v1/live/activity", headers=VIEWER())).json()["items"]
    turn = next(i for i in items if i["kind"] == "ai.turn")
    assert turn["call_id"] == "call_ai1" and turn["data"]["turn"] == 1 and turn["data"]["turn_ms"] == 1510.7
    assert "SECRET" not in json.dumps(items) and "heard" not in turn["data"]
    assert next(i for i in items if i["kind"] == "ai.turn_error")["level"] == "error"


async def test_session_lifecycle_is_published(env):
    s, created = await env.app.state.sessions.create("t1", call_ref="ref-1", caller="+919876543210", destination="+911140000000")
    await env.app.state.sessions.answer("t1", s["_id"])
    kinds = [i["kind"] for i in (await env.client.get("/v1/live/activity", headers=VIEWER())).json()["items"]]
    assert "session.created" in kinds and "session.answered" in kinds


# ------------------------------------------------------------------ per-call flow
async def test_call_flow_endpoint_is_tenant_scoped(env):
    await seed_call(env, "t1", "call_f1", state="answered", answered_at=None, ai_enabled=True)
    f = (await env.client.get("/v1/live/calls/call_f1/flow", headers=VIEWER())).json()
    assert f["overall"] == "live" and [s["key"] for s in f["stages"]][-1] == "ended"
    assert (await env.client.get("/v1/live/calls/call_f1/flow", headers=VIEWER("t2"))).status_code == 404


# ------------------------------------------------------------------ SSE over a real server
@pytest_asyncio.fixture
async def sse(db):
    s = make_settings(mongo_db=db.s.mongo_db)
    app = create_app(s, db=db, provider=MockProvider())
    async with live_server(app) as host:
        e = SimpleNamespace(app=app, host=host, db=db, repos=app.state.repos)
        await enable_tenant(e)
        yield e


class Sse:
    """One persistent reader per response (httpx cannot restart aiter_text on a stream that was already read)."""

    def __init__(self, resp):
        self.it, self.buf = resp.aiter_text().__aiter__(), ""

    async def until(self, want: set[str], timeout: float = 8.0, stop_when=None) -> list[tuple[str, dict]]:
        """Events read by THIS call, returned once every name in `want` was seen (and the optional predicate holds)."""
        got, seen = [], set()

        async def go():
            while True:
                while "\n\n" in self.buf:
                    blk, self.buf = self.buf.split("\n\n", 1)
                    if blk.startswith("event: "):
                        name = blk.split("\n")[0][7:]
                        got.append((name, json.loads(blk.split("\ndata: ", 1)[1])))
                        seen.add(name)
                        if want <= seen and (stop_when is None or stop_when(got)):
                            return
                self.buf += await self.it.__anext__()
        await asyncio.wait_for(go(), timeout)
        return got


async def test_stream_sends_hello_backlog_snapshot_then_live_activity(sse):
    base = f"http://{sse.host}"
    call = await seed_call(sse, "t1", "call_s1")
    await sse.repos.add_event(call, "state.ringing", "esl", "pre-1")  # before the stream opens: arrives in the backlog
    async with httpx.AsyncClient(base_url=base, timeout=15) as c:
        async with c.stream("GET", "/v1/live/stream", headers=VIEWER()) as r:
            assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
            rd = Sse(r)
            first = await rd.until({"hello", "backlog", "snapshot"})
            names = [n for n, _ in first]
            assert names.index("hello") < names.index("backlog") < names.index("snapshot")
            backlog = next(d for n, d in first if n == "backlog")
            assert any(i["kind"] == "state.ringing" for i in backlog)
            snap = next(d for n, d in first if n == "snapshot")
            assert snap["flows"][0]["call"]["id"] == "call_s1" and "9876543210" not in json.dumps(snap)  # viewer: masked
            # a new event while the stream is open is pushed as it happens (not on the next poll)
            await sse.repos.add_event(call, "state.answered", "esl", "live-1")
            more = await rd.until({"activity"}, stop_when=lambda g: any(i["kind"] == "state.answered" for n, d in g if n == "activity" for i in d["items"]))
            acts = [i for n, d in more if n == "activity" for i in d["items"]]
            assert [i["kind"] for i in acts] == ["state.answered"] and acts[0]["call_id"] == "call_s1"


async def test_stream_never_leaks_another_tenants_events(sse):
    mine, theirs = await seed_call(sse, "t1", "call_m"), await seed_call(sse, "t2", "call_t")
    async with httpx.AsyncClient(base_url=f"http://{sse.host}", timeout=15) as c:
        async with c.stream("GET", "/v1/live/stream", headers=VIEWER("t1")) as r:
            rd = Sse(r)
            await rd.until({"hello", "backlog", "snapshot"})
            await sse.repos.add_event(theirs, "state.answered", "esl", "other-tenant")
            await sse.repos.add_event(mine, "state.bridged", "esl", "mine")
            got = await rd.until({"activity"}, stop_when=lambda g: any(i["kind"] == "state.bridged" for n, d in g if n == "activity" for i in d["items"]))
            txt = json.dumps(got)
            assert "call_t" not in txt and "other-tenant" not in txt


async def test_stream_limit_per_tenant_and_slot_is_released(sse):
    base = f"http://{sse.host}"
    async with httpx.AsyncClient(base_url=base, timeout=15) as c:
        open_ctx = []
        try:
            for _ in range(5):
                cm = c.stream("GET", "/v1/live/stream", headers=VIEWER())
                r = await cm.__aenter__()
                open_ctx.append(cm)
                assert r.status_code == 200
            over = await c.get("/v1/live/stream", headers=VIEWER())
            assert over.status_code == 429 and over.json()["error"]["code"] == "too_many_streams"
            assert (await c.get("/v1/live/snapshot", headers=VIEWER("t2"))).status_code == 200  # another tenant is unaffected
        finally:
            for cm in open_ctx:
                await cm.__aexit__(None, None, None)
        for _ in range(60):  # a closed stream frees its slot
            async with c.stream("GET", "/v1/live/stream", headers=VIEWER()) as r:
                if r.status_code == 200:
                    break
            await asyncio.sleep(0.1)
        assert r.status_code == 200
