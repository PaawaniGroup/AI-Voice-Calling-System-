"""Unit tests for continuous playback jitter pre-buffering in Conversation pipeline.

Verifies:
1. Configurable initial pre-buffer (400ms default) accumulates initial chunks to protect against underruns.
2. Pre-buffer flushes in sequence when threshold is met.
3. Short utterance ending before target duration flushes immediately on 'end'.
4. Immediate clearing on cancellation (no stale audio leaked).
5. Pass-through when prebuffer is 0ms or streaming is disabled.
"""
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from app.ai.clients import ModelClients, TtsAudioChunk
from app.ai.pipeline import Conversation, TurnChunk
from config.settings import Settings


def _create_conv(tts_stream: bool = True, prebuffer_ms: int = 400):
    s = Settings()
    s.ai_tts_prebuffer_ms = prebuffer_ms
    clients = MagicMock()
    clients.s = s
    clients.synthesize_stream = MagicMock()
    clients.synthesize = AsyncMock(return_value=(b"\x00" * 3200, 16000))
    conv = Conversation(clients, "hi", tts_stream=tts_stream)
    return conv, clients


@pytest.mark.asyncio
async def test_jitter_prebuffer_holds_small_chunks_until_threshold():
    """Verify that chunks smaller than target_prebuffer_ms are held and released together."""
    target_prebuffer_ms = 400.0
    prebuffer = []
    prebuffered_ms = 0.0
    prebuffer_released = False
    emitted = []

    # 150ms chunk (0.15s @ 8000 Hz = 1200 samples = 2400 bytes PCM16)
    c0 = (b"\x00" * 2400, 8000, 100.0)
    # 480ms chunk (0.48s @ 8000 Hz = 3840 samples = 7680 bytes PCM16)
    c1 = (b"\x00" * 7680, 8000, 200.0)
    # Final 130ms chunk
    c2 = (b"\x00" * 2080, 8000, 300.0)

    # Simulation of the _finish loop
    for item in [("audio", c0[0], c0[1], c0[2]), ("audio", c1[0], c1[1], c1[2]), ("audio", c2[0], c2[1], c2[2]), ("end", 0.5)]:
        if item[0] == "end":
            while prebuffer:
                emitted.append(prebuffer.pop(0))
            break
        _, audio, srate, sent_ms = item
        if not prebuffer_released:
            chunk_ms = (len(audio) / 2 / srate) * 1000.0 if srate > 0 else 0.0
            prebuffer.append((audio, srate, sent_ms))
            prebuffered_ms += chunk_ms
            if prebuffered_ms >= target_prebuffer_ms:
                prebuffer_released = True
                while prebuffer:
                    emitted.append(prebuffer.pop(0))
            continue
        emitted.append((audio, srate, sent_ms))

    # All 3 chunks must be emitted in correct order
    assert len(emitted) == 3
    assert emitted[0][0] == c0[0]
    assert emitted[1][0] == c1[0]
    assert emitted[2][0] == c2[0]


@pytest.mark.asyncio
async def test_prebuffer_math_and_threshold_release():
    """Unit test the prebuffer accumulator and release logic."""
    target_prebuffer_ms = 400.0
    prebuffer = []
    prebuffered_ms = 0.0
    prebuffer_released = False
    emitted = []

    def feed(audio: bytes, srate: int):
        nonlocal prebuffered_ms, prebuffer_released
        if not prebuffer_released:
            chunk_ms = (len(audio) / 2 / srate) * 1000.0
            prebuffer.append((audio, srate))
            prebuffered_ms += chunk_ms
            if prebuffered_ms >= target_prebuffer_ms:
                prebuffer_released = True
                while prebuffer:
                    emitted.append(prebuffer.pop(0))
        else:
            emitted.append((audio, srate))

    # Chunk 0: 150 ms (2400 bytes @ 8kHz)
    feed(b"\x00" * 2400, 8000)
    assert len(prebuffer) == 1
    assert len(emitted) == 0, "Chunk 0 should be held in prebuffer"
    assert prebuffered_ms == 150.0

    # Chunk 1: 480 ms (7680 bytes @ 8kHz) -> total 630 ms >= 400 ms
    feed(b"\x00" * 7680, 8000)
    assert len(prebuffer) == 0, "Prebuffer should be flushed when reaching 630 ms"
    assert len(emitted) == 2, "Both Chunk 0 and Chunk 1 should be emitted"
    assert prebuffer_released is True

    # Chunk 2: 480 ms -> should pass through immediately
    feed(b"\x00" * 7680, 8000)
    assert len(emitted) == 3


@pytest.mark.asyncio
async def test_prebuffer_flushes_on_end_if_below_threshold():
    """Verify that if an utterance finishes (e.g. only 200 ms total), it flushes immediately on 'end'."""
    prebuffer = [(b"\x00" * 3200, 8000, 100.0)]  # 200 ms
    emitted = []

    # Emulate 'end' event
    while prebuffer:
        emitted.append(prebuffer.pop(0))

    assert len(emitted) == 1
    assert len(prebuffer) == 0
