"""tools/recording_intake_check.py on synthetic WAV files (no recordings, no network)."""
import math
import struct
import sys
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import recording_intake_check as c  # noqa: E402

RATE = 8000


def tone(s, amp=4000):
    return [int(amp * math.sin(2 * math.pi * 300 * i / RATE)) for i in range(int(RATE * s))]


def quiet(s):
    return [0] * int(RATE * s)


def write(d, name, samples, rate=RATE):
    with wave.open(str(Path(d) / f"{name}.wav"), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(struct.pack(f"<{len(samples)}h", *[max(-32768, min(32767, v)) for v in samples]))


GOOD = quiet(0.5) + tone(3.0) + quiet(0.6)


def errs(tmp_path, name, samples, rate=RATE):
    write(tmp_path, name, samples, rate)
    return c.check_file(tmp_path / f"{name}.wav")


def test_a_good_scripted_file_passes(tmp_path):
    r = errs(tmp_path, "u02_hg_S1", GOOD)
    assert r["errors"] == [] and r["info"]["duration_s"] == pytest.approx(4.1, abs=0.05) and r["info"]["lead_s"] >= 0.4 and r["info"]["tail_s"] >= 0.5


def test_names_language_codes_and_flags_are_checked(tmp_path):
    assert errs(tmp_path, "my_recording", GOOD)["errors"] == ["bad_name"]
    assert "wrong_language_code" in errs(tmp_path, "u02_en_S1", GOOD)["errors"]           # line 2 is Hinglish
    assert "wrong_flag" in errs(tmp_path, "u03_hi_S1", GOOD)["errors"]                     # line 3 needs _N
    assert errs(tmp_path, "u03_hi_S3_N", GOOD)["errors"] == []
    assert errs(tmp_path, "u09_en_S2_SP", GOOD)["errors"] == []
    assert "wrong_flag" in errs(tmp_path, "u01_hi_S1_N", GOOD)["errors"]
    assert "bad_line_number" in errs(tmp_path, "u16_hi_S1", GOOD)["errors"]


def test_duration_rate_clipping_and_silence(tmp_path):
    assert "too_short" in errs(tmp_path, "u02_hg_S1", quiet(0.5) + tone(1.0) + quiet(0.6))["errors"]
    assert "sample_rate_below_8000" in errs(tmp_path, "u02_hg_S1", GOOD, rate=6000)["errors"]
    assert "clipped" in errs(tmp_path, "u02_hg_S1", quiet(0.5) + tone(3.0, amp=40000) + quiet(0.6))["errors"]
    assert "no_speech" in errs(tmp_path, "u02_hg_S1", quiet(4.0))["errors"]
    assert "lead_silence_too_short" in errs(tmp_path, "u02_hg_S1", tone(3.0) + quiet(0.6))["errors"]
    assert "tail_silence_too_short" in errs(tmp_path, "u02_hg_S1", quiet(0.5) + tone(3.0) + quiet(0.1))["errors"]
    assert "quiet" in errs(tmp_path, "u02_hg_S1", quiet(0.5) + tone(3.0, amp=900) + quiet(0.6))["warnings"]       # above the speech threshold, below twice of it


def test_pause_lines_need_an_internal_pause_of_06_to_1_s(tmp_path):
    ok = quiet(0.5) + tone(1.5) + quiet(0.8) + tone(1.5) + quiet(0.6)
    assert errs(tmp_path, "u13_hi_S2", ok)["errors"] == [] and errs(tmp_path, "p04_hi_S1", ok)["errors"] == []
    assert "no_pause_of_0.6_to_1_s" in errs(tmp_path, "u13_hi_S2", GOOD)["errors"]
    short = quiet(0.5) + tone(1.5) + quiet(0.4) + tone(1.5) + quiet(0.6)
    assert "no_pause_of_0.6_to_1_s" in errs(tmp_path, "p04_hi_S1", short)["errors"]
    long_ = quiet(0.5) + tone(1.5) + quiet(1.6) + tone(1.5) + quiet(0.6)
    assert "pause_longer_than_1.3_s" in errs(tmp_path, "p04_hi_S1", long_)["errors"]
    assert errs(tmp_path, "u02_hg_S1", ok)["errors"] == []                                  # a scripted (non-pause) line may contain a pause


def test_open_answers_need_a_long_silent_tail_and_room_tone_must_be_quiet(tmp_path):
    spont = quiet(0.5) + tone(6.0) + quiet(2.0)
    assert errs(tmp_path, "s03_en_S2", spont)["errors"] == []
    assert "tail_silence_too_short" in errs(tmp_path, "s03_en_S2", quiet(0.5) + tone(6.0) + quiet(0.8))["errors"]
    assert "too_short" in errs(tmp_path, "s03_en_S2", quiet(0.5) + tone(2.0) + quiet(2.0))["errors"]    # open answers: 5 s at least (4.5 s here)
    assert errs(tmp_path, "roomtone_S1", quiet(5.0))["errors"] == []
    assert "room_not_quiet" in errs(tmp_path, "roomtone_S1", quiet(2.0) + tone(1.0) + quiet(2.0))["errors"]
    assert "too_short" in errs(tmp_path, "roomtone_S1", quiet(2.0))["errors"]


def test_recordings_over_the_6s_cap_are_counted_and_listed_separately_not_as_errors(tmp_path):
    write(tmp_path, "s01_hi_S1", quiet(0.5) + tone(8.0) + quiet(2.0))
    write(tmp_path, "s02_en_S1", quiet(0.5) + tone(5.5) + quiet(2.0))
    write(tmp_path, "u02_hg_S1", GOOD)
    res = c.check_dir(tmp_path)
    assert res["over_6s_cap"]["n"] == 1 and res["over_6s_cap"]["files"] == ["s01_hi_S1"] and res["over_6s_cap"]["of"] == 3
    assert res["files"]["s01_hi_S1"]["errors"] == [] and res["files"]["s01_hi_S1"]["over_6s_cap"] is True


def test_the_protocol_has_83_expected_files_and_the_check_lists_missing_and_unexpected(tmp_path):
    exp = c.expected_names()
    assert len(exp) == 30 + 20 + 3 and "u03_hi_S1_N" in exp and "u09_en_S2_SP" in exp and "p03_hi_S3" in exp and "roomtone_S3" in exp
    write(tmp_path, "u02_hg_S1", GOOD)
    write(tmp_path, "s01_hi_S1", quiet(0.5) + tone(6.0) + quiet(2.0))
    write(tmp_path, "u02_hg_S1_take2", GOOD)
    res = c.check_dir(tmp_path)
    assert res["n_files"] == 3 and "u02_hg_S1_take2" in res["unexpected"] and "u02_hg_S1" not in res["missing"] and "s01_*_S1" not in res["missing"]
    assert "u01_hi_S1" in res["missing"] and "s02_*_S1" in res["missing"] and res["errors_by_code"]["bad_name"] == ["u02_hg_S1_take2"]
    assert res["files_per_speaker"] == {"S1": 2}


def test_the_report_carries_ids_and_counts_only(tmp_path, capsys):
    write(tmp_path, "u02_hg_S1", GOOD)
    c.report(c.check_dir(tmp_path))
    out = capsys.readouterr().out
    assert "recording intake check" in out and "no audio and no words" in out and "missing from the protocol" in out
    assert "files per speaker: {'S1': 1}" in out and "RECORDINGS OVER THE 6 s CAP (counted separately, not an error): 0 of 1" in out
