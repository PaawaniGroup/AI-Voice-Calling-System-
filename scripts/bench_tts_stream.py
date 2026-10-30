"""Benchmark: complete-clip TTS (`synthesize`) vs progressive TTS (`synthesize_stream`) against a REAL RunPod TTS endpoint.

This script MEASURES nothing by itself: it needs an authorised endpoint. It never prints the API key and refuses to run without explicit settings.

  BENCH_TTS_URL      e.g. https://api.runpod.ai/v2/<id>/runsync   (host must be in AI_ALLOWED_HOSTS)
  BENCH_TTS_KEY      RunPod API key (read from the environment only)
  python -m scripts.bench_tts_stream --runs 20 --warmup 2 --out docs/evidence/bench_tts_stream.json

Per request it records, from the moment the request is sent: time to first audio, to the first 20 ms and 100 ms of audio, to the end, the audio
length and the real-time factor (total / audio). Warm-up requests are discarded; cold start is therefore NOT in the numbers unless --warmup 0.
Streaming only helps if the worker actually yields chunks: a worker that returns one finished output shows `chunks == 1` and the same
first-audio time as complete-clip mode, and the report says so.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time

from app.ai.clients import ModelClients
from config.settings import Settings

PROMPTS = {
    "hi_short": ("hi", "जी, बिल्कुल।"),
    "hi_medium": ("hi", "जी, B.Tech में आपका स्वागत है। आप किस branch में admission लेना चाहते हैं?"),
    "en_medium": ("en", "Thank you for calling. Which course are you interested in this year?"),
    "hinglish_long": ("hi", "जी, हमारे पास Computer Science, AI और Mechanical Engineering courses हैं, और merit के आधार पर scholarship भी मिलती है। "
                            "क्या मैं आपको fee structure के बारे में बताऊँ?"),
}


def pct(values: list[float], p: float) -> float:
    """Nearest-rank percentile (no interpolation: with few runs an interpolated p99 would be invented precision)."""
    s = sorted(values)
    return s[max(0, min(len(s) - 1, math.ceil(p / 100 * len(s)) - 1))] if s else float("nan")


async def one_request(clients: ModelClients, mode: str, language: str, text: str) -> dict:
    t0 = time.perf_counter()
    first = t20 = t100 = None
    audio_s, chunks = 0.0, 0
    if mode == "complete":
        pcm, rate = await clients.synthesize(text, language)
        t_end = time.perf_counter() - t0
        audio_s, chunks = len(pcm) / 2 / rate, 1
        first = t20 = t100 = t_end if audio_s > 0 else None
    else:
        async for ch in clients.synthesize_stream(text, language):
            now = time.perf_counter() - t0
            if ch.pcm:
                chunks += 1
                if first is None:
                    first = now
                audio_s += len(ch.pcm) / 2 / ch.sample_rate
                if t20 is None and audio_s >= 0.02:
                    t20 = now
                if t100 is None and audio_s >= 0.1:
                    t100 = now
        t_end = time.perf_counter() - t0
    return {"first_audio_s": first, "t20ms_s": t20, "t100ms_s": t100, "total_s": t_end, "audio_s": audio_s, "chunks": chunks,
            "rtf": (t_end / audio_s) if audio_s else None}


async def run(clients: ModelClients, *, runs: int, warmup: int, prompts: dict = PROMPTS) -> dict:
    report: dict = {"note": "measured on the endpoint given in BENCH_TTS_URL; see the script docstring for what is and is not included", "results": {}}
    for name, (lang, text) in prompts.items():
        for mode in ("complete", "stream"):
            for _ in range(warmup):
                await one_request(clients, mode, lang, text)
            rows = [await one_request(clients, mode, lang, text) for _ in range(runs)]
            agg = {}
            for key in ("first_audio_s", "t20ms_s", "t100ms_s", "total_s", "rtf"):
                vals = [r[key] for r in rows if r[key] is not None]
                agg[key] = {"p50": pct(vals, 50), "p95": pct(vals, 95), "p99": pct(vals, 99), "n": len(vals)}
            agg["chunks_p50"] = pct([float(r["chunks"]) for r in rows], 50)
            agg["audio_s_p50"] = pct([r["audio_s"] for r in rows], 50)
            if mode == "stream" and agg["chunks_p50"] <= 1:
                agg["warning"] = "the worker delivered the clip in ONE chunk: it is not streaming, so there is no latency gain to measure"
            report["results"][f"{name}/{mode}"] = agg
    return report


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=20)
    ap.add_argument("--warmup", type=int, default=2)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    url, key = os.environ.get("BENCH_TTS_URL", ""), os.environ.get("BENCH_TTS_KEY", "")
    if not url or not key:
        print("refusing to run: set BENCH_TTS_URL and BENCH_TTS_KEY (an authorised endpoint)", file=sys.stderr)
        return 2
    clients = ModelClients(Settings(), {"tts_url": url, "keys": {"tts": key}})
    try:
        report = await run(clients, runs=a.runs, warmup=a.warmup)
    finally:
        await clients.aclose()
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
