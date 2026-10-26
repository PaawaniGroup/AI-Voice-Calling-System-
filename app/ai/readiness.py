"""Readiness Gate for Voice AI Services (STT, Brain, TTS).

Ground Truth Rule:
A worker counts as READY only if a job (a warmup or a real job) completed within a
configurable window (job_window_s). Health counts ('idle 2, ready 2') alone are NEVER
proof of readiness, as RunPod workers can be blocked, stuck downloading, or queued.

States:
- READY: A job completed successfully within job_window_s, and circuit breaker is CLOSED.
- WARMING: No job completed within job_window_s, but health check reports workers exist/initializing.
- DOWN: Health check reports 0 workers / unhealthy > 0 / HTTP error, or circuit breaker is OPEN,
        or jobs are stuck in queue without completion.

Per-Tenant Flag:
- ai_readiness_required: stored in per-tenant ai_config document (default False).
  When True, call flow verifies all required services are READY before starting an AI session.
  If not READY, falls back to unready call XML greeting and politely terminates.
"""
from __future__ import annotations

import enum
import time
from typing import Any

from app.ai.circuit_breaker import CircuitBreaker, CircuitState


class ServiceState(str, enum.Enum):
    READY = "READY"
    WARMING = "WARMING"
    DOWN = "DOWN"


class ReadinessTracker:
    def __init__(
        self,
        services: tuple[str, ...] = ("stt", "brain", "tts"),
        job_window_s: float = 300.0,  # 5 minutes: completed job required within this window
        cache_ttl_s: float = 5.0,     # Cache evaluated readiness for 5 seconds
        circuit_failure_threshold: int = 3,
        circuit_recovery_timeout_s: float = 30.0,
        probe_timeout_s: float = 60.0,
    ):
        self.services = services
        self.job_window_s = job_window_s
        self.cache_ttl_s = cache_ttl_s

        self.last_job_completed: dict[str, float] = {}  # service -> monotonic timestamp
        self.last_job_id: dict[str, str] = {}
        self.breakers: dict[str, CircuitBreaker] = {
            svc: CircuitBreaker(
                svc,
                failure_threshold=circuit_failure_threshold,
                recovery_timeout_s=circuit_recovery_timeout_s,
                probe_timeout_s=probe_timeout_s,
            )
            for svc in services
        }

        # Cached state
        self._cached_eval: dict[str, ServiceState] = {}
        self._cached_time: float = 0.0

    def record_job_completion(self, service: str, job_id: str, duration_s: float = 0.0) -> None:
        """Record that a real RunPod job finished successfully on the worker."""
        svc = service.lower()
        now = time.monotonic()
        self.last_job_completed[svc] = now
        self.last_job_id[svc] = job_id
        if svc in self.breakers:
            self.breakers[svc].record_success()
        self._invalidate_cache()

    def record_job_failure(self, service: str, error: Exception | str) -> None:
        svc = service.lower()
        if svc in self.breakers:
            self.breakers[svc].record_failure(error)
        self._invalidate_cache()

    def _invalidate_cache(self) -> None:
        self._cached_time = 0.0

    def is_service_ready(self, service: str, health_data: dict[str, Any] | None = None) -> bool:
        return self.evaluate_service(service, health_data) == ServiceState.READY

    def is_tenant_call_allowed(self, tenant_ai_config: dict[str, Any], health_map: dict[str, Any] | None = None) -> tuple[bool, str]:
        """Check if call should proceed based on per-tenant ai_readiness_required flag."""
        required = bool(tenant_ai_config.get("ai_readiness_required", False))
        if not required:
            return True, "readiness_not_required"

        states = self.evaluate_all(health_map)
        needed = [s for s in ("stt", "brain", "tts") if tenant_ai_config.get(f"{s}_url")]
        unready = [s for s in needed if states.get(s) != ServiceState.READY]
        if unready:
            return False, f"services_unready:{','.join(unready)}"
        return True, "all_ready"

    def evaluate_service(
        self,
        service: str,
        health_data: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> ServiceState:
        svc = service.lower()
        t = now if now is not None else time.monotonic()

        breaker = self.breakers.get(svc)
        if breaker and breaker.state == CircuitState.OPEN:
            return ServiceState.DOWN

        last_comp = self.last_job_completed.get(svc)
        has_recent_job = last_comp is not None and (t - last_comp) <= self.job_window_s

        if has_recent_job:
            return ServiceState.READY

        if health_data is None:
            return ServiceState.DOWN

        workers = health_data.get("workers") or {}
        jobs = health_data.get("jobs") or {}

        unhealthy = workers.get("unhealthy", 0)
        throttled = workers.get("throttled", 0)
        ready_cnt = workers.get("ready", 0)
        idle_cnt = workers.get("idle", 0)
        init_cnt = workers.get("initializing", 0)
        in_queue = jobs.get("inQueue", 0)

        if unhealthy > 0 or throttled > 0:
            return ServiceState.DOWN
        if (ready_cnt + idle_cnt + init_cnt) == 0:
            return ServiceState.DOWN

        if in_queue > 0 and not has_recent_job:
            return ServiceState.WARMING if init_cnt > 0 else ServiceState.DOWN

        return ServiceState.WARMING

    def evaluate_all(
        self,
        health_map: dict[str, dict[str, Any]] | None = None,
        use_cache: bool = True,
    ) -> dict[str, ServiceState]:
        now = time.monotonic()
        if use_cache and (now - self._cached_time) < self.cache_ttl_s and self._cached_eval:
            return dict(self._cached_eval)

        res = {}
        for svc in self.services:
            h = (health_map or {}).get(svc)
            res[svc] = self.evaluate_service(svc, h, now=now)

        self._cached_eval = dict(res)
        self._cached_time = now
        return res

    def get_public_report(self, health_map: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
        """Readiness report for API status endpoint: zero secrets, zero keys, zero audio transcripts."""
        states = self.evaluate_all(health_map, use_cache=True)
        now = time.monotonic()

        out = {
            "overall_ready": all(s == ServiceState.READY for s in states.values()),
            "services": {}
        }

        for svc in self.services:
            last_ts = self.last_job_completed.get(svc)
            ago_s = round(now - last_ts, 1) if last_ts is not None else None
            b = self.breakers.get(svc)
            out["services"][svc] = {
                "state": states[svc].value,
                "last_job_completed_s_ago": ago_s,
                "circuit_state": b.state.value if b else "CLOSED",
                "total_failures": b.total_failures if b else 0,
            }
        return out


def generate_unready_call_xml(reason: str = "warming") -> str:
    """Generate English-only Plivo Speak rejection/hangup XML.

    NOTE ON PLIVO COMPLIANCE:
    The language/voice ('en-IN', 'WOMAN') and Hangup reason ('rejected') are UNVERIFIED:
    no official Plivo documentation was fetched or read in this session.
    Per hard rules, English only is strictly retained.
    """
    message = "Welcome to MME. Our voice service is currently warming up. Please call back shortly."
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Response>\n'
        f'    <!-- UNVERIFIED attributes: voice="WOMAN" language="en-IN" reason="rejected" -->\n'
        f'    <Speak language="en-IN" voice="WOMAN">{message}</Speak>\n'
        '    <Hangup reason="rejected"/>\n'
        '</Response>'
    )
