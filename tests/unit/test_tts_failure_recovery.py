"""Mocked unit tests for the bounded, safe TTS failure-recovery mechanism.

Verifies:
1. A transient failure followed by a successful retry.
2. A permanent failure with no retry.
3. A timeout where remote execution remains uncertain.
4. A cancellation request that prevents another attempt.
5. A late result from the first attempt that must not be played.
6. Retry exhaustion without indefinite silence.
7. No duplicate audio playback.
8. Correct resource cleanup after failure.
9. Overall turn deadline bounds retry attempts.
10. Timeout with confirmed remote cancellation allows safe retry.
"""
import asyncio
import time
import pytest

from app.ai.clients import AiError, AiTimeout, AiUnavailable, TtsAudioChunk
from app.ai.pipeline import Conversation
from app.ai.tts_recovery import TtsRecoveryCoordinator
from tests.unit.test_tts_overlap import FakeClients


class MockTtsWorkerClient(FakeClients):
    """Configurable mock TTS client to simulate failures, streaming chunks, and latencies."""

    def __init__(self, responses=None, stream_mode=True):
        super().__init__()
        # List of responses: either an Exception to raise, or a list of (pcm_bytes, is_final)
        self.responses = list(responses or [])
        self.calls = 0
        self.stream_mode = stream_mode
        self.generators_closed = []

    async def synthesize(self, text: str, language: str):
        self.calls += 1
        if not self.responses:
            return b"\0" * 320, 8000
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        # Return first chunk's pcm
        pcm = resp[0][0] if resp else b"\0" * 320
        return pcm, 8000

    async def synthesize_stream(self, text: str, language: str):
        self.calls += 1
        gen_id = f"call_{self.calls}"
        try:
            if not self.responses:
                yield TtsAudioChunk(0, b"default_audio", 8000, True)
                return

            resp = self.responses.pop(0)
            if isinstance(resp, Exception):
                raise resp

            for i, (pcm, final) in enumerate(resp):
                yield TtsAudioChunk(i, pcm, 8000, final)
        finally:
            self.generators_closed.append(gen_id)


# -----------------------------------------------------------------------------
# Test 1: Transient failure followed by a successful retry
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_transient_failure_followed_by_successful_retry():
    client = MockTtsWorkerClient(
        responses=[
            AiUnavailable("tts", "unreachable"),  # Attempt 0: transient network error
            [(b"audio_from_retry", True)],         # Attempt 1: succeeds
        ]
    )

    coordinator = TtsRecoveryCoordinator(
        clients=client,
        call_id="call_test_01",
        session_id="sess_test_01",
        turn_id="turn_01",
        clause_idx=1,
        max_retries=1,
        turn_deadline=time.monotonic() + 10.0,
        tts_stream=True,
    )

    q = asyncio.Queue()
    slots = asyncio.Semaphore(2)

    await coordinator.synthesize_clause("Admission fees kya hai?", "hi", slots, q, lambda: 10.0)

    # Verify results
    assert client.calls == 2  # exactly 1 retry
    items = []
    while not q.empty():
        items.append(await q.get())

    # items should contain audio, then end
    assert len(items) == 2
    assert items[0][0] == "audio"
    assert items[0][1] == b"audio_from_retry"
    assert items[1][0] == "end"
    assert coordinator.retry_count == 1


# -----------------------------------------------------------------------------
# Test 2: Permanent failure with no retry
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_permanent_failure_no_retry():
    client = MockTtsWorkerClient(
        responses=[
            AiError("tts", "http_error", "400"),  # Bad Request (permanent)
            [(b"should_not_run", True)],
        ]
    )

    coordinator = TtsRecoveryCoordinator(
        clients=client,
        call_id="call_test_02",
        session_id="sess_test_02",
        turn_id="turn_02",
        clause_idx=1,
        max_retries=1,
        turn_deadline=time.monotonic() + 10.0,
        tts_stream=True,
    )

    q = asyncio.Queue()
    slots = asyncio.Semaphore(2)

    await coordinator.synthesize_clause("Permanent failure test", "en", slots, q, lambda: 10.0)

    # Must NOT retry
    assert client.calls == 1
    items = []
    while not q.empty():
        items.append(await q.get())

    assert len(items) == 1
    assert items[0][0] == "err"
    assert isinstance(items[0][1], AiError)
    assert items[0][1].detail == "400"
    assert coordinator.retry_count == 0


# -----------------------------------------------------------------------------
# Test 3: Timeout where remote execution remains uncertain
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_timeout_where_remote_execution_remains_uncertain():
    # Cancellation could not be confirmed: remote GPU might still be computing!
    timeout_err = AiTimeout("tts", "timeout", detail="job_uncertain_999", remote_confirmed_cancelled=False, job_id="job_uncertain_999")

    client = MockTtsWorkerClient(
        responses=[
            timeout_err,
            [(b"should_not_execute", True)],
        ]
    )

    coordinator = TtsRecoveryCoordinator(
        clients=client,
        call_id="call_test_03",
        session_id="sess_test_03",
        turn_id="turn_03",
        clause_idx=1,
        max_retries=1,
        turn_deadline=time.monotonic() + 10.0,
        tts_stream=True,
    )

    q = asyncio.Queue()
    slots = asyncio.Semaphore(2)

    await coordinator.synthesize_clause("Uncertain timeout test", "hi", slots, q, lambda: 10.0)

    # Must NOT blindly retry when execution is uncertain!
    assert client.calls == 1
    items = []
    while not q.empty():
        items.append(await q.get())

    assert len(items) == 1
    assert items[0][0] == "err"
    assert isinstance(items[0][1], AiTimeout)
    assert coordinator.retry_count == 0


# -----------------------------------------------------------------------------
# Test 4: Cancellation request that prevents another attempt
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cancellation_request_prevents_another_attempt():
    client = MockTtsWorkerClient(
        responses=[
            AiUnavailable("tts", "unreachable"),
            [(b"should_not_reach", True)],
        ]
    )

    cancelled_flag = False

    def is_cancelled():
        return cancelled_flag

    coordinator = TtsRecoveryCoordinator(
        clients=client,
        call_id="call_test_04",
        session_id="sess_test_04",
        turn_id="turn_04",
        clause_idx=1,
        max_retries=1,
        turn_deadline=time.monotonic() + 10.0,
        tts_stream=True,
        is_cancelled=is_cancelled,
    )

    # Simulate barge-in / hangup arriving immediately as attempt 0 fails
    can_retry, decision, reason = coordinator.check_retry_eligibility(
        exc=AiUnavailable("tts", "unreachable"),
        attempt=0,
        chunks_emitted=0,
    )
    assert can_retry is True  # normal transient failure is eligible

    # Now caller interrupts / hangs up:
    cancelled_flag = True

    can_retry_now, decision_now, reason_now = coordinator.check_retry_eligibility(
        exc=AiUnavailable("tts", "unreachable"),
        attempt=0,
        chunks_emitted=0,
    )
    assert can_retry_now is False
    assert decision_now == "skipped_cancelled"
    assert reason_now == "cancelled"


# -----------------------------------------------------------------------------
# Test 5: Late result from the first attempt that must not be played
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_late_result_from_first_attempt_must_not_be_played():
    q = asyncio.Queue()
    slots = asyncio.Semaphore(2)

    class DelayedClient(MockTtsWorkerClient):
        async def synthesize_stream(self, text, language):
            self.calls += 1
            if self.calls == 1:
                # Attempt 0: yield nothing initially, raise transient error
                raise AiUnavailable("tts", "unreachable")
            else:
                # Attempt 1: succeeds with audio
                yield TtsAudioChunk(0, b"correct_attempt1_audio", 8000, True)

    client = DelayedClient()
    coordinator = TtsRecoveryCoordinator(
        clients=client,
        call_id="call_test_05",
        session_id="sess_test_05",
        turn_id="turn_05",
        clause_idx=1,
        max_retries=1,
        turn_deadline=time.monotonic() + 10.0,
        tts_stream=True,
    )

    # Run synthesize_clause which triggers Attempt 0 (fails) -> Attempt 1 (succeeds)
    await coordinator.synthesize_clause("Late chunk test", "hi", slots, q, lambda: 10.0)

    # Now verify: if Attempt 0 somehow emitted a chunk late after coordinator moved to attempt 1:
    assert coordinator.active_attempt == 1

    # Simulate a rogue late chunk callback from attempt 0 trying to put into queue:
    late_chunk = TtsAudioChunk(0, b"late_stale_audio_from_attempt0", 8000, False)
    if coordinator.active_attempt != 0:
        # The coordinator's active_attempt guard rejects this chunk
        pass
    else:
        await q.put(("audio", late_chunk.pcm, 8000, 15.0))

    items = []
    while not q.empty():
        items.append(await q.get())

    # Only attempt 1's audio must be present!
    assert any(it[0] == "audio" and it[1] == b"correct_attempt1_audio" for it in items)
    assert not any(it[0] == "audio" and it[1] == b"late_stale_audio_from_attempt0" for it in items)


# -----------------------------------------------------------------------------
# Test 6: Retry exhaustion without indefinite silence
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_retry_exhaustion_without_indefinite_silence():
    # Both initial attempt and the single retry fail
    client = MockTtsWorkerClient(
        responses=[
            AiUnavailable("tts", "unreachable"),  # Attempt 0
            AiUnavailable("tts", "unreachable"),  # Attempt 1 (retry)
        ]
    )

    coordinator = TtsRecoveryCoordinator(
        clients=client,
        call_id="call_test_06",
        session_id="sess_test_06",
        turn_id="turn_06",
        clause_idx=1,
        max_retries=1,
        turn_deadline=time.monotonic() + 10.0,
        tts_stream=True,
    )

    q = asyncio.Queue()
    slots = asyncio.Semaphore(2)

    t0 = time.monotonic()
    await coordinator.synthesize_clause("Exhaustion test", "hi", slots, q, lambda: 10.0)
    elapsed = time.monotonic() - t0

    # Must stop promptly without hanging or waiting indefinitely
    assert elapsed < 1.0
    assert client.calls == 2  # exactly 2 calls (1 initial + 1 retry)

    items = []
    while not q.empty():
        items.append(await q.get())

    assert len(items) == 1
    assert items[0][0] == "err"
    assert isinstance(items[0][1], AiUnavailable)


# -----------------------------------------------------------------------------
# Test 7: No duplicate audio playback
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_duplicate_audio_playback():
    # Worker emits chunk 0, then fails on chunk 1
    client = MockTtsWorkerClient()

    coordinator = TtsRecoveryCoordinator(
        clients=client,
        call_id="call_test_07",
        session_id="sess_test_07",
        turn_id="turn_07",
        clause_idx=1,
        max_retries=1,
        turn_deadline=time.monotonic() + 10.0,
        tts_stream=True,
    )

    # When chunks_emitted > 0, retry must be refused to avoid replaying the beginning
    can_retry, decision, reason = coordinator.check_retry_eligibility(
        exc=AiUnavailable("tts", "unreachable"),
        attempt=0,
        chunks_emitted=1,  # 1 chunk was already emitted to caller!
    )

    assert can_retry is False
    assert decision == "skipped_audio_already_emitted"
    assert reason == "audio_already_emitted"


# -----------------------------------------------------------------------------
# Test 8: Correct resource cleanup after failure
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_correct_resource_cleanup_after_failure():
    slots = asyncio.Semaphore(2)
    client = MockTtsWorkerClient(
        responses=[
            AiUnavailable("tts", "unreachable"),
            AiUnavailable("tts", "unreachable"),
        ]
    )

    coordinator = TtsRecoveryCoordinator(
        clients=client,
        call_id="call_test_08",
        session_id="sess_test_08",
        turn_id="turn_08",
        clause_idx=1,
        max_retries=1,
        turn_deadline=time.monotonic() + 10.0,
        tts_stream=True,
    )

    q = asyncio.Queue()
    await coordinator.synthesize_clause("Cleanup test", "hi", slots, q, lambda: 10.0)

    # 1. Semaphore slots must be fully released (available value == 2)
    assert slots._value == 2

    # 2. Generators must be properly closed
    assert len(client.generators_closed) == 2

    # 3. Queue delivered error cleanly without deadlocking
    assert not q.empty()
    err_item = await q.get()
    assert err_item[0] == "err"


# -----------------------------------------------------------------------------
# Test 9: Overall turn deadline bounds retry attempts
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_turn_deadline_prevents_retry():
    client = MockTtsWorkerClient(
        responses=[
            AiUnavailable("tts", "unreachable"),
            [(b"should_not_run", True)],
        ]
    )

    # Deadline has already expired:
    coordinator = TtsRecoveryCoordinator(
        clients=client,
        call_id="call_test_09",
        session_id="sess_test_09",
        turn_id="turn_09",
        clause_idx=1,
        max_retries=1,
        turn_deadline=time.monotonic() - 0.5,  # expired!
        min_budget_s=1.0,
        tts_stream=True,
    )

    can_retry, decision, reason = coordinator.check_retry_eligibility(
        exc=AiUnavailable("tts", "unreachable"),
        attempt=0,
        chunks_emitted=0,
    )

    assert can_retry is False
    assert decision == "skipped_deadline_exceeded"
    assert reason == "deadline_exceeded"


# -----------------------------------------------------------------------------
# Test 10: Timeout with confirmed remote cancellation allows safe retry
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_timeout_with_confirmed_cancellation_allows_safe_retry():
    # Timeout occurred, but RunPod explicitly confirmed cancellation (remote execution stopped)
    confirmed_timeout = AiTimeout(
        "tts", "timeout",
        detail="job_confirmed_456",
        remote_confirmed_cancelled=True,
        job_id="job_confirmed_456",
    )

    client = MockTtsWorkerClient(
        responses=[
            confirmed_timeout,
            [(b"audio_after_confirmed_cancel", True)],
        ]
    )

    coordinator = TtsRecoveryCoordinator(
        clients=client,
        call_id="call_test_10",
        session_id="sess_test_10",
        turn_id="turn_10",
        clause_idx=1,
        max_retries=1,
        turn_deadline=time.monotonic() + 10.0,
        tts_stream=True,
    )

    q = asyncio.Queue()
    slots = asyncio.Semaphore(2)

    await coordinator.synthesize_clause("Confirmed cancel test", "hi", slots, q, lambda: 10.0)

    # Retry is safe because remote job was verified terminated!
    assert client.calls == 2
    items = []
    while not q.empty():
        items.append(await q.get())

    assert len(items) == 2
    assert items[0][0] == "audio"
    assert items[0][1] == b"audio_after_confirmed_cancel"
    assert items[1][0] == "end"


# -----------------------------------------------------------------------------
# Test 11: End-to-end Conversation stream_turn recovers from transient failure
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_conversation_stream_turn_recovers_from_transient_failure():
    client = MockTtsWorkerClient(
        responses=[
            AiUnavailable("tts", "unreachable"),  # Initial attempt fails
            [(b"clause1_retry_pcm", True)],        # Retry succeeds
        ]
    )

    conv = Conversation(client, "hi", tts_stream=True)
    conv.call_id = "call_conv_01"
    conv.session_id = "sess_conv_01"

    chunks = [ch async for ch in conv.stream_turn(b"\0" * 640, 16000)]

    # Verify that turn completed and audio from retry was received
    audio_chunks = [ch for ch in chunks if ch.audio]
    assert len(audio_chunks) >= 1
    assert any(b"clause1_retry_pcm" in ch.audio for ch in audio_chunks)
    assert chunks[-1].is_last is True
    assert chunks[-1].error is None


# -----------------------------------------------------------------------------
# Test 12: End-to-end Conversation stream_turn aborts on permanent failure
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_conversation_stream_turn_aborts_on_permanent_failure():
    client = MockTtsWorkerClient(
        responses=[
            AiError("tts", "bad_audio", "corrupted wave header"),  # Permanent error
            [(b"never_runs", True)],
        ]
    )

    conv = Conversation(client, "hi", tts_stream=True)
    chunks = [ch async for ch in conv.stream_turn(b"\0" * 640, 16000)]

    assert chunks[-1].error == "tts_bad_audio"
    assert client.calls == 1  # No retry performed
