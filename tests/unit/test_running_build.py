"""`status` tells the operator whether the RUNNING service is the code at HEAD: exact (the service's own build stamp), inferred (process
start time vs the HEAD commit, for builds that predate the stamp), or a refusal to claim anything."""
import importlib.util
from pathlib import Path

from app.core.buildinfo import build_info

_spec = importlib.util.spec_from_file_location("plivo_testcall", Path(__file__).resolve().parents[2] / "scripts" / "plivo_testcall.py")
tc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tc)

HEAD = {"commit": "1832e47", "dirty": False, "committed_at": "2026-10-24T15:26:56+05:30"}
PROC = {"pid": 4242, "started_at": "2026-10-24T11:00:00+00:00", "cwd_is_repo": True}     # 16:30 IST: after the commit


def test_nothing_listening():
    assert tc.judge_running(None, None, HEAD)[0] == "not_running"


def test_a_dirty_tree_is_never_called_a_match():
    assert tc.judge_running({"pid": 4242, "commit": "1832e47", "dirty": False}, PROC, {**HEAD, "dirty": True})[0] == "uncertain_dirty_tree"


def test_the_services_own_stamp_gives_an_exact_answer_or_a_mismatch():
    ok = {"pid": 4242, "commit": "1832e47", "dirty": False}
    assert tc.judge_running(ok, PROC, HEAD)[0] == "exact_match"
    assert tc.judge_running({**ok, "commit": "e7c2467"}, PROC, HEAD)[0] == "mismatch"
    assert tc.judge_running({**ok, "dirty": True}, PROC, HEAD)[0] == "mismatch"


def test_a_stamp_from_another_process_is_ignored_and_the_start_time_decides():
    stale = {"pid": 1, "commit": "1832e47", "dirty": False}       # written by a previous process
    assert tc.judge_running(stale, PROC, HEAD)[0] == "inferred_match"
    early = {**PROC, "started_at": "2026-10-24T08:00:00+00:00"}  # started before the commit
    assert tc.judge_running(None, early, HEAD)[0] == "mismatch_or_unknown"
    assert tc.judge_running(None, {**PROC, "started_at": None}, HEAD)[0] == "mismatch_or_unknown"


def test_build_info_reports_this_checkout():
    b = build_info()
    assert b["source"] in ("git", "env", "none") and (b["commit"] != "unknown" or b["source"] != "git")


async def test_the_service_writes_its_build_stamp_at_startup(pv):
    stamp = await pv.db["service_runtime"].find_one({"_id": "api"})
    assert stamp and stamp["commit"] == build_info()["commit"] and isinstance(stamp["pid"], int) and stamp["started_at"]
    info = await tc.service_info(pv.db, port=1)                    # nothing listens on port 1 here
    assert info["verdict"] == "not_running" and info["build_stamp"]["commit"] == stamp["commit"]


from tests.integration.test_plivo_voice import pv  # noqa: E402,F401  (fixture: a running app)
