"""MATCH TEST for the raw PCM body (CHARGEABLE: two real STT jobs; NOT RUN). Refuses without --approved "<the operator's APPROVED: message id>".

  RUNPOD_STT_KEY=... python tools/realtime/stt_match_test.py --clip <WAV OUTSIDE every repository: mono, 16-bit, 8000 Hz, the operator's own voice> --approved "<id>" [--cap-usd 0.03]

Two jobs on a WARM speech worker with the SAME clip: first the old three-field body (flag stt_raw_pcm off), then the raw body (flag on: ONE field audio_pcm16_b64 plus sample_rate = 8000).
Printed: the character-level similarity of the two transcripts as ONE number (difflib ratio after collapsing whitespace), the character counts, and both jobs' timings. NO text is printed
or stored. PASS = similarity >= 0.95 AND both jobs ok AND the raw body was really used (no automatic fallback) AND the worker was warm (job 1 queue under 5 s). Otherwise the verdict is
FAIL or NOT_EVALUATED (cold worker: nothing is concluded). The flag stays OFF unless it passes; this tool never changes a flag.
Stops: the projected cost (elapsed + the next job 5 s) x --price-per-s reaches --cap-usd (at most 0.03), the first HTTP 401 or 403, an error in either job. A cold worker is NOT paid
for beyond the first job: the tool stops after job 1 when its queue is 5 s or more. The key is read from the environment variable named by --key-env and never printed.
"""
from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import pathlib
import sys
import time
import wave

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PASS_AT = 0.95                    # the operator's threshold; not a command-line option
CAPTURE_RATE = 8000               # Plivo
MAX_CAP_USD = 0.03
WARM_QUEUE_MS = 5000.0            # under 5 s = already warm (the program's rule: queue times, never /health)


def similarity(a: str, b: str) -> float:
    x, y = " ".join((a or "").split()), " ".join((b or "").split())
    if not x or not y:
        return 0.0
    return round(difflib.SequenceMatcher(None, x, y, autojunk=False).ratio(), 4)


def refuse_reason(approved: str, clip: str, cap_usd: float) -> str | None:
    if not (approved or "").strip():
        return "no --approved id (this creates billed STT jobs)"
    if not clip:
        return "--clip is required"
    p = pathlib.Path(clip).resolve()
    if ROOT.resolve() in p.parents:
        return "the clip must be OUTSIDE the repository (the commit guard refuses audio)"
    if not p.is_file():
        return "the clip does not exist"
    if not (0 < cap_usd <= MAX_CAP_USD):
        return f"the cap must be between 0 and {MAX_CAP_USD} USD"
    return None


def read_clip(path: str) -> bytes:
    with wave.open(path, "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != CAPTURE_RATE:
            raise ValueError("the clip must be mono, 16-bit, 8000 Hz")
        return w.readframes(w.getnframes())


def verdict(sim: float, ok_old: bool, ok_new: bool, fell_back: bool, queue_ms) -> str:
    if not (isinstance(queue_ms, (int, float)) and queue_ms < WARM_QUEUE_MS):
        return "NOT_EVALUATED"                                  # cold worker: nothing is concluded
    if not (ok_old and ok_new) or fell_back:
        return "FAIL"
    return "PASS" if sim >= PASS_AT else "FAIL"


def timing(job: dict) -> dict:
    keys = ("raw_pcm", "raw_fallback", "request_bytes", "audio_fields", "submit_ms", "delay_ms", "exec_ms", "total_ms", "polls", "poll_ms", "remainder_ms", "status")
    return {k: job.get(k) for k in keys if k in job}


async def main(argv=None, make_clients=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clip", default="")
    ap.add_argument("--tenant", default="mme-test")
    ap.add_argument("--mongo", default="mongodb://127.0.0.1:27117")
    ap.add_argument("--key-env", default="RUNPOD_STT_KEY")
    ap.add_argument("--cap-usd", type=float, default=MAX_CAP_USD)
    ap.add_argument("--price-per-s", type=float, default=0.0003)
    ap.add_argument("--timeout-s", type=float, default=60.0)
    ap.add_argument("--approved", default="")
    a = ap.parse_args(argv)
    why = refuse_reason(a.approved, a.clip, a.cap_usd)
    if why:
        print("refused:", why)
        return 2
    try:
        pcm = read_clip(a.clip)
    except (ValueError, wave.Error, OSError) as e:
        print("refused:", str(e) if isinstance(e, ValueError) else "the clip is not a readable WAV")
        return 2
    from app.ai import turn_trace
    if make_clients is None:
        from pymongo import AsyncMongoClient
        from app.ai.clients import ModelClients
        from config.settings import Settings
        doc = await AsyncMongoClient(a.mongo, serverSelectionTimeoutMS=3000)["mme_telephony"]["ai_config"].find_one({"tenant_id": a.tenant}) or {}
        base = {k: doc.get(k) for k in ("stt_url", "runpod_mode", "models", "timeouts", "paths")}
        base["key_refs"] = {"stt": a.key_env}
        base["timeouts"] = {**(base.get("timeouts") or {}), "stt": a.timeout_s}

        def make_clients(raw: bool):
            return ModelClients(Settings(_env_file=None), {**base, "stt_raw_pcm": raw})
    texts, rows, stop, t0 = {}, [], None, time.monotonic()
    for name, raw in (("old_three_field", False), ("raw_pcm", True)):
        if (time.monotonic() - t0 + 5.0) * a.price_per_s >= a.cap_usd:
            stop = "stopped at the cost cap"
            break
        tr = turn_trace.TurnTrace(t_speech_end=0.0)
        tok = turn_trace._current.set(tr)
        cl = make_clients(raw)
        row = {"body": name, "ok": True}
        try:
            out = await cl.transcribe(pcm, CAPTURE_RATE, "auto")
            texts[name] = out.get("text") or ""
        except Exception as e:
            row["ok"] = f"{getattr(e, 'kind', type(e).__name__)}:{getattr(e, 'status_code', None)}"
            if getattr(e, "status_code", None) in (401, 403):
                stop = f"stopped at HTTP {e.status_code}"
        finally:
            turn_trace._current.reset(tok)
            aclose = getattr(cl, "aclose", None)
            if aclose is not None:
                await aclose()
        job = (tr.jobs[-1] if tr.jobs else {})
        fb = any(j.get("raw_fallback") for j in tr.jobs)
        row.update(timing(job), fell_back=fb, chars=len(texts.get(name, "")))
        rows.append(row)
        print(json.dumps(row))
        if stop or row["ok"] is not True:
            break
        if name == "old_three_field" and not (isinstance(job.get("delay_ms"), (int, float)) and job["delay_ms"] < WARM_QUEUE_MS):
            stop = "worker not warm: queue of job 1 is 5 s or more; nothing was concluded"
            break
    sim = similarity(texts.get("old_three_field", ""), texts.get("raw_pcm", ""))
    done = len(rows) == 2
    v = verdict(sim, rows[0]["ok"] is True if rows else False, rows[1]["ok"] is True if done else False, bool(done and rows[1].get("fell_back")),
                rows[0].get("delay_ms") if rows else None) if done and not stop else "NOT_EVALUATED"
    print(json.dumps({"tag": "MEASURED", "similarity": sim if done else None, "pass_at": PASS_AT, "verdict": v, "stopped": stop, "jobs_run": len(rows),
                      "projected_cost_usd_upper": round((time.monotonic() - t0) * a.price_per_s, 5)}))
    return 0 if v == "PASS" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
