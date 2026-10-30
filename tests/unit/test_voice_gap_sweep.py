"""tools/voice_gap_sweep.py: the plan, the summary and the refusals. SIMULATED: no network, no RunPod; the tool itself is never run by a test."""
import argparse
import asyncio
import pathlib
import sys

from tools import voice_gap_sweep as t

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_the_plan_is_one_priming_job_then_two_repetitions_of_the_six_gaps():
    p = t.plan(2)
    assert len(p) == 13 and p[0] == ("prime", 0, 0)
    assert [g for k, r, g in p if r == 1] == [0, 2, 5, 10, 20, 40]
    assert [g for k, r, g in p if r == 2] == [40, 20, 10, 5, 2, 0]                 # the second repetition runs backwards against drift
    assert t.plan(1)[1:] == [("sweep", 1, g) for g in t.GAPS]


def _row(kind, gap, q, ok=True):
    return {"kind": kind, "planned_gap_s": gap, "queue_delay_ms": q, "ok": ok}


def test_summary_excludes_the_prime_job_and_finds_the_gap_where_the_median_first_exceeds_0_6_s():
    rows = [_row("prime", 0, 48000.0)]
    for g, qs in ((0, (150, 170)), (2, (200, 250)), (5, (400, 500)), (10, (900, 1100)), (20, (1200, 1250)), (40, (1190, 1230))):
        rows += [_row("sweep", g, q) for q in qs]
    s = t.summarize(rows)
    assert 48000.0 not in [v["max_ms"] for v in s["queue_vs_gap"].values()]
    assert s["queue_vs_gap"][5]["median_ms"] == 450.0 and s["queue_vs_gap"][10]["median_ms"] == 1000.0
    assert s["first_gap_s_where_median_queue_exceeds_0.6s"] == 10


def test_summary_when_the_queue_is_always_high_or_never_high_and_failed_jobs_are_ignored():
    high = [_row("sweep", g, 1200.0) for g in t.GAPS for _ in (0, 1)]
    assert t.summarize(high)["first_gap_s_where_median_queue_exceeds_0.6s"] == 0           # already at back-to-back: a fixed delay, not an idle effect
    low = [_row("sweep", g, 150.0) for g in t.GAPS]
    assert t.summarize(low)["first_gap_s_where_median_queue_exceeds_0.6s"] is None
    assert t.summarize([_row("sweep", 5, 9999.0, ok="timeout:None"), _row("sweep", 5, None)])["queue_vs_gap"] == {}


def test_the_table_prints_only_numbers():
    s = t.summarize([_row("sweep", 2, 200.0), _row("sweep", 10, 900.0)])
    out = t.format_table(s)
    assert "gap_s" in out and "exceeds 0.6 s: 10" in out and "देखिए" not in out


def _args(**kw):
    base = dict(approved="id", reps=2, result_file="")
    base.update(kw)
    return argparse.Namespace(**base)


def test_refusals():
    assert "no --approved" in t.refuse_reason(_args(approved=""), ROOT)
    assert "1 or 2" in t.refuse_reason(_args(reps=3), ROOT)
    assert "OUTSIDE the repository" in t.refuse_reason(_args(result_file=str(ROOT / "x.json")), ROOT)
    assert t.refuse_reason(_args(result_file="/tmp/x.json"), ROOT) is None


def test_main_refuses_without_an_approval_id(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["voice_gap_sweep.py"])
    assert asyncio.run(t.main()) == 2 and "no --approved" in capsys.readouterr().out


def test_the_tool_never_reads_worker_counts():
    src = (ROOT / "tools" / "voice_gap_sweep.py").read_text(encoding="utf-8")
    for bad in ('"idle"', '"ready"', '"running"', '"initializing"'):
        assert bad not in src, bad
