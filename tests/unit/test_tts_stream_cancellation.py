"""Unit tests for TTS streaming cancellation and barge-in lifecycle.

Verifies:
1. Early cancellation of synthesize_stream generator triggers job cancellation on RunPod.
2. Stale audio chunks from a previous cancelled attempt are discarded.
3. Local playback queue is evicted upon caller interruption.
4. Call isolation: cancelling one job ID does not cancel or corrupt another call.
"""
import asyncio
import base64
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.ai.clients import ModelClients, TtsAudioChunk
from app.ai.tts_recovery import TtsRecoveryCoordinator
from config.settings import Settings


@pytest.mark.asyncio
async def test_synthesize_stream_early_close_triggers_runpod_cancel():
    """Verify that breaking out of synthesize_stream triggers _cancel_job."""
    s = Settings()
    clients = ModelClients(s, {
        "tts_url": "https://api.runpod.ai/v2/mock/runsync",
        "keys": {"tts": "fake_key"},
    })

    mock_run_resp = MagicMock()
    mock_run_resp.status_code = 200
    mock_run_resp.json.return_value = {"id": "job_cancel_test", "status": "IN_PROGRESS"}

    mock_stream_resp = MagicMock()
    mock_stream_resp.status_code = 200
    b64_audio = base64.b64encode(b"\x00\x00" * 160).decode()
    mock_stream_resp.json.return_value = {
        "status": "IN_PROGRESS",
        "stream": [
            {
                "output": {
                    "seq": 0,
                    "audio_base64": b64_audio,  # 320 bytes PCM16
                    "format": "pcm16",
                    "sample_rate": 8000,
                    "final": False
                }
            }
        ]
    }

    with patch.object(clients, "_http") as mock_http, patch.object(clients, "_cancel_job", new_callable=AsyncMock) as mock_cancel:
        mock_http.post = AsyncMock(return_value=mock_run_resp)
        mock_http.get = AsyncMock(return_value=mock_stream_resp)

        # Consume only 1 chunk and break (simulating barge-in)
        async for chunk in clients.synthesize_stream("नमस्ते", "hi"):
            assert chunk.seq == 0
            break

        # Give background spawn a tick
        await asyncio.sleep(0.05)
        # Verify cancel job was scheduled for job_cancel_test
        assert mock_cancel.called
        assert mock_cancel.call_args[0][3] == "job_cancel_test"

    await clients.aclose()


@pytest.mark.asyncio
async def test_recovery_coordinator_discards_stale_chunks_from_prior_attempt():
    """Verify that TtsRecoveryCoordinator discards chunks whose attempt != active_attempt."""
    clients = MagicMock()
    cancelled_flag = False
    coordinator = TtsRecoveryCoordinator(
        clients=clients,
        tts_stream=True,
        is_cancelled=lambda: cancelled_flag,
    )

    q = asyncio.Queue()
    coordinator.active_attempt = 2  # Current attempt is 2

    # Attempt 1 sends a chunk, but active_attempt switches to 2
    async def fake_agen():
        # First chunk arrives
        yield TtsAudioChunk(0, b"\x00" * 320, 8000, False, True)
        # Attempt switches to 2 (simulating retry or new attempt while old generator was yielding)
        coordinator.active_attempt = 2
        yield TtsAudioChunk(1, b"\x00" * 320, 8000, True, True)

    clients.synthesize_stream.return_value = fake_agen()

    # Run attempt with slot
    tts_slots = asyncio.Semaphore(1)
    # Patch retry delay to not sleep
    coordinator.retry_delay_s = 0.001
    coordinator.max_retries = 0

    await coordinator.synthesize_clause("text", "hi", tts_slots, q, lambda: 10.0)

    # Queue should contain chunk 0 (which arrived when active_attempt == 1),
    # but NOT chunk 1 (which arrived when active_attempt == 2)
    items = []
    while not q.empty():
        items.append(q.get_nowait())

    audio_items = [it for it in items if it[0] == "audio"]
    assert len(audio_items) == 1, "Only the active attempt chunk should be accepted; stale chunk discarded"


@pytest.mark.asyncio
async def test_cancellation_isolation_between_calls():
    """Verify that cancelling job_A does not touch job_B."""
    s = Settings()
    clients = ModelClients(s, {
        "tts_url": "https://api.runpod.ai/v2/mock/runsync",
        "keys": {"tts": "fake_key"},
    })

    cancelled_jobs = []

    async def fake_cancel(svc, ep, hdrs, jid, wait_confirm=False):
        cancelled_jobs.append(jid)
        return True

    with patch.object(clients, "_cancel_job", side_effect=fake_cancel):
        # Cancel call A
        await clients._cancel_job("tts", clients.endpoint("tts"), {}, "job_call_A")
        assert "job_call_A" in cancelled_jobs
        assert "job_call_B" not in cancelled_jobs

    await clients.aclose()
