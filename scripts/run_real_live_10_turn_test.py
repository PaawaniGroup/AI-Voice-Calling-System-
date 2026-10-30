#!/usr/bin/env python3
"""REAL LIVE 10-Turn End-to-End Latency Benchmark against REAL RunPod Serverless endpoints.

CHARGEABLE: Requires explicit --yes and --budget cap.
Reads real endpoints & API keys for tenant 'mme-test' from MongoDB ai_config.
Uses slices of 'Utkarsh_voice.wav' as real caller speech input.

Usage:
    python scripts/run_real_live_10_turn_test.py --yes --budget 2.00 [--turns 10] [--wav Utkarsh_voice.wav]
"""
import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import e2e_latency_bench as eb
import live_latency as ll
from app.ai import turn_trace
from app.ai.pipeline import Conversation
from app.persistence.db import Database
from config.settings import Settings

log = logging.getLogger("mme_telephony.real_live_bench")


async def fetch_tenant_config(tenant: str = "mme-test") -> dict:
    s = Settings()
    db = Database(s)
    await db.connect()
    try:
        doc = await db.db["ai_config"].find_one({"tenant_id": tenant})
        if not doc:
            raise RuntimeError(f"No ai_config found for tenant '{tenant}' in MongoDB")
        doc.pop("_id", None)
        return doc
    finally:
        await db.close()


async def check_endpoints_health(cfg: dict) -> dict:
    import httpx
    keys = cfg.get("keys") or {}
    results = {}
    async with httpx.AsyncClient(timeout=15.0) as client:
        for svc in ("stt", "brain", "tts"):
            url = cfg.get(f"{svc}_url", "")
            base = url.replace("/runsync", "").replace("/run", "")
            key = keys.get(svc) or cfg.get(f"{svc}_api_key")
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            try:
                r = await client.get(f"{base}/health", headers=headers)
                results[svc] = {"status_code": r.status_code, "data": r.json() if r.status_code < 400 else r.text[:120]}
            except Exception as e:
                results[svc] = {"status_code": 0, "error": f"{type(e).__name__}: {e}"}
    return results


async def run_live_10_turn_benchmark(args):
    s = Settings()
    print("=" * 80)
    print("REAL LIVE 10-TURN LATENCY BENCHMARK (REAL RUNPOD ENDPOINTS)")
    print("=" * 80)
    print(f"Tenant:         {args.tenant}")
    print(f"Turns:          {args.turns}")
    print(f"Audio Source:   {args.wav}")
    print(f"Budget Cap:     ${args.budget:.2f} USD")
    print("-" * 80)

    # 1. Load config
    cfg = await fetch_tenant_config(args.tenant)
    cfg["timeouts"] = {**(cfg.get("timeouts") or {}), **{svc: 120.0 for svc in eb.SERVICES}}

    # 2. Probe health of endpoints
    print("Checking RunPod endpoint health status...")
    health = await check_endpoints_health(cfg)
    for svc, h in health.items():
        endpoint_id = cfg.get(f"{svc}_url", "").split("/")[-2]
        workers = (h.get("data") or {}).get("workers", {}) if isinstance(h.get("data"), dict) else {}
        print(f"  {svc.upper():<5} ({endpoint_id}): HTTP {h.get('status_code')} | Workers: {workers}")

    # 3. Setup audio clips
    clips = ll.load_clips(args.wav, args.clip_s, args.turns)
    print(f"Loaded {len(clips)} audio slices from {args.wav} ({args.clip_s}s each).")

    # 4. Initialize Safety Spend Ledger and BenchClients
    if isinstance(args.idle, str):
        args.idle = eb.kv(args.idle)
    if isinstance(args.price, str):
        args.price = eb.kv(args.price)

    prices = {**eb.DEFAULT_PRICES, **(args.price or {})}
    # STT default price if not specified: ~$0.000192/s (24 GB GPU)
    if "stt" not in prices:
        prices["stt"] = 0.000192

    ledger = eb.Ledger(prices, args.idle, args.budget, args.cold_reserve_s)
    clients = eb.BenchClients(s, cfg, ledger, start_zero=bool(args.idle_s))

    conv = Conversation(
        clients,
        cfg.get("default_language", "auto"),
        script_enforcement=s.ai_script_enforcement,
        counsellor_gender=s.ai_counsellor_gender,
        counsellor_name=s.ai_counsellor_name,
    )

    print("-" * 80)
    print("Beginning Live Turns...")
    print(f"{'Turn':<5} | {'V2V(ms)':<9} | {'STT(ms)':<9} | {'TTFT(ms)':<9} | {'TTS1(ms)':<9} | {'Total(ms)':<10} | {'Est. Spend':<10}")
    print("-" * 80)

    rows = []
    errors = {}

    try:
        for i, pcm in enumerate(clips):
            clients.turn_idx = i
            tr = turn_trace.TurnTrace(t_speech_end=time.monotonic())
            turn_trace.start(tr)
            err = None

            t_start = time.perf_counter()
            async for ch in conv.stream_turn(pcm, ll.RATE):
                if ch.error:
                    err = ch.error
                    break
                if ch.audio:
                    tr.mark("t_first_frame_to_plivo")

            tr.mark("t_turn_end")
            ms = tr.to_ms()
            ms["error"] = err
            ms["wall_time_ms"] = round((time.perf_counter() - t_start) * 1000, 1)

            if err:
                errors[err] = errors.get(err, 0) + 1

            derived = ll.derive(ms)
            derived["error"] = err
            derived["turn"] = i + 1
            rows.append(derived)

            v2v = derived.get("post_endpoint_v2v", "-")
            stt = derived.get("stt", "-")
            ttft = derived.get("llm_first_token", "-")
            tts1 = derived.get("tts_first_chunk", "-")
            total = derived.get("turn_total", "-")

            print(f"#{i+1:<4} | {str(v2v):<9} | {str(stt):<9} | {str(ttft):<9} | {str(tts1):<9} | {str(total):<10} | ${ledger.spent():.4f}")

            if clients.halted:
                print(f"[HALTED]: {clients.halted}")
                break

    finally:
        await clients.aclose()

    print("=" * 80)
    print(f"Benchmark finished. Total estimated spend: ${ledger.spent():.4f} of ${args.budget:.2f} cap.")

    calls = [{k: v for k, v in c.items() if k != "t_submit"} for c in clients.calls]
    warm_calls = [c for c in calls if c.get("turn") is not None and c["turn"] >= 1]

    keys = sorted({k for r in rows for k in r if isinstance(r.get(k), (int, float))})
    out = {
        "benchmark_type": "REAL_LIVE_MEASUREMENT",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tenant": args.tenant,
        "audio_source": args.wav,
        "turns_completed": len(rows),
        "errors": errors,
        "halted": clients.halted,
        "endpoints": {svc: cfg.get(f"{svc}_url", "").split("/")[-2] for svc in eb.SERVICES},
        "aggregate_all": {k: ll.summary([r[k] for r in rows if k in r and isinstance(r[k], (int, float))]) for k in keys},
        "first_turn_cold": rows[0] if rows else {},
        "warm_turns_2plus": {k: ll.summary([r[k] for r in rows[1:] if k in r and isinstance(r[k], (int, float))]) for k in keys},
        "stage_split_all": ll.stage_split(calls),
        "stage_split_warm": ll.stage_split(warm_calls),
        "cost": ledger.report(),
        "per_turn": rows,
        "calls": calls,
    }

    # Save output JSON
    out_json = Path(args.out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[OK] Telemetry written to {out_json}")

    # Generate Markdown Report
    out_md = Path(args.out_report)
    generate_live_markdown_report(out, out_md)
    print(f"[OK] Markdown report written to {out_md}")
    return out


def generate_live_markdown_report(data: dict, out_path: Path):
    L = []
    L.append("# REAL LIVE 10-Turn End-to-End Latency Measurement Report")
    L.append("")
    L.append(f"**Execution Timestamp:** `{data['timestamp']}`  ")
    L.append(f"**Tenant:** `{data['tenant']}`  ")
    L.append(f"**Endpoints:** STT `{data['endpoints']['stt']}` | Brain `{data['endpoints']['brain']}` | TTS `{data['endpoints']['tts']}`  ")
    L.append(f"**Turns Completed:** {data['turns_completed']}  ")
    L.append(f"**Total Estimated Spend:** `${data['cost']['usd_estimated_total']:.4f}` of `${data['cost']['budget_usd']:.2f}` cap  ")
    L.append("")
    L.append("---")
    L.append("")

    L.append("## 1. Executive Summary: Real Live Measurements")
    L.append("")
    t1 = data.get("first_turn_cold", {})
    warm = data.get("warm_turns_2plus", {})

    L.append("| Stage / Metric | Turn 1 (Cold / Initial) | Turns 2-10 (Warm Median) | Turns 2-10 (Warm P95) |")
    L.append("|---|:---:|:---:|:---:|")
    L.append(f"| **Post-Endpoint V2V** | **{t1.get('post_endpoint_v2v', '-')} ms** | **{warm.get('post_endpoint_v2v', {}).get('p50', '-')} ms** | **{warm.get('post_endpoint_v2v', {}).get('p95', '-')} ms** |")
    L.append(f"| **STT Transcription** | {t1.get('stt', '-')} ms | {warm.get('stt', {}).get('p50', '-')} ms | {warm.get('stt', {}).get('p95', '-')} ms |")
    L.append(f"| **LLM Time to First Token** | {t1.get('llm_first_token', '-')} ms | {warm.get('llm_first_token', {}).get('p50', '-')} ms | {warm.get('llm_first_token', {}).get('p95', '-')} ms |")
    L.append(f"| **TTS First Chunk Synthesis** | {t1.get('tts_first_chunk', '-')} ms | {warm.get('tts_first_chunk', {}).get('p50', '-')} ms | {warm.get('tts_first_chunk', {}).get('p95', '-')} ms |")
    L.append(f"| **Total Turn Duration** | {t1.get('turn_total', '-')} ms | {warm.get('turn_total', {}).get('p50', '-')} ms | {warm.get('turn_total', {}).get('p95', '-')} ms |")
    L.append("")

    L.append("## 2. Turn-by-Turn Telemetry with Real RunPod Job Timings")
    L.append("")
    L.append("| Turn | V2V (ms) | STT (ms) | Brain TTFT (ms) | TTS1 (ms) | Turn Total (ms) | RunPod STT Delay/Exec | RunPod Brain Delay/Exec | RunPod TTS Delay/Exec | Error |")
    L.append("|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|")

    for r in data.get("per_turn", []):
        stt_de = f"{r.get('runpod_stt_delay', 0):.0f}/{r.get('runpod_stt_exec', 0):.0f}" if 'runpod_stt_delay' in r else "-"
        br_de = f"{r.get('runpod_brain_delay', 0):.0f}/{r.get('runpod_brain_exec', 0):.0f}" if 'runpod_brain_delay' in r else "-"
        tts_de = f"{r.get('runpod_tts_delay', 0):.0f}/{r.get('runpod_tts_exec', 0):.0f}" if 'runpod_tts_delay' in r else "-"

        L.append(f"| **#{r.get('turn', '-')}** | {r.get('post_endpoint_v2v', '-')} | {r.get('stt', '-')} | {r.get('llm_first_token', '-')} | {r.get('tts_first_chunk', '-')} | {r.get('turn_total', '-')} | {stt_de} | {br_de} | {tts_de} | {r.get('error') or ''} |")

    L.append("")
    L.append("## 3. RunPod Serverless Infrastructure Split (Queue vs Compute)")
    L.append("")
    L.append("| Service | Total Calls | Delay Time (Queue/Cold) P50/P95 | Execution Time (Compute) P50/P95 | Sum Delay (s) | Sum Compute (s) | Delay Share of Time |")
    L.append("|---|:---:|:---:|:---:|:---:|:---:|:---:|")

    stage_split = data.get("stage_split_all", {})
    for svc in ("stt", "brain", "tts"):
        s = stage_split.get(svc, {})
        d = s.get("delay_ms", {})
        e = s.get("exec_ms", {})
        L.append(f"| **{svc.upper()}** | {s.get('calls', 0)} | {d.get('p50', '-')}/{d.get('p90', '-')} ms | {e.get('p50', '-')}/{e.get('p90', '-')} ms | {s.get('sum_delay_s', 0):.2f}s | {s.get('sum_exec_s', 0):.2f}s | {round((s.get('delay_share_of_service') or 0)*100, 1)}% |")

    L.append("")
    L.append("## 4. RunPod Spend Ledger")
    L.append("")
    cost = data.get("cost", {})
    per = cost.get("per_service", {})
    L.append(f"- **Total Spend:** `${cost.get('usd_estimated_total', 0):.4f}` of cap `${cost.get('budget_usd', 0):.2f}`")
    for svc in ("stt", "brain", "tts"):
        p = per.get(svc, {})
        L.append(f"- **{svc.upper()}:** `${p.get('usd', 0):.4f}` (compute: {p.get('job_s', 0)}s, idle tail: {p.get('tail_s', 0)}s)")
    L.append("")
    L.append("---")
    L.append("*Report generated from real live RunPod measurements.*")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(L), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tenant", default="mme-test")
    parser.add_argument("--wav", default=str(ROOT / "Utkarsh_voice.wav"))
    parser.add_argument("--turns", type=int, default=10)
    parser.add_argument("--clip-s", type=float, default=4.0)
    parser.add_argument("--idle-s", type=float, default=0.0)
    parser.add_argument("--budget", type=float, required=True, help="Stated hard cap in USD")
    parser.add_argument("--yes", action="store_true", help="Explicit confirmation to make chargeable RunPod calls")
    parser.add_argument("--idle", type=lambda v: eb.kv(v), default="stt=5,brain=5,tts=5")
    parser.add_argument("--price", type=lambda v: eb.kv(v), default={})
    parser.add_argument("--cold-reserve-s", type=float, default=60.0)
    parser.add_argument("--out-json", default=str(ROOT / "docs" / "evidence" / "real_live_10_turn_result.json"))
    parser.add_argument("--out-report", default=str(ROOT / "docs" / "evidence" / "real_live_10_turn_report.md"))
    args = parser.parse_args()

    if not args.yes:
        print("[ERROR] Refusing to run without --yes: This makes chargeable calls to RunPod.")
        sys.exit(2)

    asyncio.run(run_live_10_turn_benchmark(args))


if __name__ == "__main__":
    main()
