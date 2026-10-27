"""WP2 clause-size ramp (tenant flag `stream_ramp`, default OFF). All SIMULATED: text only, virtual clock, mock Brain and voice. No network.

Covers: defaults unchanged with the flag off; Hindi (Devanagari), Roman Hinglish, English, mixed; amounts, dates, phone numbers, dotted acronyms;
streamed in tiny chunks (a word may arrive in pieces); empty and very short replies; the skipped first-clause rewrite (counted); the optional Brain rule."""
from __future__ import annotations

import pytest

from app.ai.streaming import ramp
from app.ai.streaming.config import stream_config_from
from app.ai.streaming.playout import PlayoutConfig
from app.ai.streaming.segmenter import ClauseSegmenter, SegmenterConfig
from app.ai.streaming.turn import StreamConfig, StreamingTurn
from tests.fakeclock import FakeClock
from tests.unit.test_streaming_turn import MockBrain, MockTTS, Sink

RAMP = stream_config_from({"stream_ramp": True}).segmenter
OFF = stream_config_from({}).segmenter


def run(text: str, cfg: SegmenterConfig, chunk: int | None = None) -> list[str]:
    seg, out = ClauseSegmenter(cfg), []
    if chunk:
        for i in range(0, len(text), chunk):
            out += seg.feed(text[i:i + chunk])
    else:
        out += seg.feed(text)
    return [str(c) for c in out + seg.flush()]


# ------------------------------------------------------------------ flag OFF: unchanged
def test_flag_off_keeps_todays_segmenter_and_stream_config():
    assert OFF.ramp is False and (OFF.first_min_words, OFF.min_words, OFF.max_words) == (4, 12, 14)
    assert stream_config_from({}).ramp is False and stream_config_from({"stream_ramp": False}).ramp is False
    text = "Ji bilkul, aap B.Tech ke baare mein jaanna chahte hain, kaunsa college chahiye aapko abhi tak?"
    assert run(text, OFF) == ["Ji bilkul, aap B.Tech ke baare mein jaanna chahte hain,", "kaunsa college chahiye aapko abhi tak?"]   # first clause 9 words


def test_flag_on_sizes_and_overrides():
    assert (RAMP.ramp, RAMP.ramp_first_min_words, RAMP.first_max_words, RAMP.second_min_words, RAMP.second_max_words, RAMP.min_words, RAMP.max_words) == (True, 2, 4, 6, 8, 12, 14)
    c = stream_config_from({"stream_ramp": True, "stream_first_max_words": 5, "stream_second_max_words": 10, "stream_second_min_words": 7}).segmenter
    assert (c.first_max_words, c.second_min_words, c.second_max_words) == (5, 7, 10)


# ------------------------------------------------------------------ first clause
def test_roman_hinglish_first_clause_ends_at_the_first_comma_after_two_words():
    out = run("Ji bilkul, aap B.Tech ke baare mein jaanna chahte hain, kaunsa college chahiye aapko abhi tak?", RAMP)
    assert out[0] == "Ji bilkul," and out[1] == "aap B.Tech ke baare mein jaanna chahte hain,"       # second clause 8 words, ends at its comma


def test_no_punctuation_hard_cut_at_four_words_then_eight_then_twelve_to_fourteen():
    out = run("Haan ji aap bataiye kaunsa course chahiye aapko kyunki hum aapko sahi guidance dena chahte hain aur saath mein fees aur placement ki jaankari bhi de sakte hain.", RAMP)
    assert [len(c.split()) for c in out] == [4, 8, 14, 2]
    assert out[0] == "Haan ji aap bataiye"


def test_a_natural_break_one_word_after_the_hard_limit_is_preferred():
    assert run("Aap ko ye course lena, chahiye kyunki placement achha hai", RAMP)[0] == "Aap ko ye course lena,"      # 5 words, not 4


def test_one_word_does_not_end_the_first_clause():
    out = run("Sure, I can help you with that admission question today.", RAMP)
    assert out[0] == "Sure, I can help"                                                                          # min 2 words: the comma after word 1 is not used


@pytest.mark.parametrize("text,first", [
    ("जी बिल्कुल, मैं आपकी मदद कर सकता हूँ।", "जी बिल्कुल,"),
    ("आप इस कोर्स के लिए आवेदन कर सकते हैं और फीस भी पूछ सकते हैं", "आप इस कोर्स के"),
    ("Sure जी, आप B.Tech के लिए apply कर सकते हैं।", "Sure जी,"),
])
def test_devanagari_and_mixed_first_clauses(text, first):
    assert run(text, RAMP)[0] == first


# ------------------------------------------------------------------ protected spans
@pytest.mark.parametrize("text,first", [
    ("Fees hai Rs. 1,25,000 per year hai aur hostel alag hai", "Fees hai Rs. 1,25,000"),               # the amount stays whole
    ("Call me at 00000 00000 now please for details", "Call me at 00000 00000"),                         # a phone-shaped number (digits only) stays whole
    ("Apply before 30 June 2025 please and submit the form soon", "Apply before 30 June 2025"),         # a date stays whole
    ("Please apply on 30 June 2025 now and then", "Please apply on 30 June 2025"),
    ("Hum M.B.A. course ke liye aapki help kar sakte hain", "Hum M.B.A. course ke"),                      # a dotted acronym is one word
])
def test_a_cut_is_never_inside_an_amount_date_phone_number_or_acronym(text, first):
    assert run(text, RAMP)[0] == first


TEXTS = [
    "Ji bilkul, aap B.Tech ke baare mein jaanna chahte hain, kaunsa college chahiye aapko abhi tak?",
    "Please apply on 30 June 2025 now and then",
    "Call me at 00000 00000 now please for details",
    "आप इस कोर्स के लिए आवेदन कर सकते हैं और फीस भी पूछ सकते हैं",
    "Fees hai Rs. 1,25,000 per year hai aur hostel alag hai, bilkul theek hai?",
    "Sure जी, आप B.Tech के लिए apply कर सकते हैं।",
]


@pytest.mark.parametrize("text", TEXTS)
@pytest.mark.parametrize("chunk", [1, 2, 3, 5, 7])
def test_streamed_in_pieces_gives_the_same_clauses_as_the_whole_text(text, chunk):
    whole = run(text, RAMP)
    assert run(text, RAMP, chunk=chunk) == whole
    assert " ".join(whole) == " ".join(text.split())                                                     # nothing lost, nothing cut inside a word


# ------------------------------------------------------------------ empty and very short replies
def test_empty_and_very_short_replies():
    assert run("", RAMP) == [] and run("   ", RAMP) == []
    assert run("Haan", RAMP) == ["Haan"] and run("Haan ji", RAMP) == ["Haan ji"] and run("Haan ji.", RAMP) == ["Haan ji."]
    seg = ClauseSegmenter(RAMP)
    assert seg.feed("") == [] and seg.flush() == []


# ------------------------------------------------------------------ the skipped first-clause rewrite (StreamingTurn, virtual time)
SHORT_ROMAN = "ZZQNAME ji kaise hain, aapko batata hoon admission ke baare mein jaankari। आगे सुनिए।"


def _turn(clock, ramp_on: bool, rewrite):
    seg = RAMP if ramp_on else SegmenterConfig(first_min_words=3, min_words=6, max_words=14)
    cfg = StreamConfig(segmenter=seg, prefetch_depth=2, playout=PlayoutConfig(), ramp=ramp_on)
    return StreamingTurn(gen=7, brain_stream=MockBrain(clock, text=SHORT_ROMAN), tts=MockTTS(clock), sink=Sink(clock), clock=clock, mode="hi", cfg=cfg, rewrite=rewrite)


async def test_ramp_skips_the_rewrite_of_a_first_clause_of_four_words_or_fewer_and_counts_it():
    clock, calls = FakeClock(), []

    async def rewrite(text, mode):
        calls.append(text)
        return "जी, आप कैसे हैं।"

    out = await clock.run(_turn(clock, True, rewrite).run([]))
    assert calls == [] and out.script["first_rewrite_skipped"] == 1 and out.script["first_ok"] is False
    assert out.script["first_rewrite_skipped_roman"] == 1                                         # the skipped clause had Roman letters (a count, never the words)
    assert out.stream["clauses"][0]["stop_reason"] == "punct" and out.stream["clauses"][0]["words"] <= 4


async def test_without_the_flag_the_same_clause_still_gets_its_rewrite_and_no_skip_is_counted():
    clock, calls = FakeClock(), []

    async def rewrite(text, mode):
        calls.append(text)
        return "जी, आप कैसे हैं।"

    out = await clock.run(_turn(clock, False, rewrite).run([]))
    assert len(calls) == 1 and "first_rewrite_skipped" not in out.script


# ------------------------------------------------------------------ the optional Brain block
class _Conv:
    def __init__(self, content):
        self.messages = [{"role": "system", "content": content}, {"role": "user", "content": "x"}]


def test_brain_rule_is_appended_once_and_the_base_prompt_is_untouched():
    base = "BASE PROMPT"
    c = _Conv(base)
    assert ramp.apply_to_conversation(c) is True
    assert c.messages[0]["content"].startswith(base) and c.messages[0]["content"].endswith(ramp.RAMP_BRAIN_RULE)
    assert ramp.apply_to_conversation(c) is False and c.messages[0]["content"].count("OPENING RULE") == 1
    assert "2 to 4 words" in ramp.RAMP_BRAIN_RULE and "Never state fees, cut-offs or dates unless they come from the provided knowledge" in ramp.RAMP_BRAIN_RULE
    for w in ramp.FORBIDDEN_OPENER_WORDS:                           # the rule names every word an opener must not start with
        assert w in ramp.RAMP_BRAIN_RULE
    assert "must not agree, deny, confirm or promise" in ramp.RAMP_BRAIN_RULE


def test_the_rule_for_turns_without_a_topic_never_promises_a_callback_or_an_action():
    r = ramp.RAMP_BRAIN_RULE
    assert "nothing was asked about a topic" in r and "never promise a callback or any other action" in r
    assert "आपने जो पूछा" not in "".join(ramp.PROPOSED_OPENERS) and "जी, कहिए" not in "".join(ramp.NON_TOPICAL_OPENERS)    # dropped / replaced by the operator
    assert "आप कहिए," in ramp.NON_TOPICAL_OPENERS


def test_the_proposed_openers_are_two_to_four_words_end_in_punctuation_and_never_agree_deny_or_promise():
    for o in ramp.PROPOSED_OPENERS + ramp.NON_TOPICAL_OPENERS:
        assert 2 <= len(o.split()) <= 4 and o[-1] in ",.।"
        assert o.split()[0].strip(",.।").lower() not in {w.lower() for w in ramp.FORBIDDEN_OPENER_WORDS}


def test_brain_rule_is_not_added_without_a_system_message():
    class NoSys:
        messages = [{"role": "user", "content": "x"}]
    assert ramp.apply_to_conversation(NoSys()) is False
    assert ramp.apply_to_conversation(object()) is False


def test_ramp_flag_is_off_by_default_in_the_tenant_config():
    from app.ai.service import LATENCY_FLAGS, AiService
    from config.settings import Settings
    assert "stream_ramp" in LATENCY_FLAGS
    svc = AiService.__new__(AiService)
    svc.s = Settings()
    assert AiService.default_config(svc, "t1")["stream_ramp"] is False


# ------------------------------------------------------------------ the SIMULATED gap table
def test_simulation_worker_models_and_the_least_silence_search():
    from tools.sim_ramp_gap import WORKER_MODELS, best_plan, simulate, split_ramp
    cold = list(WORKER_MODELS.values())[2]
    assert simulate(split_ramp(23), cold) == simulate(split_ramp(23), 1)    # a second worker that is ready only after 45 s does not help this reply
    b2 = best_plan(23, 2)
    assert b2["first_sound_s"] <= 3.0 and b2["gaps_total_s"] == 0.0         # two warm workers: a plan with no silence exists
    b1 = best_plan(23, 1)
    assert b1["first_sound_s"] <= 3.0 and b1["gaps_total_s"] < simulate(split_ramp(23), 1)["gaps_total_s"]   # one worker: less silence than the plain ramp
    assert b1["last_audio_end_s"] > simulate(split_ramp(23), 1)["last_audio_end_s"]                         # ... bought with a later end of the reply
    assert best_plan(23, 1, max_first_sound_s=1.0) is None                  # nothing meets an impossible first-sound limit


def test_simulation_first_sound_is_earlier_and_one_worker_shows_a_gap_two_workers_none():
    from tools.sim_ramp_gap import simulate, split_ramp, split_today
    t1, r1 = simulate(split_today(23), 1), simulate(split_ramp(23), 1)
    t2, r2 = simulate(split_today(23), 2), simulate(split_ramp(23), 2)
    assert r1["first_sound_s"] < t1["first_sound_s"] - 0.9 and r2["first_sound_s"] == r1["first_sound_s"]
    assert r1["gap_after_first_s"] > 1.0                      # SIMULATED: with ONE voice worker the short first clause is followed by a silence
    assert r2["gap_after_first_s"] == 0.0 and t2["gap_after_first_s"] == 0.0
