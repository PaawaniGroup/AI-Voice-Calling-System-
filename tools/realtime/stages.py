"""Stage timers of the real-time latency program.

Metric (operator, 27 Oct 2026): the time from the caller's LAST VOICED FRAME to the FIRST AUDIO FRAME sent to Plivo. Audibility at the ear is not measured.
Stages (all in milliseconds, all from one clock, so the same code times the virtual-time harness (SIMULATED) and a real run (MEASURED)):

  end_of_turn          last voiced frame -> the end-of-turn decision (the silence wait; an INPUT of the harness: 700 ms today, or the replay of tools/eot_replay.py)
  stt                  STT submit -> final text (queue + execution + remainder; the split comes from the RunPod job record when the run is REAL)
  brain_first_token    Brain request sent -> first token
  brain_first_clause   Brain request sent -> the first clause exists (before the script gate)
  voice_first_audio    first clause sent to the voice -> its audio is ready
  playout_start        audio ready -> first frame written to the sink (the pre-buffer is inside this)
  to_first_audio       the sum that matters: last voiced frame -> first audio frame (end_of_turn + STT submit -> first frame)

Numbers and enumerations only: no caller word, no reply word, no key, no audio ever enters an evidence record.
"""
from __future__ import annotations

# The 7.51 s median of the last supervised call (MEASURED, REAL turns 1-6, operator): silence 0.70 + STT 1.49 + Brain 0.74 + voice 4.38 + path to the ear about 0.2.
# The "Brain" column is the first-token time only; the first-clause time (about 0.24 s after the first token) is not separated in that trace (UNKNOWN where it sits).
BASELINE_MS = {"end_of_turn": 700.0, "stt": 1490.0, "brain": 740.0, "voice": 4380.0, "ear": 200.0, "total": 7510.0}
# The stage budget of the program (ESTIMATE, to be proven stage by stage): its sum is 1.30 s; the 2.2 s median goal keeps 0.9 s for the second clause, jitter and tails.
BUDGET_MS = {"end_of_turn": 300.0, "stt": 150.0, "brain": 250.0, "voice": 450.0, "ear": 150.0, "total": 1300.0}
STAGE_KEYS = ("end_of_turn_ms", "stt_ms", "brain_first_token_ms", "brain_first_clause_ms", "voice_first_audio_ms", "playout_start_ms", "to_first_audio_ms")


class StageRecorder:
    """Takes the stage marks on the clock it is given (a virtual clock in the simulated harness, the real monotonic clock otherwise)."""

    def __init__(self, clock):
        self.clock = clock
        self.t: dict[str, float] = {}
        self.jobs: list[dict] = []          # one record per voice job: words, submit and done times (numbers)

    def mark(self, name: str) -> None:
        self.t.setdefault(name, self.clock.now())


class RecordingClients:
    """Wraps the clients of a Conversation (mock or real): marks STT submit/final, the Brain request and first token, and every voice job. Passes everything through."""

    def __init__(self, inner, rec: StageRecorder):
        self.inner, self.rec = inner, rec
        self.cfg = getattr(inner, "cfg", {})

    async def transcribe(self, pcm: bytes, rate: int, language: str) -> dict:
        self.rec.mark("t_stt_sent")
        out = await self.inner.transcribe(pcm, rate, language)
        self.rec.mark("t_stt_final")
        return out

    async def chat_stream(self, messages, tools=None):
        self.rec.mark("t_llm_sent")
        async for item in self.inner.chat_stream(messages, tools):
            if item.get("delta"):
                self.rec.mark("t_first_token")
            yield item

    async def synthesize(self, text: str, language: str):
        job = {"words": len(text.split()), "t_submit": self.rec.clock.now()}
        self.rec.jobs.append(job)
        out = await self.inner.synthesize(text, language)
        job["t_done"] = self.rec.clock.now()
        return out

    async def retrieve(self, query: str, k: int = 4):
        return await self.inner.retrieve(query, k)

    async def chat(self, messages, tools=None, temperature=None):
        return await self.inner.chat(messages, tools=tools, temperature=temperature)


class TimedSink:
    """A transport sink that only notes when the first frame and the last frame arrive (the stand-in for 'sent to Plivo')."""

    def __init__(self, clock):
        self.clock, self.t_first, self.t_last, self.frames = clock, None, None, 0

    async def send(self, gen, frame) -> None:
        now = self.clock.now()
        if self.t_first is None:
            self.t_first = now
        self.t_last, self.frames = now, self.frames + 1

    async def clear(self, gen) -> None:
        return None


def _ms(a, b):
    return None if a is None or b is None else round((b - a) * 1000.0, 1)


def derive_stages(rec: StageRecorder, stream: dict, sink: TimedSink, eot_ms: float) -> dict:
    """The stage numbers of ONE turn, from the recorder, the streaming turn's own metrics (`outcome.stream`) and the sink. Numbers only."""
    t = rec.t
    stt = _ms(t.get("t_stt_sent"), t.get("t_stt_final"))
    fa, ft = stream.get("first_audio_ready_ms"), stream.get("first_tts_sent_ms")
    ff = stream.get("first_frame_ms")
    to_first = None
    if sink.t_first is not None and t.get("t_stt_sent") is not None:
        to_first = round(eot_ms + (sink.t_first - t["t_stt_sent"]) * 1000.0, 1)
    clauses = [{"idx": c.get("idx"), "words": c.get("words"), "audio_ms": c.get("audio_ms"), "tts_wall_ms": c.get("tts_wall_ms"), "stop_reason": c.get("stop_reason"),
                "status": c.get("status")} for c in stream.get("clauses", [])]
    jobs = [{"words": j["words"], "wall_ms": _ms(j["t_submit"], j.get("t_done"))} for j in rec.jobs]
    reply_words = sum(c["words"] or 0 for c in clauses)
    return {"end_of_turn_ms": round(float(eot_ms), 1), "stt_ms": stt, "brain_first_token_ms": stream.get("ttft_ms"), "brain_first_clause_ms": stream.get("first_clause_ms"),
            "voice_first_audio_ms": None if fa is None or ft is None else round(fa - ft, 1),
            "playout_start_ms": None if ff is None or fa is None else round(ff - fa, 1), "to_first_audio_ms": to_first,
            "reply_words": reply_words, "first_clause_words": clauses[0]["words"] if clauses else None, "first_clause_stop_reason": clauses[0]["stop_reason"] if clauses else None,
            "silence_ms": stream.get("silence_ms"), "underruns": stream.get("underruns"), "gaps": len(stream.get("gaps") or []), "tts_errors": stream.get("tts_errors"),
            "clauses": clauses, "voice_jobs": jobs}


def pctl(values: list, p: float):
    """Nearest-rank percentile of the non-None values (None when there are none)."""
    v = sorted(x for x in values if isinstance(x, (int, float)))
    if not v:
        return None
    k = max(0, min(len(v) - 1, int(-(-p * len(v) // 100)) - 1))
    return v[k]


def summarize(utterances: list[dict]) -> dict:
    """median / p90 / n per stage over the utterances (None values are left out of n)."""
    out = {}
    for k in STAGE_KEYS + ("silence_ms", "underruns", "gaps", "reply_words", "first_clause_words"):
        vals = [u.get(k) for u in utterances if isinstance(u.get(k), (int, float))]
        if vals:
            s = sorted(vals)
            mid = len(s) // 2
            med = s[mid] if len(s) % 2 else round((s[mid - 1] + s[mid]) / 2, 1)
            out[k] = {"median": med, "p90": pctl(vals, 90), "n": len(vals)}
    return out
