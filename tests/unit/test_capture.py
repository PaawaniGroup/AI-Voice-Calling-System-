"""Capture state machine: pre-roll, short speech, endpointing, soft cap, barge-in, accounting, bounded queue (synthetic frames, no I/O)."""
import struct

from app.ai.capture import (CANCELLED, EXPIRED, PROCESSED, REJECTED, Capture, CaptureConfig, Utterance, UtteranceQueue)
from app.ai.clients import rms as pcm_rms

FM = 20
N = 160
SPEECH, QUIET = 2500, 100


def frame(idx: int, level: int) -> bytes:
    """160 samples at `level`, sample 0 carries the frame index so order/duplication can be read back from the PCM."""
    return struct.pack("<%dh" % N, idx, *([level] * (N - 1)))


def ids_of(pcm: bytes) -> list[int]:
    return [struct.unpack_from("<h", pcm, i)[0] for i in range(0, len(pcm), N * 2)]


class Rig:
    def __init__(self, **kw):
        self.cap = Capture(CaptureConfig(**kw))
        self.i = 0
        self.t = 0.0
        self.events = []

    def feed(self, ms, level, playing=False):
        for _ in range(ms // FM):
            f = frame(self.i, level)
            self.i += 1
            self.t += FM / 1000
            self.events += self.cap.feed(f, FM, pcm_rms(f), self.t, playing)

    def segs(self):
        return [e.segment for e in self.events if e.kind == "segment"]


def test_first_250ms_of_speech_preserved_with_pre_roll():
    r = Rig()
    r.feed(1000, QUIET)
    onset = r.i
    r.feed(1500, SPEECH)
    r.feed(1400, QUIET)
    (seg,) = r.segs()
    got = ids_of(seg.pcm)
    assert got == sorted(set(got))                      # ordered, no duplicate frame
    assert all(i in got for i in range(onset, onset + 75))   # every speech frame, from the very first one
    assert onset - got[0] == 15                         # 300 ms of pre-roll (15 frames) precede the first loud frame
    assert r.cap.stats.balanced(r.cap)


def test_short_acknowledgements_reach_stt():
    for ms in (60, 100, 180, 240):
        r = Rig()
        r.feed(400, QUIET); r.feed(ms, SPEECH); r.feed(1500, QUIET)
        assert len(r.segs()) == 1, ms
        assert r.segs()[0].speech_ms == ms


def test_noise_burst_is_rejected_with_reason_not_silently_dropped():
    r = Rig()
    r.feed(400, QUIET); r.feed(20, SPEECH); r.feed(1500, QUIET)
    assert not r.segs()
    assert [e.reason for e in r.events if e.kind == "rejected"] == ["noise_burst"]
    assert r.cap.stats.rejected > 0 and r.cap.stats.balanced(r.cap)


def test_pauses_up_to_1000ms_do_not_split():
    for pause in (650, 800, 1000):
        r = Rig()
        r.feed(400, QUIET); r.feed(400, SPEECH); r.feed(pause, QUIET); r.feed(1500, SPEECH); r.feed(1500, QUIET)
        assert len(r.segs()) == 1, pause
        assert r.segs()[0].speech_ms == 1900


def test_a_long_gap_does_split_and_utterances_do_not_leak_pre_roll():
    r = Rig()
    r.feed(400, QUIET); r.feed(500, SPEECH); r.feed(2000, QUIET); r.feed(500, SPEECH); r.feed(1500, QUIET)
    a, b = r.segs()
    assert max(ids_of(a.pcm)) < min(ids_of(b.pcm))
    assert not set(ids_of(a.pcm)) & set(ids_of(b.pcm))
    assert r.cap.stats.balanced(r.cap)


def test_trailing_silence_is_trimmed_to_budget_and_counted():
    r = Rig(trail_keep_ms=200)
    r.feed(300, QUIET); r.feed(500, SPEECH); r.feed(1500, QUIET)
    (seg,) = r.segs()
    assert len(seg.pcm) // (N * 2) * FM <= 300 + 500 + 200
    assert r.cap.stats.trimmed > 0 and r.cap.stats.balanced(r.cap)


def test_long_continuous_speech_is_never_truncated_and_soft_cap_segments_tile_exactly():
    for secs in (7.5, 10, 25):
        r = Rig()
        r.feed(400, QUIET); first = r.i
        r.feed(int(secs * 1000), SPEECH); last = r.i
        r.feed(1500, QUIET)
        segs = r.segs()
        assert [s.continues for s in segs[:-1]] == [True] * (len(segs) - 1) and segs[-1].continues is False
        merged = [i for s in segs for i in ids_of(s.pcm)]
        assert merged == sorted(set(merged))                       # no duplicate, no reorder across segments
        assert set(range(first, last)) <= set(merged)              # every speech frame survived the cap
        assert all(len(s.pcm) // (N * 2) * FM <= 8000 + FM for s in segs)
        assert r.cap.stats.balanced(r.cap)


def test_playback_does_not_discard_or_replace_a_capture_in_progress():
    r = Rig()
    r.feed(300, QUIET); r.feed(1000, 600)                          # caller (quiet voice) already talking
    r.feed(1000, 600, playing=True)                                # reply starts mid-sentence: sub-barge level
    r.feed(500, QUIET, playing=True); r.feed(1500, QUIET)
    assert [e.kind for e in r.events].count("barge") == 1          # stale reply is invalidated exactly once
    (seg,) = r.segs()
    assert seg.speech_ms == 2000                                   # nothing lost across the playback boundary
    assert r.cap.stats.balanced(r.cap)


def test_barge_in_from_idle_keeps_the_audio_before_the_trigger():
    r = Rig()
    r.feed(500, QUIET, playing=True)
    onset = r.i
    r.feed(1500, SPEECH, playing=True)
    r.feed(1500, QUIET)
    assert [e.kind for e in r.events if e.kind == "barge"] == ["barge"]
    (seg,) = r.segs()
    got = ids_of(seg.pcm)
    assert set(range(onset, onset + 75)) <= set(got) and got == sorted(set(got))
    assert r.cap.stats.balanced(r.cap)


def test_quiet_speech_during_playback_is_counted_not_silently_lost():
    r = Rig()
    r.feed(2000, 700, playing=True)                                # echo-level: below barge, documented limitation
    assert not r.events
    r.feed(600, QUIET, playing=True)
    assert r.cap.stats.playback_unmatched > 0 and r.cap.stats.balanced(r.cap)


def test_idle_gap_ends_an_utterance():
    r = Rig()
    r.feed(300, QUIET); r.feed(400, SPEECH)
    for _ in range(30):
        r.events += r.cap.idle_tick(50, r.t)
    assert len(r.segs()) == 1


def test_reset_accounts_for_everything_held():
    r = Rig()
    r.feed(300, QUIET); r.feed(400, SPEECH)
    r.cap.reset()
    assert r.cap.stats.balanced(r.cap) and not r.cap.buf and not r.cap.ring


# ------------------------------------------------------------------ queue
def utt(q, pcm=b"\0\0" * 800, t=0.0, gen=0):
    return Utterance(q.new_id(), gen, pcm, t, t, t)


def test_queue_full_merges_instead_of_dropping_and_every_utterance_ends_in_one_state():
    q = UtteranceQueue(8000, max_items=2)
    us = [utt(q, pcm=bytes([i]) * 1600) for i in range(5)]
    notes = [n for u in us for n in q.put(u)]
    assert len(q) == 2 and [k for k, _ in notes] == ["merged"] * 3
    last = q.items[-1]
    assert last.pcm == bytes([1]) * 1600 + bytes([2]) * 1600 + bytes([3]) * 1600 + bytes([4]) * 1600   # order kept, audio kept
    for u in list(q.items):
        q.finish(u, PROCESSED)
    q.finish(last, CANCELLED)            # a second terminal transition is ignored
    assert sorted(q.terminal) == [1, 2, 3, 4, 5] or len(q.terminal) == 5
    assert q.terminal[last.id] == PROCESSED


def test_queue_over_audio_ceiling_rejects_oldest_explicitly():
    q = UtteranceQueue(8000, max_items=1, max_merged_ms=100)
    a, b = utt(q, pcm=b"\0\0" * 1600), utt(q, pcm=b"\0\0" * 1600)
    q.put(a)
    assert [k for k, _ in q.put(b)] == ["rejected"]
    assert q.terminal[a.id] == REJECTED and q.log[-1][2] == "queue_full_oldest_rejected" and list(q.items) == [b]


def test_expiry_and_cancel_all_leave_terminal_states():
    q = UtteranceQueue(8000, max_items=4, expire_s=10)
    old, new, pend = utt(q, t=0.0), utt(q, t=95.0), utt(q, t=96.0)
    for u in (old, new, pend):
        q.put(u)
    got, expired = q.get_nowait(now=100.0)
    assert got is new and expired == [old] and q.terminal[old.id] == EXPIRED
    q.cancel_all("disconnect")
    assert q.terminal[pend.id] == CANCELLED and len(q) == 0
