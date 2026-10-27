"""CAPPED real sweep: voice queue against the gap since the previous job's COMPLETION. NOT RUN.

After ONE excluded priming job it sends voice jobs of one fixed neutral phrase with gaps of 0, 2, 5, 10, 20 and 40 s, measured from the previous job's completion, in two
repetitions (repetition 1 ascending, repetition 2 descending, so a drift in time does not look like a gap effect): 1 + 12 = 13 jobs at most.
Per job it records the planned and the MEASURED gap, the RunPod queue (delayTime), execution, wall time and the `jobs.completed` counter read right after the job (in the
background, so it never lengthens the gap). Prints a table of queue against gap and the gap where the median queue first exceeds 0.6 s. Numbers only: no text, no key.
No health gate: /health worker counts are not used (they are worker slots; docs/LATENCY_HEALTH_READINGS.md).
Refuses without --approved "<the operator's APPROVED: message id>" and with a result file inside the repository. Stops at the cost cap, at the first HTTP 401 or 403 and after 3
consecutive errors. The key comes from the environment variable named by --key-env (the caller puts the key file's content there for this one process); it is never printed.

  RUNPOD_TTS_KEY=... python tools/voice_gap_sweep.py --result-file /path/outside/repo/sweep.json --cap-usd 0.20 --approved "<id>"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import statistics as st
import sys
import time

TEXT = "देखिए, इस बारे में,"          # the same fixed neutral phrase as tools/voice_queue_test.py
GAPS = (0, 2, 5, 10, 20, 40)          # seconds from the previous job's completion to the next submit
THRESHOLD_MS = 600.0


def plan(reps: int = 2) -> list[tuple[str, int, int]]:
    """[(kind, repetition, planned_gap_s)]: one priming job (gap 0, excluded), then each repetition over the gaps (odd repetitions ascending, even ones descending)."""
    out = [("prime", 0, 0)]
    for r in range(1, reps + 1):
        for g in (GAPS if r % 2 else tuple(reversed(GAPS))):
            out.append(("sweep", r, g))
    return out


def summarize(rows: list[dict]) -> dict:
    """Queue against planned gap from the sweep rows (the priming job excluded): per gap n, median, min, max; and the first gap whose median queue exceeds THRESHOLD_MS."""
    by: dict[int, list[float]] = {}
    for r in rows:
        if r.get("kind") == "sweep" and r.get("ok") is True and isinstance(r.get("queue_delay_ms"), (int, float)):
            by.setdefault(int(r["planned_gap_s"]), []).append(float(r["queue_delay_ms"]))
    table = {g: {"n": len(v), "median_ms": round(st.median(v), 1), "min_ms": min(v), "max_ms": max(v)} for g, v in sorted(by.items())}
    first = next((g for g, t in table.items() if t["median_ms"] > THRESHOLD_MS), None)
    return {"queue_vs_gap": table, "first_gap_s_where_median_queue_exceeds_0.6s": first}


def refuse_reason(args, repo_root: pathlib.Path) -> str | None:
    if not args.approved.strip():
        return "refused: no --approved id (this test creates billed voice jobs)"
    if not 1 <= args.reps <= 2:
        return "refused: 1 or 2 repetitions"
    if args.result_file:
        rp = pathlib.Path(args.result_file).resolve()
        if repo_root.resolve() in rp.parents:
            return "refused: the result file must be OUTSIDE the repository"
    return None


def format_table(summary: dict) -> str:
    lines = ["gap_s  n  median_queue_ms  min  max"]
    for g, t in summary["queue_vs_gap"].items():
        lines.append(f"{g:>5}  {t['n']}  {t['median_ms']:>15}  {t['min_ms']:.0f}  {t['max_ms']:.0f}")
    lines.append(f"first gap where the median queue exceeds 0.6 s: {summary['first_gap_s_where_median_queue_exceeds_0.6s']}")
    return "\n".join(lines)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default="mme-test")
    ap.add_argument("--mongo", default="mongodb://127.0.0.1:27117")
    ap.add_argument("--key-env", default="RUNPOD_TTS_KEY")
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--idle-tail-s", type=float, default=300.0, help="worst case for the cost projection: the worker stays up this long after the last job")
    ap.add_argument("--cap-usd", type=float, default=0.20)
    ap.add_argument("--price-per-s", type=float, default=0.0003, help="the HIGHER of the two prices in use (UNVERIFIED); Billing is authoritative")
    ap.add_argument("--timeout-s", type=float, default=150.0)
    ap.add_argument("--result-file", default="")
    ap.add_argument("--approved", default="")
    a = ap.parse_args()
    root = pathlib.Path(__file__).resolve().parent.parent
    why = refuse_reason(a, root)
    if why:
        print(why)
        return 2
    sys.path.insert(0, ".")
    from pymongo import AsyncMongoClient
    from app.ai import turn_trace
    from app.ai.clients import AiError, ModelClients
    from config.settings import Settings
    from tools.voice_queue_test import classify_queue, completed_counter, projected_cost
    doc = await AsyncMongoClient(a.mongo, serverSelectionTimeoutMS=3000)["mme_telephony"]["ai_config"].find_one({"tenant_id": a.tenant}) or {}
    cfg = {k: doc.get(k) for k in ("tts_url", "tts_ulaw8k", "tts_voice", "runpod_mode", "models", "paths")}
    cfg["key_refs"] = {"tts": a.key_env}
    cfg["timeouts"] = {"tts": a.timeout_s}
    cl = ModelClients(Settings(_env_file=None), cfg)
    ids: list = []
    orig_wait = cl._runpod_wait

    async def wait(service, ep, hdrs, j, *args, **kw):
        ids.append(j.get("id") if isinstance(j, dict) else None)
        return await orig_wait(service, ep, hdrs, j, *args, **kw)

    cl._runpod_wait = wait
    rows, consecutive, stop = [], 0, None
    t0 = time.monotonic()
    t_prev_end: float | None = None
    counter_task: asyncio.Task | None = None
    try:
        for n, (kind, rep, gap) in enumerate(plan(a.reps)):
            if n and projected_cost(time.monotonic() - t0, gap, a.idle_tail_s, 0.0, a.price_per_s) >= a.cap_usd:
                stop = "stopped at the cost cap"
                break
            if t_prev_end is not None and gap > 0:
                await asyncio.sleep(max(0.0, gap - (time.monotonic() - t_prev_end)))
            t_submit = time.monotonic()
            measured_gap = None if t_prev_end is None else round(t_submit - t_prev_end, 2)
            tr = turn_trace.TurnTrace(t_speech_end=t_submit)
            turn_trace.start(tr)
            n_ids = len(ids)
            ok: object = True
            try:
                await cl.synthesize(TEXT, "hi")
                consecutive = 0
            except AiError as e:
                ok, consecutive = f"{e.kind}:{getattr(e, 'status_code', None)}", consecutive + 1
                if getattr(e, "status_code", None) in (401, 403):
                    stop = f"stopped at HTTP {e.status_code}"
            except Exception as e:
                ok, consecutive = type(e).__name__, consecutive + 1
            t_prev_end = time.monotonic()
            counter_task = asyncio.ensure_future(completed_counter(cl))          # in the background: it must not lengthen the next gap
            j = tr.jobs[0] if tr.jobs else {}
            row = {"n": n, "kind": kind, "rep": rep, "planned_gap_s": gap, "measured_gap_s": measured_gap, "ok": ok,
                   "runpod_job_id": ids[n_ids] if len(ids) > n_ids else None, "queue_delay_ms": j.get("delay_ms"), "exec_ms": j.get("exec_ms"),
                   "wall_ms": j.get("total_ms"), "class": classify_queue(j.get("delay_ms")), "_counter_task": counter_task}
            rows.append(row)
            if stop:
                break
            if consecutive >= 3:
                stop = "stopped after 3 consecutive errors"
                break
    finally:
        for r in rows:
            t = r.pop("_counter_task", None)
            try:
                r["completed_counter_after"] = await t if t is not None else None
            except Exception:
                r["completed_counter_after"] = None
        await cl.aclose()
    for r in rows:
        print(json.dumps(r))
    summary = {"tag": "MEASURED", "stopped": stop, "jobs_run": len(rows), **summarize(rows)}
    print(format_table(summary))
    print(json.dumps(summary))
    if a.result_file:
        pathlib.Path(a.result_file).write_text(json.dumps({"jobs": rows, "summary": summary}, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
