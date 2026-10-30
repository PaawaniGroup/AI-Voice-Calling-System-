"""Backoff, circuit breaker and readiness of the persistent transport. Pure code with an injectable clock and random source: no I/O.

Readiness here means COMPLETED REQUESTS (a real request or a `warmup` reply) and their first-byte times, never a health listing of worker slots."""
from __future__ import annotations

import random
import time
from collections import deque


class Backoff:
    """Exponential backoff with jitter: 0.2 s, 0.4 s, 0.8 s ... up to `maximum`, each +-`jitter` of itself."""

    def __init__(self, initial: float = 0.2, factor: float = 2.0, maximum: float = 5.0, jitter: float = 0.2, rng: random.Random | None = None):
        self.initial, self.factor, self.maximum, self.jitter, self.rng = initial, factor, maximum, jitter, rng or random.Random()
        self._n = 0

    def next(self) -> float:
        d = min(self.maximum, self.initial * (self.factor ** self._n))
        self._n += 1
        return max(0.0, d * (1.0 + self.rng.uniform(-self.jitter, self.jitter)))

    def reset(self) -> None:
        self._n = 0


class CircuitBreaker:
    """closed -> open after `failures` failures within `window_s`; open -> half-open after `cooldown_s` (ONE probe request is let through); a success closes it."""

    def __init__(self, failures: int = 3, window_s: float = 30.0, cooldown_s: float = 10.0, clock=time.monotonic):
        self.failures, self.window_s, self.cooldown_s, self.clock = failures, window_s, cooldown_s, clock
        self._fails: deque = deque()
        self._opened_at: float | None = None
        self._probing = False

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        return "half_open" if self.clock() - self._opened_at >= self.cooldown_s else "open"

    def allow(self) -> bool:
        s = self.state
        if s == "closed":
            return True
        if s == "half_open" and not self._probing:
            self._probing = True                           # exactly one probe at a time
            return True
        return False

    def record(self, ok: bool) -> None:
        now = self.clock()
        self._probing = False
        if ok:
            self._fails.clear()
            self._opened_at = None
            return
        self._fails.append(now)
        while self._fails and now - self._fails[0] > self.window_s:
            self._fails.popleft()
        if self._opened_at is not None or len(self._fails) >= self.failures:
            self._opened_at = now                          # (re)open: a failed probe restarts the cooldown

    def abandon(self) -> None:
        """The probe was cancelled by the caller: neither a success nor a failure; the next request may probe."""
        self._probing = False


_BREAKERS: dict[str, CircuitBreaker] = {}


def breaker_for(key: str, **kw) -> CircuitBreaker:
    """One breaker per (service, endpoint) for the whole process: calls share what they learn about a dead server."""
    if key not in _BREAKERS:
        _BREAKERS[key] = CircuitBreaker(**kw)
    return _BREAKERS[key]


class ReadinessTracker:
    """Ready = a request of that service completed OK recently with a first byte within `max_first_byte_ms`."""

    def __init__(self, window_s: float = 120.0, max_first_byte_ms: float = 2000.0, clock=time.monotonic):
        self.window_s, self.max_first_byte_ms, self.clock = window_s, max_first_byte_ms, clock
        self._recs: dict[str, deque] = {}

    def record(self, service: str, ok: bool, first_byte_ms: float | None) -> None:
        self._recs.setdefault(service, deque(maxlen=50)).append((self.clock(), bool(ok), first_byte_ms))

    def last_ok_age_s(self, service: str) -> float | None:
        for t, ok, fb in reversed(self._recs.get(service, ())):
            if ok and fb is not None and fb <= self.max_first_byte_ms:
                return round(self.clock() - t, 3)
        return None

    def ready(self, service: str) -> bool:
        age = self.last_ok_age_s(service)
        return age is not None and age <= self.window_s
