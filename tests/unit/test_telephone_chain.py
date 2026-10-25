"""tools/telephone_chain.py: the filter behaves like a 300-3400 Hz telephone band, and mu-law keeps the level."""
import array
import math
import sys
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import telephone_chain as t  # noqa: E402


def tone_pcm(freq, seconds=1.0, amp=8000, rate=8000):
    return array.array("h", (int(amp * math.sin(2 * math.pi * freq * i / rate)) for i in range(int(rate * seconds)))).tobytes()


def rms_db(pcm, skip=2000):
    s = array.array("h"); s.frombytes(pcm)
    body = s[skip:]
    return 20 * math.log10(max(1e-9, math.sqrt(sum(v * v for v in body) / len(body))))


def gain_db(freq):
    src = tone_pcm(freq)
    return rms_db(t.telephone_chain(src)) - rms_db(src)


def test_the_band_passes_speech_frequencies_and_removes_hum_and_the_top_edge():
    assert abs(gain_db(1000)) < 1.0
    assert abs(gain_db(500)) < 1.5 and abs(gain_db(3000)) < 1.5
    assert gain_db(50) < -40                    # mains hum
    assert gain_db(100) < -20
    assert gain_db(3900) < -4                   # just under the Nyquist frequency, past the 3400 Hz edge


def test_the_chain_keeps_length_and_is_stable_when_applied_twice():
    pcm = tone_pcm(800, 0.5)
    once = t.telephone_chain(pcm)
    assert len(once) == len(pcm)
    twice = t.telephone_chain(once)
    assert abs(rms_db(twice) - rms_db(once)) < 0.5      # a second pass changes the level only slightly (the band edges attenuate a bit more)


def test_load_variants_returns_both_versions_at_8k(tmp_path):
    p = tmp_path / "x.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(tone_pcm(1000, 1.0, rate=16000))
    v = t.load_variants(p)
    assert set(v) == {"clean8k", "phone"} and len(v["clean8k"]) == len(v["phone"])
    assert abs(len(v["clean8k"]) / 2 / 8000 - 1.0) < 0.02 and v["clean8k"] != v["phone"]
