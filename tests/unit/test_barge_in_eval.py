"""tools/barge_in_eval.py on SYNTHETIC speech (tone bursts): the harness itself behaves as documented. Spends nothing, reads no files."""
import math
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import barge_in_eval as be  # noqa: E402

RATE = 8000


def tone_speech(seconds=12, amp=4000, mod_hz=0.0):
    """Stand-in for a speech recording. mod_hz > 0 chops the amplitude like syllables (it dips below the threshold between them)."""
    n = RATE * seconds
    out = []
    for i in range(n):
        env = abs(math.sin(math.pi * mod_hz * i / RATE)) if mod_hz else 1.0
        out.append(int(amp * env * math.sin(2 * math.pi * 220 * i / RATE)))
    return struct.pack("<%dh" % n, *out)


@pytest.fixture(scope="module")
def result():
    return be.evaluate(tone_speech(), minutes=1.0)


def test_line_noise_never_interrupts_any_detector(result):
    for name, r in result.items():
        if name.startswith("S1"):
            assert all(r[d]["false_per_min"] == 0 for d in ("legacy", "duplex", "duplex-0"))


def test_backchannel_bursts_fool_the_old_rule_only(result):
    r = next(v for k, v in result.items() if k.startswith("S2"))
    assert r["legacy"]["false_triggers"] > 0 and r["duplex"]["false_triggers"] == 0 and r["duplex-0"]["false_triggers"] == 0


def test_speaker_echo_fools_the_old_rule_and_the_guard_removes_it(result):
    # (a constant tone at 3000 RMS echoed at -6 dB is 1500: above the 1000 floor. At -10 dB it would be 950, below it: real speech has peaks, a tone has not)
    r = next(v for k, v in result.items() if k.startswith("S3 speaker echo -6"))
    assert r["legacy"]["false_per_min"] > 5 and r["duplex-0"]["false_per_min"] > 5            # without the guard echo is taken for the caller
    assert r["duplex"]["false_per_min"] == 0


def test_a_real_interruption_is_heard_and_its_speech_is_kept(result):
    r = next(v for k, v in result.items() if k.startswith("S4"))
    for d in ("legacy", "duplex", "duplex-0"):
        assert r[d]["events"] > 0 and r[d]["detected"] == r[d]["events"] and r[d]["false_triggers"] == 0
    assert r["legacy"]["kept_ms_mean"] == 20                                                  # the old rule keeps only the triggering frame
    assert r["duplex"]["kept_ms_mean"] >= 250 and r["duplex-0"]["kept_ms_mean"] >= 250        # the new one keeps the whole run


def test_with_echo_present_the_guard_adds_no_false_interruptions_where_the_old_rule_adds_many(result):
    r = next(v for k, v in result.items() if k.startswith("S5 caller talks over the assistant, WITH echo -6"))
    assert r["legacy"]["false_triggers"] > 0 and r["duplex"]["false_triggers"] == 0
    # (how many REAL interruptions each hears is not asserted here: mixing two same-pitch tones makes the synthetic level fluctuate. The speech
    #  recording gives that comparison: docs/evidence/barge_in_eval_synthetic.json)


def test_syllable_gaps_cost_the_old_rule_real_interruptions_the_new_one_bridges_them():
    res = be.evaluate(tone_speech(mod_hz=5.0), minutes=1.0)
    r = next(v for k, v in res.items() if k.startswith("S4"))
    assert r["duplex-0"]["detected"] > r["legacy"]["detected"]                                  # chopped speech never gives the old rule 200 continuous ms


def test_the_legacy_mirror_uses_the_constants_of_the_service():
    from app.ai import ws
    assert be.VAD == ws.VAD_RMS and ws.BARGE_IN_MS == 200 and ws.BARGE_IN_VAD_RMS == 1000.0       # if the service rule changes, this reminds you to re-check the mirror


def test_the_sweep_runs_and_reports_the_default_row():
    rows = be.sweep(tone_speech(), minutes=0.5)
    assert any(r["stat"] == "mean" and r["ratio"] == 0.8 for r in rows) and all("heard_quiet_line" in r for r in rows)
