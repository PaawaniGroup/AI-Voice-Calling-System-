"""ONE-OFF generation of the cached acknowledgement clips (CHARGEABLE: it creates real voice jobs). Refuses without --approved "<the operator's APPROVED: message id>".

  RUNPOD_TTS_KEY=... python tools/realtime/gen_ack_clips.py --out-dir <folder OUTSIDE every repository> --cap-usd 0.15 --approved "<id>"

Jobs: ONE warm-up job (it wakes the voice endpoint; its audio is discarded) and then one job per approved phrase (app/ai/streaming/ack.py PROPOSED: 3 Hindi, 2 English).
Stops: when the projected cost reaches --cap-usd, at the first HTTP 401 or 403, after 3 consecutive errors. Active workers are not changed. The key is read from the environment
variable named by --key-env and never printed. Output: numbers only (no phrase, no audio, no key). Clips are written as raw 8 kHz mu-law + manifest.json (the layout AckSet.load
reads) to --out-dir, which must be OUTSIDE the repository; afterwards run tools/realtime/check_ack_clips.py on that folder (free).
Projected cost = (seconds since job 1 + the next job (5 s) + the idle tail (--idle-tail-s, 300 s: the endpoint stays billed until its idle timeout) + the cold start (--cold-s, 50 s
before job 1)) x --price-per-s (the HIGHER of the two prices in use, UNVERIFIED; Billing is authoritative).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import time
import wave

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.ai import codec                                  # noqa: E402
from app.ai.streaming.ack import PROPOSED, offending_words   # noqa: E402

WARMUP_TEXT = PROPOSED["hi"][0]


def plan() -> list[tuple[str, str, str]]:
    """(file, lang, text) in generation order: hi_1..hi_3, en_1..en_2. Refuses a phrase that is not neutral (the same check the loader applies)."""
    out = []
    for lang in ("hi", "en"):
        for i, text in enumerate(PROPOSED[lang], 1):
            if offending_words(text):
                raise ValueError("a proposed phrase is not neutral")
            out.append((f"{lang}_{i}.ulaw", lang, text))
    return out


def projected_cost(elapsed_s: float, idle_tail_s: float, cold_s: float, price_per_s: float, next_job_s: float = 5.0) -> float:
    return (elapsed_s + next_job_s + idle_tail_s + cold_s) * price_per_s


def refuse_reason(approved: str, out_dir: str, cap_usd: float) -> str | None:
    if not (approved or "").strip():
        return "no --approved id (this creates billed voice jobs)"
    if not out_dir:
        return "--out-dir is required"
    if ROOT.resolve() in pathlib.Path(out_dir).resolve().parents or pathlib.Path(out_dir).resolve() == ROOT.resolve():
        return "the clips must be written OUTSIDE the repository (the commit guard refuses audio)"
    if not (0 < cap_usd <= 0.15):
        return "the cap must be between 0 and 0.15 USD"
    return None


def write_set(out_dir: pathlib.Path, results: dict[str, bytes], items: list[tuple[str, str, str]]) -> int:
    """Write the mu-law files and manifest.json for the items that were generated. Returns the number of clips written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    clips = []
    for name, lang, text in items:
        raw = results.get(name)
        if raw:
            (out_dir / name).write_bytes(raw)
            with wave.open(str(out_dir / (name.rsplit(".", 1)[0] + ".wav")), "wb") as w:        # a playable copy for the operator (8 kHz mono PCM16, the decoded clip); the loader ignores it
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(codec.MULAW_RATE)
                w.writeframes(codec.mulaw_to_pcm16(raw))
            clips.append({"file": name, "lang": lang, "text": text})
    (out_dir / "manifest.json").write_text(json.dumps({"clips": clips}, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(clips)


async def main(argv=None, clients=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="")
    ap.add_argument("--tenant", default="mme-test")
    ap.add_argument("--mongo", default="mongodb://127.0.0.1:27117")
    ap.add_argument("--key-env", default="RUNPOD_TTS_KEY")
    ap.add_argument("--timeout-s", type=float, default=150.0)
    ap.add_argument("--cap-usd", type=float, default=0.15)
    ap.add_argument("--idle-tail-s", type=float, default=300.0)
    ap.add_argument("--cold-s", type=float, default=50.0)
    ap.add_argument("--price-per-s", type=float, default=0.0003)
    ap.add_argument("--approved", default="")
    a = ap.parse_args(argv)
    why = refuse_reason(a.approved, a.out_dir, a.cap_usd)
    if why:
        print("refused:", why)
        return 2
    items = plan()
    if clients is None:
        from pymongo import AsyncMongoClient
        from app.ai import turn_trace
        from app.ai.clients import ModelClients
        from config.settings import Settings
        doc = await AsyncMongoClient(a.mongo, serverSelectionTimeoutMS=3000)["mme_telephony"]["ai_config"].find_one({"tenant_id": a.tenant}) or {}
        cfg = {k: doc.get(k) for k in ("tts_url", "tts_ulaw8k", "tts_voice", "runpod_mode", "models", "timeouts", "paths")}
        cfg["key_refs"] = {"tts": a.key_env}
        cfg["timeouts"] = {**(cfg.get("timeouts") or {}), "tts": a.timeout_s}
        clients = ModelClients(Settings(_env_file=None), cfg)
    results: dict[str, bytes] = {}
    rows, consecutive, stop, t_first = [], 0, None, None
    jobs = [("warmup", WARMUP_TEXT, "hi")] + [(name, text, lang) for name, lang, text in items]
    try:
        for n, (name, text, lang) in enumerate(jobs):
            elapsed = 0.0 if t_first is None else time.monotonic() - t_first
            if projected_cost(elapsed, a.idle_tail_s, a.cold_s if t_first is None else 0.0, a.price_per_s) >= a.cap_usd:
                stop = "stopped at the cost cap"
                break
            t_first = t_first if t_first is not None else time.monotonic()
            t0, ok = time.monotonic(), True
            try:
                pcm, rate = await clients.synthesize(text, lang)
                consecutive = 0
                if name != "warmup":
                    if rate != 8000:
                        raise ValueError("unexpected sample rate")
                    results[name] = codec.pcm16_to_mulaw(pcm)
            except Exception as e:
                ok, consecutive = f"{getattr(e, 'kind', type(e).__name__)}:{getattr(e, 'status_code', None)}", consecutive + 1
                if getattr(e, "status_code", None) in (401, 403):
                    stop = f"stopped at HTTP {e.status_code}"
            row = {"job": n, "kind": "warmup" if name == "warmup" else "clip", "ok": ok, "wall_ms": round((time.monotonic() - t0) * 1000, 1),
                   "audio_ms": round(len(results[name]) / 8, 1) if name in results else None}
            rows.append(row)
            print(json.dumps(row))
            if stop:
                break
            if consecutive >= 3:
                stop = "stopped after 3 consecutive errors"
                break
    finally:
        aclose = getattr(clients, "aclose", None)
        if aclose is not None:
            await aclose()
    written = write_set(pathlib.Path(a.out_dir), results, items) if results else 0
    print(json.dumps({"tag": "MEASURED", "jobs_run": len(rows), "clips_written": written, "stopped": stop,
                      "projected_cost_usd_at_price": round(projected_cost(time.monotonic() - (t_first or time.monotonic()), a.idle_tail_s, 0.0, a.price_per_s), 4)}))
    return 0 if written == len(items) and not stop else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
