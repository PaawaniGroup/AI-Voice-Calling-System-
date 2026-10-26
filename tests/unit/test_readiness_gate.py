"""Unit tests for WP2: Readiness Gate and Circuit Breakers (SIMULATED / MOCKED).

All tests in this file use simulated in-memory state and mock fixtures (zero chargeable RunPod calls).
Measured baseline numbers: first clause 8s, later clauses 10s, probe timeout 60s.
"""
import asyncio
import time
import pytest

from app.ai.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpen,
    CircuitState,
    DeadlineExceeded,
    DEFAULT_FIRST_CLAUSE_DEADLINE_S,
    DEFAULT_LATER_CLAUSE_DEADLINE_S,
    DEFAULT_PROBE_TIMEOUT_S,
)
from app.ai.readiness import ReadinessTracker, ServiceState, generate_unready_call_xml


def test_simulated_mock_idle2_ready2_queued_is_never_ready():
    """SIMULATED: A mock reproducing 'idle 2, ready 2, jobs queued' MUST give DOWN or WARMING."""
    tracker = ReadinessTracker(job_window_s=60.0)

    mock_health = {
        "workers": {"idle": 2, "ready": 2, "running": 0, "initializing": 0, "unhealthy": 0},
        "jobs": {"inQueue": 1, "inProgress": 0, "completed": 0, "failed": 0}
    }

    state = tracker.evaluate_service("tts", mock_health)
    assert state in (ServiceState.WARMING, ServiceState.DOWN), f"Expected WARMING or DOWN, got {state}"
    assert state != ServiceState.READY, "Health counts alone must NEVER declare READY!"


def test_simulated_job_completion_grants_ready_state():
    """SIMULATED: Service becomes READY only when a job completion is recorded within the window."""
    tracker = ReadinessTracker(job_window_s=10.0)

    assert tracker.evaluate_service("stt") == ServiceState.DOWN

    tracker.record_job_completion("stt", job_id="job-12345")
    assert tracker.evaluate_service("stt") == ServiceState.READY

    t_future = time.monotonic() + 15.0
    state_expired = tracker.evaluate_service("stt", now=t_future)
    assert state_expired != ServiceState.READY, "Should no longer be READY after window expired"


def test_simulated_circuit_breaker_threshold_and_recovery():
    """SIMULATED: Circuit breaker transitions CLOSED -> OPEN (after 3 failures) -> HALF_OPEN (after recovery timeout) -> CLOSED."""
    cb = CircuitBreaker("brain", failure_threshold=3, recovery_timeout_s=0.1, probe_timeout_s=60.0)
    assert cb.state == CircuitState.CLOSED
    assert cb.can_attempt() is True
    assert cb.probe_timeout_s == 60.0

    cb.record_failure("error 1")
    cb.record_failure("error 2")
    cb.record_failure("error 3")
    assert cb.state == CircuitState.OPEN
    assert cb.can_attempt() is False

    with pytest.raises(CircuitBreakerOpen):
        asyncio.run(cb.execute_with_retry(lambda: asyncio.sleep(0.01)))

    time.sleep(0.12)
    assert cb.can_attempt() is True
    assert cb.state == CircuitState.HALF_OPEN

    cb.record_success()
    assert cb.state == CircuitState.CLOSED
    assert cb.consecutive_failures == 0


@pytest.mark.asyncio
async def test_simulated_measured_deadlines_enforcement():
    """SIMULATED: First clause deadline defaults to 8.0s, later clause 10.0s, probe timeout 60.0s."""
    cb = CircuitBreaker("tts", first_clause_deadline_s=8.0, later_clause_deadline_s=10.0, probe_timeout_s=60.0)
    assert cb.first_clause_deadline_s == DEFAULT_FIRST_CLAUSE_DEADLINE_S
    assert cb.later_clause_deadline_s == DEFAULT_LATER_CLAUSE_DEADLINE_S
    assert cb.probe_timeout_s == DEFAULT_PROBE_TIMEOUT_S

    call_count = 0

    async def slow_func():
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.3)
        return "ok"

    # Deadline of 0.05s must fail fast with DeadlineExceeded without retrying past deadline
    with pytest.raises(DeadlineExceeded):
        await cb.execute_with_retry(slow_func, deadline_s=0.05, max_retries=2, base_backoff_s=0.02)

    assert call_count == 1, "Must abort after deadline without useless retries"


@pytest.mark.asyncio
async def test_simulated_retry_on_transient_error_only():
    """SIMULATED: Bounded retries with jittered backoff on transient errors only."""
    cb = CircuitBreaker("stt")
    attempts = 0

    async def transient_then_ok():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionResetError("network glitch")
        return "success"

    def is_transient(e: Exception) -> bool:
        return isinstance(e, ConnectionResetError)

    res = await cb.execute_with_retry(
        transient_then_ok,
        deadline_s=2.0,
        max_retries=2,
        base_backoff_s=0.02,
        is_transient_fn=is_transient
    )
    assert res == "success"
    assert attempts == 2


def test_simulated_plivo_fallback_xml_english_only_and_unverified():
    """SIMULATED: Fallback XML strictly retains English only and marks voice/reason unverified."""
    xml = generate_unready_call_xml()
    assert "<Response>" in xml
    assert "<Speak" in xml
    assert 'language="en-IN"' in xml
    assert "UNVERIFIED" in xml
    assert "Hindi" not in xml
    assert "<Hangup" in xml


def test_simulated_tenant_readiness_flag():
    """SIMULATED: Test per-tenant ai_readiness_required flag gating."""
    tracker = ReadinessTracker()

    # When flag is False (default): allowed even if services are not ready
    allowed, reason = tracker.is_tenant_call_allowed({"ai_readiness_required": False, "tts_url": "http://x"})
    assert allowed is True
    assert reason == "readiness_not_required"

    # When flag is True: blocked if services are not ready
    allowed, reason = tracker.is_tenant_call_allowed({"ai_readiness_required": True, "tts_url": "http://x"})
    assert allowed is False
    assert "services_unready" in reason


def test_simulated_public_report_has_zero_secrets_or_transcripts():
    """SIMULATED: Report contains zero keys, Bearer tokens, or transcripts."""
    tracker = ReadinessTracker()
    tracker.record_job_completion("brain", "job-999")

    report = tracker.get_public_report()
    report_str = str(report)

    assert "rpa_" not in report_str
    assert "Bearer" not in report_str
    assert "transcript" not in report_str
    assert "caller" not in report_str
    assert "overall_ready" in report
    assert report["services"]["brain"]["state"] == "READY"
    assert report["services"]["tts"]["state"] == "DOWN"
