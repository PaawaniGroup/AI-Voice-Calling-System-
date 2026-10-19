"""Synthetic load harness (no PSTN, no paid calls, no AI).

  mode=fs   : real local FreeSWITCH via ESL; legs are loopback channels in the isolated `mme_test` context.
  mode=mock : controller/API/DB only; call progress is injected as synthetic ESL events.

Usage (from repo root, inside the venv):
  python tests/load/run_load.py --mode fs --calls 300 --concurrency 30 --hold 5 --out docs/load/fs_30x5s.json

Results are measurements of THIS host and configuration only. They are not a production capacity guarantee.
"""
import argparse
import asyncio
import json
import os
import platform
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.core.ids import aware  # noqa: E402
from app.main import create_app  # noqa: E402
from app.persistence.db import Database  # noqa: E402
from app.providers.base import MockProvider  # noqa: E402
from tests.conftest import auth, make_settings  # noqa: E402

try:
    import psutil
except ImportError:  # optional
    psutil = None


def pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(len(xs) * p / 100))], 4)


def summarize(xs):
    return {"n": len(xs), "p50": pct(xs, 50), "p95": pct(xs, 95), "p99": pct(xs, 99), "max": round(max(xs), 4) if xs else None}


async def main(a):
    mode = a.mode
    kw = dict(mongo_db=f"mmetel_load_{uuid.uuid4().hex[:8]}", max_active_global=a.concurrency, max_active_per_tenant=a.concurrency,
              provider_max_concurrent=a.concurrency, max_queued=a.calls + 10, max_initiation_rate=a.cps, provider_max_cps=a.cps,
              rate_limit_per_min=10**6, rate_limit_call_create_per_min=10**6, queue_max_wait_s=3600, ring_timeout_s=30,
              max_call_duration_s=max(600, a.hold * 2), initiating_timeout_s=120, max_body_bytes=65536, reconcile_grace_s=20.0, reconcile_interval_s=5.0)
    if mode == "fs":
        from esl_check import esl_password
        kw.update(fs_enabled=True, esl_password=esl_password(), provider="freeswitch_gateway",
                  fs_dial_template="loopback/9001/mme_test", esl_event_concurrency=8, esl_command_timeout_s=10)
        provider = None
    else:
        provider = MockProvider()
    s = make_settings(**kw)
    db = Database(s)
    await db.connect()
    app = create_app(s, db=db, provider=provider)
    proc = psutil.Process(os.getpid()) if psutil else None
    fs_proc = next((p for p in psutil.process_iter(["name"]) if p.info["name"] == "freeswitch"), None) if psutil else None
    samples, active_peak = [], 0
    api_lat, setup, qwait, answered_to_end = [], [], [], []
    errors = {}
    t_start = time.time()
    async with app.router.lifespan_context(app):
        repos, ctrl = app.state.repos, app.state.controller
        await repos.put_policy("t1", {"enabled": True, "caller_ids": ["+911140000000"], "max_active_calls": a.concurrency})
        if mode == "fs":
            for _ in range(100):
                if ctrl.esl_events.connected:
                    break
                await asyncio.sleep(0.1)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://load", timeout=60) as c:
            H = auth()
            stop = asyncio.Event()

            async def sampler():
                nonlocal active_peak
                while not stop.is_set():
                    act = await repos.count_active()
                    active_peak = max(active_peak, act)
                    row = {"t": round(time.time() - t_start, 1), "active": act, "queued": await repos.count_queued()}
                    if proc:
                        row.update(api_cpu=proc.cpu_percent(), api_rss_mb=round(proc.memory_info().rss / 1e6, 1))
                    if fs_proc:
                        try:
                            row.update(fs_cpu=fs_proc.cpu_percent(), fs_rss_mb=round(fs_proc.memory_info().rss / 1e6, 1))
                        except Exception:
                            pass
                    if mode == "fs":
                        try:
                            row["fs_channels"] = int((await ctrl.esl_cmd.api("show channels count")).split()[0])
                        except Exception:
                            pass
                    samples.append(row)
                    await asyncio.sleep(1.0)

            async def one(i):
                key = uuid.uuid4().hex
                t0 = time.perf_counter()
                try:
                    r = await c.post("/v1/calls", json={"to": "+919876543210", "from_number": "+911140000000"},
                                     headers={**H, "Idempotency-Key": key})
                except Exception as e:
                    errors[type(e).__name__] = errors.get(type(e).__name__, 0) + 1
                    return
                api_lat.append(time.perf_counter() - t0)
                if r.status_code != 202:
                    k = f"http_{r.status_code}"
                    errors[k] = errors.get(k, 0) + 1
                    return
                cid = r.json()["id"]
                t_accept, t_init = time.perf_counter(), None
                # wait for answer (mock mode: inject synthetic events as FreeSWITCH would send them)
                deadline = time.time() + 180
                call = None
                while time.time() < deadline:
                    call = await repos.get_call(cid)
                    if t_init is None and call["state"] != "queued":
                        t_init = time.perf_counter()
                    if mode == "mock" and call["state"] == "initiating" and call.get("fs_uuid"):
                        u = call["fs_uuid"]
                        await asyncio.sleep(0.05)
                        await ctrl.on_esl_event({"Event-Name": "CHANNEL_PROGRESS", "Unique-ID": u, "Event-Sequence": f"{i}a", "variable_mme_leg": "a"})
                        await ctrl.on_esl_event({"Event-Name": "CHANNEL_ANSWER", "Unique-ID": u, "Event-Sequence": f"{i}b", "variable_mme_leg": "a"})
                        continue
                    if call["state"] in ("answered", "bridged", "failed", "busy", "no_answer", "cancelled"):
                        break
                    await asyncio.sleep(0.05)
                if call["state"] != "answered" and call["state"] != "bridged":
                    errors[f"state_{call['state']}"] = errors.get(f"state_{call['state']}", 0) + 1
                    return
                t_ans = time.perf_counter()
                setup.append(t_ans - (t_init or t_accept))  # observed initiating->answered (50ms poll resolution)
                qwait.append((t_init or t_accept) - t_accept)
                await asyncio.sleep(a.hold)
                t1 = time.perf_counter()
                r = await c.post(f"/v1/calls/{cid}/hangup", headers=H)
                api_lat.append(time.perf_counter() - t1)
                if mode == "mock":
                    await ctrl.on_esl_event({"Event-Name": "CHANNEL_HANGUP", "Unique-ID": call["fs_uuid"], "Event-Sequence": f"{i}z",
                                             "Hangup-Cause": "NORMAL_CLEARING", "variable_mme_leg": "a"})
                for _ in range(200):
                    call = await repos.get_call(cid)
                    if call["state"] == "completed":
                        break
                    await asyncio.sleep(0.05)
                if call["state"] != "completed":
                    errors[f"final_{call['state']}"] = errors.get(f"final_{call['state']}", 0) + 1
                else:
                    answered_to_end.append(time.perf_counter() - t_ans)  # harness-observed answer->completed (monotonic)

            smp = asyncio.create_task(sampler())
            sem = asyncio.Semaphore(a.client_concurrency)

            async def guarded(i):
                async with sem:
                    await one(i)

            t_begin = time.time()
            await asyncio.gather(*(guarded(i) for i in range(a.calls)))
            elapsed = time.time() - t_begin
            stop.set()
            await smp
            # final consistency
            states = {}
            async for d in db["calls"].find({}, {"state": 1}):
                states[d["state"]] = states.get(d["state"], 0) + 1
            if os.environ.get("LOAD_DEBUG"):
                async for d in db["calls"].find({}).sort("initiated_at", 1):
                    if (aware(d["answered_at"]) - aware(d["initiated_at"])).total_seconds() > 5:
                        print("OUTLIER", {k: str(d.get(k)) for k in ("queued_at", "initiated_at", "ringing_at", "answered_at", "ended_at", "created_at")})
            reasons = {}
            async for d in db["calls"].find({"state": {"$ne": "completed"}}):
                k = f"{d['state']}/{d.get('failure_reason')}/{d.get('hangup_cause')}"
                reasons[k] = reasons.get(k, 0) + 1
            dup_origs = len(provider.originated) if provider else None
            chans = None
            if mode == "fs":
                await asyncio.sleep(2)
                chans = (await ctrl.esl_cmd.api("show channels count")).strip()
                try:
                    await ctrl.esl_cmd.api("hupall NORMAL_CLEARING mme_leg a")
                except Exception:
                    pass
            ev = ctrl.esl_events
            result = {
                "mode": mode, "calls_requested": a.calls, "target_concurrency": a.concurrency, "hold_seconds": a.hold,
                "client_concurrency": a.client_concurrency, "max_initiation_rate_cps": a.cps,
                "elapsed_s": round(elapsed, 1), "throughput_calls_per_s": round(a.calls / elapsed, 2),
                "peak_active_calls_observed": active_peak, "final_states": states, "errors": errors, "non_completed_reasons": reasons,
                "api_latency_s": summarize(api_lat), "setup_latency_initiating_to_answered_s": summarize(setup),
                "queue_wait_s": summarize(qwait), "connected_duration_observed_s": summarize(answered_to_end),
                "timing_note": "latencies use the harness monotonic clock; WSL2 wall-clock steps (observed ~50s) make DB timestamps unreliable here",
                "fs_channels_after_run": chans,
                "esl": {"events_received": ev.events_received if ev else None, "dropped": ev.events_dropped if ev else None,
                        "handler_errors": ev.handler_errors if ev else None, "last_lag_s": ev.last_lag_s if ev else None},
                "cpu_mem_peak": {k: max((r.get(k, 0) for r in samples), default=None) for k in ("api_cpu", "api_rss_mb", "fs_cpu", "fs_rss_mb")},
                "host": {"platform": platform.platform(), "cpus": os.cpu_count(),
                         "mem_gb": round(psutil.virtual_memory().total / 1e9, 1) if psutil else None, "python": platform.python_version()},
                "db": "local mongod 7.0.14 (standalone, same host)", "mock_originations": dup_origs,
                "limitations": "Single host; MongoDB, API and FreeSWITCH share CPU; loopback legs have no real codec/RTP/carrier path.",
            }
    await db.client.drop_database(s.mongo_db)
    await db.close()
    out = json.dumps(result, indent=2, default=str)
    print(out)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(out)
        Path(a.out).with_suffix(".samples.json").write_text(json.dumps(samples))
    ok = not errors and states.get("completed", 0) == a.calls
    return 0 if ok else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["fs", "mock"], default="mock")
    p.add_argument("--calls", type=int, default=100)
    p.add_argument("--concurrency", type=int, default=30)
    p.add_argument("--hold", type=float, default=2.0)
    p.add_argument("--cps", type=float, default=10.0)
    p.add_argument("--client-concurrency", type=int, default=60)
    p.add_argument("--out", default="")
    sys.exit(asyncio.run(main(p.parse_args())))
