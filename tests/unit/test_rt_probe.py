"""tools/realtime/rt_rtt_probe.py (gate G1): the probe's plumbing and refusals. SIMULATED: an in-memory server; it says nothing about any network."""
from __future__ import annotations

import json

from tools.realtime import gate as G
from tools.realtime import rt_rtt_probe as R


def test_the_selftest_measures_both_series_and_is_tagged_simulated(capsys):
    assert R.main(["--selftest", "--n", "20"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["tag"] == "SIMULATED" and out["summary"]["rt_roundtrip_ms"]["n"] == 20 and out["summary"]["warmup_request_ms"]["n"] == 20


def test_a_real_probe_refuses_without_approval_without_a_declared_origin_and_without_a_secret(capsys, monkeypatch):
    assert R.main(["--url", "wss://x.example.invalid/ws"]) == 2 and "no --approved" in capsys.readouterr().out
    assert R.main(["--url", "https://x", "--approved", "id", "--declared-from", "pc"]) == 2 and "--url wss://" in capsys.readouterr().out
    monkeypatch.delenv("RT_SECRET", raising=False)
    assert R.main(["--url", "wss://x.example.invalid/ws", "--approved", "id", "--declared-from", "pc"]) == 2 and "secret" in capsys.readouterr().out


def test_g1_is_judged_only_for_a_measured_run_from_the_cloud_host_with_enough_samples():
    gates = json.loads(G.DEFAULT_GATES.read_text(encoding="utf-8"))

    def ev(tag, frm, med, n=200):
        return R.evidence(tag, {"rt_roundtrip_ms": {"median": med, "p90": med * 1.5, "n": n}}, frm)
    st = lambda e: next(r for r in G.evaluate(e, gates) if r["id"] == "G1-roundtrip")["status"]      # noqa: E731
    assert st(ev("MEASURED", "cloud", 35.0)) == "PASS" and st(ev("MEASURED", "cloud", 41.0)) == "FAIL"
    assert st(ev("MEASURED", "pc", 20.0)) == "NOT_EVALUATED"                          # from the PC the probe is informational
    assert st(ev("SIMULATED", "cloud", 1.0)) == "NOT_EVALUATED" and st(ev("MEASURED", "cloud", 30.0, n=50)) == "NOT_EVALUATED"


def test_stats_helper():
    assert R.stats([3.0, 1.0, 2.0])["median"] == 2.0 and R.stats([])["n"] == 0 and R.stats([1.0, 2.0, 3.0, 4.0])["median"] == 2.5
