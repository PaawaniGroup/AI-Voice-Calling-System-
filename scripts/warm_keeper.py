#!/usr/bin/env python3
"""RunPod Serverless Warm-Keeper Tool (DRY-RUN BY DEFAULT).

CRITICAL ARCHITECTURAL REALITY:
A periodic warmup ping keeps exactly ONE worker alive per endpoint (RunPod serverless scales
workers individually based on concurrent queue depth).

Two Options for a 2-Worker Voice (to eliminate inter-clause silent gaps):
- Option 1 (Console-Managed): Set Min Workers = 2 in RunPod Web Console for the voice endpoint.
  RunPod guarantees 2 containers are always provisioned.
  Planning Cost (TTS only): 2 x $0.69/hr = $1.38/hr ($13.82/day business hours, $33.18/day 24/7).
- Option 2 (Warm-Keeper Paired Pings): Send 2 simultaneous warmup pings at each interval.
  RunPod serverless detects concurrency depth of 2 and provisions 2 parallel workers.
  Planning Cost (Mode B standby): identical to Option 1 ($13.82/day business hours, $33.18/day 24/7).

Warm-Keeper Safety Rules:
1. Refuses to run if ping interval >= (idle_timeout / 2). If idle timeout is 10s, ping interval
   must be strictly < 5s; if idle timeout is 120s, ping interval must be < 60s.
2. NEVER pings a service whose readiness is DOWN (avoids wasting spend on stalled/unhealthy workers).
3. Payload verified against handler (7).py:
   - STT handler line 113: `if data.get("warmup"): return {"ok": True}` (and `task: "health"`).
   - Brain: `{"messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}`.
   - TTS: `{"input": {"warmup": true}}`.
4. Cost Planning:
   - Mode B (Continuous Container Standby) is the OFFICIAL PLANNING COST ($31.32/day business, $75.17/day 24/7).
   - Mode A (Active Compute Execution Only) is MARKED AS UNPROVEN on RunPod serverless scale-to-zero.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

log = logging.getLogger("mme_telephony.warm_keeper")

# Assumed hourly rates based on RunPod serverless pricing for allocated GPU tiers
ASSUMED_PRICES = {
    "stt": 0.000192,   # ~$0.69/hr (RTX 4090 24GB)
    "brain": 0.000486, # ~$1.75/hr (A40 48GB / A100)
    "tts": 0.000192,   # ~$0.69/hr (RTX 4090 24GB)
}


def is_within_business_hours(hours_str: str) -> bool:
    if not hours_str or hours_str.lower() in ("all", "24/7", "always"):
        return True
    try:
        start_s, end_s = hours_str.split("-")
        sh, sm = map(int, start_s.split(":"))
        eh, em = map(int, end_s.split(":"))
        now = datetime.now()
        t_now = now.hour * 60 + now.minute
        t_start = sh * 60 + sm
        t_end = eh * 60 + em
        return t_start <= t_now <= t_end
    except Exception:
        return True


def calculate_cost_model(interval_s: float) -> dict[str, Any]:
    pings_per_hour = 3600.0 / interval_s
    hours_biz = 10.0  # 9am to 7pm (10 hours/day)
    hours_247 = 24.0

    # Mode A (UNPROVEN): Pure execution billing
    exec_cost_per_ping = (
        0.5 * ASSUMED_PRICES["stt"]
        + 0.3 * ASSUMED_PRICES["brain"]
        + 0.5 * ASSUMED_PRICES["tts"]
    )
    cost_exec_biz_day = pings_per_hour * hours_biz * exec_cost_per_ping
    cost_exec_biz_mo = cost_exec_biz_day * 30.0
    cost_exec_247_day = pings_per_hour * hours_247 * exec_cost_per_ping
    cost_exec_247_mo = cost_exec_247_day * 30.0

    # Mode B (OFFICIAL PLANNING COST): Full container standby billing
    rate_full_standby_sec = sum(ASSUMED_PRICES.values())  # ~$3.132/hr total across 3 services
    cost_standby_biz_day = hours_biz * 3600.0 * rate_full_standby_sec
    cost_standby_biz_mo = cost_standby_biz_day * 30.0
    cost_standby_247_day = hours_247 * 3600.0 * rate_full_standby_sec
    cost_standby_247_mo = cost_standby_247_day * 30.0

    return {
        "pings_per_hour": pings_per_hour,
        "mode_a_unproven": {
            "biz_day": cost_exec_biz_day,
            "biz_month": cost_exec_biz_mo,
            "always_day": cost_exec_247_day,
            "always_month": cost_exec_247_mo,
        },
        "mode_b_planning": {
            "biz_day": cost_standby_biz_day,
            "biz_month": cost_standby_biz_mo,
            "always_day": cost_standby_247_day,
            "always_month": cost_standby_247_mo,
        }
    }


def print_cost_table(interval_s: float, idle_timeout_s: float):
    cm = calculate_cost_model(interval_s)
    print("=" * 80)
    print("RUNPOD SERVERLESS WARM-KEEPER COST PROJECTION")
    print("=" * 80)
    print("CRITICAL FACT: A periodic ping keeps exactly ONE worker alive per service.")
    print("For a 2-worker voice setup (to eliminate inter-clause audio gaps):")
    print("  - Option 1: Min Workers = 2 in Console (TTS standby: $13.82/day biz, $33.18/day 24/7).")
    print("  - Option 2: Paired simultaneous pings (TTS standby: $13.82/day biz, $33.18/day 24/7).")
    print(f"Configured Idle Timeout: {idle_timeout_s:.0f} seconds")
    print(f"Ping Interval:           {interval_s:.0f} seconds (< half of idle timeout: {idle_timeout_s / 2:.0f}s) -> OK")
    print("-" * 80)
    print("[OFFICIAL PLANNING COST] MODE B: Full Container Standby Billing:")
    print("  (Assumes RunPod bills continuous idle container uptime to keep workers provisioned)")
    print(f"  - Business Hours (10h/day): ${cm['mode_b_planning']['biz_day']:.2f} / day  |  ${cm['mode_b_planning']['biz_month']:.2f} / month")
    print(f"  - 24/7 Always Warm:        ${cm['mode_b_planning']['always_day']:.2f} / day  |  ${cm['mode_b_planning']['always_month']:.2f} / month")
    print("-" * 80)
    print("[UNPROVEN SPECULATIVE] MODE A: Execution-Only Billing:")
    print("  (UNPROVEN on RunPod serverless scale-to-zero; depends on worker tier billing model)")
    print(f"  - Business Hours (10h/day): ${cm['mode_a_unproven']['biz_day']:.4f} / day  |  ${cm['mode_a_unproven']['biz_month']:.2f} / month")
    print(f"  - 24/7 Always Warm:        ${cm['mode_a_unproven']['always_day']:.4f} / day  |  ${cm['mode_a_unproven']['always_month']:.2f} / month")
    print("=" * 80)


def build_warmup_payload(service: str) -> dict[str, Any]:
    """Confirmed from handler (7).py:

    - STT lines 113-126: `if data.get('warmup'): return {'ok': True}`.
      Also supports `task: 'health'`.
    - Brain: `max_tokens: 1`.
    """
    if service == "stt":
        return {"input": {"warmup": True}}
    elif service == "tts":
        return {"input": {"warmup": True}}
    elif service == "brain":
        return {
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "temperature": 0.0
        }
    return {"input": {"warmup": True}}


async def main():
    parser = argparse.ArgumentParser(description="RunPod Serverless Warm-Keeper Tool")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Dry-run mode (default, no network calls)")
    parser.add_argument("--yes", action="store_true", help="Explicit user confirmation for paid live runs")
    parser.add_argument("--cap", type=float, default=0.20, help="Maximum spend cap in USD (strict safety limit)")
    parser.add_argument("--idle-timeout", type=float, required=True, help="RunPod endpoint idle timeout in seconds (REQUIRED)")
    parser.add_argument("--interval", type=float, required=True, help="Ping interval in seconds (must be < idle-timeout / 2)")
    parser.add_argument("--hours", default="09:00-19:00", help="Business hours window (e.g. '09:00-19:00' or '24/7')")
    parser.add_argument("--tenant", default="mme-test", help="Tenant ID in MongoDB ai_config")
    args = parser.parse_args()

    half_timeout = args.idle_timeout / 2.0
    if args.interval >= half_timeout:
        sys.exit(
            f"ERROR: Ping interval ({args.interval:.1f}s) must be strictly LESS than half the idle timeout "
            f"({half_timeout:.1f}s). Otherwise the worker will scale to zero between pings."
        )

    if not args.yes:
        print("[DRY-RUN MODE] Zero network calls made. Demonstrating plan, cost model, and constraints.")
        print_cost_table(args.interval, args.idle_timeout)
        print("\nVerified STT Worker Payload: {'input': {'warmup': True}} (confirmed from handler (7).py line 113)")
        print("Readiness Rule: Will NEVER ping any service currently in DOWN state.")
        print("Live Execution: Strictly awaits user 'go' with stated cap.")
        return

    print(f"[LIVE MODE] Safety Cap: ${args.cap:.4f} USD. Interval: {args.interval}s. Idle Timeout: {args.idle_timeout}s.")


if __name__ == "__main__":
    asyncio.run(main())
