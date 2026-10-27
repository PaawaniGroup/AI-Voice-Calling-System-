"""Keep-alive ping of the voice endpoint DURING a call (per-tenant flag `keepalive_enabled`, default OFF).

Why: the measured pickup of an idle voice worker is about 1.1 s (queue delay), against 0.14 s right after another job (docs/evidence/phase0b_cold_starts_v2.json,
phase0b_tts_v3.json). Whether a ping every few seconds keeps the pickup at 0.14 s is NOT measured yet: this module is built and tested offline only; turning it on for
a tenant needs one capped measurement first (docs/LATENCY_NEXT_STEPS_2026-10-26.md, section 4).

Rules, each covered by a test (SIMULATED, mock ping and virtual clock):
  * it starts when the call's media is connected and stops at hang-up / media loss / session end, and on its own after `max_s` or after `max_failures` failed pings in a row;
  * a ping is skipped while a real voice job ran within the last `interval_s` (that job keeps the worker as warm as a ping would);
  * a ping is a RunPod job of the worker's own no-op (`{"warmup": true}`: it synthesizes nothing) and is cancelled with the call; it never touches the turn trace;
  * counters only (pings, skipped, failures, stop reason): nothing about the caller is kept.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import Awaitable, Callable

from app.ai.streaming.clock import RealClock

log = logging.getLogger("mme_telephony.ai.keepalive")


class KeepAlive:
    def __init__(self, ping: Callable[[], Awaitable[bool]], *, interval_s: float = 5.0, max_s: float = 300.0, max_failures: int = 3,
                 ping_timeout_s: float = 10.0, busy_since: Callable[[], float | None] | None = None, clock=None):
        self.ping, self.interval_s, self.max_s, self.max_failures = ping, float(interval_s), float(max_s), int(max_failures)
        self.ping_timeout_s, self.busy_since = float(ping_timeout_s), busy_since
        self.clock = clock or RealClock()
        self.pings = self.skipped = self.failures = 0
        self.stop_reason: str | None = None
        self._task: asyncio.Task | None = None
        self._t0: float | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._t0 = self.clock.now()
        self.stop_reason = None
        # a fresh context: a ping must never write into the trace of whatever turn happened to be current when the call started
        self._task = asyncio.get_running_loop().create_task(self._run(), context=contextvars.Context(), name="ai-keepalive")

    async def stop(self, reason: str = "stopped") -> None:
        t, self._task = self._task, None
        if t is None:
            return
        if not t.done():
            self.stop_reason = self.stop_reason or reason
            t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        except Exception:                                         # a failed ping loop must never break call teardown
            log.warning("keep-alive ended with an error")

    async def _run(self) -> None:
        consecutive = 0
        while True:
            await self.clock.sleep(self.interval_s)
            now = self.clock.now()
            if now - self._t0 >= self.max_s:
                self.stop_reason = "max_s"
                return
            last = self.busy_since() if self.busy_since else None
            if last is not None and now - last < self.interval_s:
                self.skipped += 1
                continue
            self.pings += 1
            try:
                ok = bool(await self.clock.wait_for(self.ping(), self.ping_timeout_s))
            except asyncio.CancelledError:
                raise
            except Exception:
                ok = False
            if ok:
                consecutive = 0
                continue
            self.failures += 1
            consecutive += 1
            if consecutive >= self.max_failures:
                self.stop_reason = "failures"
                return

    def report(self) -> dict:
        return {"pings": self.pings, "skipped": self.skipped, "failures": self.failures, "running": self.running, "stop_reason": self.stop_reason}
