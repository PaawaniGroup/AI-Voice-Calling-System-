"""CI-style gate: evaluates an evidence file against docs/realtime/gates.json and, optionally, against a previous run (regression).

  python tools/realtime/gate.py EVIDENCE.json [--gates docs/realtime/gates.json] [--baseline PREVIOUS.json --max-regress-pct 5]

Exit 0 = no gate FAILED (gates that cannot be judged are reported NOT_EVALUATED, never PASS); exit 1 = at least one FAIL or a regression; exit 2 = bad input.
Rules that cannot be bent: a SIMULATED run can never pass a gate that requires MEASURED; a gate is only judged when the run's scenario and sample size match; thresholds
are read from the gates file and are not parameters of this script.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GATES = ROOT / "docs" / "realtime" / "gates.json"
OPS = {"<=": lambda a, b: a <= b, ">=": lambda a, b: a >= b}


def evaluate(evidence: dict, gates: dict) -> list[dict]:
    out = []
    for g in gates.get("gates", []):
        base = {"id": g["id"], "metric": g.get("metric"), "stat": g.get("stat"), "op": g.get("op"), "threshold": g.get("threshold")}
        if g.get("status") == "BLOCKED":
            out.append({**base, "status": "BLOCKED", "reason": g.get("blocked_by", "blocked")})
            continue
        if evidence.get("tag") != g.get("requires_tag", "MEASURED"):
            out.append({**base, "status": "NOT_EVALUATED", "reason": f"needs a {g.get('requires_tag', 'MEASURED')} run, this one is {evidence.get('tag')}"})
            continue
        sc = evidence.get("scenario", {})
        miss = {k: (v, sc.get(k)) for k, v in g.get("scenario", {}).items() if sc.get(k) != v}
        if miss:
            out.append({**base, "status": "NOT_EVALUATED", "reason": "scenario differs: " + ", ".join(f"{k} wanted {w!r} got {h!r}" for k, (w, h) in miss.items())})
            continue
        summ = evidence.get("summary", {})
        bad_c = []
        for ck, rule in g.get("constraints", {}).items():              # e.g. the reply length of the run must be about the stated one
            lo, hi = rule["median_between"]
            v = (summ.get(ck) or {}).get("median")
            if v is None or not (lo <= v <= hi):
                bad_c.append(f"{ck} median {v!r} not in [{lo}, {hi}]")
        if bad_c:
            out.append({**base, "status": "NOT_EVALUATED", "reason": "; ".join(bad_c)})
            continue
        m = summ.get(g["metric"])
        if not m or m.get(g["stat"]) is None:
            out.append({**base, "status": "NOT_EVALUATED", "reason": "metric not in the evidence"})
            continue
        if m["n"] < g.get("min_n", 1):
            out.append({**base, "status": "NOT_EVALUATED", "reason": f"n={m['n']} < required {g.get('min_n')}"})
            continue
        ok = OPS[g["op"]](m[g["stat"]], g["threshold"])
        out.append({**base, "status": "PASS" if ok else "FAIL", "value": m[g["stat"]], "n": m["n"]})
    return out


def regressions(evidence: dict, previous: dict, pct: float, metrics=("to_first_audio_ms",)) -> list[dict]:
    """Median of a metric worse than the previous run's by more than pct percent (same tag and scenario only)."""
    if evidence.get("tag") != previous.get("tag") or evidence.get("scenario") != previous.get("scenario"):
        return [{"metric": "*", "status": "NOT_COMPARABLE", "reason": "tag or scenario differs from the baseline run"}]
    out = []
    for k in metrics:
        a, b = evidence.get("summary", {}).get(k), previous.get("summary", {}).get(k)
        if not a or not b or a.get("median") is None or b.get("median") is None:
            out.append({"metric": k, "status": "NOT_COMPARABLE", "reason": "metric missing"})
        elif a["median"] > b["median"] * (1 + pct / 100.0):
            out.append({"metric": k, "status": "REGRESSION", "now": a["median"], "before": b["median"], "limit_pct": pct})
        else:
            out.append({"metric": k, "status": "OK", "now": a["median"], "before": b["median"]})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("evidence")
    ap.add_argument("--gates", default=str(DEFAULT_GATES))
    ap.add_argument("--baseline", default="")
    ap.add_argument("--max-regress-pct", type=float, default=5.0)
    a = ap.parse_args(argv)
    try:
        ev = json.loads(Path(a.evidence).read_text(encoding="utf-8"))
        gates = json.loads(Path(a.gates).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"bad input: {type(e).__name__}")
        return 2
    results = evaluate(ev, gates)
    bad = [r for r in results if r["status"] == "FAIL"]
    for r in results:
        print(json.dumps(r))
    if a.baseline:
        prev = json.loads(Path(a.baseline).read_text(encoding="utf-8"))
        reg = regressions(ev, prev, a.max_regress_pct)
        for r in reg:
            print(json.dumps(r))
        bad += [r for r in reg if r["status"] == "REGRESSION"]
    print(json.dumps({"tag": ev.get("tag"), "fail_or_regression": len(bad), "pass": sum(r["status"] == "PASS" for r in results),
                      "not_evaluated": sum(r["status"] == "NOT_EVALUATED" for r in results), "blocked": sum(r["status"] == "BLOCKED" for r in results)}))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
