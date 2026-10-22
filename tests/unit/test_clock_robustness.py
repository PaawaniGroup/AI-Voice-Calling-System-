"""Regression tests for the CONFIRMED cause of intermittent failures in tests/integration/test_ai_freeswitch.py.

Cause: the helpers used wall-clock time (`time.time()`) for a deadline (`wait_ai`) and for choosing the caller's recording
(`os.path.getmtime(f) >= time.time() - 1`). This WSL2 host's wall clock steps by about 51.7 s against the monotonic clock every few
seconds (measured with tests/diag), so the deadline fired early and the real recording was skipped ("caller heard 0.00 s")
although the media path had worked. These tests simulate such steps WITHOUT FreeSWITCH and are deterministic: the fake wall clock
is derived from the monotonic clock, never from the host's (flickering) wall clock.
"""
import itertools
import os
import struct
import time
import wave

from tests.integration import test_ai_freeswitch as T


class FakeColl:
    def __init__(self, fn):
        self.fn = fn

    async def find_one(self, q):
        return self.fn()


def stepping_wall_clock():
    """Wall clock = monotonic + 1e9, plus 51.7 s on every 3rd reading starting with the 2nd. Under the OLD wait_ai (deadline computed
    from reading #1, compared against reading #2) the deadline is already 'passed' at the first check."""
    n = itertools.count()
    return lambda: 1e9 + time.monotonic() + (51.7 if next(n) % 3 == 1 else 0.0)


async def test_wait_ai_is_immune_to_wall_clock_steps(monkeypatch):
    t0 = time.monotonic()
    coll = FakeColl(lambda: {"state": "active" if time.monotonic() - t0 > 0.5 else "starting"})
    e = type("E", (), {"db": {"ai_sessions": coll}})()
    monkeypatch.setattr(time, "time", stepping_wall_clock())
    a = await T.wait_ai(e, "sid", {"active"}, timeout=10)  # becomes active after 0.5 s; must not give up because the wall clock stepped
    assert a["state"] == "active"


def _write_wav(path, loud: bool):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(struct.pack("<4000h", *([6000 if (i // 9) % 2 else -6000 for i in range(4000)] if loud else [0] * 4000)))


def test_recording_selection_does_not_depend_on_file_mtime(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "HEARD_GLOB", str(tmp_path / "mme_ai_heard_*.wav"))
    _write_wav(tmp_path / "mme_ai_heard_old.wav", loud=False)
    before = T.heard_snapshot()
    new = tmp_path / "mme_ai_heard_new.wav"
    _write_wav(new, loud=True)
    past = time.time() - 3600  # mtime far in the past relative to the wall clock, as after a clock step
    os.utime(new, (past, past))
    audible_s, peak = T.heard_stats(before)
    assert audible_s >= 0.4 and peak > 2000
    assert T.heard_stats(T.heard_snapshot()) == (0.0, 0)  # recordings that existed before the call are never this call's


def test_failure_note_reports_a_stepped_clock(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 1000.0 + 51.7)
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(T, "_CLOCK0", 1000.0 - 100.0)
    assert T.clock_note() == "wall_clock_step_since_import=+51.7s"
