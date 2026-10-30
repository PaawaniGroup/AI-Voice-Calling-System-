"""scripts/plivo_testcall.py core: arm whitelists one caller with a 1-call/180 s cap; disarm (the kill switch) stops new calls and asks
the provider to hang up every live call, reporting a failed hangup instead of hiding it. Provider is a FAKE callable; no network."""
import importlib.util
from pathlib import Path

from tests.conftest import make_settings

_spec = importlib.util.spec_from_file_location("plivo_testcall", Path(__file__).resolve().parents[2] / "scripts" / "plivo_testcall.py")
tc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tc)

ME, DID, T = "+919876543210", "+918065530418", "t_testcall"


async def test_arm_sets_allowlist_cap_and_ai_route_and_reports_what_it_replaced(db):
    await db["tenant_policies"].insert_one({"tenant_id": T, "enabled": False, "max_active_calls": 10, "max_duration_s": 1800})
    rep = await tc.arm(db, T, ME, DID)
    pol = await db["tenant_policies"].find_one({"tenant_id": T})
    assert pol["enabled"] is True and pol["allowed_callers"] == [ME] and pol["max_active_calls"] == 1 and pol["max_duration_s"] == 180
    route = await db["inbound_routes"].find_one({"number": DID})
    assert route["action"] == "ai" and route["enabled"] is True and route["tenant_id"] == T
    assert rep["policy_before"] == {"enabled": False, "max_active_calls": 10, "max_duration_s": 1800}   # restore information
    assert "9876543210" not in str(rep)                                                                 # the report never prints a full number


async def test_status_masks_numbers_and_flags_an_open_route(db):
    s = make_settings(mongo_db=db.s.mongo_db)
    await db["tenant_policies"].insert_one({"tenant_id": T, "enabled": True})
    await db["inbound_routes"].insert_one({"number": DID, "tenant_id": T, "action": "ai", "enabled": True})
    st = await tc.status(db, T, DID, s)
    assert st["open_to_anyone"] is True and "8065530418" not in str(st)          # an AI route with no allowlist is called out, numbers masked
    await tc.arm(db, T, ME, DID)
    st = await tc.status(db, T, DID, s)
    assert st["open_to_anyone"] is False and st["policy"]["restricted_to_allowlist"] and "9876543210" not in str(st)


async def test_disarm_blocks_new_calls_and_hangs_up_every_live_call_and_reports_failures(db):
    await tc.arm(db, T, ME, DID)
    for cid, state in (("c_live1", "answered"), ("c_live2", "ringing"), ("c_done", "completed"), ("c_other", "answered")):
        await db["calls"].insert_one({"_id": cid, "tenant_id": T if cid != "c_other" else "someone_else", "state": state, "provider_call_id": "P-" + cid})
    asked = []

    async def hangup(call):
        asked.append(call["_id"])
        if call["_id"] == "c_live2":
            raise RuntimeError("plivo down")
    out = await tc.disarm(db, T, hangup)
    assert (await db["tenant_policies"].find_one({"tenant_id": T}))["enabled"] is False        # new calls are rejected from now on
    assert sorted(asked) == ["c_live1", "c_live2"]                                              # only this tenant's live calls
    res = {h["call"]: h["result"] for h in out["hangups"]}
    assert res["c_live1"] == "requested" and res["c_live2"].startswith("FAILED:RuntimeError")   # a failed hangup is visible, not hidden
    assert (await db["tenant_policies"].find_one({"tenant_id": T}))["allowed_callers"] == [ME]  # restriction survives (a re-enable stays safe)


async def test_watch_stops_when_the_first_call_ends_and_when_no_call_comes(db):
    import asyncio
    s = make_settings(mongo_db=db.s.mongo_db)
    assert "no call within" in await tc.watch(db, T, s, window_s=0.5, cap_s=180)
    await db["calls"].insert_one({"_id": "c_w", "tenant_id": T, "state": "answered", "from_number": ME})

    async def end_soon():
        await asyncio.sleep(2.5)
        await db["calls"].update_one({"_id": "c_w"}, {"$set": {"state": "completed"}})
    asyncio.create_task(end_soon())
    assert "the call ended" in await tc.watch(db, T, s, window_s=30, cap_s=180)
