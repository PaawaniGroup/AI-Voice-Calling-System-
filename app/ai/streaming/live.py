"""One live conversation turn on the token-streaming pipeline: STT -> (RAG) -> prompt -> `StreamingTurn` -> history.

It owns what `Conversation.stream_turn` owns for the old path (heard text, reply mode and tag, RAG context, tools, the conversation history, the
turn log) so the rest of the service does not change: the same `ai.record_turn` / `ai.finalize_turn` calls, the same stored fields (lengths and
numeric timings only), the same turn-trace marks. History rule: a completed turn keeps the whole reply; an interrupted turn keeps ONLY the part the
caller heard (cut at a clause or word boundary); a turn that was cancelled before any audio keeps nothing (the question is merged or re-asked).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

from app.ai import turn_trace
from app.ai.clients import AiError
from app.ai.script_enforcement import inject_turn_hint, problem_kinds, reply_mode
from app.ai.streaming.clock import RealClock
from app.ai.streaming.obs import observe_stream
from app.ai.streaming.turn import StreamingTurn, TurnOutcome, truncate_spoken

log = logging.getLogger("mme_telephony.ai.streaming")


@dataclass
class LiveResult:
    heard: str | None = None
    error: str | None = None
    mode: str = "hi"
    outcome: TurnOutcome | None = None
    timings_ms: dict = field(default_factory=dict)


async def run_streaming_turn(conv, pcm: bytes, rate: int, *, sink, gen: int, clock=None, on_created=None, on_first_frame=None) -> LiveResult:
    """`on_created(turn)` hands the StreamingTurn to the owner (so it can `silence()` it on barge-in); `on_first_frame(result_so_far, first_text)`
    fires when the first audio frame is written (the owner records the turn there, like the old path did at its first chunk)."""
    from app.ai.pipeline import TOOL_SCHEMAS, deduplicate_repetitions   # late import: pipeline imports this module's package
    clock = clock or RealClock()
    clients, res = conv.clients, LiveResult()
    conv.cancelled = False
    mark = len(conv.messages)
    t = time.perf_counter()
    turn_trace.mark("t_stt_sent")
    stt = await clients.transcribe(pcm, rate, conv.language)
    turn_trace.mark("t_stt_final")
    res.timings_ms["stt"] = round((time.perf_counter() - t) * 1000, 1)
    heard = deduplicate_repetitions((stt.get("text") or "").strip())
    if not heard:
        res.error = "no_speech_recognised"
        return res
    if getattr(conv, "glossary_correction", False):
        from app.ai.glossary_correction import correct_glossary
        heard = correct_glossary(heard)
    res.heard = heard
    lang = conv.language
    if lang == "auto" and stt.get("language"):
        lang = stt["language"]
    if conv.enforce:
        conv.current_mode = reply_mode(heard, previous_mode=conv.current_mode)
        mode = conv.current_mode
    else:
        mode = "en" if lang == "en" else "hi"
    res.mode = mode
    t_stt_final = clock.now()                                          # the cached acknowledgement (flag `ack_enabled`) counts its wait from here

    context: list = []
    if clients.cfg.get("rag_url"):
        t = time.perf_counter()
        try:
            context = await clients.retrieve(heard)
        except AiError as e:                                           # RAG is optional: degrade, do not fail the turn
            log.warning("rag unavailable: %s", e.kind)
        res.timings_ms["rag"] = round((time.perf_counter() - t) * 1000, 1)
    user = heard if not context else "CONTEXT:\n" + "\n---\n".join(context) + f"\n\nSTUDENT: {heard}"
    conv.messages.append({"role": "user", "content": user})

    def hinted(messages):
        return inject_turn_hint(messages, mode=mode, placement=conv.hint_placement, style=conv.hint_style) if conv.enforce else messages

    chat_messages = hinted(conv.messages)
    turn_trace.mark("t_llm_sent")
    t_llm = time.perf_counter()

    async def brain_stream(msgs):
        async for item in clients.chat_stream(msgs, TOOL_SCHEMAS if conv.tools else None):
            yield item

    async def tts(text: str, m: str):
        return await clients.synthesize(text, m if conv.enforce else lang)

    async def rewrite(text: str, m: str):
        return await conv._retry_script(chat_messages, text, m)       # ONE rewrite, bounded; None on timeout/error (never raises)

    async def run_tools(calls: list) -> list:
        for call in calls[:2]:
            name = (call.get("function") or {}).get("name")
            if name not in conv.tools:
                log.warning("refused unregistered tool call")
                continue
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
                result = await conv.tools[name](args)
            except Exception:
                result = "tool failed"
            conv.messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": str(result)[:1000]})
        return hinted(conv.messages)

    first_text: dict = {}

    def first_frame() -> None:
        if on_first_frame is not None:
            m = st.m
            res.timings_ms["brain"] = round(((m["t_first_token"] or clock.now()) - m["t_start"]) * 1000, 1)
            c0 = next((c for c in m["clauses"] if c["idx"] == 0), None)
            if c0 and c0.get("tts_wall_ms") is not None:
                res.timings_ms["tts"] = c0["tts_wall_ms"]
            on_first_frame(res, st.outcome.clause_texts.get(0, ""))

    ack = getattr(conv, "ack", None)                                    # AckSet of this call, or None (flag OFF / files missing)

    def ack_pick() -> bytes | None:
        clip = ack.pick("en" if mode == "en" else "hi") if ack is not None else None   # rotation counts only the clips that are really played
        return clip.pcm8 if clip is not None else None

    st = StreamingTurn(gen=gen, brain_stream=brain_stream, tts=tts, sink=sink, clock=clock, mode=mode, cfg=conv.streaming,
                       rewrite=rewrite, run_tools=run_tools if conv.tools else None, filler_pcm8=getattr(conv, "filler_pcm8", None),
                       check_script=conv.enforce, trace=turn_trace.current(), on_first_frame=first_frame,
                       ack_pick=ack_pick if ack is not None else None, ack_t0=t_stt_final)
    conv._stream = st
    if on_created is not None:
        on_created(st)
    try:
        out = await st.run(chat_messages)
    except asyncio.CancelledError:
        snap = st.silence()                                            # sync: nothing more is sent
        spoken = truncate_spoken(st.outcome.clause_texts, snap)
        _commit(conv, mark, spoken)                                    # only what the caller heard stays in the history
        res.outcome = st.outcome
        st.outcome.status, st.outcome.spoken_text = "cancelled", spoken
        st.outcome.stream = {**st._metrics(snap), "interrupt": st.interrupt_metrics()}
        _trace_marks(st)
        observe_stream(st.outcome.stream, interrupted=True)
        conv.last_stream = st.outcome
        raise
    finally:
        conv._stream = None
    res.outcome = out
    conv.last_stream = out
    _trace_marks(st)
    reply = out.reply_text.strip()
    if out.status == "error" or not reply:
        del conv.messages[mark:]
        res.error = out.error or "empty_reply"
        observe_stream(out.stream, interrupted=False, error=res.error)
        return res
    _commit(conv, mark, reply)
    res.timings_ms["tts_total"] = round(sum(c["tts_wall_ms"] or 0 for c in out.stream["clauses"]), 1)
    conv.turns.append({"heard": heard, "reply": reply, "timings_ms": dict(res.timings_ms), "mode": mode,
                       "first_gen_passed": out.script.get("first_ok") is not False, "clauses_violated": out.script.get("clauses_violating", 0),
                       "roman_tokens": []})
    log.info("turn_script_log: mode=%s first_ok=%s clauses_violated=%d first_retried=%s kinds=%s", mode, out.script.get("first_ok"),
             out.script.get("clauses_violating", 0), out.script.get("first_retried"), out.script.get("kinds"))   # kinds and counts: never words
    observe_stream(out.stream, interrupted=False)
    return res


def _commit(conv, mark: int, assistant_text: str) -> None:
    """History after a turn: the user message stays only together with an assistant text; no text -> the turn leaves no trace in the history."""
    text = (assistant_text or "").strip()
    if not text:
        del conv.messages[mark:]
        return
    conv.messages.append({"role": "assistant", "content": text})
    conv._trim()


def _trace_marks(st: StreamingTurn) -> None:
    """The old turn-trace marks, taken from the streaming turn's own monotonic stamps (first write wins)."""
    tr = turn_trace.current()
    if tr is None:
        return
    m = st.m
    for name, key in (("t_llm_first_token", "t_first_token"), ("t_llm_first_clause", "t_first_clause"), ("t_tts_sent", "t_first_tts_sent"),
                      ("t_tts_first_chunk", "t_first_enqueue")):
        if m.get(key) is not None:
            tr.mark(name, overwrite=True, at=m[key])
    acked = m.get("t_ack") is not None
    if st.playout.t_first_real is not None:
        tr.mark("t_first_frame_to_plivo", overwrite=acked, at=st.playout.t_first_real)   # the sink stamped the acknowledgement's frame first: the answer's frame wins
    if acked and st.playout.t_first is not None:
        tr.mark("t_ack_first_frame", at=st.playout.t_first)                              # a new numeric field, present only when the acknowledgement was played
