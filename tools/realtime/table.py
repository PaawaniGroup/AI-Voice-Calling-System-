"""Comparison table: every run against the 7.51 s baseline and the stage budget. Every cell carries its tag (MEASURED / SIMULATED / ESTIMATE).

  python tools/realtime/table.py run1.json [run2.json ...]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.realtime.stages import BASELINE_MS, BUDGET_MS    # noqa: E402

# how the harness stages map onto the baseline columns of the 7.51 s decomposition
ROWS = (("end_of_turn (silence wait)", "end_of_turn", ("end_of_turn_ms",)),
        ("STT final", "stt", ("stt_ms",)),
        ("Brain first clause (baseline: first token only)", "brain", ("brain_first_clause_ms",)),
        ("voice first audio + playout start", "voice", ("voice_first_audio_ms", "playout_start_ms")))


def _stage_value(summary: dict, keys: tuple) -> float | None:
    vals = [summary.get(k, {}).get("median") for k in keys]
    return None if any(v is None for v in vals) else round(sum(vals), 1)


def table(runs: list[dict]) -> str:
    head = "| stage | baseline 7.51 s (MEASURED) | budget (ESTIMATE) | " + " | ".join(f"{r.get('label', 'run')} [{r['tag']}]" for r in runs) + " |"
    sep = "|---|---|---|" + "---|" * len(runs)
    lines = [head, sep]
    for label, bkey, keys in ROWS:
        cells = []
        for r in runs:
            v = _stage_value(r["summary"], keys)
            cells.append("n/a" if v is None else f"{v:.0f} ms ({v - BASELINE_MS[bkey]:+.0f})")
        lines.append(f"| {label} | {BASELINE_MS[bkey]:.0f} ms | {BUDGET_MS[bkey]:.0f} ms | " + " | ".join(cells) + " |")
    tot = []
    for r in runs:
        m = r["summary"].get("to_first_audio_ms", {})
        tot.append("n/a" if not m else f"median {m['median']:.0f} / p90 {m['p90']:.0f} ms (n={m['n']})")
    lines.append(f"| **last voiced frame -> first audio frame (path to the ear NOT included)** | {BASELINE_MS['total'] - BASELINE_MS['ear']:.0f} ms | "
                 f"{BUDGET_MS['total'] - BUDGET_MS['ear']:.0f} ms | " + " | ".join(tot) + " |")
    lines.append("")
    lines.append("Deltas are against the baseline column. The path to the ear (baseline about 200 ms, budget 150 ms) is not measured by the harness and is not in the run columns. "
                 "A SIMULATED column tests the orchestration only and proves nothing about the models; the goal is judged on MEASURED columns only.")
    return "\n".join(lines)


def main(argv=None) -> int:
    paths = (argv if argv is not None else sys.argv[1:])
    if not paths:
        print(__doc__)
        return 2
    runs = []
    for p in paths:
        r = json.loads(Path(p).read_text(encoding="utf-8"))
        r["label"] = Path(p).stem
        runs.append(r)
    print(table(runs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
