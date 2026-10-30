"""Paced audio output on a virtual clock: 20 ms frames, lead (pre-buffer), underruns as silence + a recorded gap, fades at joins, loudness matching,
cancel and per-clause played/sent accounting. No network, no real time."""
import array
import asyncio
import math

import pytest

from app.ai.streaming.playout import PlayoutConfig, TurnPlayout, voiced_rms
from tests.fakeclock import FakeClock

RATE = 8000
FRAME_BYTES = 320


def tone(ms, amp=8000, freq=300):
    n = RATE * ms // 1000
    return array.array("h", [int(amp * math.sin(2 * math.pi * freq * i / RATE)) for i in range(n)]).tobytes()


class Sink:
    def __init__(self, clock):
        self.clock, self.frames, self.gens = clock, [], set()

    async def send(self, gen, frame):
        self.gens.add(gen)
        self.frames.append((self.clock.now(), frame))


def samples(b):
    a = array.array("h")
    a.frombytes(b)
    return a


async def play(clock, script, cfg=None, gen=1):
    """Run a TurnPlayout while `script(po, sink)` feeds it; returns (po, sink, report)."""
    sink = Sink(clock)
    po = TurnPlayout(sink, clock, gen, cfg or PlayoutConfig())
    task = asyncio.ensure_future(po.run())
    await script(po, sink)
    rep = await task
    return po, sink, rep


# ------------------------------------------------------------------ pacing
async def test_frames_are_20ms_burst_the_lead_first_then_one_per_frame_time():
    clock = FakeClock()

    async def script(po, sink):
        po.enqueue(1, 0, tone(1000), words=8)
        po.end_turn(1)

    po, sink, rep = await clock.run(play(clock, script))
    t0 = sink.frames[0][0]
    burst = math.ceil(150 / 20)
    assert all(len(f) == FRAME_BYTES for _, f in sink.frames)
    assert len(sink.frames) == 50                                              # exactly one second of 20 ms frames
    for k, (t, _) in enumerate(sink.frames):
        assert t - t0 == pytest.approx(max(0, k - burst + 1) * 0.02, abs=1e-6), k
    assert rep["underruns"] == 0 and rep["reason"] == "done"


async def test_nothing_is_sent_before_the_first_clause_and_no_silence_leads_in():
    clock = FakeClock()

    async def script(po, sink):
        await clock.sleep(3.0)                                                 # the first clause is 3 s late
        assert sink.frames == []                                               # waiting is not silence
        po.enqueue(1, 0, tone(400), words=3)
        po.end_turn(1)

    po, sink, rep = await clock.run(play(clock, script))
    assert sink.frames[0][0] - clock.t0 == pytest.approx(3.0, abs=0.3) and rep["underruns"] == 0


async def test_zero_gaps_when_the_next_clause_arrives_before_the_previous_one_ends():
    clock = FakeClock()

    async def script(po, sink):                                                # clause k+1 arrives 0.3 s before clause k has finished playing
        po.enqueue(1, 0, tone(2000), words=8)
        await clock.sleep(1.7)
        po.enqueue(1, 1, tone(2000), words=8)
        await clock.sleep(2.0)
        po.enqueue(1, 2, tone(1000), words=5)
        po.end_turn(1)

    po, sink, rep = await clock.run(play(clock, script))
    assert rep["underruns"] == 0 and rep["silence_ms"] == 0
    assert rep["sent_ms"] == pytest.approx(5000, abs=20)
    times = [t for t, _ in sink.frames]
    assert max(b - a for a, b in zip(times, times[1:])) <= 0.0201               # the timeline never stalls


async def test_a_late_clause_is_an_underrun_measured_from_when_the_far_end_runs_dry_and_the_late_clause_is_not_delayed_further():
    clock = FakeClock()
    arrival = {}

    async def script(po, sink):                                                # RTF 1.3: the second clause arrives 1.2 s after the first has finished playing
        po.enqueue(1, 0, tone(2000), words=8)
        await clock.sleep(2.0 + 1.2)
        arrival["t"] = clock.now()
        po.enqueue(1, 1, tone(2000), words=8)
        po.end_turn(1)

    po, sink, rep = await clock.run(play(clock, script))
    assert rep["underruns"] == 1
    g = rep["gaps"][0]
    assert g["before_clause"] == 1 and g["at_ms"] == pytest.approx(2000, abs=1) and g["length_ms"] == pytest.approx(1200, abs=25)
    first_b = next(t for t, _ in sink.frames if t >= arrival["t"])
    assert first_b - arrival["t"] < 0.001                                      # no silence was queued ahead: the late clause goes out the moment it exists
    assert rep["silence_ms"] == g["length_ms"]
    assert rep["sent_ms"] - rep["played_est_ms"] == pytest.approx(rep["lead_ms"], abs=1) and rep["lead_ms"] == pytest.approx(160, abs=25)   # the pre-buffer


async def test_a_clause_that_arrives_before_the_far_end_runs_dry_is_not_a_gap():
    clock = FakeClock()

    async def script(po, sink):                                                # the sender went idle, but the far end still holds 100 ms
        po.enqueue(1, 0, tone(2000), words=8)
        await clock.sleep(2.0 - 0.1)
        po.enqueue(1, 1, tone(1000), words=5)
        po.end_turn(1)

    po, sink, rep = await clock.run(play(clock, script))
    assert rep["underruns"] == 0 and rep["sent_ms"] == pytest.approx(3000, abs=20)


async def test_several_underruns_are_each_recorded():
    clock = FakeClock()

    async def script(po, sink):
        for i in range(3):
            po.enqueue(1, i, tone(500), words=4)
            await clock.sleep(0.5 + 0.4)                                       # each next clause is 0.4 s after the previous one finished
        po.end_turn(1)

    po, sink, rep = await clock.run(play(clock, script))
    assert rep["underruns"] == 2 and [g["before_clause"] for g in rep["gaps"]] == [1, 2]
    assert [round(g["length_ms"]) for g in rep["gaps"]] == pytest.approx([400, 400], abs=25)


async def test_a_dead_producer_ends_the_turn_instead_of_waiting_forever():
    clock = FakeClock()
    cfg = PlayoutConfig(max_wait_s=2.0)

    async def script(po, sink):
        po.enqueue(1, 0, tone(300), words=3)                                   # then nothing, and no end_turn

    po, sink, rep = await clock.run(play(clock, script, cfg), max_virtual_s=60)
    assert rep["reason"] == "producer_stalled" and clock.elapsed() < 3.0


# ------------------------------------------------------------------ joins and loudness
async def test_fades_remove_clicks_at_clause_joins():
    clock = FakeClock()

    async def script(po, sink):
        po.enqueue(1, 0, tone(600, amp=20000), words=4)
        po.enqueue(1, 1, tone(600, amp=20000), words=4)
        po.end_turn(1)

    po, sink, rep = await clock.run(play(clock, script, PlayoutConfig(normalize=False)))
    s = samples(b"".join(f for _, f in sink.frames))
    join = 600 * RATE // 1000
    assert abs(s[0]) < 200 and abs(s[join - 1]) < 400 and abs(s[join]) < 400 and abs(s[-1]) <= 400    # silence at both sides of the join
    window = [abs(s[i] - s[i - 1]) for i in range(join - 100, join + 100)]
    assert max(window) < 4000                                                  # no step at the join (a raw splice of two sines would jump ~20000)


async def test_loudness_is_matched_to_the_first_clause_within_the_gain_limit_and_never_clips():
    clock = FakeClock()

    async def script(po, sink):
        po.enqueue(1, 0, tone(1000, amp=8000), words=5)
        po.enqueue(1, 1, tone(1000, amp=4000), words=5)                        # half as loud: x2 (+6 dB, the limit)
        po.enqueue(1, 2, tone(1000, amp=16000), words=5)                       # twice as loud: x0.5
        po.enqueue(1, 3, tone(1000, amp=500), words=5)                         # far too quiet: clamped at +6 dB, not boosted to match
        po.end_turn(1)

    po, sink, rep = await clock.run(play(clock, script))
    s = b"".join(f for _, f in sink.frames)
    seg = lambda i: s[i * 16000:(i + 1) * 16000]                               # noqa: E731  1 s of 16-bit samples
    r = [voiced_rms(seg(i)) for i in range(4)]
    assert r[1] == pytest.approx(r[0], rel=0.12) and r[2] == pytest.approx(r[0], rel=0.12)
    assert r[3] < r[0] * 0.3                                                   # clamp: not amplified without limit
    assert max(abs(x) for x in samples(s)) <= 32767


# ------------------------------------------------------------------ cancel and accounting
async def test_cancel_stops_the_next_frame_and_reports_what_was_sent_and_what_was_played():
    clock = FakeClock()
    out = {}

    async def script(po, sink):
        po.enqueue(1, 0, tone(1000), words=6)
        po.enqueue(1, 1, tone(1000), words=6)
        po.enqueue(1, 2, tone(1000), words=6)
        await clock.sleep(1.3)                                                 # barge-in 1.3 s after the first frame
        out["n_before"] = len(sink.frames)
        out["snap"] = po.cancel()
        out["t_cancel"] = clock.now()
        await clock.sleep(1.0)

    po, sink, rep = await clock.run(play(clock, script))
    assert len(sink.frames) <= out["n_before"] + 1                             # at most the frame already in progress
    assert sink.frames[-1][0] - out["t_cancel"] < 0.021                        # interruption -> last frame: within one frame time
    snap = out["snap"]
    c = {x["idx"]: x for x in snap["clauses"]}
    assert snap["lead_ms"] == pytest.approx(160, abs=25)                       # the pre-buffer: sent but not yet played
    assert c[0]["played_est"] == 8000 and c[0]["sent"] == 8000                 # clause 0 was fully played
    assert c[1]["played_est"] == pytest.approx(2400, abs=170) and c[1]["sent"] > c[1]["played_est"]
    assert c[2]["played_est"] == 0
    assert rep["reason"] == "cancelled"


async def test_audio_for_another_generation_or_after_cancel_is_dropped_and_counted():
    clock = FakeClock()

    async def script(po, sink):
        assert po.enqueue(2, 0, tone(300), words=3) is False                   # stale generation
        po.enqueue(1, 0, tone(300), words=3)
        po.cancel()
        assert po.enqueue(1, 1, tone(300), words=3) is False                   # late audio after the cancel
        await clock.sleep(0.5)

    po, sink, rep = await clock.run(play(clock, script))
    assert rep["dropped_stale"] == 2 and sink.gens <= {1}


async def test_cancel_before_any_audio_sends_nothing():
    clock = FakeClock()

    async def script(po, sink):
        await clock.sleep(0.5)
        po.cancel()

    po, sink, rep = await clock.run(play(clock, script))
    assert sink.frames == [] and rep["reason"] == "cancelled"


def test_report_has_counts_and_no_audio_or_text():
    import json
    from app.ai.streaming.playout import TurnPlayout
    po = TurnPlayout(None, FakeClock(), 1)
    po.enqueue(1, 0, tone(100), words=2)
    blob = json.dumps(po.report())
    assert "clauses" in blob and "bytes" not in blob
