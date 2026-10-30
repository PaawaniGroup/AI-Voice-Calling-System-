"""CAPPED real test of the voice endpoint's queue time (hypothesis: the ~1.1 s per-turn voice queue is a FlashBoot revival after an idle timeout).

It creates up to 6 real voice jobs (billed) of ONE fixed neutral phrase. It refuses to run without --approved "<the operator's APPROVED: message id>".
Stops: when the projected cost reaches --cap-usd, at the first HTTP 401 or 403, after 3 consecutive errors. Output: numbers and RunPod job ids only (no text, no key);
the key is read from the environment variable named by --key-env (the caller puts the key file's content there for this one process) and is never printed.
Projected cost = (seconds since job 1 was submitted + the next gap + 5 s + the idle tail --idle-tail-s + the cold start) x --price-per-s.

  RUNPOD_TTS_KEY=... python tools/voice_queue_test.py --label idle_300s --jobs 6 --gap-s 20 --idle-tail-s 300 --cap-usd 0.15 \
        --result-file /path/outside/repo/result.json --approved "<id>"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import statistics as st
import sys
import time

TEXT = "देखिए, इस बारे में,"          # a fixed neutral 3-word phrase (app/ai/streaming/ramp.py PROPOSED_OPENERS)


def projected_cost(elapsed_s: float, next_gap_s: float, idle_tail_s: float, cold_s: float, price_per_s: float) -> float:
    return (elapsed_s + next_gap_s + 5.0 + idle_tail_s + cold_s) * price_per_s


def classify_queue(queue_ms) -> str | None:
    """The ONLY warm/cold signal: a job's own queue (delayTime). 20 s or more = cold start; under 5 s = already warm; in between = in_between. /health worker counts are
    NOT used anywhere (they look like worker slots that always add up to Max workers; UNVERIFIED)."""
    if not isinstance(queue_ms, (int, float)):
        return None
    return "cold_start" if queue_ms >= 20000 else "already_warm" if queue_ms < 5000 else "in_between"


async def completed_counter(cl) -> int | None:
    """jobs.completed from a free GET /health (a completed-jobs counter is a valid readiness signal; the worker counts next to it are not)."""
    try:
        ep = cl.endpoint("tts")
        r = await cl._http.get(f"{ep.url}/health", headers=cl._auth_headers("tts"), timeout=20)
        j = r.json() if r.status_code < 400 else {}
        v = (j.get("jobs") or {}).get("completed") if isinstance(j, dict) else None
        return int(v) if isinstance(v, (int, float)) else None
    except Exception:
        return None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--tenant", default="mme-test")
    ap.add_argument("--mongo", default="mongodb://127.0.0.1:27117")
    ap.add_argument("--key-env", default="RUNPOD_TTS_KEY")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--gap-s", type=float, default=20.0)
    ap.add_argument("--idle-tail-s", type=float, default=300.0)
    ap.add_argument("--timeout-s", type=float, default=150.0, help="client timeout of one voice job (a cold start must not be cut off)")
    ap.add_argument("--cap-usd", type=float, default=0.15)
    ap.add_argument("--price-per-s", type=float, default=0.0003, help="the HIGHER of the two prices in use (UNVERIFIED); Billing is authoritative")
    ap.add_argument("--result-file", default="")
    ap.add_argument("--approved", default="")
    a = ap.parse_args()
    if not a.approved.strip():
        print("refused: no --approved id (this test creates billed voice jobs)")
        return 2
    if not 1 <= a.jobs <= 6:
        print("refused: at most 6 jobs")
        return 2
    root = pathlib.Path(__file__).resolve().parent.parent
    if a.result_file:
        rp = pathlib.Path(a.result_file).resolve()
        if root.resolve() in rp.parents:
            print("refused: the result file must be OUTSIDE the repository")
            return 2
    sys.path.insert(0, ".")
    from pymongo import AsyncMongoClient
    from app.ai import turn_trace
    from app.ai.clients import AiError, ModelClients
    from config.settings import Settings
    doc = await AsyncMongoClient(a.mongo, serverSelectionTimeoutMS=3000)["mme_telephony"]["ai_config"].find_one({"tenant_id": a.tenant}) or {}
    cfg = {k: doc.get(k) for k in ("tts_url", "tts_ulaw8k", "tts_voice", "runpod_mode", "models", "timeouts", "paths")}
    cfg["key_refs"] = {"tts": a.key_env}
    cfg["timeouts"] = {**(cfg.get("timeouts") or {}), "tts": a.timeout_s}
    cl = ModelClients(Settings(_env_file=None), cfg)
    counter_before = await completed_counter(cl)                  # recorded with the time, before job 1
    utc_before = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(json.dumps({"label": a.label, "completed_counter_before_job_1": counter_before, "utc_before_job_1": utc_before}))
    ids: list = []
    orig_wait = cl._runpod_wait

    async def wait(service, ep, hdrs, j, *args, **kw):          # note the RunPod job id of every job (an id is not a secret)
        ids.append(j.get("id") if isinstance(j, dict) else None)
        return await orig_wait(service, ep, hdrs, j, *args, **kw)

    cl._runpod_wait = wait
    rows, consecutive, stop = [], 0, None
    t_first = None
    try:
        for i in range(a.jobs):
            if t_first is not None and projected_cost(time.monotonic() - t_first, a.gap_s, a.idle_tail_s, 0.0, a.price_per_s) >= a.cap_usd:
                stop = "stopped at the cost cap"
                break
            tr = turn_trace.TurnTrace(t_speech_end=time.monotonic())
            turn_trace.start(tr)
            n_ids = len(ids)
            t_first = t_first if t_first is not None else time.monotonic()
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
            j = tr.jobs[0] if tr.jobs else {}
            row = {"label": a.label, "job": i + 1, "runpod_job_id": ids[n_ids] if len(ids) > n_ids else None, "ok": ok,
                   "queue_delay_ms": j.get("delay_ms"), "exec_ms": j.get("exec_ms"), "wall_ms": j.get("total_ms"), "polls": j.get("polls"), "audio_s": j.get("audio_s")}
            rows.append(row)
            print(json.dumps(row))
            if stop:
                break
            if consecutive >= 3:
                stop = "stopped after 3 consecutive errors"
                break
            if i < a.jobs - 1:
                await asyncio.sleep(a.gap_s)
    finally:
        counter_after = await completed_counter(cl)
        await cl.aclose()
    good = [r for r in rows[1:] if r["ok"] is True and isinstance(r["queue_delay_ms"], (int, float))]      # job 1 (a cold start) is left out of the medians

    def rng(k):
        v = [r[k] for r in good if isinstance(r.get(k), (int, float))]
        return {"median": round(st.median(v), 1), "min": min(v), "max": max(v), "n": len(v)} if v else None

    summary = {"label": a.label, "tag": "MEASURED", "stopped": stop, "jobs_run": len(rows),
               "completed_counter": {"before_job_1": counter_before, "after_last_job": counter_after, "utc_before_job_1": utc_before},
               "job_1_class": classify_queue(rows[0]["queue_delay_ms"]) if rows else None, "jobs_2_to_n": {"queue_delay_ms": rng("queue_delay_ms"), "exec_ms": rng("exec_ms"), "wall_ms": rng("wall_ms")}}
    print(json.dumps(summary))
    if a.result_file:
        pathlib.Path(a.result_file).write_text(json.dumps({"jobs": rows, "summary": summary}, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
