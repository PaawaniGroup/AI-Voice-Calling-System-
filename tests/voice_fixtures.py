"""Generated fixtures for the Voice Studio tests. NO real voice audio is used anywhere in the repository: "speech" here is a synthetic tone (a few harmonics with a
syllable-rate envelope) over low-level noise, which is all the clip checks (levels, silence, clipping, duration) need. Deterministic (fixed random seed)."""
import array
import io
import math
import random
import struct
import wave


def _speech_samples(n, rate, amp, t0=0.0):
    out = []
    for i in range(n):
        t = t0 + i / rate
        env = 0.65 + 0.35 * math.sin(2 * math.pi * 4.0 * t)                   # about four syllables per second, never below 0.3
        out.append(amp * env * (0.7 * math.sin(2 * math.pi * 140 * t) + 0.3 * math.sin(2 * math.pi * 420 * t)))
    return out


def wav_from_segments(segments, *, rate=24000, channels=1, width=2, amp=0.4, noise=0.002, seed=7, hard_clip=False) -> bytes:
    """segments: [("silence", seconds), ("speech", seconds), ...]. `noise` is the peak of the uniform background noise (fraction of full scale)."""
    rnd = random.Random(seed)
    samples, t = [], 0.0
    for kind, secs in segments:
        n = int(rate * secs)
        if kind == "speech":
            body = _speech_samples(n, rate, amp, t)
        else:
            body = [0.0] * n
        samples.extend(b + rnd.uniform(-noise, noise) for b in body)
        t += secs
    if hard_clip:
        samples = [max(-1.0, min(1.0, s * 3.0)) for s in samples]             # driven far past full scale: flat tops
    return pack_wav(samples, rate=rate, channels=channels, width=width)


def pack_wav(samples, *, rate, channels=1, width=2) -> bytes:
    peak = {1: 127, 2: 32767, 3: 8388607, 4: 2147483647}[width]
    ints = [int(max(-1.0, min(1.0, s)) * peak) for s in samples]
    if width == 1:
        raw = bytes(v + 128 for v in ints)                                    # 8-bit WAV is unsigned
    elif width == 2:
        raw = array.array("h", ints).tobytes()
    elif width == 3:
        raw = b"".join(struct.pack("<i", v)[:3] for v in ints)
    else:
        raw = array.array("i", ints).tobytes()
    if channels == 2:
        step = width
        raw = b"".join(raw[i:i + step] * 2 for i in range(0, len(raw), step))  # the same signal in both channels
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(raw)
    return buf.getvalue()


_GOOD = {}


def good_clip(**kw) -> bytes:
    """About 10 s of "speech" with one second of silence on each side (24 kHz mono 16-bit unless changed). The default clip is generated once per process."""
    if kw:
        return wav_from_segments([("silence", 1.0), ("speech", 10.0), ("silence", 1.0)], **kw)
    if "default" not in _GOOD:
        _GOOD["default"] = wav_from_segments([("silence", 1.0), ("speech", 10.0), ("silence", 1.0)])
    return _GOOD["default"]


def wav_info(data: bytes) -> dict:
    with wave.open(io.BytesIO(data), "rb") as w:
        return {"rate": w.getframerate(), "channels": w.getnchannels(), "width": w.getsampwidth(), "seconds": w.getnframes() / w.getframerate()}
