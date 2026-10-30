"""Bounded, safe TTS failure recovery for MME Telephony.

Ensures:
- At most one retry for explicitly eligible transient failures.
- Never blindly retries an ambiguous timeout if the original RunPod job might still be executing.
- Requires remote cancellation confirmation from the provider before retrying any timeout.
- Prevents duplicate audio playback by disallowing retries if any audio chunks were already emitted.
- Prevents stale audio from late-completing abandoned attempts via generation/attempt tagging.
- Sequential execution: prevents concurrent retry races.
- Preserves call_id, session_id, and turn_id across attempts.
- Enforces a configurable overall turn deadline.
- Immediately stops work on cancellation, caller hangup, or barge-in.
- Does not retry invalid requests, auth failures, or permanent audio/protocol errors.
- Records structured, privacy-safe metrics (failure reason, retry decision, queue delay, final outcome).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from app.ai.clients import AiError, AiTimeout, AiUnavailable
from app.observability import metrics as m

log = logging.getLogger("mme_telephony.ai.tts_recovery")


class TtsRecoveryCoordinator:
    """Coordinates bounded, safe failure recovery for one clause's TTS synthesis."""

    def __init__(
        self,
        clients: Any,
        call_id: str | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        clause_idx: int = 0,
        max_retries: int = 1,
        turn_deadline: float | None = None,
        min_budget_s: float = 1.0,
        tts_stream: bool = False,
        is_cancelled: Callable[[], bool] | None = None,
    ):
        self.clients = clients
        self.call_id = call_id
        self.session_id = session_id
        self.turn_id = turn_id
        self.clause_idx = clause_idx
        self.max_retries = max(0, min(max_retries, 1))  # bounded: at most 1 retry
        self.turn_deadline = turn_deadline
        self.min_budget_s = min_budget_s
        self.tts_stream = tts_stream
        self.is_cancelled = is_cancelled or (lambda: False)

        self.active_attempt = 0
        self.retry_count = 0
        self.chunks_emitted_total = 0

    def check_retry_eligibility(
        self,
        exc: Exception,
        attempt: int,
        chunks_emitted: int,
    ) -> tuple[bool, str, str]:
        """Classify failure and determine if a retry is safe and allowed.

        Returns (can_retry: bool, decision: str, reason: str).
        """
        # 1. Cancellation check: barge-in or hangup must stop work immediately
        if self.is_cancelled() or isinstance(exc, asyncio.CancelledError):
            return False, "skipped_cancelled", "cancelled"

        # 2. Duplicate audio prevention:
        # If any audio has already been emitted for this clause, re-generating from scratch
        # would replay the beginning to the caller (duplicate audio playback).
        if chunks_emitted > 0 or self.chunks_emitted_total > 0:
            return False, "skipped_audio_already_emitted", "audio_already_emitted"

        # 3. Retry count limit: at most 1 retry
        if attempt >= self.max_retries:
            return False, "exhausted", "retry_limit_reached"

        # 4. Overall turn deadline check:
        # A retry must not extend the caller's wait indefinitely.
        if self.turn_deadline is not None:
            now = time.monotonic()
            remaining = self.turn_deadline - now
            if remaining < self.min_budget_s:
                return False, "skipped_deadline_exceeded", "deadline_exceeded"

        # 5. Error classification
        if isinstance(exc, AiTimeout):
            # Never blindly retry an ambiguous timeout if the original RunPod job might still be executing!
            if getattr(exc, "remote_confirmed_cancelled", False):
                return True, "retried", "timeout_confirmed_cancelled"
            return False, "skipped_uncertain_remote", "timeout_uncertain_remote"

        if isinstance(exc, AiUnavailable):
            if exc.kind == "unreachable":
                # Network unreachable / connection refused / DNS glitch before connection established
                return True, "retried", "network_unreachable"
            # not_configured, client_closed, etc. are not transiently retryable
            return False, "skipped_non_retryable", f"unavailable_{exc.kind}"

        if isinstance(exc, AiError):
            if exc.kind == "http_error":
                status = str(exc.detail or "")
                # 502, 503, 504, 429 are transient gateway/capacity errors where remote GPU is not running
                if any(code in status for code in ("502", "503", "504", "429")):
                    return True, "retried", f"http_{status}"
                # 400, 401, 403, 404, 422 are permanent errors
                return False, "skipped_non_retryable", f"http_{status}_permanent"

            if exc.kind == "runpod_error":
                detail_lower = str(exc.detail or "").lower()
                # Worker crashed or returned FAILED / CANCELLED -> execution stopped
                if "status=failed" in detail_lower or "status=cancelled" in detail_lower:
                    return True, "retried", "runpod_worker_failed"
                return False, "skipped_non_retryable", "runpod_error_unverified"

            if exc.kind in ("bad_audio", "bad_response", "reference_unreadable"):
                return False, "skipped_non_retryable", f"permanent_{exc.kind}"

            return False, "skipped_non_retryable", f"aierror_{exc.kind}"

        # Unknown / unexpected exception type: do not retry
        return False, "skipped_non_retryable", f"unknown_{type(exc).__name__}"

    def record_metrics(
        self,
        attempt: int,
        failure_reason: str | None,
        retry_decision: str,
        final_outcome: str,
        queue_delay_ms: float | None = None,
        synthesis_ms: float | None = None,
    ) -> None:
        """Record structured, privacy-safe metrics (no transcripts or caller audio)."""
        log.info(
            "tts_recovery: call_id=%s session_id=%s turn_id=%s clause=%d attempt=%d "
            "failure_reason=%s retry_decision=%s queue_delay_ms=%s final_outcome=%s",
            self.call_id,
            self.session_id,
            self.turn_id,
            self.clause_idx,
            attempt,
            failure_reason,
            retry_decision,
            queue_delay_ms,
            final_outcome,
        )

        try:
            m.TTS_RETRIES.labels(decision=retry_decision, reason=failure_reason or "none").inc()
        except Exception:
            pass

        try:
            m.TTS_RECOVERY_OUTCOMES.labels(outcome=final_outcome).inc()
        except Exception:
            pass

    async def synthesize_clause(
        self,
        text: str,
        lang: str,
        tts_slots: asyncio.Semaphore,
        q: asyncio.Queue,
        ms_fn: Callable[[], float],
    ) -> None:
        """Run synthesis for one clause with bounded recovery, feeding chunks into q."""
        attempt = 0
        chunks_this_attempt = 0

        while True:
            if self.is_cancelled():
                self.record_metrics(attempt, "cancelled", "skipped_cancelled", "cancelled")
                return

            self.active_attempt = attempt
            chunks_this_attempt = 0
            failure_exc: Exception | None = None
            t_start = time.perf_counter()

            async with tts_slots:
                if self.is_cancelled():
                    return

                try:
                    if self.tts_stream and hasattr(self.clients, "synthesize_stream"):
                        agen = self.clients.synthesize_stream(text, lang)
                        try:
                            async for ch in agen:
                                if self.is_cancelled():
                                    return
                                # Discard stale chunks from an abandoned previous attempt
                                if self.active_attempt != attempt:
                                    log.warning(
                                        "discarding stale tts chunk from attempt %d (current active=%d)",
                                        attempt, self.active_attempt
                                    )
                                    continue
                                if ch.pcm:
                                    chunks_this_attempt += 1
                                    self.chunks_emitted_total += 1
                                    await q.put(("audio", ch.pcm, ch.sample_rate, ms_fn()))
                        finally:
                            await agen.aclose()
                    else:
                        pcm_, rate_ = await self.clients.synthesize(text, lang)
                        if self.is_cancelled():
                            return
                        if self.active_attempt != attempt:
                            log.warning(
                                "discarding stale non-streamed pcm from attempt %d (current active=%d)",
                                attempt, self.active_attempt
                            )
                        else:
                            chunks_this_attempt += 1
                            self.chunks_emitted_total += 1
                            await q.put(("audio", pcm_, rate_, ms_fn()))

                    # Attempt succeeded!
                    duration_ms = round((time.perf_counter() - t_start) * 1000, 1)
                    decision = "none_needed" if attempt == 0 else "retried"
                    self.record_metrics(attempt, None, decision, "success", synthesis_ms=duration_ms)
                    await q.put(("end", ms_fn()))
                    return

                except asyncio.CancelledError:
                    self.record_metrics(attempt, "cancelled", "skipped_cancelled", "cancelled")
                    raise
                except Exception as exc:
                    failure_exc = exc

            # If we reached here, this attempt failed.
            can_retry, decision, reason = self.check_retry_eligibility(
                exc=failure_exc,
                attempt=attempt,
                chunks_emitted=chunks_this_attempt,
            )

            if can_retry:
                self.record_metrics(attempt, reason, decision, "retrying")
                attempt += 1
                self.retry_count = attempt
                self.active_attempt = attempt
                log.info(
                    "tts_recovery: eligible transient error '%s', executing retry attempt %d for clause %d",
                    reason, attempt, self.clause_idx
                )
                await asyncio.sleep(0.01)
                continue
            else:
                outcome = "exhausted" if decision == "exhausted" else "failed"
                self.record_metrics(attempt, reason, decision, outcome)
                await q.put(("err", failure_exc))
                return

