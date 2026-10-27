"""Clip preparation of Voice Studio (app/voices/clip.py). Fixtures are generated tones over low noise (tests/voice_fixtures.py): no real voice audio."""
import subprocess
from types import SimpleNamespace

import pytest

from app.voices import clip
from tests.voice_fixtures import good_clip, pack_wav, wav_from_segments, wav_info

NO_FFMPEG = lambda name: None  # noqa: E731


def prep(data, **kw):
    return clip.prepare_clip(data, which=kw.pop("which", NO_FFMPEG), **kw)


def rejected(data, **kw):
    with pytest.raises(clip.ClipRejected) as e:
        prep(data, **kw)
    return e.value


def test_a_good_clip_becomes_mono_24khz_16bit_with_the_silence_trimmed():
    p = prep(good_clip())
    info = wav_info(p.wav)
    assert (info["rate"], info["channels"], info["width"]) == (24000, 1, 2)
    r = p.report
    assert 10.0 <= r["duration_s"] <= 10.8                      # 10 s of speech + about 0.15 s of silence kept on each side
    assert 0.7 <= r["trimmed_start_s"] <= 0.95 and 0.7 <= r["trimmed_end_s"] <= 0.95
    assert abs(info["seconds"] - r["duration_s"]) < 0.05
    assert r["original_duration_s"] == pytest.approx(12.0, abs=0.05)
    assert -12.0 < r["peak_dbfs"] < -6.0 and r["noise_floor_dbfs"] < -45.0
    assert r["speech_s_first_6s"] > 5.0 and r["speech_s_first_10s"] > 8.5          # reported for the first 6 s and the first 10 s
    assert r["clipped_fraction"] == 0 and r["snr_db"] > 25 and r["warnings"] == []


@pytest.mark.parametrize("rate,channels,width", [(44100, 2, 2), (48000, 1, 2), (22050, 1, 2), (16000, 1, 1), (24000, 1, 3), (8000, 2, 2)])
def test_other_rates_channels_and_sample_widths_are_converted(rate, channels, width):
    data = wav_from_segments([("silence", 0.6), ("speech", 6.0), ("silence", 0.6)], rate=rate, channels=channels, width=width)
    p = prep(data)
    info = wav_info(p.wav)
    assert (info["rate"], info["channels"], info["width"]) == (24000, 1, 2)
    assert 6.0 <= p.report["duration_s"] <= 6.8


def test_it_reports_the_seconds_of_speech_in_the_first_6_and_10_seconds():
    # 2 s speech, 5 s of silence INSIDE the clip, 6 s speech: the first 6 s hold about 2 s of speech, the first 10 s about 5 s
    p = prep(wav_from_segments([("silence", 0.5), ("speech", 2.0), ("silence", 5.0), ("speech", 6.0), ("silence", 0.5)]))
    r = p.report
    assert r["speech_s_first_6s"] == pytest.approx(2.0 + 0.0, abs=0.9) and r["speech_s_first_10s"] == pytest.approx(5.0, abs=1.0)
    assert [w["code"] for w in r["warnings"]] == ["little_speech_first_6s"]


def test_noise_floor_is_measured_and_a_noisy_room_is_a_warning_not_a_refusal():
    p = prep(wav_from_segments([("silence", 1.0), ("speech", 8.0), ("silence", 1.0)], amp=0.7, noise=0.06))
    assert p.report["noise_floor_dbfs"] > -40.0
    assert "noisy_room" in [w["code"] for w in p.report["warnings"]]


def test_a_quiet_recording_is_a_warning():
    p = prep(wav_from_segments([("silence", 1.0), ("speech", 8.0), ("silence", 1.0)], amp=0.04, noise=0.0005))
    assert -40.0 < p.report["peak_dbfs"] < -26.0
    assert "quiet_recording" in [w["code"] for w in p.report["warnings"]]


@pytest.mark.parametrize("segments,code", [
    ([("silence", 1.0), ("speech", 2.5), ("silence", 1.0)], "clip_too_short"),     # about 2.8 s after trimming
    ([("silence", 1.0), ("speech", 1.0), ("silence", 1.0)], "clip_silent"),        # less than 2 s of speech in all
    ([("silence", 8.0)], "clip_silent"),
    ([("silence", 1.0), ("speech", 33.0), ("silence", 1.0)], "clip_too_long"),
])
def test_clips_that_are_too_short_silent_or_too_long_are_refused_with_the_measured_report(segments, code):
    e = rejected(wav_from_segments(segments, rate=8000))
    assert e.code == code and e.message and e.report.get("peak_dbfs") is not None


def test_a_very_quiet_clip_is_treated_as_silent():
    e = rejected(wav_from_segments([("silence", 1.0), ("speech", 8.0), ("silence", 1.0)], amp=0.003, noise=0.0002))
    assert e.code == "clip_silent"


def test_a_clipped_clip_is_refused():
    e = rejected(wav_from_segments([("silence", 1.0), ("speech", 8.0), ("silence", 1.0)], amp=0.9, hard_clip=True))
    assert e.code == "clip_clipped" and e.report["clipped_fraction"] > clip.MAX_CLIPPED_FRACTION


def test_input_longer_than_two_minutes_is_refused_before_any_analysis():
    e = rejected(pack_wav([0.0] * (8000 * 130), rate=8000, width=1))
    assert e.code == "clip_too_long" and e.report["original_duration_s"] >= 130


def test_something_that_is_not_a_wav_is_refused_and_the_message_says_wav_only():
    e = rejected(b"ID3\x03\x00\x00" + b"\x00" * 500)
    assert e.code == "clip_unsupported_format" and "WAV" in e.message
    assert rejected(b"").code == "clip_unreadable"


def test_a_float_wav_is_unsupported_and_a_damaged_header_is_unreadable():
    good = bytearray(good_clip())
    good[20:22] = (3).to_bytes(2, "little")                      # WAVE_FORMAT_IEEE_FLOAT
    assert rejected(bytes(good)).code == "clip_unsupported_format"
    assert rejected(b"RIFF\x10\x00\x00\x00WAVEfmt garbage").code == "clip_unreadable"


def test_more_than_two_channels_is_refused():
    data = bytearray(good_clip())
    data[22:24] = (6).to_bytes(2, "little")
    assert rejected(bytes(data)).code in ("clip_unsupported_format", "clip_unreadable")


def test_the_decoder_report_says_what_this_server_can_read():
    r = clip.decoder_report(which=NO_FFMPEG)
    assert r["wav_pcm"] is True and r["ffmpeg"] is False and "WAV only" in r["accepted"]
    r = clip.decoder_report(which=lambda n: "/usr/bin/ffmpeg")
    assert r["ffmpeg"] is True and "ffmpeg" in r["accepted"] and "/usr" not in str(r)         # presence only: no path is reported


def test_when_ffmpeg_exists_other_formats_go_through_it_by_pipes_only():
    seen = {}

    def runner(cmd, **kw):
        seen["cmd"], seen["input"], seen["kw"] = cmd, kw["input"], kw
        return SimpleNamespace(returncode=0, stdout=good_clip(), stderr=b"")

    p = prep(b"ID3 pretend this is an mp3", which=lambda n: "/usr/bin/ffmpeg", runner=runner)
    assert p.report["duration_s"] > 10 and seen["input"].startswith(b"ID3")
    cmd = seen["cmd"]
    assert cmd[0] == "/usr/bin/ffmpeg" and "pipe:0" in cmd and "pipe:1" in cmd and cmd[cmd.index("-protocol_whitelist") + 1] == "pipe"     # no files, no network
    assert seen["kw"]["timeout"] == clip.FFMPEG_TIMEOUT_S and "shell" not in seen["kw"]


def test_a_failing_converter_is_a_clean_refusal():
    bad = lambda cmd, **kw: SimpleNamespace(returncode=1, stdout=b"", stderr=b"x")           # noqa: E731
    assert rejected(b"ID3 not audio", which=lambda n: "/usr/bin/ffmpeg", runner=bad).code == "clip_unreadable"

    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 20)
    assert rejected(b"ID3 not audio", which=lambda n: "/usr/bin/ffmpeg", runner=boom).code == "clip_unreadable"


def test_a_wav_ffmpeg_would_still_be_tried_for_when_pcm_decoding_is_unsupported():
    good = bytearray(good_clip())
    good[20:22] = (3).to_bytes(2, "little")
    p = prep(bytes(good), which=lambda n: "/usr/bin/ffmpeg",
             runner=lambda cmd, **kw: SimpleNamespace(returncode=0, stdout=good_clip(), stderr=b""))
    assert p.report["duration_s"] > 10
