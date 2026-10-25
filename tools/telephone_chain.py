# -*- coding: utf-8 -*-
"""
tools/telephone_chain.py - make a recording sound like it came through the telephone line. Pure Python (no numpy, no scipy), offline.

    8 kHz sampling  ->  band-pass 300-3400 Hz (4th-order Butterworth high-pass + low-pass, as cascaded biquads)  ->  G.711 mu-law (encode and decode)

`load_variants(path)` returns both versions of one recording: "clean8k" (converted to 8 kHz mono only) and "phone" (the full chain). The spikes run both and report them
separately: a recogniser or an end-of-turn rule that works on clean audio may not work on the line. Limits: this is a model of the line, not the line itself
(no packet loss, no codec of the mobile network, no echo); the first supervised call shows the real thing.
"""
import array
import audioop
import math
import wave
from pathlib import Path

RATE = 8000
HP_HZ, LP_HZ = 300.0, 3400.0
BUTTER4_Q = (0.5411961, 1.3065630)       # the two second-order sections of a 4th-order Butterworth filter


def _biquad(kind: str, f0: float, q: float, fs: int = RATE):
    """RBJ audio-EQ-cookbook coefficients (b0, b1, b2, a1, a2) normalised by a0, for a 'lp' or 'hp' section."""
    w0 = 2 * math.pi * f0 / fs
    cw, alpha = math.cos(w0), math.sin(w0) / (2 * q)
    if kind == "lp":
        b0, b1, b2 = (1 - cw) / 2, 1 - cw, (1 - cw) / 2
    else:
        b0, b1, b2 = (1 + cw) / 2, -(1 + cw), (1 + cw) / 2
    a0, a1, a2 = 1 + alpha, -2 * cw, 1 - alpha
    return (b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)


SECTIONS = [_biquad("hp", HP_HZ, q) for q in BUTTER4_Q] + [_biquad("lp", LP_HZ, q) for q in BUTTER4_Q]


def bandpass(samples) -> list:
    """Filter a list of numbers through the four sections (direct form I). Returns floats."""
    x = list(samples)
    for b0, b1, b2, a1, a2 in SECTIONS:
        y, x1, x2, y1, y2 = [], 0.0, 0.0, 0.0, 0.0
        for v in x:
            out = b0 * v + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
            x2, x1, y2, y1 = x1, v, y1, out
            y.append(out)
        x = y
    return x


def telephone_chain(pcm8k: bytes) -> bytes:
    """16-bit mono PCM at 8 kHz in, the same after the band-pass and a mu-law round trip out."""
    s = array.array("h")
    s.frombytes(pcm8k[: len(pcm8k) // 2 * 2])
    filtered = bandpass(s)
    clipped = array.array("h", (max(-32768, min(32767, int(round(v)))) for v in filtered))
    return audioop.ulaw2lin(audioop.lin2ulaw(clipped.tobytes(), 2), 2)


def to_8k_mono(path: Path) -> bytes:
    """16-bit mono PCM at 8 kHz from any WAV (other sample widths, stereo and other rates are converted)."""
    with wave.open(str(path), "rb") as w:
        ch, width, rate, raw = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.readframes(w.getnframes())
    if width != 2:
        raw = audioop.lin2lin(raw, width, 2)
    if ch == 2:
        raw = audioop.tomono(raw, 2, 0.5, 0.5)
    if rate != RATE:
        raw = audioop.ratecv(raw, 2, 1, rate, RATE, None)[0]
    return raw


def load_variants(path: Path) -> dict:
    clean = to_8k_mono(path)
    return {"clean8k": clean, "phone": telephone_chain(clean)}
