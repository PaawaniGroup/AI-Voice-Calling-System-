#!/usr/bin/env python3
"""RunPod Serverless TTS Voice Concurrency Probe (WP4).

PREPARED BUT NOT RUN WITHOUT EXPLICIT USER GO AND CAP.

Purpose:
Probe RunPod TTS endpoint with 2 and 3 simultaneous synthesis requests to observe whether
RunPod schedules concurrent GPU workers and eliminates inter-clause audio gaps.

Prerequisites (User Console Action required):
1. In RunPod Console, set TTS endpoint Max Workers = 3.
2. In RunPod Console, set Idle Timeout = 10s.
3. User must provide explicit go with stated budget cap (cap $0.05).

Note on Voice Gaps:
With RunPod Max Workers = 1, TTS requests are strictly serialized in queue. For a typical
clause taking ~2.63s to synthesize and ~1.5s of audio duration, a silent gap of 1.0–2.0s
between clauses is mathematically inevitable.
With Max Workers >= 2, clause 2 synthesizes concurrently while clause 1 plays, eliminating gaps.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

log = logging.getLogger("mme_telephony.concurrency_probe")


async def main():
    parser = argparse.ArgumentParser(description="RunPod TTS Concurrency Probe (WP4)")
    parser.add_argument("--yes", action="store_true", help="Explicit user confirmation for live probe")
    parser.add_argument("--cap", type=float, default=0.05, help="Budget cap in USD (default $0.05)")
    parser.add_argument("--concurrency", type=int, choices=[2, 3], default=2, help="Number of concurrent synthesis jobs")
    parser.add_argument("--key-file", help="Path to TTS key file (never printed)")
    args = parser.parse_args()

    if not args.yes:
        print("=" * 80)
        print("RUNPOD TTS CONCURRENCY PROBE (DRY RUN / PLAN ONLY)")
        print("=" * 80)
        print("STATUS: NOT RUN. Awaiting user explicit 'go' and confirmation of Console settings.")
        print("")
        print("Configuration Plan:")
        print(f"  - Target Concurrency: {args.concurrency} simultaneous requests")
        print(f"  - Budget Cap:         ${args.cap:.2f} USD")
        print("  - Test Phrases:       Short normalized clauses (e.g. 4 words, 12 words)")
        print("")
        print("Console Checklist required prior to running:")
        print("  [ ] In RunPod Console, verify TTS endpoint Max Workers >= 3")
        print("  [ ] In RunPod Console, verify Idle Timeout = 10 seconds")
        print("  [ ] Endpoint must be warm (ready >= 1)")
        print("")
        print("Serialisation & Gaps Warning:")
        print("  With Max Workers = 1, RunPod queues requests sequentially. A 28-word reply")
        print("  has ~2 seconds of silent gaps between clauses. With 2+ active workers,")
        print("  the second clause synthesizes in parallel, achieving zero gap.")
        print("=" * 80)
        return

    # Live execution branch (only runs when --yes is explicitly given)
    print(f"[LIVE PROBE] Authorization confirmed. Cap: ${args.cap:.4f} USD. Concurrency: {args.concurrency}")


if __name__ == "__main__":
    asyncio.run(main())
