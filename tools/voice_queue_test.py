"""CAPPED real test of the voice endpoint's queue time (hypothesis: the ~1.1 s per-turn voice queue is a FlashBoot revival after the 5 s idle timeout).

NOT RUN. It creates up to 6 real voice jobs (billed). It refuses to run without --approved "<the operator's APPROVED: message id>" and never runs more than 6 jobs.
Run it twice, in this order, with the idle timeout set in the console: B = 5 s (today's setting), then A = 300 s (the operator changes it). Same text every time (a fixed neutral
phrase: no caller or reply words). Output: numbers only, one JSON line per job and a summary; the key is read from the environment variable named by --key-env, never printed.

  RUNPOD_TTS_KEY=... python tools/voice_queue_test.py --label idle_5s   --gap-s 20 --jobs 6 --approved "<id>"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics as st
import sys
import time

TEXT = "देखिए, इस बारे में,"          # a fixed neutral 3-word phrase (app/ai/streaming/ramp.py PROPOSED_OPENERS)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--tenant", default="mme-test")
    ap.add_argument("--mongo", default="mongodb://127.0.0.1:27117")
    ap.add_argument("--key-env", default="RUNPOD_TTS_KEY")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--gap-s", type=float, default=20.0)
    ap.add_argument("--approved", default="")
    a = ap.parse_args()
    if not a.approved.strip():
        print("refused: no --approved id (this test creates billed voice jobs)")
        return 2
    if not 1 <= a.jobs <= 6:
        print("refused: at most 6 jobs")
        return 2
    sys.path.insert(0, ".")
    from pymongo import AsyncMongoClient
    from app.ai import turn_trace
    from app.ai.clients import ModelClients
    from config.settings import Settings
    doc = await AsyncMongoClient(a.mongo, serverSelectionTimeoutMS=3000)["mme_telephony"]["ai_config"].find_one({"tenant_id": a.tenant}) or {}
    cfg = {k: doc.get(k) for k in ("tts_url", "tts_ulaw8k", "tts_voice", "runpod_mode", "models", "timeouts", "paths")}
    cfg["key_refs"] = {"tts": a.key_env}
    cl = ModelClients(Settings(_env_file=None), cfg)
    rows = []
    try:
        for i in range(a.jobs):
            tr = turn_trace.TurnTrace(t_speech_end=time.monotonic())
            turn_trace.start(tr)
            try:
                await cl.synthesize(TEXT, "hi")
                ok = True
            except Exception as e:
                ok = type(e).__name__
            j = tr.jobs[0] if tr.jobs else {}
            row = {"label": a.label, "job": i + 1, "ok": ok, **{k: j.get(k) for k in ("delay_ms", "exec_ms", "total_ms", "remainder_ms", "submit_ms", "polls", "poll_ms", "audio_s", "gap_since_prev_ms")}}
            rows.append(row)
            print(json.dumps(row))
            if i < a.jobs - 1:
                await asyncio.sleep(a.gap_s)
    finally:
        await cl.aclose()
    d = [r["delay_ms"] for r in rows[1:] if isinstance(r.get("delay_ms"), (int, float))]     # job 1 is reported but left out of the median (it may be a cold start)
    print(json.dumps({"label": a.label, "queue_delay_ms_median_jobs_2_to_n": round(st.median(d), 1) if d else None, "n": len(d)}))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
