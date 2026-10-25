"""tools/eot_replay.py on synthetic signals (no recordings, no network)."""
import math
import struct
import sys
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import eot_replay as e  # noqa: E402


def tone(seconds, amp=3000):
    n = int(8000 * seconds)
    return b"".join(struct.pack("<h", int(amp * math.sin(2 * math.pi * 300 * i / 8000))) for i in range(n))


def quiet(seconds):
    return b"\x00\x00" * int(8000 * seconds)


def test_a_pause_is_split_only_by_thresholds_shorter_than_the_pause():
    pcm = tone(1.0) + quiet(0.5) + tone(1.0)
    res = e.analyze({"u01": pcm}, [700, 550, 450, 350])
    cut = {T: r["n_files_with_false_cutoff"] for T, r in res["thresholds"].items()}
    assert cut == {"700": 0, "550": 0, "450": 1, "350": 1}
    assert res["internal_pause_ms"]["u01"] == 500


def test_an_utterance_without_pauses_gets_exactly_one_decision_at_every_threshold():
    for T in (700, 350):
        d = e.simulate(tone(1.5), T)
        assert len(d) == 1 and d[0]["at_ms"] == pytest.approx(1500 + T, abs=40)


def test_silence_alone_gives_no_decision_and_a_short_blip_is_not_speech():
    assert e.simulate(quiet(2.0), 350) == []
    assert e.simulate(tone(0.1) + quiet(1.0), 350) == []          # under MIN_SPEECH_MS (200 ms)


def test_hysteresis_ignores_a_click_inside_the_final_silence():
    pcm = tone(1.0) + quiet(0.3) + tone(0.04) + quiet(1.0)         # a 40 ms click, shorter than resume_ms (60 ms)
    with_h = e.simulate(pcm, 350, hysteresis=True)
    without_h = e.simulate(pcm, 350, hysteresis=False)
    assert len(with_h) == 1 and with_h[0]["at_ms"] < without_h[0]["at_ms"] + 1


def test_wav_loading_converts_rate_and_channels(tmp_path):
    p = tmp_path / "x.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", int(3000 * math.sin(2 * math.pi * 300 * i / 16000))) for i in range(16000)))
    pcm = e.load_wav(p)
    assert abs(len(pcm) / 2 / 8000 - 1.0) < 0.02


def test_every_recording_is_replayed_in_both_versions_reported_separately(tmp_path):
    for name, pcm in (("u01_hi_S1", tone(1.0) + quiet(0.8) + tone(1.0)), ("u02_en_S1", tone(1.5)), ("roomtone_S1", quiet(2.0))):
        with wave.open(str(tmp_path / f"{name}.wav"), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000); w.writeframes(pcm)
    res = e.run_dir(tmp_path, [700, 350])
    assert set(res) == {"clean8k", "phone"}
    for r in res.values():
        assert r["n_files"] == 2                                              # the room-tone file is not an utterance
        assert r["thresholds"]["700"]["n_files_with_false_cutoff"] == 1       # the 0.8 s pause is longer than 700 ms
        assert r["thresholds"]["350"]["files_without_any_decision"] == []


def test_zero_cutoffs_only_bound_the_true_rate_by_one_minus_005_to_the_one_over_n():
    for n, pct in ((6, 0.39), (30, 0.095), (150, 0.02)):
        assert e.upper_bound(0, n) == pytest.approx(1 - 0.05 ** (1 / n), abs=1e-6)
        assert e.upper_bound(0, n) == pytest.approx(pct, abs=0.005)
    ub = e.upper_bound(3, 30)                                    # the bound solves P(X <= k) = 0.05
    assert e.binom_cdf(3, 30, ub) == pytest.approx(0.05, abs=1e-6)
    lb = e.lower_bound(3, 30)
    assert 1 - e.binom_cdf(2, 30, lb) == pytest.approx(0.05, abs=1e-6) and lb < 3 / 30 < ub


def test_small_samples_give_a_range_not_a_pass_and_150_clean_ones_pass():
    small = e.judge(0, 30)
    assert small["verdict"] == "RANGE ONLY" and small["upper_95"] == pytest.approx(0.095, abs=0.002) and "too small" in small["note"] and "150" in small["note"]
    assert e.judge(0, 148)["verdict"] == "RANGE ONLY" and e.judge(0, 149)["verdict"] == "PASS" and e.judge(0, 150)["verdict"] == "PASS"   # "about 150": the exact limit is n = 149
    assert e.judge(10, 30)["verdict"] == "NO-GO"                 # clearly above 5% even with 30 files
    assert e.judge(1, 150)["verdict"] == "RANGE ONLY"


def test_groups_follow_the_file_names():
    assert [e.group_of(f) for f in ("u02_hg_S1", "u13_hi_S2", "p07_hi_S1", "s03_en_S3", "x")] == ["scripted", "pause", "pause", "spontaneous", "other"]


def test_a_decision_forced_by_the_6_second_cap_is_not_a_false_cutoff():
    long_speech = tone(7.0)                                      # one unbroken 7 s answer: the 6 s cap splits it whatever the silence threshold is
    res = e.analyze({"s01_hi_S1": long_speech}, [700, 350])
    for r in res["thresholds"].values():
        assert r["n_files_with_false_cutoff"] == 0 and r["files_with_a_6s_cap_decision"] == ["s01_hi_S1"]


def test_recordings_over_the_6s_cap_are_counted_and_listed_separately_and_excluded_from_a_second_reading():
    res = e.analyze({"s01_hi_S1": tone(7.0), "u01_hi_S1": tone(1.5), "u02_hg_S1": tone(5.0)}, [700, 350])
    assert res["over_6s_cap"]["n"] == 1 and res["over_6s_cap"]["of"] == 3 and res["over_6s_cap"]["files"] == ["s01_hi_S1"]
    for r in res["thresholds"].values():
        assert r["without_the_files_over_the_6s_cap"]["n"] == 2


def test_the_reading_says_plainly_that_a_pass_needs_about_150_and_that_range_only_keeps_700_ms(capsys):
    res = e.analyze({"u01_hi_S1": tone(1.5), "u02_hg_S1": tone(1.2)}, [700, 350])
    rd = res["reading"]
    assert rd["pass_possible"] is False and all(v == "RANGE ONLY" for v in rd["verdicts"].values())
    assert "A PASS is impossible below about 150 clean utterances" in rd["pass_rule"] and "n=2" in rd["pass_rule"]
    assert "STAYS AT 700 ms for now" in rd["silence_wait"]
    e.report(res, "clean8k")
    out = capsys.readouterr().out
    assert "PLAINLY: A PASS is impossible below about 150 clean utterances" in out and "PLAINLY: No threshold reached PASS, so the silence wait STAYS AT 700 ms for now" in out
    assert "RECORDINGS OVER THE 6 s CAP (reported separately): 0 of 2" in out


def test_with_150_clean_files_and_no_cutoffs_a_threshold_can_pass():
    files = {f"u{(i % 12) + 1:02d}_hi_S1_{i}": tone(1.5) for i in range(150)}
    res = e.analyze(files, [350])
    assert res["reading"]["verdicts"]["350"] == "PASS" and res["reading"]["pass_possible"] is True and "A threshold reached PASS" in res["reading"]["silence_wait"]


def test_report_states_how_much_the_result_proves(capsys):
    e.report(e.analyze({"u01_hi_S1": tone(1.0), "u02_hg_S1": tone(1.2)}, [700]), "phone")
    out = capsys.readouterr().out
    assert "[phone]" in out and "HOW MUCH THIS PROVES" in out and "39% at n=6, 9.5% at n=30, 2% at n=150" in out and "RANGE ONLY" in out


def test_report_prints_the_table(capsys):
    e.report(e.analyze({"u01": tone(1.0) + quiet(0.5) + tone(1.0)}, [700, 350]))
    out = capsys.readouterr().out
    assert "silence ms" in out and "350" in out and "true-rate range (95%)" in out
