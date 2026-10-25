"""Full-duplex input (flag `full_duplex_input`): the barge-in detector, the echo guard, the pre-roll and the end-of-speech hysteresis. Pure logic, no network."""
import audioop
import struct

import pytest

from app.ai.duplex import BargeInDetector, DuplexConfig, OutboundLevel, PreRoll, duplex_config_from, silence_resets

FRAME_MS = 20
VAD = 450.0                      # the service's VAD threshold; barge-in threshold = max(450 x 2.2, 1000) = 1000


def frame(marker: int = 1, n: int = 160) -> bytes:
    """A 20 ms frame at 8 kHz whose content carries a marker (so tests can see which frames were kept)."""
    return struct.pack("<h", marker) * n


def run(det, pattern, outbound=0.0):
    """pattern: list of (rms, ms). Feeds 20 ms frames; returns the audio-time (ms) at which the detector fired, or None."""
    t = 0
    for rms, ms in pattern:
        for _ in range(ms // FRAME_MS):
            t += FRAME_MS
            if det.feed(frame(), rms, FRAME_MS, outbound):
                return t
    return None


def detector(**kw):
    return BargeInDetector(DuplexConfig(**kw), VAD)


# ------------------------------------------------------------------ backchannel and the minimum voiced time
def test_a_short_sound_is_not_an_interruption_but_250_ms_of_speech_is():
    assert run(detector(), [(3000, 240)]) is None                                 # "hmm" / a cough: under 250 ms
    assert run(detector(), [(3000, 260)]) == 260                                   # fires on the frame that completes 250 ms (rounded up to a frame)
    assert run(detector(min_voiced_ms=400), [(3000, 380)]) is None and run(detector(min_voiced_ms=400), [(3000, 420)]) == 400    # configurable


def test_the_old_rule_would_have_fired_at_200_ms_the_new_one_waits():
    assert run(detector(min_voiced_ms=200), [(3000, 220)]) == 200
    assert run(detector(min_voiced_ms=250), [(3000, 220)]) is None


def test_the_threshold_floor_and_the_vad_multiplier_still_apply():
    assert run(detector(), [(900, 600)]) is None                                   # below the 1000 floor: line noise or far-away speech
    assert run(detector(), [(1100, 300)]) == 260                                    # just above the floor: speech
    d = BargeInDetector(DuplexConfig(), 1000.0)                                    # a noisier line: VAD 1000 -> threshold 2200
    assert run(d, [(2000, 600)]) is None and run(BargeInDetector(DuplexConfig(), 1000.0), [(2300, 300)]) is not None


# ------------------------------------------------------------------ hysteresis: syllable gaps do not break the run
def test_short_gaps_inside_speech_are_bridged_longer_ones_start_over():
    assert run(detector(), [(3000, 100), (200, 80), (3000, 160)]) is not None      # 100 voiced + 80 quiet + 160 voiced = 260 voiced ms
    assert run(detector(), [(3000, 100), (200, 120), (3000, 160)]) is None         # a 120 ms gap (> 100) ends the first run: 100, then 160: never 250
    assert run(detector(dropout_ms=200), [(3000, 100), (200, 160), (3000, 160)]) is not None      # configurable


def test_many_syllables_with_small_gaps_add_up():
    pattern = [(3000, 60), (300, 40)] * 6                                          # 6 x 60 ms voiced with 40 ms gaps
    assert run(detector(), pattern) is not None


def test_a_gap_before_any_speech_does_nothing():
    d = detector()
    for _ in range(20):
        assert d.feed(frame(), 100, FRAME_MS) is False
    assert d.voiced_ms == 0 and d.take() == b""


# ------------------------------------------------------------------ the speech that triggered it is kept
def test_the_whole_run_is_kept_including_the_bridged_gap():
    d = detector()
    seq = [(3000, 100, 1), (200, 60, 2), (3000, 160, 3)]
    fired = False
    for rms, ms, marker in seq:
        for _ in range(ms // FRAME_MS):
            fired = d.feed(frame(marker), rms, FRAME_MS) or fired
    assert fired
    kept = d.take()
    assert len(kept) == (100 + 60 + 160) // FRAME_MS * 320                          # every frame of the run, 16-bit samples
    marks = [struct.unpack_from("<h", kept, i)[0] for i in range(0, len(kept), 320)]
    assert marks == [1] * 5 + [2] * 3 + [3] * 8                                    # in order, nothing dropped
    assert d.voiced_ms == 0 and d.take() == b""                                    # taking it resets the detector


# ------------------------------------------------------------------ the echo guard
def test_speaker_echo_quieter_than_the_assistant_is_ignored_a_caller_louder_than_the_echo_is_not():
    assert run(detector(), [(2500, 600)], outbound=4000) is None                   # 2500 < 0.8 x 4000 = 3200: the echo of the assistant
    assert run(detector(), [(4500, 300)], outbound=4000) is not None               # a person talking over it: clearly louder than the echo
    assert run(detector(), [(1500, 300)], outbound=0) is not None                  # the same level with a quiet assistant is speech


def test_the_echo_guard_can_be_switched_off_and_tuned():
    assert run(detector(echo_ratio=0.0), [(1500, 300)], outbound=4000) is not None
    assert run(detector(echo_ratio=0.2), [(1500, 300)], outbound=4000) is not None  # 0.2 x 4000 = 800 < 1000 floor
    assert run(detector(echo_ratio=0.6), [(2500, 300)], outbound=4000) is not None  # 0.6 x 4000 = 2400 < 2500
    assert run(detector(echo_ratio=0.8), [(2500, 600)], outbound=4000) is None      # 0.8 x 4000 = 3200


def test_echo_suppressed_frames_are_counted_for_diagnostics():
    d = detector()
    run(d, [(2500, 200)], outbound=4000)
    assert d.echo_suppressed_frames == 10 and d.triggers == 0


def test_the_loudness_statistic_mean_is_gentler_than_max_for_speech_with_peaks():
    o = OutboundLevel()
    o.add_next(0.0, 0.1, 6000)                     # a loud syllable ...
    o.add_next(0.0, 0.4, 1000)                     # ... in a quiet stretch
    assert o.recent(0.5, 500, "max") == 6000
    assert o.recent(0.5, 500, "mean") == pytest.approx((6000 * 0.1 + 1000 * 0.4) / 0.5, rel=0.01) == pytest.approx(2000, rel=0.01)
    assert o.recent(9.0, 500, "mean") == 0.0 and DuplexConfig().echo_stat == "mean"


# ------------------------------------------------------------------ outbound level tracking
def test_outbound_level_follows_the_playback_timeline_not_the_send_time():
    o = OutboundLevel()
    o.add_next(10.0, 1.0, 3000)                    # sent at t=10, plays 10..11
    o.add_next(10.0, 1.0, 500)                     # queued behind it: plays 11..12
    assert o.recent_max(10.5, 100) == 3000
    assert o.recent_max(11.5, 100) == 500          # the loud part has passed
    assert o.recent_max(11.5, 700) == 3000         # ... but the window still reaches back to it (echo arrives late)
    assert o.recent_max(9.0, 100) == 0.0           # not audible yet
    assert o.recent_max(13.0, 100) == 0.0          # long over


def test_a_whole_clip_is_split_so_the_loudness_follows_the_speech():
    loud, quiet = struct.pack("<h", 4000) * 800, struct.pack("<h", 300) * 800      # 100 ms each at 8 kHz
    o = OutboundLevel()
    o.add_clip(0.0, loud + quiet, 8000)
    assert o.recent_max(0.05, 20) == pytest.approx(4000, rel=0.01)
    assert o.recent_max(0.15, 20) == pytest.approx(300, rel=0.05)


def test_old_spans_are_dropped():
    o = OutboundLevel(keep_s=1.0)
    o.add_next(0.0, 0.1, 100)
    o.add_next(5.0, 0.1, 200)
    assert len(o._spans) == 1


# ------------------------------------------------------------------ pre-roll
def test_pre_roll_keeps_the_last_300_ms_and_hands_them_over_without_the_newest_frame():
    p = PreRoll(300)
    for i in range(40):                            # 800 ms of frames, markers 0..39
        p.push(frame(i), FRAME_MS)
    kept = p.drain(exclude_last=True)
    marks = [struct.unpack_from("<h", kept, i)[0] for i in range(0, len(kept), 320)]
    assert marks == list(range(25, 39))            # 14 frames = 280 ms (the 15th is the newest, excluded): about the last 300 ms
    assert p.drain() == b""


def test_pre_roll_of_zero_keeps_nothing():
    p = PreRoll(0)
    p.push(frame(), FRAME_MS)
    assert p.drain(exclude_last=False) == b""


# ------------------------------------------------------------------ end-of-speech hysteresis
def test_a_short_loud_blip_does_not_restart_the_silence_timer_a_real_resumption_does():
    c = DuplexConfig(resume_ms=60)
    assert silence_resets(True, 40, c) is False     # a 40 ms click
    assert silence_resets(True, 60, c) is True      # 60 ms of voice: the caller is talking again
    assert silence_resets(False, 20, c) is True     # before speech is confirmed there is nothing to postpone
    assert silence_resets(True, 20, DuplexConfig(resume_ms=0)) is True      # hysteresis off


# ------------------------------------------------------------------ configuration
def test_defaults_are_the_provisional_values_and_tenant_values_override_them():
    d = duplex_config_from({})
    assert (d.min_voiced_ms, d.dropout_ms, d.echo_ratio, d.resume_ms, d.pre_roll_ms) == (250, 100, 0.8, 60, 300)
    d = duplex_config_from({"duplex_min_voiced_ms": 400, "duplex_echo_ratio": 0.0, "duplex_pre_roll_ms": 0, "duplex_dropout_ms": None})
    assert (d.min_voiced_ms, d.echo_ratio, d.pre_roll_ms, d.dropout_ms) == (400, 0.0, 0, 100)
