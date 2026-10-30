"""CAPPED real test: STT-only /run (+ status polls) versus /runsync with the same clip. NOT RUN.

Creates up to 20 real STT jobs (billed). Refuses to run without --approved "<the operator's APPROVED: message id>". The clip is the operator's own recording
(a mono 16-bit WAV, 3-5 s; its path is given by the operator and it is never copied into the repository). The two modes alternate, 5 s apart; the first job of each mode is
reported but left out of the medians. Output: numbers only (no transcript, no key, no file name). The key is read from --key-env (default RUNPOD_STT_KEY).

  RUNPOD_STT_KEY=... python tools/stt_run_vs_runsync.py --clip /path/to/clip.wav --jobs-per-mode 10 --approved "<id>"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics as st
import sys
import time
import wave


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clip", required=True)
    ap.add_argument("--tenant", default="mme-test")
    ap.add_argument("--mongo", default="mongodb://127.0.0.1:27117")
    ap.add_argument("--key-env", default="RUNPOD_STT_KEY")
    ap.add_argument("--jobs-per-mode", type=int, default=10)
    ap.add_argument("--gap-s", type=float, default=5.0)
    ap.add_argument("--approved", default="")
    a = ap.parse_args()
    if not a.approved.strip():
        print("refused: no --approved id (this test creates billed STT jobs)")
        return 2
    if not 2 <= a.jobs_per_mode <= 10:
        print("refused: 2 to 10 jobs per mode")
        return 2
    with wave.open(a.clip, "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            print("refused: the clip must be mono 16-bit PCM")
            return 2
        rate, pcm = w.getframerate(), w.readframes(w.getnframes())
    secs = len(pcm) / 2 / rate
    if not 1.0 <= secs <= 8.0:
        print("refused: the clip must be 1 to 8 seconds")
        return 2
    sys.path.insert(0, ".")
    from pymongo import AsyncMongoClient
    from app.ai import turn_trace
    from app.ai.clients import ModelClients
    from config.settings import Settings
    doc = await AsyncMongoClient(a.mongo, serverSelectionTimeoutMS=3000)["mme_telephony"]["ai_config"].find_one({"tenant_id": a.tenant}) or {}
    base = {k: doc.get(k) for k in ("stt_url", "models", "timeouts", "paths")}
    base["key_refs"] = {"stt": a.key_env}
    clients = {m: ModelClients(Settings(_env_file=None), {**base, "runpod_mode": m}) for m in ("run", "runsync")}
    rows = {"run": [], "runsync": []}
    try:
        for i in range(a.jobs_per_mode):
            for mode in ("run", "runsync"):                                        # alternate, so slow drift hits both modes equally
                tr = turn_trace.TurnTrace(t_speech_end=time.monotonic())
                turn_trace.start(tr)
                t0 = time.perf_counter()
                try:
                    await clients[mode].transcribe(pcm, rate, "auto")
                    ok = True
                except Exception as e:
                    ok = type(e).__name__
                client_ms = round((time.perf_counter() - t0) * 1000, 1)
                j = tr.jobs[0] if tr.jobs else {}
                row = {"mode": mode, "n": i + 1, "ok": ok, "client_ms": client_ms, **{k: j.get(k) for k in ("submit_ms", "polls", "delay_ms", "exec_ms", "remainder_ms", "request_bytes", "convert_ms")}}
                rows[mode].append(row)
                print(json.dumps(row))
                await asyncio.sleep(a.gap_s)
    finally:
        for c in clients.values():
            await c.aclose()
    for mode, rs in rows.items():
        v = [r["client_ms"] for r in rs[1:] if r["ok"] is True]
        rem = [r["remainder_ms"] for r in rs[1:] if r["ok"] is True and isinstance(r.get("remainder_ms"), (int, float))]
        print(json.dumps({"mode": mode, "audio_s": round(secs, 2), "n": len(v), "client_ms_median": round(st.median(v), 1) if v else None,
                          "remainder_ms_median": round(st.median(rem), 1) if rem else None}))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
