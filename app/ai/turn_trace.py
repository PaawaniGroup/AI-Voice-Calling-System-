"""Per-turn latency trace (Step 0 of the realtime work).

One `TurnTrace` per caller utterance. Marks are taken on the MONOTONIC clock (the WSL host wall clock steps by tens of seconds,
so `time.time()` deltas are meaningless here) and reported as milliseconds relative to `t_speech_end`, so every stored value is
"how long after the caller stopped talking did this happen". The trace travels in a ContextVar so the model clients, pipeline and
media adapter can mark it without any signature change (asyncio tasks inherit the context they were created in).

Marks (first write wins unless `overwrite=True`):
  t_speech_end          last loud inbound frame (acoustic end of speech)
  t_endpoint            VAD decided the utterance is over (t_endpoint - t_speech_end == the silence window actually waited)
  t_stt_sent / t_stt_final
  t_llm_sent / t_llm_first_token / t_llm_first_clause
  t_tts_sent / t_tts_first_chunk
  t_first_frame_to_plivo   first audio frame handed to the transport
  t_turn_end            last audio of the turn handed to the transport
`voice_to_voice = t_first_frame_to_plivo - t_speech_end`.
"""
from __future__ import annotations

import contextvars
import logging
import time
import uuid

log = logging.getLogger("mme_telephony.ai.trace")

MARKS = ("t_speech_end", "t_endpoint", "t_stt_sent", "t_stt_final", "t_llm_sent", "t_llm_first_token", "t_llm_first_clause",
         "t_tts_sent", "t_tts_first_chunk", "t_first_frame_to_plivo", "t_turn_end")

_current: contextvars.ContextVar["TurnTrace | None"] = contextvars.ContextVar("turn_trace", default=None)


class TurnTrace:
    def __init__(self, t_speech_end: float | None = None, t_endpoint: float | None = None, endpoint_silence_ms: float | None = None):
        self.trace_id = uuid.uuid4().hex[:12]
        self.marks: dict[str, float] = {}
        self.runpod: dict[str, dict] = {}   # service -> {"delay_ms", "exec_ms", "calls"} of the FIRST call (the one that gates latency)
        self.meta: dict[str, float] = {}
        if t_speech_end is not None:
            self.marks["t_speech_end"] = t_speech_end
        if t_endpoint is not None:
            self.marks["t_endpoint"] = t_endpoint
        if endpoint_silence_ms is not None:
            self.meta["endpoint_silence_ms"] = endpoint_silence_ms

    def mark(self, name: str, overwrite: bool = False, at: float | None = None) -> None:
        if overwrite or name not in self.marks:
            self.marks[name] = time.monotonic() if at is None else at

    def note_runpod(self, service: str, delay_ms: float | None, exec_ms: float | None) -> None:
        rec = self.runpod.setdefault(service, {"calls": 0})
        rec["calls"] += 1
        if rec["calls"] == 1:
            rec["delay_ms"], rec["exec_ms"] = delay_ms, exec_ms

    def to_ms(self) -> dict:
        """Flat numeric dict safe to store in Mongo / publish: relative marks, voice_to_voice, RunPod delay/exec."""
        base = self.marks.get("t_speech_end")
        if base is None and self.marks:
            base = min(self.marks.values())
        out: dict = {}
        if base is not None:
            for k, v in self.marks.items():
                out[k] = round((v - base) * 1000, 1)
            if "t_first_frame_to_plivo" in out:
                out["voice_to_voice"] = out["t_first_frame_to_plivo"]
        out.update(self.meta)
        for svc, rec in self.runpod.items():
            if rec.get("delay_ms") is not None:
                out[f"{svc}_delay_ms"] = rec["delay_ms"]
            if rec.get("exec_ms") is not None:
                out[f"{svc}_exec_ms"] = rec["exec_ms"]
        return out


def start(trace: TurnTrace) -> contextvars.Token:
    return _current.set(trace)


def current() -> TurnTrace | None:
    return _current.get()


def mark(name: str, overwrite: bool = False) -> None:
    tr = _current.get()
    if tr is not None:
        tr.mark(name, overwrite=overwrite)


def note_runpod_response(service: str, j: object) -> None:
    """Log and record RunPod's own `delayTime` (queue + cold start) vs `executionTime` (compute), both in ms."""
    if not isinstance(j, dict):
        return
    d, e = j.get("delayTime"), j.get("executionTime")
    d = float(d) if isinstance(d, (int, float)) else None
    e = float(e) if isinstance(e, (int, float)) else None
    if d is None and e is None:
        return
    log.info("runpod service=%s delay_ms=%s exec_ms=%s", service, d, e)
    tr = _current.get()
    if tr is not None:
        tr.note_runpod(service, d, e)
