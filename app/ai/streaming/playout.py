"""Paced audio output for ONE assistant turn (token-streaming pipeline).

Clauses arrive as PCM16 at the transport rate (8 kHz) and leave as fixed 20 ms frames on a monotonic timeline:
  * the sink is kept `prebuffer_ms` AHEAD of real time (the first frames go out in a burst, then one frame per 20 ms) so network jitter does not
    underrun the far end; if the process falls behind (event-loop stall) it catches up from the timeline, it never drifts;
  * when the next clause is late nothing is sent and the far end hears silence once it has played out what it holds; sending silence frames
    instead would sit `lead` ahead and delay the late clause by that much. The gap is measured (when the far end ran dry, how long, which clause
    followed) and the timeline is re-based, with a fresh lead, when audio resumes. A clause that arrives before the far end runs dry is no gap;
  * every clause is faded in/out (default 15 ms) so joins do not click, and gain-matched to the turn's first clause (clamped, peak-limited) so
    the voice keeps one loudness;
  * accounting: for each clause the samples actually sent and the samples estimated played (the far end starts playing at the first send and plays
    in real time), so a barge-in can keep only the part that was heard. Counts only: no text and no audio is kept here.
`cancel()` is synchronous and takes effect before the next frame (the caller then clears the far end's queue).
"""
from __future__ import annotations

import array
import asyncio
import audioop
import math
from collections import deque
from dataclasses import dataclass, field

RATE = 8000


@dataclass
class ClauseAccount:
    idx: int
    words: int
    samples: int          # length after processing
    start: int = -1       # index of its first sample in the sent stream (silence frames included)
    sent: int = 0
    played_est: int = 0
    real: bool = True     # False for the cached acknowledgement: audio that is not part of the answer


@dataclass
class Gap:
    at_ms: float          # start, relative to the first frame
    length_ms: float = 0.0
    before_clause: int = -1


@dataclass
class PlayoutConfig:
    rate: int = RATE
    frame_ms: int = 20
    prebuffer_ms: int = 150
    fade_ms: int = 15
    normalize: bool = True
    max_gain_db: float = 6.0
    max_wait_s: float = 30.0      # the producer died or never delivers: end the turn instead of sending silence forever


def _fade(pcm: bytes, n_in: int, n_out: int) -> bytes:
    a = array.array("h")
    a.frombytes(pcm)
    n = len(a)
    n_in, n_out = min(n_in, n), min(n_out, n)
    for i in range(n_in):
        a[i] = int(a[i] * (i / n_in))
    for i in range(n_out):
        a[n - 1 - i] = int(a[n - 1 - i] * (i / n_out))
    return a.tobytes()


def voiced_rms(pcm: bytes, rate: int = RATE) -> float:
    """RMS over the voiced 20 ms windows only (windows under 5 % of the loudest are pauses and do not pull the level down)."""
    win = rate // 50 * 2
    levels = [audioop.rms(pcm[i:i + win], 2) for i in range(0, len(pcm) - win + 1, win)]
    if not levels:
        return float(audioop.rms(pcm, 2)) if pcm else 0.0
    thr = max(levels) * 0.05
    voiced = [v for v in levels if v > thr]
    return math.sqrt(sum(v * v for v in voiced) / len(voiced)) if voiced else 0.0


class TurnPlayout:
    def __init__(self, sink, clock, gen: int, cfg: PlayoutConfig | None = None):
        self.sink, self.clock, self.gen = sink, clock, gen
        self.cfg = cfg or PlayoutConfig()
        self.frame = self.cfg.rate * self.cfg.frame_ms // 1000          # samples per frame
        self._q: deque[list] = deque()                                   # [account, bytearray]
        self.accounts: dict[int, ClauseAccount] = {}
        self.gaps: list[Gap] = []
        self.closed = False                                              # cancelled or finished: nothing more is accepted or sent
        self.cancelled = False
        self._ended = False                                              # the producer will deliver no more clauses
        self._wake = asyncio.Event()
        self._ref_rms: float | None = None
        self.t_first = None                                              # clock time of the first frame sent (of any kind)
        self.t_first_real = None                                         # clock time of the first frame that carries a real clause (not the cached acknowledgement)
        self.sending_real = True                                         # kind of the frame being handed to the sink right now (read by the sink's callbacks)
        self.on_first_real = None                                        # called once, right after the first real frame was handed to the sink
        self.t_last_send = None
        self._seg_t0 = 0.0                                               # a segment = audio sent without the far end running dry in between
        self._seg_pos0 = 0
        self._played_base = 0                                            # samples played in earlier segments
        self.stream_pos = 0                                             # samples handed to the sink so far (silence included)
        self.frames_sent = 0
        self.dropped_stale = 0
        self.reason: str | None = None

    # ------------------------------------------------------------------ producer side
    def enqueue(self, gen: int, idx: int, pcm: bytes, words: int = 0, real: bool = True) -> bool:
        """Add a clause (PCM16 at cfg.rate). Stale audio (another generation, or after cancel/finish) is dropped and counted.
        `real=False` is the cached acknowledgement: it is faded but neither level-matched nor used as the turn's loudness reference."""
        if self.closed or gen != self.gen or not pcm:
            self.dropped_stale += 1
            return False
        pcm = self._process(pcm, real)
        acct = ClauseAccount(idx=idx, words=words, samples=len(pcm) // 2, real=real)
        self.accounts[idx] = acct
        self._q.append([acct, bytearray(pcm)])
        self._wake.set()
        return True

    def end_turn(self, gen: int) -> None:
        if gen == self.gen:
            self._ended = True
            self._wake.set()

    def _process(self, pcm: bytes, real: bool = True) -> bytes:
        pcm = pcm[: len(pcm) // 2 * 2]
        c = self.cfg
        if c.normalize and real:
            r = voiced_rms(pcm, c.rate)
            if self._ref_rms is None:
                if r > 0:
                    self._ref_rms = r                                    # the first clause sets the level the rest of the turn follows
            elif r > 0:
                g = self._ref_rms / r
                lim = 10 ** (c.max_gain_db / 20)
                g = min(max(g, 1 / lim), lim)
                peak = audioop.max(pcm, 2) or 1
                g = min(g, 32000 / peak)                                 # never clip
                if abs(g - 1.0) > 0.02:
                    pcm = audioop.mul(pcm, 2, g)
        fade = c.rate * c.fade_ms // 1000
        return _fade(pcm, fade, fade) if fade else pcm

    # ------------------------------------------------------------------ consumer side
    def _buffered(self) -> int:
        return sum(len(b) // 2 for _, b in self._q)

    def _take(self, n: int) -> tuple[bytes, int, bool]:
        """Up to n samples from the queue (spanning clause joins); updates per-clause sent counts. Returns (pcm, samples, carries a real clause)."""
        out = bytearray()
        got = 0
        real = False
        while got < n and self._q:
            acct, buf = self._q[0]
            if acct.start < 0:
                acct.start = self.stream_pos + got
            take = min(n - got, len(buf) // 2)
            out += buf[: take * 2]
            del buf[: take * 2]
            acct.sent += take
            got += take
            real = real or acct.real
            if not buf:
                self._q.popleft()
        return bytes(out), got, real

    async def run(self) -> dict:
        """Send the turn's audio on the 20 ms timeline. Returns when the producer has ended and the audio is out, on cancel, or on max_wait_s."""
        c = self.cfg
        frame_s = c.frame_ms / 1000
        try:
            while not self._q and not self._ended and not self.closed:    # nothing is sent before the first clause exists
                self._wake.clear()
                try:
                    await self.clock.wait_for(self._wake.wait(), c.max_wait_s)
                except asyncio.TimeoutError:
                    self.reason = "no_audio"
                    return self.report()
            if self.closed or not self._q:
                self.reason = self.reason or ("cancelled" if self.cancelled else "no_audio")
                return self.report()
            # pre-buffer: wait for prebuffer_ms of audio (or the end of the turn) so the first frames are not followed by an immediate underrun
            need = c.rate * c.prebuffer_ms // 1000
            while self._buffered() < need and not self._ended and not self.closed:
                self._wake.clear()
                try:
                    await self.clock.wait_for(self._wake.wait(), 0.2)
                except asyncio.TimeoutError:
                    break
            burst = max(0, math.ceil(c.prebuffer_ms / c.frame_ms))
            self.t_first = self._seg_t0 = self.clock.now()
            self._seg_pos0 = 0
            k = 0                                                           # frames sent in the current segment
            while not self.closed:
                due = self._seg_t0 + max(0, k - burst + 1) * frame_s       # frames 0..burst-1 go out at once (the lead), then one per frame time
                if due > self.clock.now():
                    await self.clock.sleep_until(due)
                else:
                    await asyncio.sleep(0)
                if self.closed:
                    break
                pcm, got, real = self._take(self.frame)
                if got == 0:
                    await self._flush()                                    # a sink that batches frames must not hold audio back while we wait
                    if self._ended:
                        break                                              # the turn's audio is all out
                    # UNDERRUN: the next clause is late. Nothing is sent (silence frames would sit `lead` ahead and delay the late clause by that
                    # much); the far end plays out what it holds and then hears silence. The gap is measured from the moment it runs dry.
                    dry_at = self._seg_t0 + (self.stream_pos - self._seg_pos0) / c.rate
                    t_wait = self.clock.now()
                    while not self._q and not self._ended and not self.closed:
                        self._wake.clear()
                        left = c.max_wait_s - (self.clock.now() - t_wait)
                        if left <= 0:
                            self.reason = "producer_stalled"
                            break
                        try:
                            await self.clock.wait_for(self._wake.wait(), left)
                        except asyncio.TimeoutError:
                            self.reason = "producer_stalled"
                            break
                    if self.closed or self.reason:
                        break
                    if not self._q:
                        break                                              # ended while waiting
                    now = self.clock.now()
                    if now - dry_at > frame_s / 2:                         # the far end really ran dry: an audible gap, and a fresh timeline
                        if self.t_first_real is not None:                  # waiting for the FIRST real clause after the cached acknowledgement is not an underrun
                            self.gaps.append(Gap(at_ms=(dry_at - self.t_first) * 1000, length_ms=(now - dry_at) * 1000, before_clause=self._next_idx()))
                        self._played_base += self.stream_pos - self._seg_pos0
                        self._seg_t0, self._seg_pos0, k = now, self.stream_pos, 0
                    continue
                if got < self.frame:                                       # tail of a clause: pad the frame (<20 ms of silence, inside the fade-out)
                    pcm += b"\x00\x00" * (self.frame - got)
                await self._send(pcm, real)
                k += 1
            self.reason = self.reason or ("cancelled" if self.cancelled else "done")
            await self._flush()
        finally:
            self.closed = True
        return self.report()

    def _next_idx(self) -> int:
        return self._q[0][0].idx if self._q else max(self.accounts, default=-1) + 1

    async def _flush(self) -> None:
        flush = getattr(self.sink, "flush", None)
        if flush is not None and not self.closed:
            await flush()

    async def _send(self, pcm: bytes, real: bool = True) -> None:
        self.sending_real = real
        await self.sink.send(self.gen, pcm)
        self.stream_pos += len(pcm) // 2
        self.frames_sent += 1
        self.t_last_send = self.clock.now()
        if real and self.t_first_real is None:
            self.t_first_real = self.t_last_send
            if self.on_first_real is not None:
                self.on_first_real()

    # ------------------------------------------------------------------ control and accounting
    def cancel(self) -> dict:
        """Synchronous: nothing more is sent after the frame in progress. Returns the accounting at this instant."""
        snap = self.report()
        self.cancelled = True
        self.closed = True
        self._q.clear()
        self._wake.set()
        return snap

    def played_samples(self, at: float | None = None) -> int:
        """Samples the far end has played by `at` (it starts at the first frame and plays in real time, never more than was sent)."""
        if self.t_first is None:
            return 0
        t = self.clock.now() if at is None else at
        in_seg = max(0, min(self.stream_pos - self._seg_pos0, int((t - self._seg_t0) * self.cfg.rate)))
        return self._played_base + in_seg

    def report(self) -> dict:
        played = self.played_samples()
        clauses = []
        for idx in sorted(self.accounts):
            a = self.accounts[idx]
            a.played_est = 0 if a.start < 0 else max(0, min(a.sent, played - a.start))
            clauses.append({"idx": a.idx, "words": a.words, "samples": a.samples, "sent": a.sent, "played_est": a.played_est, "real": a.real})
        rate = self.cfg.rate
        return {"gen": self.gen, "reason": self.reason, "clauses": clauses, "frames_sent": self.frames_sent,
                "sent_ms": round(self.stream_pos / rate * 1000, 1), "played_est_ms": round(played / rate * 1000, 1),
                "lead_ms": round((self.stream_pos - played) / rate * 1000, 1),
                "underruns": len(self.gaps), "gaps": [{"at_ms": round(g.at_ms, 1), "length_ms": round(g.length_ms, 1), "before_clause": g.before_clause} for g in self.gaps],
                "silence_ms": round(sum(g.length_ms for g in self.gaps), 1), "dropped_stale": self.dropped_stale,
                "t_first": self.t_first, "t_first_real": self.t_first_real, "t_last_send": self.t_last_send}
