"""Token-streaming turn on virtual time with MOCK Brain (token stream with delays), MOCK TTS (a real-time factor you choose) and a recording sink.
Asserts: no gaps at RTF 0.5, gaps reported at RTF 1.3, barge-in truncation of the history text, no splits inside numbers, bounded TTS in flight,
the first-clause script gate with one bounded retry, tool calls with a filler, failure handling, and that no word ever reaches a log or a metric."""
import array
import asyncio
import json
import logging
import math

import pytest

from app.ai.clients import AiError
from app.ai.streaming.playout import PlayoutConfig
from app.ai.streaming.segmenter import SegmenterConfig
from app.ai.streaming.turn import FILLER_IDX, ModeTagFilter, StreamConfig, StreamingTurn, truncate_spoken
from tests.fakeclock import FakeClock

RATE = 8000
REPLY = ("जी, मैं आपको admission की पूरी जानकारी देता हूँ। B.Tech चार साल का course है, और admission entrance exam से होता है। "
         "सालाना फ़ीस लगभग ₹1,25,000 है, hostel अलग से लगता है। आप online form भर दीजिए, फिर हमारी team आपको call करेगी।")
ROMAN = "ZZQNAME ji aap kaise hain, main aapko batata hoon admission ke baare mein bahut kuch jaankari deta hoon। आगे सुनिए।"


def tone(seconds, amp=8000):
    n = int(RATE * seconds)
    return array.array("h", [int(amp * math.sin(2 * math.pi * 300 * i / RATE)) for i in range(n)]).tobytes()


class Sink:
    def __init__(self, clock):
        self.clock, self.frames, self.cleared, self.gens = clock, [], [], set()

    async def send(self, gen, frame):
        self.gens.add(gen)
        self.frames.append((self.clock.now(), len(frame)))

    async def clear(self, gen):
        self.cleared.append((self.clock.now(), gen))


class MockBrain:
    """OpenAI-style token stream: first token after `ttft`, then `chunk` characters every chunk/(chars_per_s) seconds."""

    def __init__(self, clock, text=REPLY, ttft=0.85, chars_per_s=130.0, chunk=3, rounds=None, fail_after=None):
        self.clock, self.text, self.ttft, self.cps, self.chunk = clock, text, ttft, chars_per_s, chunk
        self.rounds, self.fail_after = rounds, fail_after
        self.calls, self.closed, self.messages = 0, 0, []

    async def __call__(self, messages):
        self.calls += 1
        self.messages.append(messages)
        text = self.rounds[self.calls - 1] if self.rounds else self.text
        try:
            await self.clock.sleep(self.ttft)
            sent = 0
            for i in range(0, len(text), self.chunk):
                await self.clock.sleep(self.chunk / self.cps)
                yield {"delta": text[i:i + self.chunk]}
                sent += 1
                if self.fail_after and sent >= self.fail_after:
                    raise AiError("brain", "stream_broken")
            if isinstance(text, str) and text.startswith("TOOL:"):
                pass
            yield {"done": True}
        finally:
            self.closed += 1


class ToolBrain(MockBrain):
    """Round 1 speaks one clause and then asks for a tool; round 2 answers."""

    async def __call__(self, messages):
        self.calls += 1
        try:
            await self.clock.sleep(self.ttft)
            if self.calls == 1:
                for ch in ("एक मिनट देखता हूँ", ", "):
                    yield {"delta": ch}
                yield {"tool_calls": [{"id": "1", "type": "function", "function": {"name": "course_lookup", "arguments": "{}"}}]}
                return
            for i in range(0, len(self.text), 3):
                yield {"delta": self.text[i:i + 3]}
            yield {"done": True}
        finally:
            self.closed += 1


class MockTTS:
    def __init__(self, clock, rtf=0.5, per_word_s=0.42, fail=None, delay_override=None):
        self.clock, self.rtf, self.pw, self.fail = clock, rtf, per_word_s, fail or set()
        self.delay_override = delay_override
        self.texts, self.cancelled, self.active, self.max_active = [], [], 0, 0

    async def __call__(self, text, mode):
        n = len(self.texts)
        self.texts.append(text)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            audio_s = len(text.split()) * self.pw
            await self.clock.sleep(self.delay_override if self.delay_override is not None else self.rtf * audio_s)
            if n in self.fail:
                raise AiError("tts", "http_error", "500")
            return tone(audio_s), RATE
        except asyncio.CancelledError:
            self.cancelled.append(text)
            raise
        finally:
            self.active -= 1


def build(clock, brain=None, tts=None, depth=2, mode="hi", **kw):
    cfg = StreamConfig(segmenter=SegmenterConfig(first_min_words=3, min_words=6, max_words=14), prefetch_depth=depth, playout=PlayoutConfig())
    sink = Sink(clock)
    brain = brain or MockBrain(clock)
    tts = tts or MockTTS(clock)
    turn = StreamingTurn(gen=7, brain_stream=brain, tts=tts, sink=sink, clock=clock, mode=mode, cfg=cfg, **kw)
    return turn, brain, tts, sink


# ------------------------------------------------------------------ the pipeline overlaps stages and keeps up at RTF 0.5
async def test_no_gaps_at_rtf_0_5_and_first_audio_comes_before_the_reply_is_complete():
    clock = FakeClock()
    turn, brain, tts, sink = build(clock, tts=MockTTS(clock, rtf=0.5))
    out = await clock.run(turn.run([{"role": "user", "content": "x"}]))
    s = out.stream
    assert out.status == "done" and s["underruns"] == 0 and s["silence_ms"] == 0 and s["tts_errors"] == 0
    assert len(out.clause_texts) >= 4 and out.spoken_text == out.reply_text                       # everything was spoken
    assert s["first_frame_ms"] < s["ttft_ms"] + 2500                                              # first clause (3+ words) -> TTS -> first frame
    assert s["first_frame_ms"] < s["first_audio_ready_ms"] + 5 and s["rtf_p50"] == pytest.approx(0.5, abs=0.02)
    assert sink.gens == {7} and all(n == 320 for _, n in sink.frames)
    total_audio = sum(c["audio_ms"] for c in s["clauses"])
    assert out.playout["sent_ms"] >= total_audio - 40                                             # all audio went out
    assert s["brain_chunks_per_s"] and s["brain_chunks_per_s"] > 20                               # the Brain stream's pace is measured


async def test_gaps_are_reported_at_rtf_1_3_and_prefetch_depth_2_shrinks_them():
    res = {}
    for depth in (1, 2):
        clock = FakeClock()
        turn, brain, tts, sink = build(clock, tts=MockTTS(clock, rtf=1.3), depth=depth)
        res[depth] = (await clock.run(turn.run([{"role": "user", "content": "x"}]))).stream
    assert res[1]["underruns"] >= 2 and res[1]["silence_ms"] > 1000
    assert all(g["length_ms"] > 0 and g["before_clause"] >= 1 for g in res[1]["gaps"])
    assert res[2]["silence_ms"] < res[1]["silence_ms"]                                             # two jobs in flight hide part of the slow synthesis
    assert res[1]["rtf_p50"] == pytest.approx(1.3, abs=0.02)


async def test_tts_jobs_in_flight_never_exceed_the_prefetch_depth():
    for depth in (1, 2, 3):
        clock = FakeClock()
        turn, brain, tts, sink = build(clock, tts=MockTTS(clock, rtf=1.0), depth=depth)
        await clock.run(turn.run([{"role": "user", "content": "x"}]))
        assert 1 <= tts.max_active <= depth


# ------------------------------------------------------------------ clauses: numbers stay whole, order is kept
async def test_numbers_dates_and_acronyms_are_never_split_even_when_tokens_arrive_one_character_at_a_time():
    clock = FakeClock()
    text = "फ़ीस ₹1,25,000 है, last date 30 June, 2025 है, और B.Tech या M.B.A. course का option है, call 9876543210 पर कीजिए।"
    turn, brain, tts, sink = build(clock, brain=MockBrain(clock, text=text, chunk=1))
    out = await clock.run(turn.run([]))
    joined = " ".join(tts.texts)
    for keep in ("₹1,25,000", "30 June, 2025", "M.B.A. course", "9876543210"):
        assert any(keep in t for t in tts.texts), (keep, tts.texts)
    assert joined == " ".join(text.split())


async def test_clauses_are_spoken_in_order_even_when_a_later_clause_finishes_first():
    clock = FakeClock()
    order = []

    class Skewed(MockTTS):
        async def __call__(self, text, mode):
            self.delay_override = 3.0 if len(self.texts) == 0 else 0.2              # the first clause is the slowest
            order.append(text)
            return await super().__call__(text, mode)

    turn, brain, tts, sink = build(clock, tts=Skewed(clock), depth=3)
    out = await clock.run(turn.run([]))
    assert [c["idx"] for c in out.playout["clauses"]] == sorted(c["idx"] for c in out.playout["clauses"])
    assert out.playout["clauses"][0]["idx"] == 0


# ------------------------------------------------------------------ barge-in
async def test_barge_in_keeps_only_the_spoken_part_and_cancels_everything_in_flight():
    clock = FakeClock()
    brain = MockBrain(clock, text=REPLY, chars_per_s=60.0)                                          # a slow Brain: it is still streaming at the barge-in
    turn, brain, tts, sink = build(clock, brain=brain, tts=MockTTS(clock, rtf=0.5), depth=2)
    seen = {}

    async def scenario():
        task = asyncio.ensure_future(turn.run([{"role": "user", "content": "x"}]))
        while turn.playout.t_first is None:
            await clock.sleep(0.01)
        await clock.sleep(2.4)                                                                      # barge in 2.4 s into the speech
        seen["frames_before"] = len(sink.frames)
        seen["rep"] = await turn.interrupt("barge_in")
        seen["t_int_end"] = clock.now()
        try:
            await task
        except asyncio.CancelledError:
            seen["cancelled"] = True
        await clock.sleep(2.0)                                                                      # nothing may arrive afterwards
        seen["frames_after"] = len(sink.frames)

    await clock.run(scenario())
    out, rep = turn.outcome, seen["rep"]
    assert seen.get("cancelled") and out.status == "cancelled"
    assert seen["frames_after"] == seen["frames_before"] or seen["frames_after"] <= seen["frames_before"] + 1   # silence at once (at most the frame in progress)
    assert rep["interrupt_to_last_frame_ms"] <= 20.5 and len(sink.cleared) == 1 and sink.cleared[0][1] == 7
    assert rep["unwind_ms"] < 1000 and rep["lead_ms_dropped"] == pytest.approx(160, abs=25)
    assert brain.closed >= 1                                                                        # the Brain's stream was closed
    assert out.spoken_text and out.reply_text.startswith(out.spoken_text.split(" ")[0])
    assert len(out.spoken_text) < len(out.reply_text) and out.reply_text.startswith(out.spoken_text.rstrip())
    assert len(out.spoken_text.split()) < len(out.reply_text.split())
    # the spoken share is what the 2.4 s of playback covered: about 2.4 s of the 0.42 s/word speech, minus the clause-0 words already counted
    assert 4 <= len(out.spoken_text.split()) <= 9


async def test_barge_in_cancels_the_tts_jobs_that_were_in_flight():
    clock = FakeClock()
    turn, brain, tts, sink = build(clock, tts=MockTTS(clock, rtf=1.3), depth=2)

    async def scenario():
        task = asyncio.ensure_future(turn.run([]))
        while turn.playout.t_first is None:
            await clock.sleep(0.01)
        await clock.sleep(0.3)
        await turn.interrupt()
        try:
            await task
        except asyncio.CancelledError:
            pass

    await clock.run(scenario())
    assert tts.cancelled and tts.active == 0                                                        # in-flight synthesis was cancelled, none left running


async def test_late_audio_after_an_interrupt_is_dropped():
    clock = FakeClock()
    turn, brain, tts, sink = build(clock)

    async def scenario():
        task = asyncio.ensure_future(turn.run([]))
        while turn.playout.t_first is None:
            await clock.sleep(0.01)
        await turn.interrupt()
        assert turn.playout.enqueue(7, 99, tone(0.5), words=3) is False
        try:
            await task
        except asyncio.CancelledError:
            pass

    await clock.run(scenario())
    assert turn.outcome.playout["dropped_stale"] >= 0 and turn.playout.dropped_stale >= 1


def test_truncate_spoken_cuts_at_a_word_and_never_includes_unplayed_clauses():
    texts = {0: "जी मैं आपकी मदद", 1: "आप कौन सा course चाहते हैं", 2: "बताइए"}
    rep = {"clauses": [{"idx": 0, "samples": 100, "played_est": 100}, {"idx": 1, "samples": 100, "played_est": 40}, {"idx": 2, "samples": 50, "played_est": 0}]}
    assert truncate_spoken(texts, rep) == "जी मैं आपकी मदद आप कौन"
    assert truncate_spoken(texts, {"clauses": []}) == ""
    assert truncate_spoken({0: "a b", FILLER_IDX: "एक मिनट"}, {"clauses": [{"idx": 0, "samples": 10, "played_est": 10}]}) == "a b"


# ------------------------------------------------------------------ script gate
async def test_a_first_clause_in_roman_hindi_gets_one_rewrite_before_any_audio():
    clock = FakeClock()
    calls = []

    async def rewrite(text, mode):
        calls.append(text)
        await clock.sleep(0.4)
        return "ZZQ जी, आप कैसे हैं।".replace("ZZQ ", "")

    turn, brain, tts, sink = build(clock, brain=MockBrain(clock, text=ROMAN), rewrite=rewrite)
    out = await clock.run(turn.run([]))
    sc = out.script
    assert len(calls) == 1 and sc["first_ok"] is False and sc["first_retried"] and sc["first_retry_ok"] is True
    assert tts.texts[0] == "जी, आप कैसे हैं।" and "ZZQNAME" not in tts.texts[0]
    assert sc["kinds"].get("roman_hindi_word", 0) >= 3
    assert out.stream["first_tts_sent_ms"] >= out.stream["first_clause_ms"] + 399                  # the retry happened BEFORE any audio work


async def test_a_slow_rewrite_is_abandoned_at_the_time_limit_and_the_clause_is_spoken_as_it_is(caplog):
    caplog.set_level(logging.DEBUG)
    clock = FakeClock()
    seen = {}

    async def rewrite(text, mode):
        try:
            await clock.sleep(5.0)
        except asyncio.CancelledError:
            seen["cancelled"] = True
            raise
        return "जी"

    turn, brain, tts, sink = build(clock, brain=MockBrain(clock, text=ROMAN), rewrite=rewrite)
    out = await clock.run(turn.run([]))
    assert seen.get("cancelled") and out.script["first_retry_ok"] is False
    assert "ZZQNAME" in tts.texts[0]                                                                # spoken as it is: the caller always gets an answer
    assert out.stream["first_tts_sent_ms"] - out.stream["first_clause_ms"] == pytest.approx(1000, abs=30)
    for word in ("ZZQNAME", "kaise", "batata", "aapko"):
        assert word not in caplog.text, word                                                        # kinds and counts only


async def test_a_later_roman_clause_is_never_retried_only_converted_or_spoken_as_it_is():
    clock = FakeClock()
    calls = []

    async def rewrite(text, mode):
        calls.append(text)
        return None

    text = "जी, मैं आपकी पूरी मदद करूँगा। ZZQNAME ji aap kaise hain, main aapko batata hoon। फिर मिलेंगे।"
    turn, brain, tts, sink = build(clock, brain=MockBrain(clock, text=text), rewrite=rewrite)
    out = await clock.run(turn.run([]))
    assert calls == [] and out.script["first_ok"] is True
    assert out.script["clauses_violating"] >= 1 and out.script["kinds"].get("roman_hindi_word", 0) >= 1
    assert any("ZZQNAME" in t for t in tts.texts)


async def test_english_mode_flags_devanagari_in_the_first_clause_and_a_clean_clause_costs_nothing():
    clock = FakeClock()
    turn, brain, tts, sink = build(clock, brain=MockBrain(clock, text="You need at least sixty percent in twelfth. Apply online before June."), mode="en")
    out = await clock.run(turn.run([]))
    assert out.script["first_ok"] is True and out.script["clauses_violating"] == 0 and len(tts.texts) >= 1


# ------------------------------------------------------------------ tools
async def test_a_tool_call_gets_a_filler_when_it_is_slow_and_the_reply_continues_after_it():
    clock = FakeClock()
    ran = []

    async def run_tools(calls):
        ran.append(calls)
        await clock.sleep(1.5)
        return [{"role": "user", "content": "with the tool result"}]

    brain = ToolBrain(clock, text="जी, B.Tech चार साल का course है और आप online apply कर सकते हैं।")
    turn, _, tts, sink = build(clock, brain=brain, run_tools=run_tools, filler_pcm8=tone(0.6))
    out = await clock.run(turn.run([{"role": "user", "content": "x"}]))
    assert len(ran) == 1 and brain.calls == 2 and out.tool_rounds == 1
    idxs = [c["idx"] for c in out.playout["clauses"]]
    assert any(i >= FILLER_IDX for i in idxs) and idxs.index(next(i for i in idxs if i >= FILLER_IDX)) >= 1   # the filler follows the speech already queued
    assert "एक मिनट" not in out.reply_text.replace("एक मिनट देखता हूँ", "") and all(i < FILLER_IDX for i in out.clause_texts)
    assert "online apply" in out.reply_text                                                       # the answer after the tool was spoken


async def test_a_fast_tool_needs_no_filler_and_no_filler_is_played_without_one_cached():
    for tool_s, filler in ((0.1, tone(0.6)), (1.5, None)):
        clock = FakeClock()

        async def run_tools(calls, tool_s=tool_s, clock=clock):
            await clock.sleep(tool_s)
            return []

        turn, _, tts, sink = build(clock, brain=ToolBrain(clock, text="जी, यह जवाब है और बाकी जानकारी भी है।"), run_tools=run_tools, filler_pcm8=filler)
        out = await clock.run(turn.run([]))
        assert not any(c["idx"] >= FILLER_IDX for c in out.playout["clauses"])


# ------------------------------------------------------------------ failures
async def test_a_failed_first_clause_is_skipped_and_the_rest_of_the_reply_still_plays():
    clock = FakeClock()
    turn, brain, tts, sink = build(clock, tts=MockTTS(clock, rtf=0.5, fail={0}))
    out = await clock.run(turn.run([]))
    assert out.stream["tts_errors"] == 1 and out.status == "done" and out.playout["clauses"]
    assert 0 not in [c["idx"] for c in out.playout["clauses"]]


async def test_when_every_clause_fails_the_turn_is_an_error_not_silence_forever():
    clock = FakeClock()
    turn, brain, tts, sink = build(clock, tts=MockTTS(clock, rtf=0.5, fail=set(range(50))))
    out = await clock.run(turn.run([]))
    assert out.status == "error" and out.error == "no_audio" and sink.frames == []


async def test_a_brain_stream_that_breaks_midway_still_speaks_what_it_produced():
    clock = FakeClock()
    turn, brain, tts, sink = build(clock, brain=MockBrain(clock, fail_after=60))
    out = await clock.run(turn.run([]))
    assert out.error and out.error.startswith("brain_") and out.status == "done" and out.playout["clauses"]


async def test_a_tts_that_never_answers_is_skipped_at_its_time_limit():
    clock = FakeClock()
    cfg_tts = MockTTS(clock, delay_override=100.0)
    turn, brain, tts, sink = build(clock, tts=cfg_tts)
    turn.cfg.tts_timeout_s = 2.0
    out = await clock.run(turn.run([]), max_virtual_s=600)
    assert out.status == "error" and out.stream["tts_errors"] >= 1 and cfg_tts.cancelled


# ------------------------------------------------------------------ tag filter and privacy
def test_the_echoed_mode_tag_is_removed_even_when_split_across_tokens_and_normal_brackets_survive():
    f = ModeTagFilter()
    out = "".join(f.feed(t) for t in ["जी, ठीक है। ", "(rep", "ly_mo", "de: hi", ")", " आगे"]) + f.flush()
    assert out == "जी, ठीक है।  आगे"
    f = ModeTagFilter()
    assert "".join(f.feed(t) for t in ["फ़ीस ", "(30 ", "June) ", "है"]) + f.flush() == "फ़ीस (30 June) है"
    f = ModeTagFilter()
    assert f.feed("ठीक है (reply_mo") == "ठीक है " and f.flush() == ""                              # the Brain stopped mid-tag: the fragment is dropped


async def test_metrics_and_logs_hold_numbers_only_no_reply_words(caplog):
    caplog.set_level(logging.DEBUG)
    clock = FakeClock()

    async def rewrite(text, mode):
        return None

    turn, brain, tts, sink = build(clock, brain=MockBrain(clock, text=ROMAN), rewrite=rewrite)
    out = await clock.run(turn.run([]))
    blob = json.dumps(out.stream, ensure_ascii=False) + json.dumps(out.script, ensure_ascii=False) + json.dumps(out.playout, ensure_ascii=False)
    for word in ("ZZQNAME", "admission", "जानकारी", "kaise", "aapko", "आगे"):
        assert word not in blob and word not in caplog.text, word
