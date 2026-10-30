"""Controlled test calls: with `allowed_callers` set on the tenant policy ONLY those numbers reach the AI; every other caller is
rejected before a call record, an AI session or any model request exists. Also: the policy's call-length cap lands on the call.
Plivo and the model servers are MOCKS (same harness as test_plivo_voice)."""
import xml.etree.ElementTree as ET

import pytest

from tests.conftest import auth
from tests.integration.test_plivo_voice import DID, ADMIN, form, post_answer, pv  # noqa: F401  (`pv` is a fixture)

MINE = "+919876543210"          # the default caller in test_plivo_voice.form()
PLATFORM = lambda: auth("t1", ("platform:admin",))  # noqa: E731


async def _calls(pv):
    return await pv.db["calls"].count_documents({})


def _is_reject(text: str) -> bool:
    return ET.fromstring(text).find("Hangup") is not None and ET.fromstring(text).find("Stream") is None


async def test_only_the_allowed_caller_reaches_the_ai(pv):
    await pv.repos.put_policy("t1", {"enabled": True, "max_active_calls": 5, "allowed_callers": [MINE]})
    ok = await post_answer(pv, form())                                   # From 919876543210 == the allowed number
    assert ok.status_code == 200 and ET.fromstring(ok.text).find("Stream") is not None
    assert await _calls(pv) == 1
    for other in ("919000000001", "+919876543211", "12025550123"):
        r = await post_answer(pv, form(**{"From": other}))
        assert r.status_code == 200 and _is_reject(r.text), other
    assert await _calls(pv) == 1                                         # rejected callers left no call record
    assert await pv.db["ai_sessions"].count_documents({}) == 0           # and no AI session (the stream was never opened)
    assert [k for k, _ in pv.models.calls] == []                         # and no model request: no RunPod spend for strangers


async def test_number_formats_of_the_allowed_caller_all_match(pv):
    await pv.repos.put_policy("t1", {"enabled": True, "max_active_calls": 5, "allowed_callers": [MINE]})
    for frm in ("919876543210", "+919876543210"):
        r = await post_answer(pv, form(**{"From": frm}))
        assert ET.fromstring(r.text).find("Stream") is not None, frm


async def test_an_unreadable_caller_is_rejected_when_an_allowlist_is_set(pv):
    await pv.repos.put_policy("t1", {"enabled": True, "max_active_calls": 5, "allowed_callers": [MINE]})
    for frm in ("", "anonymous", "abc"):
        r = await post_answer(pv, form(**{"From": frm}))
        assert _is_reject(r.text), repr(frm)
    assert await _calls(pv) == 0


async def test_without_an_allowlist_behaviour_is_unchanged(pv):
    """Opt-in: the existing policy (no allowed_callers) still answers any caller."""
    await pv.repos.put_policy("t1", {"enabled": True, "max_active_calls": 5})
    r = await post_answer(pv, form(**{"From": "919000000001"}))
    assert ET.fromstring(r.text).find("Stream") is not None


async def test_policy_api_normalizes_and_stores_allowed_callers_and_the_call_cap_lands_on_the_call(pv):
    r = await pv.c.put("/v1/admin/tenants/t1/policy", json={"enabled": True, "max_active_calls": 1, "max_duration_s": 180,
                                                            "allowed_callers": ["9876543210"]}, headers=PLATFORM())
    assert r.status_code == 200, r.text
    assert r.json()["allowed_callers"] == [MINE] and r.json()["max_duration_s"] == 180
    ok = await post_answer(pv, form())
    assert ET.fromstring(ok.text).find("Stream") is not None
    call = await pv.db["calls"].find_one({})
    assert call["max_duration_s"] == 180                                  # the 3-minute cap is on the call (the controller enforces it)
    bad = await pv.c.put("/v1/admin/tenants/t1/policy", json={"enabled": True, "allowed_callers": ["not a number"]}, headers=PLATFORM())
    assert bad.status_code in (400, 422)


async def test_second_call_is_busy_while_the_first_is_live_when_capacity_is_one(pv):
    await pv.repos.put_policy("t1", {"enabled": True, "max_active_calls": 1, "allowed_callers": [MINE]})
    first = await post_answer(pv, form())
    assert ET.fromstring(first.text).find("Stream") is not None
    second = await post_answer(pv, form())
    assert ET.fromstring(second.text).find("Stream") is None and await _calls(pv) == 1
