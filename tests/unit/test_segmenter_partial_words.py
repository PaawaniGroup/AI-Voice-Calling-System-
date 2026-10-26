"""Bug: the existing segmenter could cut between a number and its unit/month ("30 | June", "5 | lakh", "12 | percent", "10 | बजे") when the next word was still
arriving in pieces (the pair is only recognised once the next word is complete). Every chunking must give the same clauses as the whole text.
SIMULATED: text only, no network."""
import pytest

from app.ai.streaming.segmenter import ClauseSegmenter, SegmenterConfig

FILL = "aap ko ye course lena chahiye kyunki placement achha hai aur fees bhi"          # 13 words: the 14th word is the first word of the pair
CASES = {
    "date": (FILL + " 30 June 2025 tak apply karna hoga", "30 June 2025"),
    "lakh": (FILL + " 5 lakh rupaye lagenge saal ke liye", "5 lakh"),
    "percent": (FILL + " 12 percent discount milega abhi", "12 percent"),
    "devanagari_unit": (FILL + " 10 बजे aana hoga office mein", "10 बजे"),
    # controls that were never split (protected by the first character of the next word or by the complete token):
    "phone": (FILL + " 98765 43210 par call kijiye abhi", "98765 43210"),
    "plus_phone": (FILL + " +91 98765 43210 par call kijiye", "+91 98765 43210"),
    "rupee_amount": (FILL + " Rs. 1,25,000 lagenge saal ke liye", "Rs. 1,25,000"),
    "acronym": (FILL + " M.B.A. course ke liye apply karein", "M.B.A. course"),
}


def run(text, chunk=None, **cfg):
    seg, out = ClauseSegmenter(SegmenterConfig(**cfg)), []
    if chunk:
        for i in range(0, len(text), chunk):
            out += seg.feed(text[i:i + chunk])
    else:
        out += seg.feed(text)
    return [str(c) for c in out + seg.flush()]


@pytest.mark.parametrize("name", list(CASES))
@pytest.mark.parametrize("chunk", [1, 2, 3, 5])
def test_the_clauses_do_not_depend_on_how_the_text_arrives(name, chunk):
    text, protected = CASES[name]
    whole = run(text)                                                          # default (flag-off) configuration
    assert run(text, chunk) == whole
    assert " ".join(whole) == text and any(protected in c for c in whole), whole


@pytest.mark.parametrize("name", ["date", "lakh", "percent", "devanagari_unit"])
@pytest.mark.parametrize("chunk", [1, 2, 3, 5])
def test_a_number_and_its_unit_stay_in_one_clause_with_the_ramp_too(name, chunk):
    text, protected = CASES[name]
    assert any(protected in c for c in run(text, chunk, ramp=True))


def test_a_clause_is_still_released_the_moment_the_next_word_shows_up():
    seg = ClauseSegmenter(SegmenterConfig(first_min_words=2, min_words=3))
    assert seg.feed("जी, मैं आपकी मदद करूँगा।") == []
    assert seg.feed(" आप") == ["जी, मैं आपकी मदद करूँगा।"]                    # not delayed: the last word before it is not a number


def test_after_a_number_the_cut_waits_for_the_next_word_to_be_complete():
    seg = ClauseSegmenter(SegmenterConfig(first_min_words=2, min_words=2, max_words=3))
    assert seg.feed("fees hai 30 Ju") == []                                    # "30 | Ju": not decided yet
    assert seg.feed("ne 2025 tak") == []                                       # "30 June" is a date: no cut between them
    assert " ".join(str(c) for c in seg.flush()).count("30 June") == 1
