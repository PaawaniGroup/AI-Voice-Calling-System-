"""Benchmark the REAL STT / Brain / TTS endpoints (never mocks) and write a JSON evidence file.

    MME_TEL_AI_API_KEY=... python scripts/ai_bench.py --stt http://host:port --brain http://host:port --tts http://host:port \
        --wav speech_sample.wav --repeats 10 --levels 1,2,4,8 --out docs/evidence/ai_bench_<date>.json

What it measures (each stage separately, so the slow one is visible): single-request latency (p50/p95/p99/max), a concurrency
sweep (N simultaneous requests, error rate, latency), and TTS real-time factor. It refuses to run without URLs, and the STT
stage needs a REAL speech recording (`--wav`, mono 16-bit PCM): it will not invent audio, so without one STT is reported as
"skipped". Output contains hosts/ports, model ids, timestamps and statistics only: no transcripts, no keys, no audio.
Exit code 1 if any request failed. Results describe THIS deployment on THIS day; they are not a capacity guarantee.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import sys
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ai.clients import ModelClients  # noqa: E402
from config.settings import Settings  # noqa: E402

TEXTS = {
    "hi": "नमस्ते, मैं आपकी एडमिशन काउंसलिंग में मदद करने के लिए यहाँ हूँ। आप किस कोर्स में रुचि रखते हैं?",
    "en": "Hello, I can help you with course selection, eligibility and admission fees. Which course are you interested in?",
    "hinglish": "Namaste, main aapki admission counselling mein madad karunga. Aap kaunsa course dekh rahe hain, BTech ya MBA?",
}
PROMPTS = {
    "hi": "मुझे बीटेक के बारे में बताइए, पात्रता क्या है?",
    "en": "What is the eligibility for a BTech admission?",
    "hinglish": "BTech ke liye eligibility kya hai aur fees kitni hoti hai?",
}


def pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, max(0, int(round(p / 100 * len(xs) + 0.5)) - 1))], 1)


def summarize(ms: list[float]) -> dict:
    return {"n": len(ms), "p50_ms": pct(ms, 50), "p95_ms": pct(ms, 95), "p99_ms": pct(ms, 99),
            "max_ms": round(max(ms), 1) if ms else None, "mean_ms": round(statistics.fmean(ms), 1) if ms else None}


async def timed(fn):
    t = time.perf_counter()
    try:
        out = await fn()
        return (time.perf_counter() - t) * 1000, out, None
    except Exception as e:  # classified, never printed with content
        return (time.perf_counter() - t) * 1000, None, f"{type(e).__name__}:{getattr(e, 'kind', '')}"


async def sweep(make_call, levels: list[int], repeats: int) -> dict:
    out = {}
    for n in levels:
        lat, errs = [], {}
        t0 = time.perf_counter()
        for _ in range(max(1, repeats // max(1, n)) or 1):
            res = await asyncio.gather(*[timed(make_call) for _ in range(n)])
            for ms, _, err in res:
                if err:
                    errs[err] = errs.get(err, 0) + 1
                else:
                    lat.append(ms)
        wall = time.perf_counter() - t0
        out[str(n)] = {**summarize(lat), "errors": errs, "wall_s": round(wall, 2), "throughput_rps": round((len(lat) + sum(errs.values())) / wall, 2)}
    return out


async def run(args, transport=None) -> dict:
    urls = {"stt": args.stt, "brain": args.brain, "tts": args.tts}
    hosts = ",".join(sorted({urlparse(u).hostname for u in urls.values() if u}))
    s = Settings(_env_file=None, env="test", provider="mock", fs_enabled=False, ai_allowed_hosts=hosts, ai_max_concurrency=max(args.levels) * 2,
                 ai_request_timeout_s=args.timeout, webhook_allow_private=True)
    cfg = {"stt_url": args.stt or "", "brain_url": args.brain or "", "tts_url": args.tts or "", "rag_url": "",
           "timeouts": {"stt": args.timeout, "brain": args.timeout * 2, "tts": args.timeout}, "models": {}}
    c = ModelClients(s, cfg, transport)
    started = datetime.now(timezone.utc)
    result: dict = {"started_at": started.isoformat(), "host": {"platform": platform.platform(), "cpus": os.cpu_count(), "python": platform.python_version()},
                    "endpoints": {k: (f"{urlparse(v).scheme}://{urlparse(v).netloc}" if v else None) for k, v in urls.items()},
                    "models": {"stt": s.ai_stt_model, "brain": s.ai_brain_model, "tts": s.ai_tts_model}, "repeats": args.repeats,
                    "levels": args.levels, "stages": {}, "note": "measured against the endpoints above on this date; not a capacity guarantee"}
    try:
        health = {k: (await c.health(k))["status"] for k in ("stt", "brain", "tts") if urls[k]}
        result["health"] = health
        # ---- TTS
        if args.tts:
            per = {}
            for lang, text in TEXTS.items():
                lat, rtf, errs = [], [], {}
                for _ in range(args.repeats):
                    ms, out, err = await timed(lambda text=text, lang=lang: c.synthesize(text, lang))
                    if err:
                        errs[err] = errs.get(err, 0) + 1
                    else:
                        pcm, rate = out
                        lat.append(ms)
                        rtf.append(ms / 1000 / max(0.001, len(pcm) / 2 / rate))
                per[lang] = {**summarize(lat), "errors": errs, "real_time_factor_p50": round(statistics.median(rtf), 3) if rtf else None}
            result["stages"]["tts"] = {"per_language": per, "concurrency": await sweep(lambda: c.synthesize(TEXTS["en"], "en"), args.levels, args.repeats)}
        # ---- Brain
        if args.brain:
            msgs = {k: [{"role": "user", "content": v}] for k, v in PROMPTS.items()}
            per = {}
            for lang, m in msgs.items():
                lat, errs = [], {}
                for _ in range(args.repeats):
                    ms, _o, err = await timed(lambda m=m: c.chat(m))
                    (errs.__setitem__(err, errs.get(err, 0) + 1) if err else lat.append(ms))
                per[lang] = {**summarize(lat), "errors": errs}
            result["stages"]["brain"] = {"per_language": per, "concurrency": await sweep(lambda: c.chat(msgs["en"]), args.levels, args.repeats)}
        # ---- STT (needs real speech)
        if args.stt and args.wav:
            with wave.open(args.wav, "rb") as w:
                if w.getnchannels() != 1 or w.getsampwidth() != 2:
                    raise SystemExit("--wav must be mono 16-bit PCM")
                pcm, rate = w.readframes(w.getnframes()), w.getframerate()
            lat, errs, lens = [], {}, []
            for _ in range(args.repeats):
                ms, out, err = await timed(lambda: c.transcribe(pcm, rate, "auto"))
                if err:
                    errs[err] = errs.get(err, 0) + 1
                else:
                    lat.append(ms)
                    lens.append(len(out["text"]))
            result["stages"]["stt"] = {**summarize(lat), "errors": errs, "audio_seconds": round(len(pcm) / 2 / rate, 2), "transcript_chars_median": statistics.median(lens) if lens else None,
                                       "concurrency": await sweep(lambda: c.transcribe(pcm, rate, "auto"), args.levels, args.repeats)}
        elif args.stt:
            result["stages"]["stt"] = {"skipped": "needs --wav with a REAL speech recording; synthetic audio would not measure STT"}
    finally:
        await c.aclose()
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    result["any_errors"] = bool(json.dumps(result).count('"errors": {"'))
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stt"), ap.add_argument("--brain"), ap.add_argument("--tts"), ap.add_argument("--wav")
    ap.add_argument("--repeats", type=int, default=10), ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--levels", type=lambda v: [int(x) for x in v.split(",")], default=[1, 2, 4])
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    if not (args.stt or args.brain or args.tts):
        print("refusing to run: give at least one real endpoint (--stt/--brain/--tts). Nothing is simulated.")
        return 2
    res = asyncio.run(run(args))
    text = json.dumps(res, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 1 if res["any_errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
