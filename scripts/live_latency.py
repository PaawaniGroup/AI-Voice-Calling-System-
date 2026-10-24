"""Live latency run against the REAL RunPod endpoints configured for a tenant (never mocks). CHARGEABLE: needs --yes.

    python scripts/live_latency.py --tenant mme-test --turns 20 --wav Utkarsh_voice.wav --yes --out docs/evidence/latency_legacy.json
    python scripts/live_latency.py ... --idle-s 900 --turns 3 --tag cold   # first turn after the workers went idle

Each turn feeds a ~4 s clip of the wav through the same `Conversation.stream_turn` the phone path uses (STT -> Brain -> TTS), with a
`TurnTrace` (app/ai/turn_trace.py). Offline there is no VAD or Plivo, so:
  * t_speech_end is the instant the clip is handed to STT: `post_endpoint_v2v` = first audio chunk ready - that instant.
    The real phone voice_to_voice additionally includes the end-of-speech silence window (700 ms legacy; logged as endpoint_silence_ms)
    plus the Plivo hop, so  phone_v2v ~= post_endpoint_v2v + endpoint window.
  * "first audio" is the first TTS audio returned to the pipeline (the moment ws.py would write the first playAudio frame).
Call count: each legacy turn = 1 STT + 1 Brain (+1 per tool call) + 1 TTS per sentence (typically 2-3)  => about 4-5 RunPod calls/turn.
Secrets are read from the tenant's ai_config / environment and are never printed. Output holds timings and counts only (no transcripts).
"""
from __future__ import annotations

import argparse
import asyncio
import audioop
import json
import statistics
import sys
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.path.insert(0, str(Path(__file__).resolve().parent))

import e2e_latency_bench as eb  # noqa: E402  (spend ledger + per-call RunPod delay/exec instrumentation)
from app.ai import turn_trace  # noqa: E402
from app.ai.pipeline import Conversation  # noqa: E402
from config.settings import Settings  # noqa: E402

RATE = 16000


def pct(xs: list[float], p: float):
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 1)


def summary(xs: list[float]) -> dict:
    return {"n": len(xs), "p50": pct(xs, 50), "p95": pct(xs, 95), "min": round(min(xs), 1) if xs else None,
            "max": round(max(xs), 1) if xs else None}


def load_clips(path: str, clip_s: float, n: int) -> list[bytes]:
    """mono 16 kHz PCM16 windows of a real recording; consecutive windows so turns differ. Refuses to invent audio."""
    with wave.open(path, "rb") as w:
        ch, width, rate, frames = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.readframes(w.getnframes())
    if width != 2:
        raise SystemExit("wav must be 16-bit PCM")
    if ch == 2:
        frames = audioop.tomono(frames, 2, 0.5, 0.5)
    if rate != RATE:
        frames = audioop.ratecv(frames, 2, 1, rate, RATE, None)[0]
    size = int(clip_s * RATE) * 2
    clips = [frames[i:i + size] for i in range(0, len(frames) - size + 1, size)]
    if not clips:
        raise SystemExit("wav shorter than one clip")
    return [clips[i % len(clips)] for i in range(n)]


def load_cfg(s: Settings, tenant: str) -> dict:
    from pymongo import MongoClient
    c = MongoClient(s.mongo_uri.get_secret_value(), serverSelectionTimeoutMS=3000)
    try:
        doc = c[s.mongo_db]["ai_config"].find_one({"tenant_id": tenant})
    finally:
        c.close()
    if not doc:
        raise SystemExit(f"no ai_config for tenant {tenant!r}")
    return {k: v for k, v in doc.items() if k != "_id"}


async def one_turn(conv: Conversation, pcm: bytes) -> dict:
    tr = turn_trace.TurnTrace(t_speech_end=time.monotonic())
    turn_trace.start(tr)
    err = None
    async for ch in conv.stream_turn(pcm, RATE):
        if ch.error:
            err = ch.error
            break
        if ch.audio:
            tr.mark("t_first_frame_to_plivo")  # first audio ready == ws.py would send the first frame now
    tr.mark("t_turn_end")
    ms = tr.to_ms()
    ms["error"] = err
    return ms


def derive(ms: dict) -> dict:
    g = ms.get
    d = {}
    if g("t_stt_final") is not None and g("t_stt_sent") is not None:
        d["stt"] = g("t_stt_final") - g("t_stt_sent")
    if g("t_llm_first_token") is not None and g("t_llm_sent") is not None:
        d["llm_first_token"] = g("t_llm_first_token") - g("t_llm_sent")
    if g("t_tts_first_chunk") is not None and g("t_tts_sent") is not None:
        d["tts_first_chunk"] = g("t_tts_first_chunk") - g("t_tts_sent")
    if g("voice_to_voice") is not None:
        d["post_endpoint_v2v"] = g("voice_to_voice")
    if g("t_turn_end") is not None:
        d["turn_total"] = g("t_turn_end")
    for svc in ("stt", "brain", "tts"):
        for k in ("delay", "exec"):
            if g(f"{svc}_{k}_ms") is not None:
                d[f"runpod_{svc}_{k}"] = g(f"{svc}_{k}_ms")
    return d


def stage_split(calls: list[dict]) -> dict:
    """RunPod delayTime (queue + cold start) vs executionTime (compute) per service: p50/p95 and each stage's share of the total."""
    out, total = {}, 0.0
    for svc in eb.SERVICES:
        cs = [c for c in calls if c["service"] == svc and c.get("delay_ms") is not None and c.get("exec_ms") is not None]
        d, e = [c["delay_ms"] for c in cs], [c["exec_ms"] for c in cs]
        out[svc] = {"calls": len(cs), "delay_ms": summary(d), "exec_ms": summary(e), "client_ms": summary([c["client_ms"] for c in cs]),
                    "sum_delay_s": round(sum(d) / 1000, 2), "sum_exec_s": round(sum(e) / 1000, 2),
                    "delay_share_of_service": round(sum(d) / (sum(d) + sum(e)), 3) if (sum(d) + sum(e)) else None}
        total += sum(d) + sum(e)
    for svc in eb.SERVICES:
        v = out[svc]
        v["share_of_all_runpod_time"] = round((v["sum_delay_s"] + v["sum_exec_s"]) * 1000 / total, 3) if total else None
    return out


async def run(args) -> dict:
    s = Settings()
    cfg = load_cfg(s, args.tenant)
    cfg["timeouts"] = {**(cfg.get("timeouts") or {}), **{svc: 900.0 for svc in eb.SERVICES}}  # cold starts must not abort silently
    clips = load_clips(args.wav, args.clip_s, args.turns)
    ledger = eb.Ledger(args.price, args.idle, args.budget, args.cold_reserve_s)
    clients = eb.BenchClients(s, cfg, ledger, start_zero=bool(args.idle_s))
    conv = Conversation(clients, cfg.get("default_language", "auto"), script_enforcement=s.ai_script_enforcement,
                        counsellor_gender=s.ai_counsellor_gender, counsellor_name=s.ai_counsellor_name)
    rows, errors = [], {}
    if args.idle_s:
        print(f"idling {args.idle_s}s so serverless workers go cold ...", flush=True)
        await asyncio.sleep(args.idle_s)
    try:
        for i, pcm in enumerate(clips):
            clients.turn_idx = i
            ms = await one_turn(conv, pcm)
            if ms.get("error"):
                errors[ms["error"]] = errors.get(ms["error"], 0) + 1
            rows.append(derive(ms))
            print(f"turn {i + 1:>2}: v2v(post-endpoint)={rows[-1].get('post_endpoint_v2v')} ms  stt={rows[-1].get('stt')}  "
                  f"llm={rows[-1].get('llm_first_token')}  tts1={rows[-1].get('tts_first_chunk')}  err={ms.get('error')}  "
                  f"est_spend=${ledger.spent():.4f}", flush=True)
            if clients.halted:
                print(f"HALTED: {clients.halted}", flush=True)
                break
    finally:
        await clients.aclose()
    calls = [{k: v for k, v in c.items() if k != "t_submit"} for c in clients.calls]
    keys = sorted({k for r in rows for k in r})
    warm_calls = [c for c in calls if c["turn"] is not None and c["turn"] >= 1]
    out = {"started_at": datetime.now(timezone.utc).isoformat(), "tag": args.tag, "path": args.path, "turns": len(rows), "errors": errors,
           "clip_s": args.clip_s, "idle_s": args.idle_s, "halted": clients.halted,
           "endpoints": {svc: clients.endpoint(svc).url.rsplit("/", 1)[-1] for svc in eb.SERVICES},
           "all": {k: summary([r[k] for r in rows if k in r]) for k in keys},
           "first_turn": rows[0] if rows else {},
           "warm_turns_2plus": {k: summary([r[k] for r in rows[1:] if k in r]) for k in keys},
           "stage_split_all": stage_split(calls), "stage_split_turns_2plus": stage_split(warm_calls),
           "cost": ledger.report(), "calls": calls, "per_turn": rows,
           "note": "post_endpoint_v2v excludes the end-of-speech silence window and the Plivo hop; see the script docstring"}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tenant", default="mme-test")
    ap.add_argument("--wav", default="Utkarsh_voice.wav")
    ap.add_argument("--turns", type=int, default=20)
    ap.add_argument("--clip-s", type=float, default=4.0)
    ap.add_argument("--idle-s", type=float, default=0.0, help="sleep this long before turn 1 (first-turn-after-idle run)")
    ap.add_argument("--path", choices=("legacy",), default="legacy", help="pipeline under test ('realtime' is added with the realtime pipeline)")
    ap.add_argument("--tag", default="")
    ap.add_argument("--out")
    ap.add_argument("--yes", action="store_true", help="required: this makes chargeable RunPod calls")
    ap.add_argument("--budget", type=float, required=True, help="hard cap in USD (estimated from delayTime + executionTime + idle tails); the run stops there")
    ap.add_argument("--idle", type=lambda v: eb.kv(v), required=True, help="idle timeouts in seconds, e.g. stt=5,brain=5,tts=5")
    ap.add_argument("--price", type=lambda v: eb.kv(v), default={}, help="USD per second per service, e.g. stt=0.000192 (brain/tts default to the operator-given values)")
    ap.add_argument("--cold-reserve-s", type=float, default=60.0, help="seconds of billing reserved before each call for an unknown cold start")
    args = ap.parse_args(argv)
    args.price = {**eb.DEFAULT_PRICES, **args.price}
    for name in ("idle", "price"):
        if set(getattr(args, name)) != set(eb.SERVICES):
            print(f"--{name} must give all of {eb.SERVICES}; got {sorted(getattr(args, name))}")
            return 2
    if not args.yes:
        print(f"refusing to run without --yes: {args.turns} turns = roughly {args.turns * 4}-{args.turns * 5} chargeable RunPod calls "
              f"(STT + Brain + several TTS per turn). Nothing was sent.")
        return 2
    res = asyncio.run(run(args))
    text = json.dumps(res, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
    print(json.dumps({k: res[k] for k in ("turns", "errors", "halted", "stage_split_all", "stage_split_turns_2plus", "cost")}, indent=2))
    return 1 if (res["errors"] or res["halted"]) else 0


if __name__ == "__main__":
    sys.exit(main())
