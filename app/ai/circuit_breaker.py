"""Per-service Circuit Breaker for Model Services (STT, Brain, TTS).

Measured Clause Latencies (Ground Truth):
- 4 words: 2.63s voice wall time
- 12 words: 4.32s voice wall time
- 16 words: 5.08s voice wall time
- Warm pickup queue/overhead: up to 1.1s

Deadlines Policy:
- First Clause Deadline: 8.0s (default, configurable)
- Later Clauses Deadline: 10.0s (default, configurable)
- Cold starts are handled strictly by the Readiness Gate, NEVER by the clause deadline.
- Circuit Breaker Half-Open Probe Timeout: 60.0s (allows a recovery probe ample time).

Features:
- States: CLOSED (normal), OPEN (fail fast), HALF_OPEN (probing recovery).
- Configurable failure threshold, recovery timeout, and probe timeout.
- Metrics tracking (calls, failures, consecutive failures, transitions).
- Bounded retries with jittered exponential backoff ONLY for transient errors.
- Never retries after deadline expiry, interrupt, or task cancellation.
"""
from __future__ import annotations

import asyncio
import enum
import random
import time
from typing import Any, Callable, Coroutine

DEFAULT_FIRST_CLAUSE_DEADLINE_S = 8.0
DEFAULT_LATER_CLAUSE_DEADLINE_S = 10.0
DEFAULT_PROBE_TIMEOUT_S = 60.0


class CircuitState(str, enum.Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreakerOpen(Exception):
    def __init__(self, service: str, message: str = "Circuit breaker is OPEN"):
        super().__init__(f"[{service}] {message}")
        self.service = service


class DeadlineExceeded(Exception):
    def __init__(self, service: str, timeout_s: float):
        super().__init__(f"[{service}] Deadline exceeded ({timeout_s:.1f}s)")
        self.service = service
        self.timeout_s = timeout_s


class CircuitBreaker:
    def __init__(
        self,
        service: str,
        failure_threshold: int = 3,
        recovery_timeout_s: float = 30.0,
        probe_timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
        first_clause_deadline_s: float = DEFAULT_FIRST_CLAUSE_DEADLINE_S,
        later_clause_deadline_s: float = DEFAULT_LATER_CLAUSE_DEADLINE_S,
    ):
        self.service = service
        self.failure_threshold = failure_threshold
        self.recovery_timeout_s = recovery_timeout_s
        self.probe_timeout_s = probe_timeout_s
        self.first_clause_deadline_s = first_clause_deadline_s
        self.later_clause_deadline_s = later_clause_deadline_s

        self.state = CircuitState.CLOSED
        self.consecutive_failures = 0
        self.consecutive_successes = 0
        self.last_failure_time: float | None = None
        self.last_state_change: float = time.monotonic()

        # Metrics
        self.total_calls = 0
        self.total_failures = 0
        self.total_successes = 0
        self.transitions = 0

    def can_attempt(self) -> bool:
        """Check if a call is allowed through the circuit breaker."""
        now = time.monotonic()
        if self.state == CircuitState.CLOSED:
            return True
        elif self.state == CircuitState.OPEN:
            if self.last_failure_time and (now - self.last_failure_time) >= self.recovery_timeout_s:
                self._transition_to(CircuitState.HALF_OPEN)
                return True
            return False
        elif self.state == CircuitState.HALF_OPEN:
            return True
        return False

    def record_success(self) -> None:
        self.total_calls += 1
        self.total_successes += 1
        self.consecutive_failures = 0

        if self.state == CircuitState.HALF_OPEN:
            self._transition_to(CircuitState.CLOSED)

    def record_failure(self, error: Exception | str) -> None:
        self.total_calls += 1
        self.total_failures += 1
        self.consecutive_failures += 1
        self.consecutive_successes = 0
        self.last_failure_time = time.monotonic()

        if self.state in (CircuitState.CLOSED, CircuitState.HALF_OPEN):
            if self.state == CircuitState.HALF_OPEN or self.consecutive_failures >= self.failure_threshold:
                self._transition_to(CircuitState.OPEN)

    def _transition_to(self, new_state: CircuitState) -> None:
        if self.state != new_state:
            self.state = new_state
            self.transitions += 1
            self.last_state_change = time.monotonic()
            if new_state == CircuitState.CLOSED:
                self.consecutive_failures = 0
            elif new_state == CircuitState.HALF_OPEN:
                self.consecutive_successes = 0

    def metrics(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "state": self.state.value,
            "consecutive_failures": self.consecutive_failures,
            "total_calls": self.total_calls,
            "total_failures": self.total_failures,
            "total_successes": self.total_successes,
            "transitions": self.transitions,
            "first_clause_deadline_s": self.first_clause_deadline_s,
            "later_clause_deadline_s": self.later_clause_deadline_s,
            "probe_timeout_s": self.probe_timeout_s,
        }

    async def execute_with_retry(
        self,
        func: Callable[[], Coroutine[Any, Any, Any]],
        deadline_s: float | None = None,
        max_retries: int = 1,
        base_backoff_s: float = 0.2,
        is_transient_fn: Callable[[Exception], bool] | None = None,
    ) -> Any:
        """Execute async function guarded by circuit breaker with finite deadline and jittered backoff.

        If in HALF_OPEN state, uses probe_timeout_s (60s) unless a smaller deadline is specified.
        """
        if not self.can_attempt():
            raise CircuitBreakerOpen(self.service, f"Circuit is {self.state.value}")

        effective_deadline = deadline_s
        if effective_deadline is None:
            effective_deadline = self.probe_timeout_s if self.state == CircuitState.HALF_OPEN else self.first_clause_deadline_s

        t_start = time.monotonic()
        last_err: Exception | None = None

        for attempt in range(max_retries + 1):
            remaining = effective_deadline - (time.monotonic() - t_start)
            if remaining <= 0:
                self.record_failure("deadline_exceeded")
                raise DeadlineExceeded(self.service, effective_deadline)

            try:
                res = await asyncio.wait_for(func(), timeout=remaining)
                self.record_success()
                return res
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                last_err = DeadlineExceeded(self.service, effective_deadline)
                self.record_failure(last_err)
                raise last_err
            except Exception as e:
                last_err = e
                self.record_failure(e)
                is_transient = is_transient_fn(e) if is_transient_fn else False
                if attempt < max_retries and is_transient:
                    jitter = random.uniform(0.8, 1.2)
                    backoff = min(base_backoff_s * (2 ** attempt) * jitter, 2.0)
                    if (time.monotonic() - t_start + backoff) >= effective_deadline:
                        break
                    await asyncio.sleep(backoff)
                else:
                    break

        if last_err is not None:
            raise last_err
        raise DeadlineExceeded(self.service, effective_deadline)
