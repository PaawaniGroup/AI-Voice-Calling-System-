"""Integration tests against the LOCAL FreeSWITCH via real ESL, using loopback legs in the isolated `mme_test`
dialplan context (config/freeswitch/mme_test_dialplan.xml). No carrier, no PSTN, no AI. Skipped if FreeSWITCH or
the test context is unavailable."""
import asyncio
import contextlib
import socket
import sys
from pathlib import Path

import httpx
import pytest

from app.main import create_app
from app.persistence.db import Database
from tests.conftest import auth, call_body, enable_tenant, idem, make_settings

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from esl_check import esl_password  # noqa: E402

pytestmark = pytest.mark.freeswitch


def _fs_up() -> bool:
    try:
        socket.create_connection(("127.0.0.1", 8021), timeout=1).close()
    except OSError:
        return False
    return bool(esl_password())


@pytest.fixture(autouse=True)
def _need_fs():
    if not _fs_up():
        pytest.skip("local FreeSWITCH ESL not reachable / no password available")


@contextlib.asynccontextmanager
async def fs_env(db: Database, dial="loopback/9001/mme_test", cleanup=True, **kw):
    s = make_settings(mongo_db=db.s.mongo_db, fs_enabled=True, esl_password=esl_password(), provider="freeswitch_gateway",
                      fs_dial_template=dial, fs_transfer_template="loopback/9004/mme_test",
                      esl_reconnect_initial_s=0.2, esl_reconnect_max_s=1.0, esl_idle_timeout_s=30,
                      reconcile_interval_s=1.0, ring_timeout_s=20, **kw)
    d = Database(s)
    await d.connect()
    app = create_app(s, db=d)
    async with app.router.lifespan_context(app):
        for _ in range(100):
            if app.state.controller.esl_events.connected:
                break
            await asyncio.sleep(0.05)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            e = type("E", (), {})()
            e.client, e.app, e.ctrl, e.repos, e.db, e.s = c, app, app.state.controller, app.state.repos, d, s
            with contextlib.suppress(Exception):  # clear stale test channels from earlier aborted runs
                await app.state.controller.esl_cmd.api("hupall NORMAL_CLEARING mme_leg x")
            yield e
        if cleanup:  # test hygiene: remove only channels created by this service
            with contextlib.suppress(Exception):
                await app.state.controller.esl_cmd.api("hupall NORMAL_CLEARING mme_leg a")
                await app.state.controller.esl_cmd.api("hupall NORMAL_CLEARING mme_leg x")
    await d.close()


async def wait_for(env, cid, states, timeout=10):
    states = {states} if isinstance(states, str) else set(states)
    end = asyncio.get_event_loop().time() + timeout
    c = None
    while asyncio.get_event_loop().time() < end:
        c = await env.repos.get_call(cid)
        if c["state"] in states:
            return c
        await asyncio.sleep(0.1)
    raise AssertionError(f"timeout waiting {states}; at {c['state']} cause={c.get('hangup_cause')}")


async def place(env):
    r = await env.client.post("/v1/calls", json=call_body(), headers={**auth(), **idem()})
    assert r.status_code == 202, r.text
    return r.json()["id"]


async def test_connect_subscribe_ready(db):
    await enable_tenant(type("X", (), {"repos": __import__("app.persistence.repos", fromlist=["Repos"]).Repos(db, db.s)}))
    async with fs_env(db) as env:
        ev = env.ctrl.esl_events
        assert ev.connected and ev.subscriptions == 1
        r = await env.client.get("/health/ready")
        assert r.status_code == 200 and r.json()["checks"]["freeswitch"] == "ok"
        assert "FreeSWITCH" in await env.ctrl.esl_cmd.api("version")
        assert "total" in await env.ctrl.esl_cmd.api("show channels count")


async def test_wrong_password_never_connects(db):
    from app.telephony.esl import EslEventClient
    ev = EslEventClient("127.0.0.1", 8021, "definitely-wrong", handler=lambda e: asyncio.sleep(0), connect_timeout=2,
                        reconnect_initial=0.1, reconnect_max=0.2, jitter=0.1)
    await ev.start()
    await asyncio.sleep(1.0)
    assert not ev.connected and ev.last_error == "EslError"
    await ev.stop()


async def test_outbound_answer_correlate_hangup(db):
    async with fs_env(db) as env:
        await enable_tenant(env)
        cid = await place(env)
        c = await wait_for(env, cid, "answered")
        out = await env.ctrl.esl_cmd.api(f"uuid_exists {c['fs_uuid']}")
        assert out.strip() == "true"  # application call id <-> FreeSWITCH UUID correlation
        assert c.get("connected_duration_s") is None
        r = await env.client.post(f"/v1/calls/{cid}/hangup", headers=auth())
        assert r.status_code == 200
        done = await wait_for(env, cid, "completed")
        assert done["connected_duration_s"] >= 0 and done["ended_at"]
        assert (await env.ctrl.esl_cmd.api(f"uuid_exists {c['fs_uuid']}")).strip() == "false"
        types = [e["type"] for e in (await env.client.get(f"/v1/calls/{cid}/events", headers=auth())).json()["items"]]
        assert "state.answered" in types and "state.completed" in types


@pytest.mark.parametrize("ext,state", [("9002", "busy"), ("9003", "failed")])
async def test_busy_and_unroutable(db, ext, state):
    async with fs_env(db, dial=f"loopback/{ext}/mme_test") as env:
        await enable_tenant(env)
        cid = await place(env)
        c = await wait_for(env, cid, state)
        assert c.get("connected_duration_s") is None and c.get("answered_at") is None


async def test_cancel_while_ringing(db):
    async with fs_env(db, dial="loopback/9005/mme_test") as env:
        await enable_tenant(env)
        cid = await place(env)
        await wait_for(env, cid, {"initiating", "ringing"})
        await asyncio.sleep(0.5)
        r = await env.client.post(f"/v1/calls/{cid}/cancel", headers=auth())
        assert r.status_code == 200
        c = await wait_for(env, cid, "cancelled")
        assert c.get("connected_duration_s") is None


async def test_transfer_on_live_channel(db):
    async with fs_env(db) as env:
        await enable_tenant(env)
        cid = await place(env)
        await wait_for(env, cid, "answered")
        r = await env.client.post(f"/v1/calls/{cid}/transfer", json={"destination": "+919999999999"}, headers=auth())
        assert r.status_code == 202, r.text
        c = await wait_for(env, cid, {"bridged", "completed", "failed"})
        assert c["state"] == "bridged", c
        assert c["transfer"]["attempts"][0]["outcome"] == "completed"
        await env.client.post(f"/v1/calls/{cid}/hangup", headers=auth())
        await wait_for(env, cid, "completed")


async def test_esl_reconnect_without_duplicate_subscription_and_events_resume(db):
    async with fs_env(db) as env:
        await enable_tenant(env)
        ev = env.ctrl.esl_events
        assert ev.subscriptions == 1
        ev.writer.close()  # simulate a dropped connection
        for _ in range(100):
            if ev.subscriptions == 2 and ev.connected:
                break
            await asyncio.sleep(0.1)
        assert ev.subscriptions == 2 and ev.reconnect_count >= 1
        cid = await place(env)
        await wait_for(env, cid, "answered")
        # exactly one ANSWERED transition despite reconnect (dedup + single subscription)
        evs = (await env.client.get(f"/v1/calls/{cid}/events", headers=auth())).json()["items"]
        assert [e["type"] for e in evs].count("state.answered") == 1
        await env.client.post(f"/v1/calls/{cid}/hangup", headers=auth())
        await wait_for(env, cid, "completed")


async def test_recovery_after_controller_restart(db):
    """Call connected -> service stops -> channel is killed while no controller is listening -> a fresh service
    instance reconciles persisted state against FreeSWITCH and finalises the call (no duplicate origination)."""
    async with fs_env(db, cleanup=False) as env1:
        await enable_tenant(env1)
        cid = await place(env1)
        c = await wait_for(env1, cid, "answered")
        uuid_ = c["fs_uuid"]
        esl_cmd_1 = env1.ctrl.esl_cmd
    # env1 stopped. Kill the channel using an independent client.
    from app.telephony.esl import EslCommandClient
    k = EslCommandClient("127.0.0.1", 8021, esl_password(), connect_timeout=3, reconnect_initial=0.2, reconnect_max=1, jitter=0.1)
    await k.api(f"uuid_kill {uuid_} NORMAL_CLEARING")
    await k.stop()
    assert esl_cmd_1 is not None
    async with fs_env(db, cleanup=False) as env2:
        got = await wait_for(env2, cid, {"completed", "failed"}, timeout=15)
        assert got["state"] == "completed"
        assert got["hangup_cause"] in ("RECONCILED", "NORMAL_CLEARING")
        assert await db["calls"].count_documents({}) == 1


async def test_reconcile_marks_uncertain_origination_without_channel(db):
    async with fs_env(db) as env:
        await enable_tenant(env)
        from app.core.ids import new_uuid, utcnow
        from datetime import timedelta
        now = utcnow() - timedelta(seconds=120)  # older than ring timeout + margin => safe to resolve
        await env.repos.insert_call({
            "_id": "call_uncertain1", "tenant_id": "t1", "direction": "outbound", "provider": "freeswitch_gateway",
            "from_number": "+911140000000", "to_number": "+919876543210", "state": "initiating", "state_version": 1,
            "initiated_at": now, "originate_started_at": now, "fs_uuid": new_uuid(), "origination_uncertain": True})
        stats = await env.ctrl.reconcile()
        assert stats["finalized"] == 1 and stats["uncertain_resolved"] == 1
        c = await env.repos.get_call("call_uncertain1")
        assert c["state"] == "failed" and c["failure_reason"] == "origination_uncertain_no_channel"


async def test_freeswitch_unavailable_holds_queue_durably(db):
    """Controller pointed at a closed ESL port: calls are accepted, persisted as queued, nothing is lost or originated."""
    s = make_settings(mongo_db=db.s.mongo_db, fs_enabled=True, esl_password="x", esl_port=1, provider="freeswitch_gateway",
                      fs_dial_template="loopback/9001/mme_test", esl_reconnect_initial_s=0.1, esl_reconnect_max_s=0.2,
                      esl_connect_timeout_s=0.5)
    d = Database(s)
    await d.connect()
    app = create_app(s, db=d)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            repos = app.state.repos
            await repos.put_policy("t1", {"enabled": True, "caller_ids": ["+911140000000"]})
            assert (await c.get("/health/live")).status_code == 200
            r = await c.get("/health/ready")
            assert r.status_code == 503 and r.json()["checks"]["freeswitch"] == "unavailable"
            r = await c.post("/v1/calls", json=call_body(), headers={**auth(), **idem()})
            assert r.status_code == 202
            await asyncio.sleep(1.0)
            assert (await repos.get_call(r.json()["id"]))["state"] == "queued"
    await d.close()


async def test_reconcile_does_not_finalize_recent_initiating_call(db):
    """Regression (found by load test): a call still originating must not be finalized by reconciliation."""
    async with fs_env(db) as env:
        await enable_tenant(env)
        from app.core.ids import new_uuid, utcnow
        now = utcnow()
        await env.repos.insert_call({
            "_id": "call_young1", "tenant_id": "t1", "direction": "outbound", "provider": "freeswitch_gateway",
            "from_number": "+911140000000", "to_number": "+919876543210", "state": "initiating", "state_version": 1,
            "initiated_at": now, "originate_started_at": now, "fs_uuid": new_uuid()})
        stats = await env.ctrl.reconcile()
        assert stats["finalized"] == 0
        assert (await env.repos.get_call("call_young1"))["state"] == "initiating"


async def test_reconcile_hangs_up_orphan_channel_of_terminal_call(db):
    async with fs_env(db) as env:
        await enable_tenant(env)
        from app.core.ids import new_uuid, utcnow
        u = new_uuid()
        await env.ctrl.esl_cmd.api(f"originate {{origination_uuid={u},mme_leg=a}}loopback/9001/mme_test &park()")
        await asyncio.sleep(0.8)
        assert (await env.ctrl.esl_cmd.api(f"uuid_exists {u}")).strip() == "true"
        await env.repos.insert_call({
            "_id": "call_orphan1", "tenant_id": "t1", "direction": "outbound", "provider": "freeswitch_gateway",
            "from_number": "+911140000000", "to_number": "+919876543210", "state": "failed", "state_version": 3,
            "fs_uuid": u, "created_at": utcnow()})
        stats = await env.ctrl.reconcile()
        assert stats.get("orphans_killed") == 1
        await asyncio.sleep(0.5)
        assert (await env.ctrl.esl_cmd.api(f"uuid_exists {u}")).strip() == "false"

async def test_inbound_routed_call_is_tracked_answered_and_rejected_when_unrouted(db):
    """Inbound path through the real `mme_inbound` dialplan context (loopback leg stands in for a carrier call)."""
    async with fs_env(db) as env:
        await enable_tenant(env)
        await db["inbound_routes"].insert_one({"number": "+911140001111", "tenant_id": "t1", "action": "park", "enabled": True})
        esl = env.ctrl.esl_cmd
        u1 = "11111111-aaaa-bbbb-cccc-000000000001"
        await esl.bgapi(f"originate {{origination_uuid={u1},mme_leg=x}}loopback/911140001111/mme_inbound &park()", "22222222-aaaa-bbbb-cccc-000000000001")
        call = None
        for _ in range(50):
            call = await db["calls"].find_one({"direction": "inbound"})
            if call and call["state"] == "answered":
                break
            await asyncio.sleep(0.1)
        assert call and call["state"] == "answered" and call["tenant_id"] == "t1" and call["to_number"] == "+911140001111"
        await esl.api(f"uuid_kill {call['fs_uuid']} NORMAL_CLEARING")
        done = await wait_for(env, call["_id"], {"completed", "failed"})
        assert done["state"] == "completed"
        # unrouted number: rejected, no call record created
        before = await db["calls"].count_documents({})
        await esl.bgapi("originate {mme_leg=x}loopback/911140009999/mme_inbound &park()", "22222222-aaaa-bbbb-cccc-000000000002")
        await asyncio.sleep(1.5)
        assert await db["calls"].count_documents({}) == before
        assert (await esl.api("show channels count")).startswith("0 total")


async def test_inbound_rejected_when_tenant_disabled_or_over_capacity(db):
    async with fs_env(db) as env:
        await env.repos.put_policy("t1", {"enabled": False})
        await db["inbound_routes"].insert_one({"number": "+911140001111", "tenant_id": "t1", "action": "park", "enabled": True})
        await env.ctrl.esl_cmd.bgapi("originate {mme_leg=x}loopback/911140001111/mme_inbound &park()", str(__import__("uuid").uuid4()))
        await asyncio.sleep(1.5)
        assert await db["calls"].count_documents({}) == 0
        assert (await env.ctrl.esl_cmd.api("show channels count")).startswith("0 total")
        await env.repos.put_policy("t1", {"enabled": True, "max_active_calls": 1})
        await env.ctrl.esl_cmd.bgapi("originate {mme_leg=x}loopback/911140001111/mme_inbound &park()", str(__import__("uuid").uuid4()))
        await asyncio.sleep(1.0)
        await env.ctrl.esl_cmd.bgapi("originate {mme_leg=x}loopback/911140001111/mme_inbound &park()", str(__import__("uuid").uuid4()))
        await asyncio.sleep(1.5)
        assert await db["calls"].count_documents({"direction": "inbound"}) == 1  # second rejected (tenant cap 1)
        await env.ctrl.esl_cmd.api("hupall NORMAL_CLEARING")
