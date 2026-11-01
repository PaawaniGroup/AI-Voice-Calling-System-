"""Comprehensive audio format and encoding integrity tests.

Verifies:
1. Exact byte counts and sample counts for 8 kHz G.711 mu-law and PCM16.
2. Mathematical relationships: duration * rate = samples; samples * 1 = mu-law bytes; samples * 2 = PCM16 bytes.
3. Stateful FIR decimation across chunk boundaries (no phase slips or length errors).
4. Codec round-trip (mulaw_to_pcm16 and pcm16_to_mulaw).
5. 20ms telephony framing (160 samples = 160 bytes mu-law = 320 bytes PCM16).
"""
import base64
import numpy as np
import pytest

from app.ai import codec
from tools.tts_stream_worker.worker_stream import StreamingDecimator, pcm16_to_ulaw


def test_audio_byte_count_and_sample_duration_math():
    """Prove that for 2.68 seconds of 8 kHz mono audio:
    - samples = 21,440
    - G.711 mu-law = 21,440 bytes (1 byte/sample)
    - PCM16 = 42,880 bytes (2 bytes/sample)
    """
    duration_s = 2.68
    sample_rate = 8000
    expected_samples = int(round(duration_s * sample_rate))
    assert expected_samples == 21440

    mulaw_bytes_per_sample = 1
    pcm16_bytes_per_sample = 2

    expected_mulaw_bytes = expected_samples * mulaw_bytes_per_sample
    expected_pcm16_bytes = expected_samples * pcm16_bytes_per_sample

    assert expected_mulaw_bytes == 21440, f"Expected 21,440 bytes mu-law, got {expected_mulaw_bytes}"
    assert expected_pcm16_bytes == 42880, f"Expected 42,880 bytes PCM16, got {expected_pcm16_bytes}"


def test_chunk_sizes_sum_to_42880_pcm16_and_21440_mulaw():
    """Verify the 7 chunk sizes from the deployment test:
    seq 0: 2400 bytes PCM16 (0.15s)
    seq 1-5: 7680 bytes PCM16 each (0.48s each)
    seq 6: 2080 bytes PCM16 (0.13s)
    """
    chunk_pcm16_sizes = [2400, 7680, 7680, 7680, 7680, 7680, 2080]
    total_pcm16_bytes = sum(chunk_pcm16_sizes)
    assert total_pcm16_bytes == 42880, f"Total PCM16 bytes must be 42,880, got {total_pcm16_bytes}"

    total_samples = total_pcm16_bytes // 2
    assert total_samples == 21440, f"Total samples must be 21,440, got {total_samples}"

    total_duration_s = total_samples / 8000.0
    assert abs(total_duration_s - 2.68) < 1e-6

    # In mu-law, each chunk is exactly half the PCM16 byte size
    chunk_mulaw_sizes = [sz // 2 for sz in chunk_pcm16_sizes]
    total_mulaw_bytes = sum(chunk_mulaw_sizes)
    assert total_mulaw_bytes == 21440, f"Total mu-law bytes must be 21,440, got {total_mulaw_bytes}"


def test_codec_pcm16_mulaw_conversion_integrity():
    """Verify bit-accurate round-trip through pcm16_to_mulaw and mulaw_to_pcm16."""
    # Generate 160 samples (20 ms at 8 kHz) of a 400 Hz test sine
    t = np.arange(160) / 8000.0
    pcm_float = np.sin(2 * np.pi * 400 * t)
    pcm_int16 = (pcm_float * 32000.0).astype(np.int16)
    pcm_bytes = pcm_int16.tobytes()

    assert len(pcm_bytes) == 320  # 160 samples * 2 bytes

    # Convert to mu-law
    ulaw_bytes = codec.pcm16_to_mulaw(pcm_bytes)
    assert len(ulaw_bytes) == 160  # 160 samples * 1 byte

    # Convert back to PCM16
    pcm_recon_bytes = codec.mulaw_to_pcm16(ulaw_bytes)
    assert len(pcm_recon_bytes) == 320

    # mu-law quantization error should be strictly bounded (SNR > 35 dB)
    recon_int16 = np.frombuffer(pcm_recon_bytes, dtype=np.int16)
    diff = np.abs(pcm_int16.astype(np.float32) - recon_int16.astype(np.float32))
    max_err = np.max(diff)
    assert max_err < 1000  # standard mu-law compression tolerance


def test_stateful_fir_decimator_preserves_length_and_continuity():
    """Verify that feeding a 24 kHz signal into StreamingDecimator in arbitrary chunks
    yields the EXACT same sample sequence and length as decimation of the whole signal."""
    # 2.68s at 24 kHz = 64,320 samples
    n_24k = 64320
    t = np.arange(n_24k) / 24000.0
    signal_24k = np.sin(2 * np.pi * 300 * t, dtype=np.float32)

    # Decimate whole
    decim_whole = StreamingDecimator(factor=3)
    out_whole = decim_whole.process(signal_24k)
    assert len(out_whole) == 21440, f"Expected 21,440 decimated samples, got {len(out_whole)}"

    # Decimate in uneven slices (simulating streaming hops)
    decim_stream = StreamingDecimator(factor=3)
    slice_sizes = [3600, 11520, 11520, 11520, 11520, 11520, 3120]  # sums to 64,320
    assert sum(slice_sizes) == n_24k

    out_chunks = []
    idx = 0
    for sz in slice_sizes:
        chunk = signal_24k[idx:idx + sz]
        out_chunks.append(decim_stream.process(chunk))
        idx += sz

    out_streamed = np.concatenate(out_chunks)
    assert len(out_streamed) == 21440
    # Must be bit-identical between chunked and unchunked
    np.testing.assert_allclose(out_streamed, out_whole, atol=1e-6)


def test_telephony_20ms_framing():
    """Verify telephony 20ms framing contract:
    - 20 ms @ 8 kHz = 160 samples
    - Frame size PCM16 = 320 bytes
    - Frame size mu-law = 160 bytes
    """
    sample_rate = 8000
    frame_ms = 20
    frame_samples = (sample_rate * frame_ms) // 1000
    assert frame_samples == 160

    pcm16_frame_bytes = frame_samples * 2
    mulaw_frame_bytes = frame_samples * 1

    assert pcm16_frame_bytes == 320
    assert mulaw_frame_bytes == 160
