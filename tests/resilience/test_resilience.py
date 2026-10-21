import asyncio
import uuid

import httpx
import pytest
from pymongo.errors import ServerSelectionTimeoutError

from app.main import create_app
from app.persistence.repos import Repos
from app.providers.base import MockProvider
from tests.conftest import auth, call_body, enable_tenant, idem, make_settings
from tests.integration.test_api_calls import esl, make_call, wait_state


async def test_database_unavailable_returns_503_without_leaking(env, monkeypatch):
    await enable_tenant(env)

    async def boom(*a, **k):
        raise ServerSelectionTimeoutError("mongodb://user:secret@host:27017 timed out")

    monkeypatch.setattr(Repos, "count_active", boom)
    r = await env.client.post("/v1/calls", json=call_body(), headers={**auth(), **idem()})
    assert r.status_code == 503 and r.json()["error"]["code"] == "database_unavailable"
    assert "secret" not in r.text and "mongodb" not in r.text
    # failed creation must not leave the idempotency key burned
    key = {"Idempotency-Key": "k" * 12}
    assert (await env.client.post("/v1/calls", json=call_body(), headers={**auth(), **key})).status_code == 503
    monkeypatch.undo()
    assert (await env.client.post("/v1/calls", json=call_body(), headers={**auth(), **key})).status_code == 202


async def test_readiness_reports_database_down(env, monkeypatch):
    async def boom():
        raise ServerSelectionTimeoutError("x")

    monkeypatch.setattr(env.db, "ping", boom)
    r = await env.client.get("/health/ready")
    assert r.status_code == 503 and r.json()["checks"]["database"] == "unavailable"
    assert (await env.client.get("/health/live")).status_code == 200  # liveness unaffected


async def test_startup_fails_fast_when_database_unreachable():
    s = make_settings(mongo_uri="mongodb://127.0.0.1:1", mongo_server_selection_timeout_ms=300, mongo_connect_timeout_ms=300)
    app = create_app(s)
    with pytest.raises(Exception):
        async with app.router.lifespan_context(app):
            pass


async def test_state_survives_service_restart_and_idempotency_persists(db):
    """A new app instance over the same database continues the call; replayed request does not create a 2nd call."""
    s = db.s
    prov1 = MockProvider()
    app1 = create_app(s, db=db, provider=prov1)
    key = uuid.uuid4().hex
    async with app1.router.lifespan_context(app1):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app1), base_url="http://t") as c:
            await app1.state.repos.put_policy("t1", {"enabled": True, "caller_ids": ["+911140000000"]})
            r1 = await c.post("/v1/calls", json=call_body(), headers={**auth(), "Idempotency-Key": key})
    cid = r1.json()["id"]
    prov2 = MockProvider()
    app2 = create_app(s, db=db, provider=prov2)
    async with app2.router.lifespan_context(app2):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app2), base_url="http://t") as c:
            r2 = await c.post("/v1/calls", json=call_body(), headers={**auth(), "Idempotency-Key": key})
            assert r2.status_code == 200 and r2.json()["id"] == cid
            assert await db["calls"].count_documents({}) == 1
            # fs_uuid is persisted BEFORE the provider call, so wait for the origination itself (not just the uuid)
            for _ in range(100):
                if len(prov1.originated) + len(prov2.originated) >= 1:
                    break
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.5)  # a duplicate origination, if there were one, would show up in this window
    total_originations = len(prov1.originated) + len(prov2.originated)
    assert total_originations == 1  # exactly one origination across the restart


async def test_concurrent_event_race_yields_single_consistent_terminal_state(env):
    await enable_tenant(env)
    c = await make_call(env)
    c = await wait_state(env, c["id"], "initiating")
    u = c["fs_uuid"]
    await env.ctrl.on_esl_event(esl("CHANNEL_ANSWER", u))
    evs = [esl("CHANNEL_HANGUP", u, **{"Hangup-Cause": "NORMAL_CLEARING"}),
           esl("CHANNEL_HANGUP_COMPLETE", u, **{"Hangup-Cause": "NORMAL_CLEARING"}),
           esl("CHANNEL_HANGUP", u, **{"Hangup-Cause": "USER_BUSY"}),
           esl("CHANNEL_BRIDGE", u, **{"Other-Leg-Unique-ID": str(uuid.uuid4())})]
    await asyncio.gather(*(env.ctrl.on_esl_event(e) for e in evs))
    got = await env.repos.get_call(c["_id"])
    assert got["state"] in ("completed", "bridged")  # bridge may legitimately precede hangup
    if got["state"] != "completed":  # ordering race: a later hangup must still finalise
        await env.ctrl.on_esl_event(esl("CHANNEL_HANGUP", u, **{"Hangup-Cause": "NORMAL_CLEARING"}))
        got = await env.repos.get_call(c["_id"])
    assert got["state"] == "completed" and got["ended_at"] is not None
    ended = got["ended_at"]
    await env.ctrl.on_esl_event(esl("CHANNEL_HANGUP", u, **{"Hangup-Cause": "USER_BUSY"}))
    assert (await env.repos.get_call(c["_id"]))["ended_at"] == ended  # terminal state is immutable


async def test_malformed_events_do_not_crash_handler(env):
    for ev in [{}, {"Event-Name": "CHANNEL_ANSWER"}, {"Event-Name": "CHANNEL_HANGUP", "Unique-ID": "not-a-uuid"},
               {"Event-Name": "BACKGROUND_JOB", "Job-UUID": "zzz"}, {"Event-Name": "CHANNEL_CREATE", "Unique-ID": "x", "Call-Direction": "inbound"}]:
        await env.ctrl.on_esl_event(ev)


async def test_queue_timeout_fails_waiting_calls(env):
    await enable_tenant(env)
    env.ctrl.max_active = 0
    c = await make_call(env)
    env.ctrl.s = env.s.model_copy(update={"queue_max_wait_s": 0})
    await asyncio.sleep(0.05)
    await env.ctrl.sweep_timeouts()
    got = await env.repos.get_call(c["id"])
    assert got["state"] == "failed" and got["failure_reason"] == "queue_timeout"


async def test_max_duration_backstop_hangs_up(env):
    await enable_tenant(env, max_duration_s=10)
    c = await make_call(env)
    c = await wait_state(env, c["id"], "initiating")
    await env.ctrl.on_esl_event(esl("CHANNEL_ANSWER", c["fs_uuid"]))
    from datetime import timedelta
    from app.core.ids import utcnow
    await env.db["calls"].update_one({"_id": c["_id"]}, {"$set": {"answered_at": utcnow() - timedelta(seconds=60)}})
    await env.ctrl.sweep_timeouts()
    assert env.prov.hangups and env.prov.hangups[-1][1] == "ALLOTTED_TIMEOUT"
    await env.ctrl.on_esl_event(esl("CHANNEL_HANGUP", c["fs_uuid"], **{"Hangup-Cause": "ALLOTTED_TIMEOUT"}))
    got = await env.repos.get_call(c["_id"])
    assert got["state"] == "completed" and got["end_reason"] == "max_duration_reached"
