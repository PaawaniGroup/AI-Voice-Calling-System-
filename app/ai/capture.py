"""Caller-audio capture for one call: pre-roll, endpointing, barge-in detection and the bounded utterance queue.

Pure logic (no I/O, no clock reads, no asyncio) so every rule is testable with synthetic frames. `serve_loop` in `ws.py` owns one
`Capture` and one `UtteranceQueue` per call; nothing here is module-level or shared between calls.

Capture state machine (independent of what the assistant is doing):
    IDLE ──first loud frame──► CAPTURING ──silence ≥ endpoint──► flush ──► IDLE
      │ (frames kept in a bounded pre-roll ring)    │ soft cap reached ──► emit a `continues` segment at the quietest frame, keep capturing
      └─ while the assistant plays: onset needs `barge_rms` for `barge_ms` (ring keeps the audio, so the onset is not lost)
Rules:
  * Capture starts at the FIRST loud frame (no 200 ms gate); the pre-roll ring supplies the audio before it. Validity (is this an
    utterance or a click?) is decided at the endpoint from the amount of loud audio, never by discarding frames early.
  * Playback never discards or replaces a capture in progress. If a capture is in progress (or starts) while the assistant plays, a
    `barge` event is raised once so the caller of this class can invalidate the stale reply; the audio is untouched.
  * Every frame fed is counted exactly once: frames_in == captured + trimmed + rejected + ring_evicted + playback_unmatched + held.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

# Utterance terminal states (exactly one per accepted utterance)
PROCESSED, REJECTED, CANCELLED, FAILED, EXPIRED = "processed", "rejected", "cancelled", "failed", "expired"
TERMINAL = (PROCESSED, REJECTED, CANCELLED, FAILED, EXPIRED)


@dataclass
class CaptureConfig:
    rate: int = 8000
    vad_rms: float = 450.0
    barge_rms: float = 1000.0
    barge_ms: int = 200
    silence_ms: int = 1100          # trailing silence that ends an utterance with ≥ short_speech_ms of speech
    short_silence_ms: int = 1200    # same for short starters ("haan ..."): callers continue after them more often
    short_speech_ms: int = 600
    pre_roll_ms: int = 300
    max_utterance_ms: int = 8000    # soft cap: emit a `continues` segment and keep capturing (never discard)
    min_valid_ms: int = 60          # less loud audio than this in a whole capture = noise burst, rejected WITH a reason
    trail_keep_ms: int = 250        # trailing silence kept in the audio handed to STT (the rest is counted as trimmed)
    split_search_ms: int = 1500     # at the soft cap, look this far back for the quietest frame to split at


@dataclass
class Segment:
    pcm: bytes
    speech_ms: int
    continues: bool          # True: more audio of the SAME utterance follows (soft cap); the consumer must merge
    t_speech_end: float
    t_endpoint: float
    pre_roll_ms: int = 0     # audio included from before the first loud frame
    frames: int = 0


@dataclass
class Event:
    kind: str                # "segment" | "barge" | "rejected"
    segment: Segment | None = None
    reason: str = ""


@dataclass
class _F:
    data: bytes
    ms: int
    rms: float
    loud: bool
    playing: bool


@dataclass
class Stats:
    frames_in: int = 0
    ms_in: int = 0
    captured: int = 0
    trimmed: int = 0
    rejected: int = 0
    ring_evicted: int = 0
    playback_unmatched: int = 0   # heard while the assistant played, below the barge threshold, never part of a capture
    pre_roll_included: int = 0
    segments: int = 0
    barges: int = 0

    def held(self, cap: "Capture") -> int:
        return len(cap.ring) + len(cap.buf)

    def balanced(self, cap: "Capture") -> bool:
        return self.frames_in == (self.captured + self.trimmed + self.rejected + self.ring_evicted
                                  + self.playback_unmatched + self.held(cap))


class Capture:
    def __init__(self, cfg: CaptureConfig):
        self.cfg = cfg
        self.ring: deque[_F] = deque()
        self.ring_ms = 0
        self.buf: list[_F] = []
        self.capturing = False
        self.loud_ms = 0            # loud audio in the current capture
        self.silence_ms = 0
        self.last_loud_at = 0.0
        self.run_ms = 0             # consecutive barge-level audio while the assistant plays (IDLE)
        self.barged = False         # barge already raised for this capture
        self.continued = False      # a `continues` segment of this utterance was already emitted
        self.pre_roll_taken = 0
        self.stats = Stats()
        self._cap_ms = cfg.pre_roll_ms + cfg.barge_ms + 100

    # ------------------------------------------------------------------ helpers
    @property
    def buffered_ms(self) -> int:
        return sum(f.ms for f in self.buf)

    def _required_silence(self) -> int:
        return self.cfg.short_silence_ms if self.loud_ms < self.cfg.short_speech_ms else self.cfg.silence_ms

    def _evict(self, f: _F) -> None:
        if f.playing:
            self.stats.playback_unmatched += 1
        else:
            self.stats.ring_evicted += 1

    def _push_ring(self, f: _F) -> None:
        self.ring.append(f)
        self.ring_ms += f.ms
        while self.ring_ms > self._cap_ms and len(self.ring) > 1:
            old = self.ring.popleft()
            self.ring_ms -= old.ms
            self._evict(old)

    def _start(self, run_ms: int) -> None:
        """Begin a capture: take the onset run plus the pre-roll from the ring, evict (and count) the rest."""
        want = self.cfg.pre_roll_ms + run_ms
        taken: list[_F] = []
        total = 0
        while self.ring and total < want:
            f = self.ring.pop()
            taken.append(f)
            total += f.ms
        while self.ring:
            self._evict(self.ring.popleft())
        self.ring_ms = 0
        taken.reverse()
        acc = 0
        n_run = 0
        for f in reversed(taken):          # frames that make up the onset run itself; the earlier ones are pre-roll
            if acc >= run_ms:
                break
            acc += f.ms
            n_run += 1
        n_pre = len(taken) - n_run
        self.buf = taken
        self.capturing = True
        self.loud_ms = sum(f.ms for f in taken if f.loud)
        self.silence_ms = 0
        self.barged = False
        self.pre_roll_taken = sum(f.ms for f in taken[:n_pre])
        self.stats.pre_roll_included += n_pre

    def _segment(self, frames: list[_F], continues: bool, now: float, t_endpoint: float, trim_tail: bool) -> Segment | None:
        keep = frames
        if trim_tail:
            tail = 0
            i = len(keep)
            while i > 0 and not keep[i - 1].loud:
                i -= 1
            quiet = keep[i:]
            budget = self.cfg.trail_keep_ms
            kept_quiet: list[_F] = []
            for f in quiet:
                if budget >= f.ms:
                    kept_quiet.append(f)
                    budget -= f.ms
                else:
                    break
            self.stats.trimmed += len(quiet) - len(kept_quiet)
            keep = keep[:i] + kept_quiet
        if not keep:
            return None
        speech = sum(f.ms for f in keep if f.loud)
        self.stats.captured += len(keep)
        self.stats.segments += 1
        return Segment(pcm=b"".join(f.data for f in keep), speech_ms=speech, continues=continues,
                       t_speech_end=self.last_loud_at or now, t_endpoint=t_endpoint, pre_roll_ms=self.pre_roll_taken,
                       frames=len(keep))

    def _flush(self, now: float) -> list[Event]:
        frames, self.buf = self.buf, []
        loud, self.loud_ms = self.loud_ms, 0
        self.capturing = False
        self.silence_ms = 0
        self.run_ms = 0
        self.barged = False
        taken = self.pre_roll_taken
        continued, self.continued = self.continued, False
        if loud < self.cfg.min_valid_ms and not continued:
            self.stats.rejected += len(frames)
            return [Event("rejected", reason="noise_burst")]
        seg = self._segment(frames, False, now, now, trim_tail=True)
        if seg is None:       # the utterance already went out in `continues` segments: always close it with a (possibly empty) final one
            seg = Segment(pcm=b"", speech_ms=0, continues=False, t_speech_end=self.last_loud_at or now, t_endpoint=now)
        seg.pre_roll_ms = taken
        return [Event("segment", seg)]

    def _soft_cap(self, now: float) -> list[Event]:
        """The capture reached `max_utterance_ms` while the caller is still talking: split at the quietest recent frame and
        keep capturing. Nothing is dropped and nothing is duplicated; the consumer merges `continues` segments."""
        n = len(self.buf)
        look = 0
        lo = n
        while lo > 0 and look < self.cfg.split_search_ms:
            lo -= 1
            look += self.buf[lo].ms
        best = min(range(max(lo, 1), n), key=lambda i: (self.buf[i].rms, -i), default=n)
        head, tail = self.buf[:best + 1], self.buf[best + 1:]
        self.buf = tail
        seg = self._segment(head, True, now, now, trim_tail=False)
        self.loud_ms = sum(f.ms for f in tail if f.loud)
        self.continued = True
        self.pre_roll_taken = 0
        return [Event("segment", seg)] if seg else []

    # ------------------------------------------------------------------ main entry
    def feed(self, data: bytes, ms: int, frame_rms: float, now: float, playing: bool) -> list[Event]:
        c = self.cfg
        st = self.stats
        st.frames_in += 1
        st.ms_in += ms
        events: list[Event] = []
        loud = frame_rms >= c.vad_rms
        f = _F(data, ms, frame_rms, loud, playing)

        if self.capturing:
            self.buf.append(f)
            if loud:
                self.loud_ms += ms
                self.silence_ms = 0
                self.last_loud_at = now
            else:
                self.silence_ms += ms
            if playing and not self.barged and self.loud_ms >= c.barge_ms:
                self.barged = True       # the caller is talking while a reply plays/starts: that reply is stale
                st.barges += 1
                events.append(Event("barge"))
            if self.silence_ms >= self._required_silence():
                events += self._flush(now)
            elif self.buffered_ms >= c.max_utterance_ms:
                events += self._soft_cap(now)
            return events

        # ---- IDLE
        if playing:
            strong = frame_rms >= max(c.vad_rms * 2.2, c.barge_rms)
            self.run_ms = self.run_ms + ms if strong else 0
            if strong:
                self.last_loud_at = now
            self._push_ring(f)
            if self.run_ms >= c.barge_ms:
                self._start(self.run_ms)
                self.barged = True
                st.barges += 1
                events.append(Event("barge"))
            return events

        self.run_ms = 0
        if loud:
            self.last_loud_at = now
            self._push_ring(f)
            self._start(ms)
            return events
        self._push_ring(f)
        return events

    def idle_tick(self, ms: int, now: float) -> list[Event]:
        """The transport sent nothing for `ms`: count it as silence so an utterance still ends."""
        if not self.capturing:
            return []
        self.silence_ms += ms
        if self.silence_ms >= self._required_silence():
            return self._flush(now)
        return []

    def reset(self) -> None:
        """Explicit boundary (call end): everything held is accounted as evicted, nothing leaks."""
        self.stats.ring_evicted += len(self.ring) + len(self.buf)
        self.ring.clear()
        self.buf = []
        self.ring_ms = 0
        self.capturing = False
        self.loud_ms = self.silence_ms = self.run_ms = 0
        self.continued = False


# ====================================================================== utterance queue
@dataclass
class Utterance:
    id: int
    gen: int
    pcm: bytes
    t_speech_end: float
    t_endpoint: float
    enqueued_at: float
    speech_ms: int = 0
    parts: int = 1
    state: str = "queued"
    reason: str = ""
    retries: int = 0


class UtteranceQueue:
    """Bounded FIFO of captured utterances for ONE call. Policy when full: the NEWEST utterance is merged into the last queued one
    (same caller, same backlog: one longer utterance, order preserved, audio kept); only if that would exceed `max_merged_ms`
    is the OLDEST item rejected, explicitly (terminal state REJECTED + reason), never silently. Items older than `expire_s` are
    EXPIRED when the worker looks at them. Every utterance ends in exactly one terminal state."""

    def __init__(self, rate: int, max_items: int = 4, max_merged_ms: int = 30000, expire_s: float = 90.0):
        self.rate, self.max_items, self.max_merged_ms, self.expire_s = rate, max_items, max_merged_ms, expire_s
        self.items: deque[Utterance] = deque()
        self.terminal: dict[int, str] = {}
        self.log: list[tuple[int, str, str]] = []     # (utterance id, state, reason) for every transition to a terminal state
        self._ids = 0
        self._event = None

    def __len__(self) -> int:
        return len(self.items)

    def _ms(self, pcm: bytes) -> int:
        return len(pcm) * 500 // self.rate

    def new_id(self) -> int:
        self._ids += 1
        return self._ids

    def finish(self, u: Utterance, state: str, reason: str = "") -> None:
        if u.id in self.terminal:      # exactly one terminal state
            return
        u.state, u.reason = state, reason
        self.terminal[u.id] = state
        self.log.append((u.id, state, reason))

    def put(self, u: Utterance) -> list[tuple[str, Utterance]]:
        """Enqueue; returns the notable events [("merged"|"rejected", utterance)] for metrics/logging."""
        notes: list[tuple[str, Utterance]] = []
        if len(self.items) >= self.max_items:
            last = self.items[-1]
            if self._ms(last.pcm) + self._ms(u.pcm) <= self.max_merged_ms:
                last.pcm += u.pcm
                last.parts += 1
                last.speech_ms += u.speech_ms
                last.t_endpoint = u.t_endpoint
                last.t_speech_end = u.t_speech_end
                last.gen = u.gen
                self.finish(u, PROCESSED, "merged_into_%d" % last.id)   # audio preserved inside `last`
                notes.append(("merged", last))
                return notes
            old = self.items.popleft()
            self.finish(old, REJECTED, "queue_full_oldest_rejected")
            notes.append(("rejected", old))
        self.items.append(u)
        return notes

    def get_nowait(self, now: float) -> tuple[Utterance | None, list[Utterance]]:
        """Next live utterance and the items that expired on the way (already marked EXPIRED)."""
        expired: list[Utterance] = []
        while self.items:
            u = self.items.popleft()
            if now - u.enqueued_at > self.expire_s:
                self.finish(u, EXPIRED, "waited_too_long")
                expired.append(u)
                continue
            return u, expired
        return None, expired

    def cancel_all(self, reason: str) -> list[Utterance]:
        out = list(self.items)
        self.items.clear()
        for u in out:
            self.finish(u, CANCELLED, reason)
        return out

    def pop_coalesced(self, now: float) -> tuple[Utterance | None, list[Utterance]]:
        """Next live utterance; everything already waiting BEHIND it is merged into it (one STT job and one reply for the backlog,
        order and audio preserved, bounded by `max_merged_ms`). Returns (utterance, expired items)."""
        u, expired = self.get_nowait(now)
        if u is None:
            return None, expired
        while self.items and self._ms(u.pcm) + self._ms(self.items[0].pcm) <= self.max_merged_ms:
            nxt = self.items.popleft()
            if now - nxt.enqueued_at > self.expire_s:
                self.finish(nxt, EXPIRED, "waited_too_long")
                expired.append(nxt)
                continue
            u.pcm += nxt.pcm
            u.parts += nxt.parts
            u.speech_ms += nxt.speech_ms
            u.gen = nxt.gen
            u.t_speech_end, u.t_endpoint = nxt.t_speech_end, nxt.t_endpoint
            self.finish(nxt, PROCESSED, "merged_into_%d" % u.id)
        return u, expired

    def requeue_front(self, u: Utterance) -> None:
        u.state = "queued"
        self.items.appendleft(u)
