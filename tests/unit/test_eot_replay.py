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


def test_report_prints_the_table(capsys):
    e.report(e.analyze({"u01": tone(1.0) + quiet(0.5) + tone(1.0)}, [700, 350]))
    out = capsys.readouterr().out
    assert "false" not in out.lower() or "go/no-go" in out
    assert "silence ms" in out and "350" in out
