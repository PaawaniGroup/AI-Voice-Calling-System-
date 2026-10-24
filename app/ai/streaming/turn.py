"""One assistant turn of the token-streaming pipeline (per-tenant flag `streaming_pipeline`, default OFF).

    Brain token stream -> tag filter -> clause segmenter -> script gate -> TTS prefetch (<= depth in flight) -> ordered hand-over -> paced playout

Structured concurrency: one TaskGroup owns the producer (Brain stream), the dispatcher/TTS jobs and the playout loop; cancelling the turn cancels all
of them, which closes the Brain's HTTP stream and cancels the in-flight RunPod TTS jobs (the clients do that on CancelledError). Every audio hand-over
carries the turn's generation id, so late audio of an interrupted turn is dropped, not played. Nothing here logs caller or reply words: only kinds,
counts, lengths and timings.

Script rule (Devanagari flow): the FIRST clause is checked before it is spoken and gets ONE bounded rewrite (before any audio exists); later clauses
are converted with the verified lexicon when that is safe, otherwise spoken as they are, and only the kind/count of the problem is logged.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable

from app.ai import codec
from app.ai.script_enforcement import check_reply, enforce_clause, problem_kinds, strip_mode_tag
from app.ai.streaming.playout import PlayoutConfig, TurnPlayout
from app.ai.streaming.segmenter import ClauseSegmenter, SegmenterConfig, is_wordlike

log = logging.getLogger("mme_telephony.ai.streaming")

FILLER_IDX = 10_000        # clause indexes from here on are fillers (never part of the reply text)


@dataclass
class StreamConfig:
    segmenter: SegmenterConfig = field(default_factory=SegmenterConfig)
    playout: PlayoutConfig = field(default_factory=PlayoutConfig)
    prefetch_depth: int = 2          # TTS jobs in flight (PROVISIONAL: set from the TTS re-measurement and the concurrency probe)
    clause_queue: int = 8            # clauses waiting for a TTS slot (bounded: backpressure on the Brain reader)
    retry_timeout_s: float = 1.0     # the one first-clause rewrite
    tts_timeout_s: float = 25.0      # a clause whose TTS takes longer is skipped (its job is cancelled)
    stop_wait_s: float = 1.0         # interrupt(): how long to wait for the tasks to unwind
    max_tool_rounds: int = 2
    filler_after_s: float = 0.4      # a tool slower than this gets the filler phrase (when one is available)


class ModeTagFilter:
    """Removes an echoed `(reply_mode: hi)` tag from the token stream, even when the tag is split across tokens."""
    _FULL = re.compile(r"\(\s*reply_mode\s*[:=]\s*\w*\s*\)", re.IGNORECASE)
    _TAG = "(reply_mode:"

    def __init__(self):
        self.held = ""

    @classmethod
    def _could_become_tag(cls, rest: str) -> bool:
        n = re.sub(r"\s+", "", rest.lower())
        return len(rest) < 32 and (cls._TAG.startswith(n) or re.fullmatch(r"\(reply_mode[:=]\w*", n) is not None)

    def feed(self, text: str) -> str:
        buf = self.held + text
        self.held = ""
        out = []
        while buf:
            i = buf.find("(")
            if i < 0:
                out.append(buf)
                break
            out.append(buf[:i])
            rest = buf[i:]
            m = self._FULL.match(rest)
            if m:
                buf = rest[m.end():]
                continue
            if self._could_become_tag(rest):
                self.held = rest                                    # could still become the tag: wait for more tokens
                break
            out.append("(")
            buf = rest[1:]
        return "".join(out)

    def flush(self) -> str:
        """End of the stream: a held fragment that is the start of the tag is dropped (the Brain stopped mid-tag), anything else is released."""
        rest, self.held = self.held, ""
        n = re.sub(r"\s+", "", rest.lower())
        return "" if len(n) >= 3 and self._could_become_tag(rest) else rest


@dataclass
class TurnOutcome:
    status: str = "done"                    # done | cancelled | error
    error: str | None = None
    reply_text: str = ""                    # what was produced (after script conversion), in clause order: for the history of a completed turn
    spoken_text: str = ""                   # the part that was actually played (barge-in: truncated at a clause or word boundary)
    clause_texts: dict = field(default_factory=dict)   # idx -> text, in memory only
    playout: dict = field(default_factory=dict)
    stream: dict = field(default_factory=dict)         # metrics (numbers only)
    script: dict = field(default_factory=dict)         # kinds and counts only
    tool_rounds: int = 0


def truncate_spoken(clause_texts: dict, playout_report: dict) -> str:
    """The text of what the caller actually heard: whole clauses that were played, then the played share of the interrupted clause cut at a word."""
    parts: list[str] = []
    accounts = {c["idx"]: c for c in playout_report.get("clauses", [])}
    for idx in sorted(i for i in clause_texts if i < FILLER_IDX):
        a = accounts.get(idx)
        if not a or not a["samples"] or a["played_est"] <= 0:
            break
        frac = a["played_est"] / a["samples"]
        text = clause_texts[idx]
        if frac >= 0.97:
            parts.append(text)
            continue
        words = text.split()
        keep = max(1, int(round(len(words) * frac)))
        parts.append(" ".join(words[:keep]))
        break
    return " ".join(parts)


class StreamingTurn:
    """`brain_stream(messages)` yields {"delta"|"tool_calls"|"done"}; `tts(text, mode)` returns (pcm16, sample_rate); `sink` has
    `async send(gen, frame_pcm16_8k)` and `async clear(gen)`; `rewrite(text, mode)` returns a rewritten first clause or None."""

    def __init__(self, *, gen: int, brain_stream: Callable[[list], AsyncIterator[dict]], tts: Callable[[str, str], Awaitable[tuple[bytes, int]]],
                 sink, clock, mode: str, cfg: StreamConfig | None = None, rewrite: Callable[[str, str], Awaitable[str | None]] | None = None,
                 run_tools: Callable[[list], Awaitable[list]] | None = None, filler_pcm8: bytes | None = None, check_script: bool = True,
                 trace=None, on_first_frame: Callable[[], None] | None = None):
        self.gen, self.brain_stream, self.tts, self.sink, self.clock, self.mode = gen, brain_stream, tts, sink, clock, mode
        self.cfg = cfg or StreamConfig()
        self.rewrite, self.run_tools, self.filler_pcm8, self.check_script = rewrite, run_tools, filler_pcm8, check_script
        self.on_first_frame = on_first_frame
        self.trace = trace
        self.playout = TurnPlayout(_MarkingSink(sink, on_first_frame), clock, gen, self.cfg.playout)
        self.outcome = TurnOutcome()
        self._q: asyncio.Queue = asyncio.Queue(maxsize=self.cfg.clause_queue)
        self._results: dict[int, bytes | None] = {}
        self._next = 0
        self._inflight: set[asyncio.Task] = set()
        self._task: asyncio.Task | None = None
        self._words: dict[int, int] = {}
        self.m: dict = {"t_start": None, "t_first_token": None, "t_first_clause": None, "t_first_tts_sent": None, "t_first_enqueue": None,
                        "brain_chunks": 0, "brain_span_s": 0.0, "clauses": [], "tts_errors": 0, "tool_rounds": 0}
        self._last_chunk_at = None
        self._script = {"first_ok": None, "first_retried": False, "first_retry_ok": None, "kinds": {}, "clauses_converted": 0, "clauses_violating": 0}
        self._fail_all = False

    # ------------------------------------------------------------------ run
    async def run(self, messages: list) -> TurnOutcome:
        self._task = asyncio.current_task()
        self.m["t_start"] = self.clock.now()
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._produce(messages))
                tg.create_task(self._dispatch(tg))
                play = tg.create_task(self.playout.run())
        except asyncio.CancelledError:
            self.outcome.status = "cancelled"
            raise
        rep = play.result()
        self.outcome.playout = rep
        self.outcome.reply_text = " ".join(self.outcome.clause_texts[i] for i in sorted(self.outcome.clause_texts) if i < FILLER_IDX)
        self.outcome.spoken_text = truncate_spoken(self.outcome.clause_texts, rep)
        if not rep["clauses"] and not self.outcome.error:
            self.outcome.error = "no_audio"
        if self.outcome.error and not rep["clauses"]:
            self.outcome.status = "error"
        self._finish_metrics(rep)
        return self.outcome

    # ------------------------------------------------------------------ Brain -> clauses
    async def _produce(self, messages: list) -> None:
        seg = ClauseSegmenter(self.cfg.segmenter)
        tags = ModeTagFilter()
        idx = 0
        cur = messages
        tool_round = 0
        try:
            while True:
                tool_calls = None
                async for item in self.brain_stream(cur):
                    if item.get("tool_calls"):
                        tool_calls = item["tool_calls"]
                        break
                    d = item.get("delta")
                    if not d:
                        continue
                    now = self.clock.now()
                    if self.m["t_first_token"] is None:
                        self.m["t_first_token"] = now
                    self.m["brain_chunks"] += 1
                    self._last_chunk_at = now
                    for clause in seg.feed(tags.feed(d)):
                        idx = await self._emit(clause, idx)
                if tool_calls and self.run_tools and tool_round < self.cfg.max_tool_rounds:
                    tool_round += 1
                    self.m["tool_rounds"] = self.outcome.tool_rounds = tool_round
                    cur = await self._tool_round(tool_calls)
                    continue
                break
            tail = tags.flush()
            for clause in (seg.feed(tail) if tail else []) + seg.flush():
                idx = await self._emit(clause, idx)
        except asyncio.CancelledError:
            raise
        except Exception as e:                                                     # Brain failure: what was already produced still plays
            self.outcome.error = f"brain_{getattr(e, 'kind', type(e).__name__)}"
            log.warning("streaming brain error: %s", type(e).__name__)
        finally:
            if self.m["t_first_token"] is not None and self._last_chunk_at is not None:
                self.m["brain_span_s"] = self._last_chunk_at - self.m["t_first_token"]
            await self._q.put(None)

    async def _tool_round(self, tool_calls: list) -> list:
        """Run the tool(s) while a short filler phrase covers the wait (when one is available). Speech already handed over is not cut mid-word."""
        task = asyncio.ensure_future(self.run_tools(tool_calls))
        try:
            try:
                return await self.clock.wait_for(asyncio.shield(task), self.cfg.filler_after_s)
            except asyncio.TimeoutError:
                if self.filler_pcm8:
                    self.playout.enqueue(self.gen, FILLER_IDX + self.m["tool_rounds"], self.filler_pcm8, words=2)
                return await task
        except asyncio.CancelledError:
            task.cancel()
            raise

    def _problems(self, clause: str) -> list[str]:
        """Script problems of ONE clause: Roman-letter Hindi (hi mode) or Devanagari (en mode). The whole-reply rule "Devanagari must be present" is
        not applied to a clause (a short clause can legitimately be all English terms)."""
        return [p for p in check_reply(clause, self.mode)[1] if p != "no_devanagari_in_hi_mode"]

    def _count(self, problems: list[str]) -> None:
        for k, n in problem_kinds(problems).items():                              # kinds and counts only: the offending words stay out of every record
            self._script["kinds"][k] = self._script["kinds"].get(k, 0) + n
        self._script["clauses_violating"] += 1

    async def _first_clause_rewrite(self, clause: str) -> str | None:
        """The ONE bounded retry, before any audio exists. Returns a clean rewrite or None (timeout, error, still failing)."""
        self._script["first_retried"] = True
        try:
            new = await self.clock.wait_for(self.rewrite(clause, self.mode), self.cfg.retry_timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception:                                                          # includes the time limit: the clause is then spoken as it is
            return None
        new = " ".join(strip_mode_tag(new or "").split())
        return new if new and not self._problems(new) else None

    async def _emit(self, clause: str, idx: int) -> int:
        clause = " ".join(strip_mode_tag(clause).split())
        if not clause or not is_wordlike(clause):
            return idx
        text = clause
        if self.m["t_first_clause"] is None:
            self.m["t_first_clause"] = self.clock.now()                            # the first clause exists (before the script gate and its retry)
        if self.check_script:
            problems = self._problems(clause)
            if problems:
                self._count(problems)
                fixed = None
                if idx == 0 and self.rewrite is not None:
                    self._script["first_ok"] = False
                    fixed = await self._first_clause_rewrite(clause)
                    self._script["first_retry_ok"] = fixed is not None
                if fixed is None:
                    fixed, _, _ = enforce_clause(clause, self.mode)               # verified-lexicon conversion; else unchanged (it logs kinds only)
                    if fixed != clause:
                        self._script["clauses_converted"] += 1
                text = fixed
            elif idx == 0:
                self._script["first_ok"] = True
        self.outcome.clause_texts[idx] = text
        self._words[idx] = len(text.split())
        await self._q.put((idx, text))
        return idx + 1

    # ------------------------------------------------------------------ clauses -> TTS -> playout
    async def _dispatch(self, tg: asyncio.TaskGroup) -> None:
        sem = asyncio.Semaphore(max(1, self.cfg.prefetch_depth))
        while True:
            item = await self._q.get()
            if item is None:
                break
            await sem.acquire()                                                    # at most `prefetch_depth` TTS jobs in flight
            t = tg.create_task(self._synth(item[0], item[1], sem))
            self._inflight.add(t)
            t.add_done_callback(self._inflight.discard)
        if self._inflight:
            await asyncio.gather(*list(self._inflight), return_exceptions=True)
        self._drain_ready(final=True)
        self.playout.end_turn(self.gen)

    async def _synth(self, idx: int, text: str, sem: asyncio.Semaphore) -> None:
        rec = {"idx": idx, "words": self._words.get(idx, 0), "chars": len(text), "tts_wall_ms": None, "audio_ms": None, "rtf": None, "status": "ok"}
        self.m["clauses"].append(rec)
        t0 = self.clock.now()
        if self.m["t_first_tts_sent"] is None:
            self.m["t_first_tts_sent"] = t0
        try:
            pcm, rate = await self.clock.wait_for(self.tts(text, self.mode), self.cfg.tts_timeout_s)
            wall = self.clock.now() - t0
            pcm8 = codec.resample_pcm16(pcm, rate, self.playout.cfg.rate) if rate != self.playout.cfg.rate else pcm
            audio_s = len(pcm8) / 2 / self.playout.cfg.rate
            rec.update(tts_wall_ms=round(wall * 1000, 1), audio_ms=round(audio_s * 1000, 1), rtf=round(wall / audio_s, 3) if audio_s else None)
            self._results[idx] = pcm8
        except asyncio.CancelledError:
            rec["status"] = "cancelled"
            raise
        except Exception as e:                                                     # this clause is skipped; the rest of the reply still plays
            rec["status"] = "timeout" if isinstance(e, asyncio.TimeoutError) else f"error:{getattr(e, 'kind', type(e).__name__)}"
            self.m["tts_errors"] += 1
            self._results[idx] = None
            log.warning("streaming tts clause failed: %s", rec["status"])
        finally:
            sem.release()
        self._drain_ready()

    def _drain_ready(self, final: bool = False) -> None:
        """Hand finished clauses to the playout strictly in order; a failed clause is skipped, never waited for."""
        while self._next in self._results:
            pcm = self._results.pop(self._next)
            if pcm:
                if self.m["t_first_enqueue"] is None:
                    self.m["t_first_enqueue"] = self.clock.now()
                self.playout.enqueue(self.gen, self._next, pcm, words=self._words.get(self._next, 0))
            self._next += 1

    # ------------------------------------------------------------------ interruption
    async def interrupt(self, reason: str = "barge_in") -> dict:
        """Barge-in / new utterance. Order matters: (1) no frame is sent after this call (sync), (2) the far end's queue is cleared,
        (3) the Brain stream and the in-flight TTS jobs are cancelled and the tasks unwound. Returns the accounting and the timings."""
        t_int = self.clock.now()
        snap = self.playout.cancel()
        try:
            await self.sink.clear(self.gen)
        except Exception as e:
            log.warning("sink clear failed: %s", type(e).__name__)
        t_clear = self.clock.now()
        task = self._task
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            try:
                await self.clock.wait_for(asyncio.wait({task}), self.cfg.stop_wait_s)
            except asyncio.TimeoutError:
                log.warning("streaming turn did not unwind within %.1fs", self.cfg.stop_wait_s)
        t_done = self.clock.now()
        o = self.outcome
        o.status, o.playout = "cancelled", snap
        o.spoken_text = truncate_spoken(o.clause_texts, snap)
        o.reply_text = " ".join(o.clause_texts[i] for i in sorted(o.clause_texts) if i < FILLER_IDX)
        last = snap.get("t_last_send")
        o.stream = {**self._metrics(snap), "interrupt": {
            "reason": reason, "interrupt_to_last_frame_ms": round(max(0.0, ((self.playout.t_last_send or t_int) - t_int)) * 1000, 1),
            "clear_ms": round((t_clear - t_int) * 1000, 1), "unwind_ms": round((t_done - t_int) * 1000, 1),
            "spoken_clauses": sum(1 for c in snap["clauses"] if c["played_est"] > 0), "lead_ms_dropped": snap["lead_ms"]}}
        return o.stream["interrupt"]

    # ------------------------------------------------------------------ metrics (numbers only; never text)
    def _metrics(self, rep: dict) -> dict:
        m, t0 = self.m, self.m["t_start"]
        rel = lambda t: round((t - t0) * 1000, 1) if (t is not None and t0 is not None) else None   # noqa: E731
        cl = [c for c in m["clauses"] if c["rtf"] is not None]
        chunks_per_s = round((m["brain_chunks"] - 1) / m["brain_span_s"], 1) if m["brain_span_s"] > 0 and m["brain_chunks"] > 1 else None
        return {"ttft_ms": rel(m["t_first_token"]), "first_clause_ms": rel(m["t_first_clause"]), "first_tts_sent_ms": rel(m["t_first_tts_sent"]),
                "first_audio_ready_ms": rel(m["t_first_enqueue"]), "first_frame_ms": rel(rep.get("t_first")),
                "brain_chunks": m["brain_chunks"], "brain_chunks_per_s": chunks_per_s,
                "clauses": [{k: v for k, v in c.items()} for c in m["clauses"]],
                "rtf_p50": sorted(c["rtf"] for c in cl)[len(cl) // 2] if cl else None, "rtf_max": max((c["rtf"] for c in cl), default=None),
                "underruns": rep.get("underruns", 0), "gaps": rep.get("gaps", []), "silence_ms": rep.get("silence_ms", 0.0),
                "tts_errors": m["tts_errors"], "prefetch_depth": self.cfg.prefetch_depth, "script": dict(self._script)}

    def _finish_metrics(self, rep: dict) -> None:
        self.outcome.stream = self._metrics(rep)
        self.outcome.script = dict(self._script)
        if self.trace is not None:
            self.trace.stream = self.outcome.stream


class _MarkingSink:
    """Wraps the transport sink: notes the first frame (time to first audio) without the playout knowing about traces."""

    def __init__(self, sink, on_first_frame):
        self.sink, self.on_first_frame, self._seen = sink, on_first_frame, False

    async def send(self, gen: int, frame: bytes) -> None:
        await self.sink.send(gen, frame)
        if not self._seen:
            self._seen = True
            if self.on_first_frame:
                self.on_first_frame()

    async def clear(self, gen: int) -> None:
        await self.sink.clear(gen)
