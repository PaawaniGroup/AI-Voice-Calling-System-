"""Gate, table and stage helpers of the real-time latency program. SIMULATED: no network, no model."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.realtime import gate as G
from tools.realtime import harness as H
from tools.realtime import stages as S
from tools.realtime import table as T

GATES = json.loads((G.DEFAULT_GATES).read_text(encoding="utf-8"))


def _ev(tag="MEASURED", med=2000.0, p90=2800.0, n=30, plan="ramp", workers=2, reply=23, stt=140.0):
    return {"tag": tag, "scenario": {"clause_plan": plan, "voice_workers": workers},
            "summary": {"to_first_audio_ms": {"median": med, "p90": p90, "n": n}, "reply_words": {"median": reply, "p90": reply, "n": n},
                        "stt_ms": {"median": stt, "p90": stt, "n": n}}}


def _status(results, gid):
    return next(r for r in results if r["id"] == gid)


def test_the_operators_thresholds_are_pinned_so_that_changing_one_is_a_deliberate_edit_of_this_test():
    by = {g["id"]: g for g in GATES["gates"]}
    assert (by["A-median"]["threshold"], by["A-median"]["op"]) == (2200, "<=") and (by["A-p90"]["threshold"], by["A-p90"]["op"]) == (3000, "<=")
    assert by["FS-first-sound"]["threshold"] == 1200 and by["FS-first-sound"]["status"] == "BLOCKED"
    assert by["A-median"]["scenario"] == {"clause_plan": "ramp", "voice_workers": 2} and by["A-median"]["constraints"]["reply_words"]["median_between"] == [20, 26]
    assert all(g.get("requires_tag", "MEASURED") == "MEASURED" for g in GATES["gates"])


def test_a_simulated_run_can_never_pass_a_measured_gate():
    r = G.evaluate(_ev(tag="SIMULATED", med=1.0), GATES)
    assert not any(x["status"] == "PASS" for x in r)
    assert _status(r, "A-median")["status"] == "NOT_EVALUATED" and "SIMULATED" in _status(r, "A-median")["reason"]


def test_a_measured_run_in_the_stated_scenario_passes_and_fails_on_the_numbers():
    ok = G.evaluate(_ev(med=2100, p90=2900), GATES)
    assert _status(ok, "A-median")["status"] == "PASS" and _status(ok, "A-p90")["status"] == "PASS"
    bad = G.evaluate(_ev(med=2300, p90=3100), GATES)
    assert _status(bad, "A-median")["status"] == "FAIL" and _status(bad, "A-p90")["status"] == "FAIL"
    assert _status(G.evaluate(_ev(med=2200), GATES), "A-median")["status"] == "PASS"                 # the bound itself passes


def test_the_goal_is_tied_to_the_reply_length_the_clause_plan_the_worker_count_and_the_sample_size():
    assert _status(G.evaluate(_ev(reply=12), GATES), "A-median")["status"] == "NOT_EVALUATED"       # a 12-word reply is another scenario
    assert _status(G.evaluate(_ev(reply=40), GATES), "A-median")["status"] == "NOT_EVALUATED"
    assert "scenario differs" in _status(G.evaluate(_ev(plan="today"), GATES), "A-median")["reason"]
    assert "scenario differs" in _status(G.evaluate(_ev(workers=1), GATES), "A-median")["reason"]
    assert "n=5" in _status(G.evaluate(_ev(n=5), GATES), "A-median")["reason"]
    assert _status(G.evaluate(_ev(n=12), GATES), "A-p90")["status"] == "NOT_EVALUATED"               # p90 needs more samples than the median


def test_the_first_clause_figure_is_judged_separately_from_the_goal():
    r = G.evaluate(_ev(med=3000, stt=140), GATES)
    assert _status(r, "FC-stt")["status"] == "PASS" and _status(r, "A-median")["status"] == "FAIL"


def test_first_sound_is_blocked_and_says_why():
    r = _status(G.evaluate(_ev(), GATES), "FS-first-sound")
    assert r["status"] == "BLOCKED" and "acknowledgement audio" in r["reason"] and "does not exist" in r["reason"]


def test_regression_gate_compares_only_like_with_like(tmp_path):
    a, b = _ev(med=2000), _ev(med=2100)                                               # +5.0% is the limit itself: still OK
    assert G.regressions(b, a, 5.0)[0]["status"] == "OK"
    assert G.regressions(_ev(med=2150), a, 5.0)[0]["status"] == "REGRESSION"          # +7.5%
    assert G.regressions(_ev(med=2300), a, 5.0)[0]["status"] == "REGRESSION"
    assert G.regressions(_ev(tag="SIMULATED"), a, 5.0)[0]["status"] == "NOT_COMPARABLE"
    assert G.regressions(_ev(workers=1), a, 5.0)[0]["status"] == "NOT_COMPARABLE"


def test_gate_exit_codes(tmp_path):
    good, worse, prev = tmp_path / "good.json", tmp_path / "worse.json", tmp_path / "prev.json"
    good.write_text(json.dumps(_ev(med=2000)))
    worse.write_text(json.dumps(_ev(med=2500)))
    prev.write_text(json.dumps(_ev(med=2000)))
    assert G.main([str(good)]) == 0
    assert G.main([str(worse)]) == 1                                                                  # over the 2.2 s goal
    assert G.main([str(good), "--baseline", str(prev)]) == 0
    assert G.main([str(tmp_path / "missing.json")]) == 2


def test_the_table_has_the_baseline_the_budget_and_every_runs_tag():
    runs = [dict(H.run_simulated(H.scenario("today"), 2), label="today"), dict(H.run_simulated(H.scenario("ramp", voice_workers=2), 2), label="ramp")]
    t = T.table(runs)
    assert "7.51 s (MEASURED)" in t and "budget (ESTIMATE)" in t and "today [SIMULATED]" in t and "ramp [SIMULATED]" in t
    assert "path to the ear" in t and "700 ms" in t and "4380 ms" in t


def test_baseline_and_budget_sums():
    assert sum(S.BASELINE_MS[k] for k in ("end_of_turn", "stt", "brain", "voice", "ear")) == S.BASELINE_MS["total"] == 7510.0
    assert sum(S.BUDGET_MS[k] for k in ("end_of_turn", "stt", "brain", "voice", "ear")) == S.BUDGET_MS["total"] == 1300.0


def test_percentile_and_summary_helpers():
    assert S.pctl([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 90) == 9 and S.pctl([5], 90) == 5 and S.pctl([], 50) is None and S.pctl([None, 3], 50) == 3
    s = S.summarize([{"to_first_audio_ms": 1.0}, {"to_first_audio_ms": 3.0}, {"to_first_audio_ms": None}])
    assert s["to_first_audio_ms"] == {"median": 2.0, "p90": 3.0, "n": 2}
