"""Metrics and the one log line of a streaming turn: numbers and kinds only, never a word of the caller or the reply."""
from __future__ import annotations

import logging

from app.observability import metrics as m

log = logging.getLogger("mme_telephony.ai.streaming")


def observe_stream(s: dict, *, interrupted: bool, error: str | None = None) -> None:
    outcome = "error" if error else ("interrupted" if interrupted else "done")
    m.STREAM_TURNS.labels(outcome).inc()
    if s.get("first_frame_ms") is not None:
        m.STREAM_FIRST_FRAME.observe(s["first_frame_ms"] / 1000)
    m.STREAM_UNDERRUNS.inc(s.get("underruns", 0))
    for g in s.get("gaps", []):
        m.STREAM_GAP_SECONDS.observe(g["length_ms"] / 1000)
    for c in s.get("clauses", []):
        if c.get("rtf") is not None:
            m.STREAM_TTS_RTF.observe(c["rtf"])
    itr = s.get("interrupt")
    if itr and itr.get("interrupt_to_last_frame_ms") is not None:
        m.STREAM_BARGE_SILENCE.observe(itr["interrupt_to_last_frame_ms"] / 1000)
    log.info("stream_turn: outcome=%s ttft_ms=%s first_clause_ms=%s first_frame_ms=%s clauses=%d chunks_per_s=%s rtf_p50=%s underruns=%d silence_ms=%s "
             "tts_errors=%s depth=%s interrupt_to_last_frame_ms=%s", outcome, s.get("ttft_ms"), s.get("first_clause_ms"), s.get("first_frame_ms"),
             len(s.get("clauses", [])), s.get("brain_chunks_per_s"), s.get("rtf_p50"), s.get("underruns", 0), s.get("silence_ms"), s.get("tts_errors"),
             s.get("prefetch_depth"), (itr or {}).get("interrupt_to_last_frame_ms"))
