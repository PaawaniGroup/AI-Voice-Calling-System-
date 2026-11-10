"""Decode caller audio to float32 mono at the model rate. Real polyphase resampling, never a header rewrite."""
from __future__ import annotations

import base64
import binascii
import io
import math
import wave

import numpy as np
from scipy.signal import resample_poly

from .response_schema import AsrError

_MULAW: np.ndarray | None = None


def _mulaw_table() -> np.ndarray:
    global _MULAW
    if _MULAW is None:
        u = (~np.arange(256, dtype=np.uint8)).astype(np.int32)
        sign, exp, man = u & 0x80, (u >> 4) & 7, u & 0x0F
        mag = (((man << 3) + 0x84) << exp) - 0x84
        _MULAW = np.where(sign != 0, -mag, mag).astype(np.int16)
    return _MULAW


def mulaw_decode(data: bytes) -> np.ndarray:
    return _mulaw_table()[np.frombuffer(data, dtype=np.uint8)]


def decode_audio(audio_b64, audio_format, sample_rate, max_bytes: int = 32 * 1024 * 1024):
    """Return (int16 mono ndarray, source sample rate)."""
    if not isinstance(audio_b64, str) or not audio_b64:
        raise AsrError("invalid_audio", "audio_base64 is required")
    try:
        raw = base64.b64decode(audio_b64, validate=True)
    except (binascii.Error, ValueError):
        raise AsrError("invalid_audio", "audio_base64 is not valid base64")
    if not raw:
        raise AsrError("empty_audio", "audio is empty")
    if len(raw) > max_bytes:
        raise AsrError("oversized_audio", f"audio exceeds {max_bytes} bytes")
    fmt = (audio_format or "wav").lower()
    if fmt == "wav":
        try:
            with wave.open(io.BytesIO(raw)) as w:
                ch, sw, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
                frames = w.readframes(n)
        except (wave.Error, EOFError):
            raise AsrError("invalid_audio", "not a decodable PCM WAV file")
        if sw != 2:
            raise AsrError("invalid_audio", f"only 16-bit PCM WAV supported (got {sw * 8}-bit)")
        pcm = np.frombuffer(frames, dtype="<i2")
        if ch > 1:
            pcm = pcm[: len(pcm) // ch * ch].reshape(-1, ch).mean(axis=1).astype(np.int16)
        return pcm, sr
    try:
        rate = int(sample_rate)
    except (TypeError, ValueError):
        rate = 0
    if rate <= 0:
        raise AsrError("invalid_sample_rate", "sample_rate is required for raw formats")
    if fmt in ("mulaw", "ulaw", "pcmu"):
        return mulaw_decode(raw), rate
    if fmt in ("pcm16", "pcm_s16le", "raw"):
        if len(raw) % 2:
            raise AsrError("invalid_audio", "PCM16 buffer length must be even")
        return np.frombuffer(raw, dtype="<i2"), rate
    raise AsrError("unsupported_format", f"audio_format {audio_format!r} not supported")


def preprocess(audio_b64, audio_format, sample_rate, target_sr: int = 16000, max_seconds: float = 60.0):
    pcm, sr = decode_audio(audio_b64, audio_format, sample_rate)
    if sr < 4000 or sr > 96000:
        raise AsrError("invalid_sample_rate", f"unsupported sample rate {sr}")
    if len(pcm) == 0:
        raise AsrError("empty_audio", "audio has no samples")
    dur = len(pcm) / sr
    if dur > max_seconds:
        raise AsrError("audio_too_long", f"audio {dur:.1f}s exceeds max {max_seconds}s")
    x = pcm.astype(np.float32) / 32768.0
    if sr != target_sr:
        g = math.gcd(sr, target_sr)
        x = resample_poly(x, target_sr // g, sr // g).astype(np.float32)
    return x, {"source_sample_rate": sr, "model_sample_rate": target_sr,
               "audio_duration_ms": round(dur * 1000, 3), "samples": int(len(x))}
