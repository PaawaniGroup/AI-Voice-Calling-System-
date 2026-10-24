"""End-to-end voice-pipeline latency benchmark against the REAL RunPod Serverless endpoints (never mocks). CHARGEABLE.

    export RUNPOD_STT_KEY=... RUNPOD_BRAIN_KEY=... RUNPOD_TTS_KEY=...      # keys come ONLY from the environment
    python scripts/e2e_latency_bench.py --scenario s1 --clips-dir caller_clips \
        --idle stt=5,brain=5,tts=5 --price stt=<usd/s> --yes --min-workers-confirmed

Scenarios (each = one conversation of >= 10 turns through the app's real `Conversation.stream_turn`: STT -> Brain -> TTS per clause):
  s1  cold call   : wait past every idle timeout, confirm 0 workers via /health, then converse; two long mid-call pauses
  s2  warm call   : run right after s1; same gap pattern WITHOUT the long pauses
  s3  early wakeup: wait to zero again, fire a tiny job at all three endpoints when the call "starts", then converse as s1

Safety: refuses to send anything without --yes AND --min-workers-confirmed. Estimated spend (delayTime + executionTime + idle timeout,
times the price per second) is tracked live and the run halts when it reaches --budget. 3 consecutive errors on an endpoint halt the run
(no retry loops). Jobs whose id is known are cancelled on abort. Output holds timings, counts and request ids only: no audio, no
transcripts, no keys. Offline there is no VAD / Plivo hop: latency is measured from the instant the (silence-trimmed) caller clip is
handed to STT to the first TTS audio returned, exactly like scripts/live_latency.py.
"""
from __future__ import annotations

import argparse
import asyncio
import audioop
import contextvars
import json
import logging
import os
import random
import sys
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ai import turn_trace  # noqa: E402
from app.ai.clients import AiUnavailable, ModelClients  # noqa: E402
from app.ai.pipeline import Conversation  # noqa: E402
from config.settings import Settings  # noqa: E402

RATE = 16000
SERVICES = ("stt", "brain", "tts")
KEY_ENV = {"stt": "RUNPOD_STT_KEY", "brain": "RUNPOD_BRAIN_KEY", "tts": "RUNPOD_TTS_KEY"}
COLD_DELAY_MS = 5000.0
DEFAULT_PRICES = {"brain": 0.000486, "tts": 0.000192}   # given by the operator (48 GB / 24 GB); STT (24 GB Pro) must be supplied
_cur: contextvars.ContextVar = contextvars.ContextVar("bench_call", default=None)


class StopBench(Exception):
    pass


def pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 1)


def kv(text: str, cast=float) -> dict:
    out = {}
    for part in text.split(","):
        k, _, v = part.partition("=")
        out[k.strip()] = cast(v)
    return out


# ------------------------------------------------------------------------------------------------ spend ledger
class Ledger:
    """Estimated spend = (delay + exec) of every job + the idle tail each endpoint keeps billing after its last job."""

    def __init__(self, prices: dict, idle: dict, budget: float, cold_reserve_s: float):
        self.price, self.idle, self.budget, self.cold_reserve_s = prices, idle, budget, cold_reserve_s
        self.job_s = {s: 0.0 for s in SERVICES}
        self.idle_s = {s: 0.0 for s in SERVICES}
        self.last_end: dict = {}
        self.used: set = set()

    def spent(self) -> float:
        return sum((self.job_s[s] + self.idle_s[s]) * self.price[s] for s in SERVICES)

    def tails(self, extra: str | None = None) -> float:
        return sum(self.idle[s] * self.price[s] for s in (self.used | ({extra} if extra else set())))

    def projected(self, svc: str) -> float:
        return self.spent() + self.tails(svc) + self.cold_reserve_s * self.price[svc]

    def before(self, svc: str) -> None:
        if self.projected(svc) >= self.budget:
            raise StopBench(f"budget cap ${self.budget:.2f} reached (estimated ${self.spent():.4f} spent + idle tails + cold-start reserve)")

    def begin(self, svc: str, now: float) -> None:
        if svc in self.last_end:
            self.idle_s[svc] += min(max(0.0, now - self.last_end[svc]), self.idle[svc])
        self.used.add(svc)

    def end(self, svc: str, now: float, billed_s: float) -> None:
        self.job_s[svc] += billed_s
        self.last_end[svc] = now

    def report(self) -> dict:
        per = {s: {"job_s": round(self.job_s[s], 2), "idle_s": round(self.idle_s[s], 2),
                   "tail_s": self.idle[s] if s in self.used else 0.0,
                   "usd": round((self.job_s[s] + self.idle_s[s] + (self.idle[s] if s in self.used else 0.0)) * self.price[s], 5)} for s in SERVICES}
        return {"per_service": per, "usd_estimated_total": round(sum(v["usd"] for v in per.values()), 5), "budget_usd": self.budget,
                "basis": "estimate from RunPod delayTime+executionTime, idle gaps (capped at the idle timeout) and one idle tail per used endpoint; NOT an invoice"}


# ------------------------------------------------------------------------------------------------ instrumented clients
class BenchClients(ModelClients):
    def __init__(self, s, cfg, ledger: Ledger, start_zero: bool, transport=None):
        super().__init__(s, cfg, transport)
        self.ledger, self.start_zero = ledger, start_zero
        self.calls: list = []
        self.consec = {x: 0 for x in SERVICES}
        self.halted: str | None = None
        self.turn_idx: int | None = None
        self.role = "turn"
        self.inflight: dict = {}   # request id -> service (jobs whose id is known; used to cancel on abort)

    async def _runpod_wait(self, service, ep, hdrs, j):
        rec = _cur.get()
        if isinstance(j, dict) and j.get("id"):
            self.inflight[j["id"]] = service
            if rec is not None:
                rec["request_id"] = j["id"]
        res = await super()._runpod_wait(service, ep, hdrs, j)
        if isinstance(res, dict):
            if res.get("id"):
                self.inflight.pop(res["id"], None)
            if rec is not None:
                rec["request_id"] = res.get("id") or rec["request_id"]
                for src, dst in (("delayTime", "delay_ms"), ("executionTime", "exec_ms")):
                    if isinstance(res.get(src), (int, float)):
                        rec[dst] = float(res[src])
                out = res.get("output")
                out = out[0] if isinstance(out, list) and out else out
                usage = out.get("usage") if isinstance(out, dict) else None
                if isinstance(usage, dict):
                    rec["prompt_tokens"], rec["completion_tokens"] = usage.get("prompt_tokens"), usage.get("completion_tokens")
        return res

    async def _timed(self, svc: str, make):
        if self.halted:
            raise AiUnavailable(svc, "halted", self.halted)
        try:
            self.ledger.before(svc)
        except StopBench as e:
            self.halted = str(e)
            raise AiUnavailable(svc, "budget", str(e)) from None
        rec = {"service": svc, "turn": self.turn_idx, "role": self.role, "request_id": None, "delay_ms": None, "exec_ms": None,
               "prompt_tokens": None, "completion_tokens": None, "error": None, "t_submit": time.monotonic()}
        gap_ok = svc in self.ledger.last_end and (rec["t_submit"] - self.ledger.last_end[svc]) <= self.ledger.idle[svc]
        first_zero = (svc not in self.ledger.last_end and self.start_zero) or (svc in self.ledger.last_end and not gap_ok)
        self.ledger.begin(svc, rec["t_submit"])
        tok = _cur.set(rec)
        try:
            out = await make()
            self.consec[svc] = 0
            return out
        except asyncio.CancelledError:
            rec["error"] = "cancelled"
            raise
        except BaseException as e:
            rec["error"] = f"{type(e).__name__}:{getattr(e, 'kind', '')}"
            self.consec[svc] += 1
            raise
        finally:
            t1 = time.monotonic()
            rec["client_ms"] = round((t1 - rec["t_submit"]) * 1000, 1)
            rec["cold_delay"] = bool(rec["delay_ms"] is not None and rec["delay_ms"] > COLD_DELAY_MS)
            rec["cold_inferred"] = first_zero
            rec["cold"] = rec["cold_delay"] or first_zero
            billed = ((rec["delay_ms"] or 0) + (rec["exec_ms"] or 0)) / 1000 if rec["delay_ms"] is not None else rec["client_ms"] / 1000
            self.ledger.end(svc, t1, billed)
            _cur.reset(tok)
            self.calls.append(rec)
            if self.consec[svc] >= 3 and not self.halted:
                self.halted = f"3 consecutive errors on {svc}: stopped, no retries"

    async def transcribe(self, pcm, rate, language):
        return await self._timed("stt", lambda: ModelClients.transcribe(self, pcm, rate, language))

    async def chat(self, messages, tools=None, temperature=None):
        return await self._timed("brain", lambda: ModelClients.chat(self, messages, tools, temperature))

    async def synthesize(self, text, language):
        return await self._timed("tts", lambda: ModelClients.synthesize(self, text, language))

    async def cancel_inflight(self) -> list:
        done = []
        for rid, svc in list(self.inflight.items()):
            try:
                ep = self.endpoint(svc)
                r = await self._http.post(f"{ep.url}/cancel/{rid}", headers=self._auth_headers(svc), timeout=15)
                done.append({"service": svc, "request_id": rid, "http": r.status_code})
            except Exception as e:  # reported, never swallowed silently
                done.append({"service": svc, "request_id": rid, "error": type(e).__name__})
        return done

    async def worker_counts(self) -> dict:
        out = {}
        for svc in SERVICES:
            ep = self.endpoint(svc)
            r = await self._http.get(f"{ep.url}/health", headers=self._auth_headers(svc), timeout=30)
            j = r.json() if r.status_code < 400 else {}
            w = j.get("workers", {}) if isinstance(j, dict) else {}
            out[svc] = {**{k: w.get(k) for k in ("idle", "ready", "running", "initializing", "throttled", "unhealthy")},
                        "jobs": j.get("jobs") if isinstance(j, dict) else None, "http": r.status_code}
        return out


def alive(counts: dict) -> int:
    return sum(int(counts[s].get(k) or 0) for s in SERVICES for k in ("idle", "ready", "running", "initializing"))


# ------------------------------------------------------------------------------------------------ audio / config
def load_clips(directory: str) -> list[dict]:
    d = Path(directory)
    manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    clips = []
    for m in manifest:
        with wave.open(str(d / m["file"]), "rb") as w:
            ch, width, rate, frames = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.readframes(w.getnframes())
        if width != 2:
            raise SystemExit(f"{m['file']}: must be 16-bit PCM")
        if ch == 2:
            frames = audioop.tomono(frames, 2, 0.5, 0.5)
        if rate != RATE:
            frames = audioop.ratecv(frames, 2, 1, rate, RATE, None)[0]
        clips.append({**m, "pcm": trim_trailing_silence(frames)})
    return clips


def trim_trailing_silence(pcm: bytes) -> bytes:
    """Cut trailing non-voice so the clip ends at the last voiced sample (20 ms windows, < 5 % of the loudest window = silence)."""
    win = RATE // 50 * 2
    wins = [pcm[i:i + win] for i in range(0, len(pcm), win)]
    levels = [audioop.rms(w, 2) if w else 0 for w in wins]
    thr = max(levels, default=0) * 0.05
    last = max((i for i, v in enumerate(levels) if v > thr), default=len(wins) - 1)
    return pcm[:(last + 1) * win]


def load_cfg(s: Settings, tenant: str) -> dict:
    from pymongo import MongoClient
    c = MongoClient(s.mongo_uri.get_secret_value(), serverSelectionTimeoutMS=3000)
    try:
        doc = c[s.mongo_db]["ai_config"].find_one({"tenant_id": tenant})
    finally:
        c.close()
    if not doc:
        raise SystemExit(f"no ai_config for tenant {tenant!r}")
    cfg = {k: v for k, v in doc.items() if k != "_id"}
    missing = [v for v in KEY_ENV.values() if not os.environ.get(v)]
    if missing:
        raise SystemExit(f"missing environment variables: {', '.join(missing)} (keys are read only from the environment)")
    cfg["keys"] = {svc: os.environ[KEY_ENV[svc]] for svc in SERVICES}   # env wins over whatever is stored in the DB
    for svc in SERVICES:
        cfg.pop(f"{svc}_api_key", None)
    cfg["timeouts"] = {**(cfg.get("timeouts") or {}), **{svc: 900.0 for svc in SERVICES}}   # 15 min: cold starts must never abort silently
    return cfg


# ------------------------------------------------------------------------------------------------ one turn
async def run_turn(conv: Conversation, clients: BenchClients, clip: dict, idx: int, gap_before: float | None) -> dict:
    clients.turn_idx, clients.role = idx, "turn"
    n0 = len(clients.calls)
    tr = turn_trace.TurnTrace(t_speech_end=time.monotonic())
    turn_trace.start(tr)
    t0 = time.monotonic()
    arrivals, err, first_timings = [], None, {}
    async for ch in conv.stream_turn(clip["pcm"], RATE):
        if ch.error:
            err = ch.error
            break
        if ch.timings_ms and not first_timings:
            first_timings = dict(ch.timings_ms)
        if ch.audio:
            arrivals.append((time.monotonic(), len(ch.audio) / 2 / ch.sample_rate))
    calls = [c for c in clients.calls[n0:] if c["turn"] == idx and c["role"] == "turn"]
    by = lambda svc: [c for c in calls if c["service"] == svc]   # noqa: E731
    play_end, gaps = None, []
    for t, dur in arrivals:   # simulated playback: a clause arriving after the previous one finished = a gap in speech
        if play_end is None or t >= play_end:
            if play_end is not None:
                gaps.append(round(t - play_end, 3))
            play_end = t + dur
        else:
            play_end += dur
    tts = by("tts")
    audio_s = sum(d for _, d in arrivals)
    gen_s = sum((c["exec_ms"] or 0) for c in tts) / 1000
    brain = by("brain")
    row = {"turn": idx, "language": clip.get("lang"), "kind": "retrieval" if clip.get("needs_retrieval") else "plain",
           "clip_reused": bool(clip.get("_reused")), "gap_before_s": gap_before, "error": err,
           "latency_ms": round((arrivals[0][0] - t0) * 1000, 1) if arrivals else None,
           "turn_total_ms": round((time.monotonic() - t0) * 1000, 1),
           "clauses": len(arrivals), "audio_s": round(audio_s, 2), "tts_gen_s": round(gen_s, 2),
           "tts_rtf": round(gen_s / audio_s, 3) if audio_s else None,
           "tts_rtf_per_clause": [round(((c["exec_ms"] or 0) / 1000) / d, 3) for c, (_, d) in zip(tts, arrivals) if d],
           "speech_gaps_s": gaps, "brain_prompt_tokens": brain[0]["prompt_tokens"] if brain else None,
           "brain_completion_tokens": brain[0]["completion_tokens"] if brain else None,
           "rag_ms": first_timings.get("rag"), "calls": [{k: v for k, v in c.items() if k != "t_submit"} for c in calls],
           "stage_ms": {k: v for k, v in tr.to_ms().items() if k.startswith("t_")}}
    return row


# ------------------------------------------------------------------------------------------------ scenario
async def wait_for_zero(clients: BenchClients, idle: dict, skip_wait: bool) -> dict:
    longest = max(idle.values())
    snaps = {"before_wait": await clients.worker_counts()}
    if not skip_wait:
        print(f"waiting {longest + 15:.0f}s (longest idle timeout {longest:.0f}s + 15s margin) so every endpoint scales to zero ...", flush=True)
        left = longest + 15
        while left > 0:
            await asyncio.sleep(min(60, left))
            left -= 60
    deadline = time.monotonic() + 2 * longest + 60
    while True:
        snaps["after_wait"] = await clients.worker_counts()
        print("health after wait:", json.dumps({s: {k: v for k, v in snaps["after_wait"][s].items() if k in ("idle", "ready", "running", "initializing")} for s in SERVICES}), flush=True)
        if alive(snaps["after_wait"]) == 0:
            return snaps
        if time.monotonic() > deadline:
            raise StopBench("endpoints did not reach 0 workers: min workers is probably not 0 (see after_wait counts)")
        await asyncio.sleep(15)


async def ping(clients: BenchClients, svc: str) -> None:
    clients.role = "warmup"
    if svc == "stt":
        await clients.transcribe(b"\x00\x00" * (RATE // 2), RATE, "auto")   # 0.5 s of digital silence: wake-up only, not a measurement
    elif svc == "brain":
        old = clients.cfg.get("max_tokens")
        clients.cfg["max_tokens"] = 1
        try:
            await clients.chat([{"role": "user", "content": "hi"}])
        finally:
            clients.cfg["max_tokens"] = old
    else:
        await clients.synthesize("हाँ।", "hi")


async def run_scenario(args, clients: BenchClients, clips: list[dict], s: Settings, cfg: dict) -> dict:
    rng = random.Random(args.seed)
    idle_max = max(args.idle.values())
    long_turns = set() if args.scenario == "s2" else set(args.long_pause_turns)
    gaps = [None] + [(idle_max + 10.0) if i in long_turns else round(rng.uniform(4, 12), 2) for i in range(1, args.turns)]
    out: dict = {"gap_plan_s": gaps, "health": {}}
    pings: list = []
    if args.scenario in ("s1", "s3"):
        out["health"] = await wait_for_zero(clients, args.idle, args.skip_idle_wait)
    if args.scenario == "s3":
        t_call = time.monotonic()
        pings = [asyncio.create_task(ping(clients, svc)) for svc in SERVICES]   # fired the moment the call starts
        print(f"early wake-up pings sent; caller starts speaking in {args.first_speech_delay_s}s", flush=True)
        await asyncio.sleep(args.first_speech_delay_s)
        out["ping_to_first_speech_s"] = round(time.monotonic() - t_call, 2)
    conv = Conversation(clients, cfg.get("default_language", "auto"), script_enforcement=s.ai_script_enforcement,
                        counsellor_gender=s.ai_counsellor_gender, counsellor_name=s.ai_counsellor_name)
    rows = []
    try:
        for i in range(args.turns):
            if i > 0:
                print(f"  caller pause {gaps[i]}s", flush=True)
                await asyncio.sleep(gaps[i])
            row = await run_turn(conv, clients, clips[i], i, gaps[i])
            rows.append(row)
            print(f"turn {i + 1:>2}: latency={row['latency_ms']} ms clauses={row['clauses']} prompt_tok={row['brain_prompt_tokens']} "
                  f"cold={[c['service'] for c in row['calls'] if c['cold']]} err={row['error']} est_spend=${clients.ledger.spent():.4f}", flush=True)
            if clients.halted:
                break
    finally:
        if pings:
            await asyncio.gather(*pings, return_exceptions=True)
    out["turns"] = rows
    out["warmup_calls"] = [{k: v for k, v in c.items() if k != "t_submit"} for c in clients.calls if c["role"] == "warmup"]
    return out


async def run(args) -> dict:
    logging.getLogger().setLevel(logging.WARNING)
    s = Settings()
    cfg = load_cfg(s, args.tenant)
    clips = load_clips(args.clips_dir)
    skipped = []
    if not cfg.get("rag_url"):
        skipped = [c.get("label", c["file"]) for c in clips if c.get("needs_retrieval")]
        clips = [c for c in clips if not c.get("needs_retrieval")]
    if not clips:
        raise SystemExit("no usable clips")
    seq = []
    for i in range(args.turns):
        c = dict(clips[i % len(clips)])
        c["_reused"] = i >= len(clips)
        seq.append(c)
    ledger = Ledger(args.price, args.idle, args.budget, args.cold_reserve_s)
    clients = BenchClients(s, cfg, ledger, start_zero=args.scenario in ("s1", "s3"))
    started = datetime.now(timezone.utc)
    result: dict = {"scenario": args.scenario, "started_at": started.isoformat(), "turns_planned": args.turns, "seed": args.seed,
                    "endpoints": {svc: clients.endpoint(svc).url.rsplit("/", 1)[-1] for svc in SERVICES},
                    "idle_timeout_s": args.idle, "price_per_s": args.price, "gpu": {"stt": "24 GB Pro", "brain": "48 GB", "tts": "24 GB"},
                    "pipeline": {"script_enforcement": s.ai_script_enforcement, "tts_normalize": s.ai_tts_normalize,
                                 "brain_streaming": "NO: /runsync returns the whole reply; clauses are split after it arrives"},
                    "retrieval": {"rag_url_set": bool(cfg.get("rag_url")), "skipped_clips": skipped,
                                  "tools": "none registered in the app (Conversation.tools is empty), so no tool turns run"},
                    "caller_audio": {"source": args.audio_source, "stt_numbers_optimistic": args.audio_source != "recorded"},
                    "clips_reused": sum(1 for c in seq if c["_reused"]), "halted": None, "cancelled_jobs": []}
    try:
        result.update(await run_scenario(args, clients, seq, s, cfg))
    except StopBench as e:
        result["halted"] = str(e)
    except (asyncio.CancelledError, KeyboardInterrupt):
        result["halted"] = "aborted by operator"
        result["cancelled_jobs"] = await clients.cancel_inflight()
        raise
    finally:
        result["halted"] = result["halted"] or clients.halted
        result["calls"] = [{k: v for k, v in c.items() if k != "t_submit"} for c in clients.calls]
        result["cost"] = ledger.report()
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        await clients.aclose()
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def summarize(res: dict) -> None:
    rows = [r for r in res.get("turns", []) if r["latency_ms"] is not None]
    warm = [r["latency_ms"] for r in rows[1:]]
    print("\n=== summary (details: output JSON; full report: scripts/e2e_latency_report.py) ===")
    print("halted:", res.get("halted"))
    if rows:
        print(f"first turn: {rows[0]['latency_ms']} ms | turns 2-N: p50={pct(warm, 50)} p95={pct(warm, 95)} max={max(warm) if warm else None} ms")
    print("estimated spend: $", res["cost"]["usd_estimated_total"], "of cap $", res["cost"]["budget_usd"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", choices=("s1", "s2", "s3"), required=True)
    ap.add_argument("--tenant", default="mme-test")
    ap.add_argument("--clips-dir", required=True, help="dir with manifest.json: [{file, lang, needs_retrieval, label}] + 16-bit mono wavs")
    ap.add_argument("--audio-source", choices=("recorded", "synthesized"), default="recorded")
    ap.add_argument("--idle", type=lambda v: kv(v), required=True, help="idle timeouts in seconds, e.g. stt=5,brain=5,tts=5")
    ap.add_argument("--price", type=lambda v: kv(v), default={}, help="USD per second, e.g. stt=0.000192 (brain/tts default to the operator-given values)")
    ap.add_argument("--budget", type=float, default=1.50, help="hard cap in USD (estimated)")
    ap.add_argument("--cold-reserve-s", type=float, default=60.0, help="seconds of billing reserved before each call for an unknown cold start")
    ap.add_argument("--turns", type=int, default=10)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--long-pause-turns", type=lambda v: [int(x) for x in v.split(",")], default=[4, 8],
                    help="0-based turns preceded by a pause of (longest idle timeout + 10 s); ignored for s2")
    ap.add_argument("--first-speech-delay-s", type=float, default=3.0, help="s3: seconds between the wake-up pings and the caller's first words")
    ap.add_argument("--skip-idle-wait", action="store_true", help="only if you already waited past every idle timeout (health is still checked)")
    ap.add_argument("--out")
    ap.add_argument("--yes", action="store_true", help="required: this makes chargeable RunPod calls")
    ap.add_argument("--min-workers-confirmed", action="store_true", help="required: you confirmed min workers = 0 on all three endpoints")
    args = ap.parse_args(argv)
    args.price = {**DEFAULT_PRICES, **args.price}
    for name in ("idle", "price"):
        if set(getattr(args, name)) != set(SERVICES):
            print(f"--{name} must give all of {SERVICES}; got {sorted(getattr(args, name))}")
            return 2
    if args.turns < 10:
        print("--turns must be >= 10")
        return 2
    args.out = args.out or f"docs/evidence/e2e_latency_{args.scenario}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    if not (args.yes and args.min_workers_confirmed):
        print(f"refusing to send anything: scenario {args.scenario}, {args.turns} turns (~{args.turns * 4} RunPod jobs), budget cap ${args.budget:.2f}.\n"
              "Re-run with --yes --min-workers-confirmed after you have confirmed min workers = 0 on all three endpoints.")
        return 2
    res = asyncio.run(run(args))
    summarize(res)
    return 1 if res.get("halted") else 0


if __name__ == "__main__":
    sys.exit(main())
