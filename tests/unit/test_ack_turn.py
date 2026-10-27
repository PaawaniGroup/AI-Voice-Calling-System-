"""Cached acknowledgement inside the streaming turn, on VIRTUAL time with MOCK Brain, MOCK TTS and a recording sink. SIMULATED: nothing here measures real latency
or touches a real voice; the "acknowledgement" is a synthetic tone."""
import asyncio

import pytest

from app.ai.clients import AiError
from app.ai.streaming.turn import ACK_IDX, FILLER_IDX, StreamConfig, StreamingTurn
from app.ai.streaming.playout import PlayoutConfig
from app.ai.streaming.segmenter import SegmenterConfig
from tests.fakeclock import FakeClock
from tests.unit.test_streaming_turn import MockBrain, MockTTS, Sink, tone

ACK = tone(1.0, amp=3000)
Q = [{"role": "user", "content": "x"}]


class FrameSink(Sink):
    """Records which frames belong to the acknowledgement (its tone has a lower amplitude than the answer's)."""

    async def send(self, gen, frame):
        await super().send(gen, frame)
        peak = max(abs(int.from_bytes(frame[i:i + 2], "little", signed=True)) for i in range(0, len(frame), 2))
        self.frames[-1] = (*self.frames[-1], peak)


def build(clock, *, ack=True, after=0.3, tts=None, brain=None, picks=None, hook=None, depth=2, ack_t0=None):
    cfg = StreamConfig(segmenter=SegmenterConfig(first_min_words=3, min_words=6, max_words=14), prefetch_depth=depth, playout=PlayoutConfig(), ack_after_s=after)
    sink = FrameSink(clock)

    def pick():
        if picks is not None:
            picks.append(clock.now())
        return ACK

    t = StreamingTurn(gen=7, brain_stream=brain or MockBrain(clock), tts=tts or MockTTS(clock, delay_override=3.0), sink=sink, clock=clock, mode="hi", cfg=cfg,
                      on_first_frame=hook, ack_pick=pick if ack else None, ack_t0=ack_t0)
    return t, sink


async def test_a_slow_first_clause_gets_the_acknowledgement_after_the_wait_and_the_answer_follows_without_overlap():
    clock = FakeClock()
    t, sink = build(clock)
    out = await clock.run(t.run(Q))
    ack = next(c for c in out.playout["clauses"] if c["idx"] == ACK_IDX)
    first_real = next(c for c in out.playout["clauses"] if c["idx"] == 0)
    assert out.status == "done" and ack["real"] is False and first_real["real"] is True
    assert ack["sent"] == ack["samples"] == 8000                                   # the whole 1 s phrase went out, never cut
    acc = t.playout.accounts
    assert acc[ACK_IDX].start == 0 and acc[0].start >= acc[ACK_IDX].samples        # one timeline: the answer starts after the phrase ends (no overlap)
    t_first = sink.frames[0][0]
    assert t_first - t.m["t_start"] == pytest.approx(0.3, abs=0.03)                # played at the configured wait after the turn started
    s = out.stream
    assert s["ack_ms"] == 1000 and s["ack_at_ms"] == pytest.approx(300, abs=30)
    assert s["first_frame_ms"] > s["ack_first_frame_ms"] + 1000                    # the answer's first frame is the real one, later than the phrase


async def test_the_wait_is_counted_from_the_speech_to_text_final_not_from_the_turn_start():
    clock = FakeClock()
    t0 = clock.now() - 0.25                                                        # the final was 250 ms ago (e.g. RAG took that long)
    t, sink = build(clock, ack_t0=t0)
    await clock.run(t.run(Q))
    assert sink.frames[0][0] - t.m["t_start"] == pytest.approx(0.05, abs=0.03)


async def test_a_fast_first_clause_needs_no_acknowledgement_and_the_rotation_is_untouched():
    clock = FakeClock()
    picks = []
    t, sink = build(clock, after=2.5, tts=MockTTS(clock, delay_override=0.1), picks=picks)   # the real audio is ready long before 2.5 s
    out = await clock.run(t.run(Q))
    assert picks == [] and not any(c["idx"] == ACK_IDX for c in out.playout["clauses"]) and out.stream["ack_ms"] is None
    assert out.status == "done" and out.spoken_text == out.reply_text


async def test_the_acknowledgement_is_never_part_of_the_reply_the_history_or_the_heard_text():
    clock = FakeClock()
    t, _ = build(clock)
    out = await clock.run(t.run(Q))
    assert all(i < FILLER_IDX for i in out.clause_texts) and ACK_IDX not in out.clause_texts
    assert out.spoken_text == out.reply_text and out.reply_text.count(" ") > 5
    assert not any(c["idx"] == ACK_IDX for c in out.playout["clauses"] if c.get("real"))


async def test_barge_in_during_the_acknowledgement_silences_it_and_nothing_is_counted_as_heard():
    clock = FakeClock()
    t, sink = build(clock)
    seen = {}

    async def scenario():
        task = asyncio.ensure_future(t.run(Q))
        while t.playout.t_first is None:
            await clock.sleep(0.01)
        await clock.sleep(0.5)                                                     # the phrase is playing (0.3 s .. 1.3 s)
        seen["before"] = len(sink.frames)
        await t.interrupt("barge_in")
        try:
            await task
        except asyncio.CancelledError:
            seen["cancelled"] = True
        await clock.sleep(2.0)
        seen["after"] = len(sink.frames)

    await clock.run(scenario())
    assert seen.get("cancelled") and seen["before"] > 0
    assert seen["after"] <= seen["before"] + 1 and sink.cleared and sink.cleared[0][1] == 7   # silence at once (at most the frame in progress), far end cleared
    o = t.outcome
    assert o.status == "cancelled" and o.spoken_text == "" and o.reply_text == ""   # heard text stays empty: the phrase is not an answer
    assert t.playout.t_first_real is None and ACK_IDX not in o.clause_texts


async def test_when_the_answer_never_comes_the_phrase_does_not_make_the_turn_a_success():
    clock = FakeClock()
    t, _ = build(clock, brain=MockBrain(clock, text="जी, मैं आपको बता सकता हूँ।"), tts=MockTTS(clock, delay_override=0.9, fail={0, 1, 2}))
    out = await clock.run(t.run(Q))
    assert any(c["idx"] == ACK_IDX for c in out.playout["clauses"])                 # the phrase did play (the first clause fails after 0.9 s) ...
    assert out.status == "error" and out.error == "no_audio"                        # ... but with no real audio the turn is still an error


async def test_waiting_for_the_first_clause_after_the_phrase_is_not_an_underrun():
    clock = FakeClock()
    t, sink = build(clock, tts=MockTTS(clock, delay_override=6.0))                 # the answer is 5 s after the phrase ended
    out = await clock.run(t.run(Q))
    assert out.stream["underruns"] == 0 and out.stream["silence_ms"] == 0 and out.stream["ack_ms"] == 1000


async def test_the_first_frame_hook_fires_once_and_only_for_the_real_audio():
    clock = FakeClock()
    seen = []
    t, sink = build(clock, hook=lambda: seen.append(clock.now()))
    out = await clock.run(t.run(Q))
    assert len(seen) == 1 and seen[0] - t.m["t_start"] > 3.0                       # the answer, not the phrase at 0.3 s
    assert seen[0] == pytest.approx(t.m["t_start"] + out.stream["first_frame_ms"] / 1000, abs=0.01)


async def test_the_phrase_keeps_its_own_level_and_does_not_set_the_loudness_of_the_answer():
    clock = FakeClock()
    t, sink = build(clock)
    await clock.run(t.run(Q))
    peaks_ack = {f[2] for f in sink.frames[:40]}
    assert max(peaks_ack) <= 3000 + 5                                              # the quiet phrase was not boosted to the answer's level
    ref = t.playout._ref_rms                                                       # the turn's loudness reference comes from the first REAL clause
    assert ref and ref > 3000 * 0.5


async def test_without_the_flag_nothing_changes():
    clock = FakeClock()
    t, sink = build(clock, ack=False)
    out = await clock.run(t.run(Q))
    assert not any(c["idx"] >= FILLER_IDX for c in out.playout["clauses"]) and out.stream["ack_ms"] is None and out.stream["ack_first_frame_ms"] is None
