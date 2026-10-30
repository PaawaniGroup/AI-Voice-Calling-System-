#!/usr/bin/env python3
"""Preflight wake script for RunPod AI endpoints.

Wakes endpoints in strict sequential order:
  1. Brain  (RUNPOD_BRAIN_KEY)
  2. Voice  (RUNPOD_TTS_KEY)
  3. STT    (RUNPOD_STT_KEY)

Features:
  - Dry-run by default (--wake required to execute).
  - Uses process-scoped environment variables only.
  - Polls until each answers.
  - Reports each pickup time.
  - Bounded by --cap-seconds (stops if cap reached).
  - Stops immediately on the first error.
  - Never logs or exposes raw keys.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx
from app.ai.key_refs import mask_key


def get_required_key(env_var_name: str, service: str) -> str:
    val = os.environ.get(env_var_name, "").strip()
    if not val:
        raise RuntimeError(f"Missing required environment variable '{env_var_name}' for {service}. Refusing to proceed.")
    return val


def get_service_payload(service: str) -> dict:
    if service == "brain":
        return {"input": {"prompt": "ping", "max_tokens": 1}}
    elif service == "voice":
        return {"input": {"text": "नमस्ते", "voice": "utkarsh", "output_format": "ulaw8k"}}
    elif service == "stt":
        return {"input": {"warmup": True}}
    raise ValueError(f"Unknown service: {service}")


async def wake_single_service(
    service: str,
    url: str,
    api_key: str,
    client: httpx.AsyncClient,
    remaining_budget_s: float,
    poll_interval_s: float = 2.0
) -> float:
    """Polls endpoint until it answers with HTTP 200 or status COMPLETED. Returns pickup time in seconds."""
    t0 = time.monotonic()
    hdrs = {"Authorization": f"Bearer {api_key}"}
    payload = get_service_payload(service)
    target_url = url.rstrip("/")
    if not target_url.endswith("/runsync") and not target_url.endswith("/run"):
        target_url += "/runsync"

    while True:
        elapsed = time.monotonic() - t0
        if elapsed >= remaining_budget_s:
            raise TimeoutError(f"Budget cap exceeded while waiting for {service} after {round(elapsed, 1)}s")

        try:
            r = await client.post(target_url, json=payload, headers=hdrs, timeout=min(30.0, remaining_budget_s - elapsed))
            if r.status_code == 200:
                body = r.json() if "application/json" in r.headers.get("Content-Type", "") else {}
                status = body.get("status") if isinstance(body, dict) else None
                # If /run was used, it may return IN_QUEUE or IN_PROGRESS
                if status in ("IN_QUEUE", "IN_PROGRESS"):
                    await asyncio.sleep(poll_interval_s)
                    continue
                pickup_time = round(time.monotonic() - t0, 2)
                return pickup_time
            elif r.status_code in (401, 403):
                raise RuntimeError(f"{service} authentication failed (HTTP {r.status_code}). Check key.")
            elif r.status_code >= 500:
                # Cold start or transient gateway error, retry if budget allows
                await asyncio.sleep(poll_interval_s)
                continue
            else:
                raise RuntimeError(f"{service} returned unexpected status HTTP {r.status_code}: {r.text[:100]}")
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            await asyncio.sleep(poll_interval_s)
            continue


async def run_preflight_wake(
    brain_url: str,
    voice_url: str,
    stt_url: str,
    dry_run: bool = True,
    cap_seconds: float = 180.0,
    transport: httpx.AsyncBaseTransport | None = None
) -> dict:
    t_start = time.monotonic()
    print("=" * 65)
    print("PREFLIGHT WAKE: Brain -> Voice -> STT")
    print(f"Cap Budget:   {cap_seconds}s")
    print(f"Mode:         {'DRY-RUN (no network calls)' if dry_run else 'LIVE WAKE'}")
    print("=" * 65)

    # 1. Check environment variables
    brain_key = get_required_key("RUNPOD_BRAIN_KEY", "Brain")
    voice_key = get_required_key("RUNPOD_TTS_KEY", "Voice")
    stt_key = get_required_key("RUNPOD_STT_KEY", "STT")

    print(f"1. RUNPOD_BRAIN_KEY: {mask_key(brain_key)} -> {brain_url}")
    print(f"2. RUNPOD_TTS_KEY:   {mask_key(voice_key)} -> {voice_url}")
    print(f"3. RUNPOD_STT_KEY:   {mask_key(stt_key)} -> {stt_url}")

    if dry_run:
        print("\n[DryRun] Environment variables verified and order established. No calls made.")
        return {"status": "dry_run", "cap_seconds": cap_seconds}

    results = {}
    async with httpx.AsyncClient(transport=transport) as client:
        # Order: 1. Brain, 2. Voice, 3. STT
        sequence = [
            ("brain", brain_url, brain_key),
            ("voice", voice_url, voice_key),
            ("stt", stt_url, stt_key),
        ]

        for svc, url, key in sequence:
            elapsed_total = time.monotonic() - t_start
            budget_left = cap_seconds - elapsed_total
            if budget_left <= 0:
                print(f"ERROR: Cap budget of {cap_seconds}s reached before waking {svc}.")
                results[svc] = {"status": "cap_exceeded"}
                break

            print(f"\nWaking {svc.upper()} ({url})...")
            try:
                pickup_s = await wake_single_service(svc, url, key, client, remaining_budget_s=budget_left)
                print(f"  --> {svc.upper()} is READY (pickup: {pickup_s}s)")
                results[svc] = {"status": "ok", "pickup_seconds": pickup_s}
            except Exception as e:
                print(f"  --> ERROR waking {svc.upper()}: {e}")
                results[svc] = {"status": "error", "error": str(e)}
                print(f"Halting preflight sequence on first error ({svc}).")
                break

    total_duration = round(time.monotonic() - t_start, 2)
    print("\n" + "=" * 65)
    print(f"Preflight finished in {total_duration}s. Results: {results}")
    print("=" * 65)
    return {"status": "completed", "total_seconds": total_duration, "services": results}


def main():
    parser = argparse.ArgumentParser(description="Preflight wake RunPod endpoints in order: Brain -> Voice -> STT")
    parser.add_argument("--wake", action="store_true", help="Execute live wake calls (dry-run by default)")
    parser.add_argument("--cap-seconds", type=float, default=180.0, help="Total execution budget cap in seconds (default 180s)")
    parser.add_argument("--brain-url", default="https://api.runpod.ai/v2/zx6o64kfl8ecj0/runsync", help="Brain endpoint URL")
    parser.add_argument("--voice-url", default="https://api.runpod.ai/v2/6bmxas8ccwzhrv/runsync", help="Voice endpoint URL")
    parser.add_argument("--stt-url", default="https://api.runpod.ai/v2/kp9zlne48yhs80/runsync", help="STT endpoint URL")
    args = parser.parse_args()

    asyncio.run(
        run_preflight_wake(
            brain_url=args.brain_url,
            voice_url=args.voice_url,
            stt_url=args.stt_url,
            dry_run=not args.wake,
            cap_seconds=args.cap_seconds
        )
    )


if __name__ == "__main__":
    main()
