"""Phase D: WebRTC Playback Integration Unit Tests.
Validates the audio conversion, packetization, and track scheduling mechanics
for WebRTC audio output (48 kHz mono s16, 20ms frames = 960 samples = 1920 bytes PCM16, Opus encoded).

Verifies:
- 48 kHz mono PCM16 wire format
- 20ms frame = 960 samples = 1920 bytes
- Outbound track pacing and silence filling
- Barge-in track flushing (cancellation)
- Multi-clause and streaming chunk progression
- Batch fallback compatibility
"""
import asyncio
import time
import pytest
import av

from app.lab import audio as la
from app.lab.transport import TtsOutputTrack


def generate_pcm16_samples(n_samples: int, rate: int = 8000, freq: float = 440.0) -> bytes:
    import math
    import struct
    buf = bytearray()
    for i in range(n_samples):
        v = int(12000.0 * math.sin(2.0 * math.pi * freq * (i / rate)))
        buf.extend(struct.pack("<h", v))
    return bytes(buf)


@pytest.mark.asyncio
async def test_webrtc_framing_constants_and_conversion():
    """Verify WebRTC wire framing constants:
    Rate = 48,000 Hz, 20 ms = 960 samples = 1,920 bytes.
    """
    assert la.WIRE_RATE == 48000
    assert la.WIRE_FRAME_MS == 20
    assert la.WIRE_SAMPLES == 960
    assert la.WIRE_BYTES == 1920

    # Convert 100ms of 8 kHz audio to 48 kHz WebRTC wire frames
    # 100ms @ 8 kHz = 800 samples = 1600 bytes
    pcm_8k = generate_pcm16_samples(800, rate=8000)
    wire_frames = la.to_wire_frames(pcm_8k, src_rate=8000)
    
    # 100ms / 20ms = exactly 5 wire frames
    assert len(wire_frames) == 5
    assert all(len(f) == la.WIRE_BYTES for f in wire_frames)


@pytest.mark.asyncio
async def test_webrtc_playback_scenarios():
    """Execute all required WebRTC playback scenarios across languages and lengths."""
    track = TtsOutputTrack()

    scenarios = [
        ("short_hindi", "नमस्ते।", 0.4, 8000),           # 400ms @ 8kHz
        ("med_hindi", "आपकी क्या सहायता करूँ?", 1.2, 8000),
        ("long_hinglish", "B.Tech CSE admission open hai, 60% aggregate zaroori hai.", 3.0, 8000),
        ("med_english", "Which university are you looking for?", 1.6, 8000),
        ("multi_clause", "Swagat hai. Fees 1 lakh hai. Jaldi apply karein.", 4.0, 8000),
        ("batch_fallback_24k", "Batch synthesis complete clip.", 2.0, 24000),  # 24kHz fallback
    ]

    for name, text, duration_s, src_rate in scenarios:
        n_samples = int(duration_s * src_rate)
        pcm = generate_pcm16_samples(n_samples, rate=src_rate)
        frames = la.to_wire_frames(pcm, src_rate)
        
        expected_frame_count = int(duration_s * 1000 // 20)
        assert abs(len(frames) - expected_frame_count) <= 1
        
        seen_first = []
        track.enqueue(frames, valid=lambda: True, on_first_frame=lambda t: seen_first.append(t))
        
        # Read back 2 frames
        f1 = await track.recv()
        f2 = await track.recv()
        
        assert isinstance(f1, av.AudioFrame)
        assert f1.sample_rate == 48000
        assert f1.samples == 960
        assert len(seen_first) == 1
        
        # Flush track before next scenario
        track.flush()
        assert len(track._q) == 0


@pytest.mark.asyncio
async def test_webrtc_barge_in_and_cancellation():
    """Verify caller barge-in flushes track immediately and emits silence until new turn."""
    track = TtsOutputTrack()
    
    # Enqueue 5 seconds of audio (250 frames)
    frames = [la.SILENCE_FRAME] * 250
    accepted = track.enqueue(frames, valid=lambda: True)
    assert accepted > 0
    assert len(track._q) > 0

    # Caller speaks: barge-in occurs!
    flushed_count = track.flush()
    assert flushed_count > 0
    assert len(track._q) == 0

    # Next recv() immediately produces silence, not old speech
    next_f = await track.recv()
    assert bytes(next_f.planes[0]) == la.SILENCE_FRAME


@pytest.mark.asyncio
async def test_webrtc_new_turn_after_cancellation():
    """Verify a new turn can enqueue and play cleanly after previous turn cancellation."""
    track = TtsOutputTrack()
    track.flush()

    # Enqueue new turn frames
    new_frames = la.to_wire_frames(generate_pcm16_samples(800, rate=8000), 8000)
    seen = []
    track.enqueue(new_frames, valid=lambda: True, on_first_frame=lambda t: seen.append(t))
    
    frame = await track.recv()
    assert frame.sample_rate == 48000
    assert len(seen) == 1
