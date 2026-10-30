"""Latency regression harness: caller audio -> STT -> Brain -> voice -> playout, stage by stage, numbers-only evidence.

TWO modes, never mixed up in the evidence (`tag`):

  SIMULATED (default, free, offline)  the REAL streaming code (segmenter, StreamingTurn, playout, ramp) runs on a VIRTUAL clock against mock models whose timing
        models are parameters (defaults = the MEASURED medians of the last supervised call and of the 26-27 Oct voice tests). It tests the orchestration (clause plan,
        prefetch, queueing, gaps, pre-buffer) and any change of it; it says NOTHING about the models' real speed. Every number is tagged SIMULATED.
  REAL (--real; chargeable, needs --approved "<id>" and a cap)  the same code against the tenant's real endpoints, one recording per utterance, with the stage timers of
        tools/realtime/stages.py on the real clock and the RunPod job records. Stops at the cost cap, at the first HTTP 401/403 and after 3 consecutive errors.
        Recordings stay in a folder OUTSIDE every repository; only numbers and file indexes are written (never a word, a name, audio or a key). NOT RUN yet.

  python tools/realtime/harness.py --scenario ramp --workers 2 --n 12 --evidence out.json
  python tools/realtime/harness.py --real --recordings <folder> --approved "<id>" --cap-usd 0.30 --evidence out.json
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import random
import sys
import time
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ai.pipeline import Conversation                       # noqa: E402
from app.ai.streaming.config import stream_config_from         # noqa: E402
from app.ai.streaming.live import run_streaming_turn           # noqa: E402
from tools.realtime import stages as S                         # noqa: E402
from tools.sim_ramp_gap import BASE_QUEUE_S, audio, wall       # noqa: E402


@dataclass
class Scenario:
    """The knobs of a simulated turn. Defaults: MEASURED medians (last supervised call, warm turns 1-6; voice tests of 26-27 Oct)."""
    reply_words: int = 23                 # the length of the Brain's reply
    first_punct_at: int | None = 9        # word index of the first comma in the reply (MEASURED median first clause: 9 words); the ramp opener rule makes it 3
    later_punct_every: int = 8            # a comma every N words after that (None = none until the end)
    ramp: bool = False                    # tenant flag stream_ramp (clause plan 2-4 / 6-8 / 12-14 words)
    voice_workers: int = 1                # warm voice workers
    voice_queue_s: float = 1.2            # the queue of one voice job (MEASURED warm 1.16-1.25 s; back-to-back 0.15 s)
    cold_second_worker_s: float | None = None   # the second worker needs this long before its first job (cold start: 41-52 s MEASURED); None = warm
    stt_queue_exec_remainder_ms: tuple = (139.0, 612.0, 695.0)   # MEASURED medians of warm turns 1-6
    brain_ttft_ms: float = 737.0          # MEASURED median, request sent -> first token
    brain_chars_per_s: float = 210.0      # ESTIMATE: 9 words in the 244 ms between first token and first clause
    eot_ms: float = 700.0                 # the silence wait (an input; the replay of tools/eot_replay.py gives it per utterance)
    prefetch_depth: int = 2
    prebuffer_ms: int = 150
    jitter_pct: float = 0.0               # +-% multiplicative jitter of STT, Brain and voice times, seeded
    seed: int = 1
    clause_plan: str = field(default="", init=False)

    def __post_init__(self) -> None:
        self.clause_plan = "ramp" if self.ramp else "today"


def scenario(name: str, **over) -> Scenario:
    """Named presets: 'today' (9-word first clause) and 'ramp' (the Brain opens with a 3-word phrase, the ramp plan is on)."""
    base = {"today": {}, "ramp": {"ramp": True, "first_punct_at": 3}}[name]
    return Scenario(**{**base, **over})


def reply_text(sc: Scenario) -> str:
    """A synthetic reply of `reply_words` neutral placeholder words with the configured comma positions (no real words are ever used)."""
    words = [f"w{i}" for i in range(1, sc.reply_words + 1)]
    marks = set()
    if sc.first_punct_at:
        marks.add(sc.first_punct_at)
        if sc.later_punct_every:
            marks.update(range(sc.first_punct_at + sc.later_punct_every, sc.reply_words, sc.later_punct_every))
    out = []
    for i, w in enumerate(words, 1):
        out.append(w + ("," if i in marks and i < sc.reply_words else "") + ("." if i == sc.reply_words else ""))
    return " ".join(out)


class SimClients:
    """Mock models on the harness clock. Voice: a pool of workers, each job waits for the earliest free worker, runs for queue + (measured wall time of its length)."""

    def __init__(self, clock, sc: Scenario, rng: random.Random):
        self.clock, self.sc, self.rng, self.cfg = clock, sc, rng, {}
        self.free = [0.0] + [sc.cold_second_worker_s if sc.cold_second_worker_s is not None else 0.0] * (sc.voice_workers - 1)
        self.t0 = clock.now()                                          # worker free times are relative to the start of the run

    def _j(self, x: float) -> float:
        return x * (1.0 + self.rng.uniform(-self.sc.jitter_pct, self.sc.jitter_pct) / 100.0) if self.sc.jitter_pct else x

    async def transcribe(self, pcm: bytes, rate: int, language: str) -> dict:
        await self.clock.sleep(self._j(sum(self.sc.stt_queue_exec_remainder_ms)) / 1000.0)
        return {"text": "placeholder utterance", "language": "hi"}

    async def chat_stream(self, messages, tools=None):
        await self.clock.sleep(self._j(self.sc.brain_ttft_ms) / 1000.0)
        text = reply_text(self.sc)
        for i in range(0, len(text), 3):
            await self.clock.sleep(3 / self.sc.brain_chars_per_s)
            yield {"delta": text[i:i + 3]}
        yield {"done": True}

    async def synthesize(self, text: str, language: str):
        words = len(text.split())
        dur = self._j(self.sc.voice_queue_s + (wall(words) - BASE_QUEUE_S))
        now = self.clock.now() - self.t0
        i = min(range(len(self.free)), key=lambda k: self.free[k])
        start = max(now, self.free[i])
        self.free[i] = start + dur
        await self.clock.sleep(self.free[i] - now)
        return b"\x00\x00" * int(audio(words) * 8000), 8000


def _conversation(rc, sc: Scenario | None, cfg_doc: dict | None = None) -> Conversation:
    cfg = stream_config_from({"streaming_pipeline": True, **(cfg_doc or {}), **({"stream_ramp": sc.ramp} if sc else {})})
    if sc is not None:
        cfg.prefetch_depth = sc.prefetch_depth
        cfg.playout = dataclasses.replace(cfg.playout, prebuffer_ms=sc.prebuffer_ms)
    d = cfg_doc or {}
    conv = Conversation(clients=rc, language="hi", streaming=cfg, devanagari_hindi=bool(d.get("devanagari_hindi")),
                        glossary_correction=bool(d.get("glossary_correction")), strict_grounding=bool(d.get("strict_grounding")))
    if (sc.ramp if sc is not None else d.get("stream_ramp")):
        from app.ai.streaming.ramp import apply_to_conversation     # the same optional Brain block the service appends for a tenant with stream_ramp
        apply_to_conversation(conv)
    return conv


async def run_one(clock, inner, sc: Scenario | None, eot_ms: float, pcm: bytes = b"", rate: int = 8000, cfg_doc: dict | None = None) -> dict:
    """ONE turn through the real streaming code; the stage numbers of the turn."""
    rec = S.StageRecorder(clock)
    conv = _conversation(S.RecordingClients(inner, rec), sc, cfg_doc)
    sink = S.TimedSink(clock)
    res = await run_streaming_turn(conv, pcm, rate, sink=sink, gen=1, clock=clock)
    if res.outcome is None:
        return {"error": res.error or "no_outcome"}
    return S.derive_stages(rec, res.outcome.stream, sink, eot_ms)


def run_simulated(sc: Scenario, n: int = 12) -> dict:
    """n simulated utterances on a virtual clock; the evidence record (tag SIMULATED)."""
    from tests.fakeclock import FakeClock

    async def go() -> list[dict]:
        utts = []
        for i in range(n):
            clock = FakeClock()
            inner = SimClients(clock, sc, random.Random(sc.seed * 1000 + i))
            utts.append(await clock.run(run_one(clock, inner, sc, sc.eot_ms)))
        return utts
    utts = asyncio.run(go())
    return evidence("SIMULATED", asdict(sc), utts)


def evidence(tag: str, scenario_doc: dict, utts: list[dict]) -> dict:
    ok = [u for u in utts if "error" not in u]
    return {"schema": 1, "tag": tag, "scenario": scenario_doc, "n_utterances": len(utts), "n_errors": len(utts) - len(ok),
            "summary": S.summarize(ok), "utterances": utts}


# ------------------------------------------------------------------ REAL mode (chargeable; refuses without approval; NOT RUN by the assistant yet)
def refuse_reason(args, repo_root: Path = ROOT) -> str | None:
    if not (args.approved or "").strip():
        return "refused: no --approved id (a real run creates billed STT, Brain and voice jobs)"
    if not args.recordings:
        return "refused: --recordings <folder> is required (the operator's own lines, outside every repository)"
    rp = Path(args.recordings).resolve()
    if repo_root.resolve() in rp.parents or rp == repo_root.resolve():
        return "refused: the recordings folder must be OUTSIDE the repository"
    if args.cap_usd <= 0:
        return "refused: --cap-usd must be positive"
    return None


def read_wav_8k(path: Path) -> bytes:
    """mono 16-bit PCM at 8 kHz (resampled if the file has another rate). Nothing about the content is read or kept beyond the samples."""
    from app.ai import codec
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError("a recording must be mono 16-bit PCM")
        rate, pcm = w.getframerate(), w.readframes(w.getnframes())
    return pcm if rate == 8000 else codec.resample_pcm16(pcm, rate, 8000)


async def run_real(args) -> int:
    from pymongo import AsyncMongoClient
    from app.ai import turn_trace
    from app.ai.clients import AiError, ModelClients
    from app.ai.streaming.clock import RealClock
    from config.settings import Settings
    files = sorted(Path(args.recordings).glob("*.wav"))
    if not files:
        print("refused: no .wav files in the recordings folder")
        return 2
    doc = await AsyncMongoClient(args.mongo, serverSelectionTimeoutMS=3000)["mme_telephony"]["ai_config"].find_one({"tenant_id": args.tenant}) or {}
    cfg = {k: doc.get(k) for k in ("stt_url", "brain_url", "tts_url", "rag_url", "models", "timeouts", "paths", "runpod_mode", "tts_ulaw8k", "tts_voice",
                                   "streaming_pipeline", "devanagari_hindi", "stream_ramp", "tts_adaptive_poll", "stt_raw_pcm", "glossary_correction", "strict_grounding")}
    cfg["key_refs"] = {"stt": args.stt_key_env, "brain": args.brain_key_env, "tts": args.tts_key_env}
    clients = ModelClients(Settings(_env_file=None), cfg)
    clock = RealClock()
    utts, consecutive, spent_s, stop = [], 0, 0.0, None
    try:
        for idx, f in enumerate(files):
            tr = turn_trace.TurnTrace(t_speech_end=time.monotonic())
            turn_trace.start(tr)
            try:
                u = await run_one(clock, clients, None, args.eot_ms, pcm=read_wav_8k(f), cfg_doc=cfg)
                consecutive = 0
            except AiError as e:
                u, consecutive = {"error": f"{e.kind}:{getattr(e, 'status_code', None)}"}, consecutive + 1
                if getattr(e, "status_code", None) in (401, 403):
                    stop = f"stopped at HTTP {e.status_code}"
            except Exception as e:
                u, consecutive = {"error": type(e).__name__}, consecutive + 1
            u["file_index"] = idx
            u["jobs"] = tr.evidence().get("jobs", [])
            spent_s += sum(((j.get("delay_ms") or 0) + (j.get("exec_ms") or 0)) / 1000.0 for j in u["jobs"])
            utts.append(u)
            if stop or consecutive >= 3:
                stop = stop or "stopped after 3 consecutive errors"
                break
            if spent_s * args.price_per_s >= args.cap_usd:
                stop = "stopped at the cost cap"
                break
    finally:
        await clients.aclose()
    ev = evidence("MEASURED", {"mode": "real", "clause_plan": "ramp" if cfg.get("stream_ramp") else "today", "voice_workers": args.voice_workers_declared,
                               "tenant_flags": {k: bool(cfg.get(k)) for k in ("stream_ramp", "tts_adaptive_poll", "stt_raw_pcm", "glossary_correction", "strict_grounding")},
                               "eot_ms": args.eot_ms, "stopped": stop, "estimated_gpu_seconds": round(spent_s, 1)}, utts)
    Path(args.evidence).write_text(json.dumps(ev, indent=1), encoding="utf-8")
    print(json.dumps({"tag": "MEASURED", "n_utterances": ev["n_utterances"], "n_errors": ev["n_errors"], "stopped": stop, "summary": ev["summary"]}))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--real", action="store_true")
    ap.add_argument("--scenario", choices=("today", "ramp"), default="today")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--queue-s", type=float, default=1.2)
    ap.add_argument("--reply-words", type=int, default=23)
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--jitter-pct", type=float, default=0.0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--evidence", default="")
    ap.add_argument("--recordings", default="")
    ap.add_argument("--approved", default="")
    ap.add_argument("--cap-usd", type=float, default=0.30)
    ap.add_argument("--price-per-s", type=float, default=0.0003)
    ap.add_argument("--eot-ms", type=float, default=700.0)
    ap.add_argument("--voice-workers-declared", type=int, default=None, help="the voice workers the operator has set in the console (a REAL run cannot see them; the gate needs it)")
    ap.add_argument("--tenant", default="mme-test")
    ap.add_argument("--mongo", default="mongodb://127.0.0.1:27117")
    ap.add_argument("--stt-key-env", default="RUNPOD_STT_KEY")
    ap.add_argument("--brain-key-env", default="RUNPOD_BRAIN_KEY")
    ap.add_argument("--tts-key-env", default="RUNPOD_TTS_KEY")
    a = ap.parse_args(argv)
    if a.real:
        why = refuse_reason(a)
        if why:
            print(why)
            return 2
        if not a.evidence:
            print("refused: --evidence <file> is required for a real run")
            return 2
        return asyncio.run(run_real(a))
    ev = run_simulated(scenario(a.scenario, voice_workers=a.workers, voice_queue_s=a.queue_s, reply_words=a.reply_words, jitter_pct=a.jitter_pct, seed=a.seed), a.n)
    if a.evidence:
        Path(a.evidence).write_text(json.dumps(ev, indent=1), encoding="utf-8")
    print(json.dumps({"tag": "SIMULATED", "scenario": ev["scenario"], "summary": ev["summary"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
