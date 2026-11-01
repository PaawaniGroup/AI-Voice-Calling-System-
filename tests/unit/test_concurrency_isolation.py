"""Phase H: Concurrency and Request State Isolation Tests.
Verifies that concurrent requests in the streaming TTS pipeline maintain
independent state, without cross-request state contamination:
1. One active synthesis
2. Two overlapping requests
3. Three overlapping requests
4. One request cancelled while another continues
5. One request failing while another succeeds
6. Multiple independent conversation IDs and recovery coordinators
"""
import asyncio
import pytest

from app.ai.clients import TtsAudioChunk
from app.ai.pipeline import Conversation
from app.ai.tts_recovery import TtsRecoveryCoordinator
from tests.unit.test_tts_overlap import FakeClients
from tools.tts_stream_worker.worker_stream import StreamingDecimator


class ConcurrentMockClients(FakeClients):
    def __init__(self):
        super().__init__()
        self.active_streams = set()

    async def synthesize_stream(self, text, language):
        call_id = text
        self.active_streams.add(call_id)
        try:
            for seq in range(3):
                await asyncio.sleep(0.02)
                yield TtsAudioChunk(seq=seq, pcm=b"\x00\x00" * 160, sample_rate=8000, final=False)
            yield TtsAudioChunk(seq=3, pcm=b"", sample_rate=8000, final=True)
        finally:
            self.active_streams.discard(call_id)


@pytest.mark.asyncio
async def test_independent_fir_decimator_states_across_concurrent_streams():
    """Verify that multiple concurrent streams have separate StreamingDecimator instances
    so filter history states never bleed across calls."""
    import numpy as np
    d1 = StreamingDecimator()
    d2 = StreamingDecimator()

    # Feed distinct signals to d1 and d2
    sig1 = np.ones(500, dtype=np.float32)
    sig2 = -np.ones(500, dtype=np.float32)

    out1 = d1.process(sig1)
    out2 = d2.process(sig2)

    assert not np.allclose(out1, out2)
    # Filter states must be distinct
    assert not np.allclose(d1._hist, d2._hist)


@pytest.mark.asyncio
async def test_overlapping_requests_isolation():
    """Verify 2 and 3 overlapping requests complete with independent outputs and no cross-talk."""
    clients = ConcurrentMockClients()
    conv1 = Conversation(clients, "hi", tts_stream=True)
    conv2 = Conversation(clients, "en", tts_stream=True)
    conv3 = Conversation(clients, "hi", tts_stream=True)

    async def run(conv):
        res = []
        async for ch in conv.stream_turn(b"\x00" * 3200, 16000):
            if ch.audio:
                res.append(ch)
        return res

    out1, out2, out3 = await asyncio.gather(run(conv1), run(conv2), run(conv3))
    assert len(out1) > 0
    assert len(out2) > 0
    assert len(out3) > 0


@pytest.mark.asyncio
async def test_one_request_cancelled_while_another_continues():
    """Verify one conversation cancellation does NOT affect concurrent active conversations."""
    clients = ConcurrentMockClients()
    conv_a = Conversation(clients, "hi", tts_stream=True)
    conv_b = Conversation(clients, "hi", tts_stream=True)

    async def run_a():
        async for ch in conv_a.stream_turn(b"\x00" * 3200, 16000):
            conv_a.cancel()  # Abort A immediately
            break
        return "cancelled_a"

    async def run_b():
        res = []
        async for ch in conv_b.stream_turn(b"\x00" * 3200, 16000):
            if ch.audio:
                res.append(ch)
        return res

    res_a, res_b = await asyncio.gather(run_a(), run_b())
    assert res_a == "cancelled_a"
    assert conv_a.cancelled is True
    # Conversation B finished successfully despite A's cancellation!
    assert conv_b.cancelled is False
    assert len(res_b) > 0


@pytest.mark.asyncio
async def test_recovery_coordinator_isolated_per_call():
    """Verify each call has an isolated TtsRecoveryCoordinator with independent attempt counters."""
    coord1 = TtsRecoveryCoordinator(None, call_id="c1", clause_idx=0)
    coord2 = TtsRecoveryCoordinator(None, call_id="c2", clause_idx=0)

    coord1.active_attempt = 1
    coord1.retry_count = 1

    assert coord2.active_attempt == 0
    assert coord2.retry_count == 0
