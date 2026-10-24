"""Proving the caller allowlist on a service without the operator's number: a signed Answer from a number that is NOT allowed must get a
<Hangup>, create no call and no AI session. `prove` only runs when the running build is proven to contain the allowlist code, arms with a
dummy number and ALWAYS disarms. Plivo and the models are MOCKS; the service is the real app on a local port."""
import importlib.util
from pathlib import Path

from tests.integration.test_plivo_voice import BASE, DID, TOKEN, pv  # noqa: F401  (`pv` is a fixture)

_spec = importlib.util.spec_from_file_location("plivo_testcall", Path(__file__).resolve().parents[2] / "scripts" / "plivo_testcall.py")
tc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tc)

ME = "+919876543210"
OTHER = "+919000000009"


def _kw(pv, **over):
    return {"service_url": f"http://{pv.host}", "public_base": BASE, "token": TOKEN, "tenant": "t1", "did": DID, "from_number": OTHER, **over}


async def test_probe_from_a_stranger_is_rejected_without_a_trace(pv):
    await tc.arm(pv.db, "t1", ME, DID)
    res = await tc.probe(pv.db, **_kw(pv))
    assert res == {"ok": True, "http": 200, "hangup_in_response": True, "stream_in_response": False, "new_calls": 0, "new_ai_sessions": 0}
    assert await pv.db["calls"].count_documents({}) == 0 and pv.models.calls == []


async def test_probe_refuses_to_run_when_it_would_be_answered(pv):
    # no allowlist: the AI would answer (and spend), so nothing is sent
    await pv.repos.put_policy("t1", {"enabled": True, "max_active_calls": 5})
    res = await tc.probe(pv.db, **_kw(pv))
    assert res["ok"] is False and "ANSWERED" in res["refused"] and await pv.db["calls"].count_documents({}) == 0
    # the probe's From is the allowed number: it would be answered
    await tc.arm(pv.db, "t1", ME, DID)
    res = await tc.probe(pv.db, **_kw(pv, from_number=ME))
    assert res["ok"] is False and "in the allowlist" in res["refused"] and await pv.db["calls"].count_documents({}) == 0
    # no route for the DID: a rejection would prove nothing
    res = await tc.probe(pv.db, **_kw(pv, did="+911140009999"))
    assert res["ok"] is False and "no enabled route" in res["refused"]


async def test_a_service_without_the_allowlist_fails_the_probe(pv, monkeypatch):
    """What an OLD build would do: answer the stranger. The probe must report that as a failure (and that is why `prove` gates first)."""
    await tc.arm(pv.db, "t1", ME, DID)
    from app.services import plivo_calls
    orig = plivo_calls.handle_answer

    async def old_build(app, params, **kw):          # the same handler with the allowlist switched off
        pol = await app.state.repos.get_policy("t1")
        await app.state.repos.put_policy("t1", {**{k: v for k, v in pol.items() if k not in ("_id", "tenant_id", "updated_at")}, "allowed_callers": []})
        try:
            return await orig(app, params, **kw)
        finally:
            await app.state.repos.put_policy("t1", {"allowed_callers": [ME]})
    monkeypatch.setattr("app.api.routes.plivo_calls.handle_answer", old_build)
    res = await tc.probe(pv.db, **_kw(pv))
    assert res["ok"] is False and res["stream_in_response"] is True and res["new_calls"] == 1


def test_gate_logic_for_the_running_build():
    me = {"pid": 7, "started_at": "2999-01-01T00:00:00+00:00"}
    assert tc.includes_commit("HEAD", tc.ALLOWLIST_COMMIT) is True
    assert tc.includes_commit("0000000", tc.ALLOWLIST_COMMIT) is False and tc.includes_commit("unknown", "HEAD") is False and tc.includes_commit(None, "HEAD") is False
    assert tc.running_has_allowlist({"listener": me, "build_stamp": {"pid": 7, "commit": "HEAD"}})[0] is True              # its own stamp
    assert tc.running_has_allowlist({"listener": me, "build_stamp": {"pid": 7, "commit": "0000000"}})[0] is False           # stamp says an older build
    assert tc.running_has_allowlist({"listener": me, "build_stamp": {"pid": 1, "commit": "0000000"}})[0] is True            # stale stamp ignored: started after the commit
    assert tc.running_has_allowlist({"listener": {"pid": 7, "started_at": "2000-01-01T00:00:00+00:00"}, "build_stamp": None})[0] is False
    assert tc.running_has_allowlist({"listener": None, "build_stamp": None})[0] is False


async def test_prove_refuses_when_the_running_build_is_not_proven_and_arms_nothing(pv):
    before = await pv.db["tenant_policies"].find_one({"tenant_id": "t1"})
    res = await tc.prove(pv.db, pv.s, service_url=f"http://{pv.host}", port=1, did=DID, dummy_caller="+911140000000", probe_from=OTHER, tenant="t1")
    assert res["ok"] is False and "not proven" in res["refused"] and "probe" not in res
    assert await pv.db["tenant_policies"].find_one({"tenant_id": "t1"}) == before          # nothing was armed


async def test_prove_arms_with_a_dummy_number_probes_and_always_disarms(pv, monkeypatch):
    async def proven(db, port=8800):
        return {"verdict": "exact_match", "listener": {"pid": 1}, "build_stamp": {"pid": 1, "commit": "HEAD"}}
    monkeypatch.setattr(tc, "service_info", proven)
    res = await tc.prove(pv.db, pv.s, service_url=f"http://{pv.host}", port=8800, did=DID, dummy_caller="+911140000000", probe_from=OTHER, tenant="t1")
    assert res["ok"] is True and res["probe"]["hangup_in_response"] and res["probe"]["new_calls"] == 0
    pol = await pv.db["tenant_policies"].find_one({"tenant_id": "t1"})
    assert pol["enabled"] is False and pol["allowed_callers"] == ["+911140000000"]          # disarmed; never the operator's number
    assert "9000000009" not in str(res) and "1140000000" not in str(res)                    # no number in the report (after scrubbing)
