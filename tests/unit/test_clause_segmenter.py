"""Incremental clause segmenter: boundaries, minimum/maximum words, and the protected spans it must never cut inside."""
import pytest

from app.ai.streaming.segmenter import ClauseSegmenter, SegmenterConfig


def run(text, chunk=3, **cfg):
    """Feed `text` in small pieces (as tokens arrive) and flush; returns the clauses."""
    seg = ClauseSegmenter(SegmenterConfig(**cfg))
    out = []
    for i in range(0, len(text), chunk):
        out += seg.feed(text[i:i + chunk])
    return out + seg.flush()


def joined(clauses):
    return " ".join(clauses)


# ------------------------------------------------------------------ boundaries
def test_splits_after_each_boundary_character_followed_by_a_space():
    text = "जी, आपका स्वागत है; हम आपकी पूरी मदद करेंगे। आप कौन सा course चाहते हैं? बताइए आप क्या जानना चाहते हैं!"
    clauses = run(text, first_min_words=2, min_words=3, max_words=30)
    assert clauses == ["जी, आपका स्वागत है;", "हम आपकी पूरी मदद करेंगे।", "आप कौन सा course चाहते हैं?", "बताइए आप क्या जानना चाहते हैं!"]
    assert joined(clauses) == text


def test_the_same_clauses_come_out_however_the_text_is_chopped_into_tokens():
    text = "जी, B.Tech चार साल का course है, और admission entrance exam से होता है। फ़ीस सालाना ₹1,25,000 है।"
    base = run(text, chunk=1000, first_min_words=2, min_words=4)
    for chunk in (1, 2, 3, 5, 7, 11):
        assert run(text, chunk=chunk, first_min_words=2, min_words=4) == base, chunk


def test_a_clause_is_released_as_soon_as_the_next_word_arrives_not_at_the_end_of_the_stream():
    seg = ClauseSegmenter(SegmenterConfig(first_min_words=2, min_words=3))
    assert seg.feed("जी, मैं आपकी मदद करूँगा।") == []              # the boundary is not confirmed until a word follows
    assert seg.feed(" आप") == ["जी, मैं आपकी मदद करूँगा।"]           # released the moment the next word shows up
    assert seg.emitted == 1 and seg.pending_words == 1


def test_the_first_clause_may_be_short_later_clauses_need_the_minimum():
    text = "जी, ज़रूर। मैं आपको admission की पूरी जानकारी देता हूँ। ठीक है? अब आप बताइए।"
    clauses = run(text, first_min_words=2, min_words=6, max_words=30)
    assert clauses[0] == "जी, ज़रूर।"                                     # 2 words: allowed as the first clause
    assert all(len(c.split()) >= 6 for c in clauses[1:-1])                # the short "ठीक है?" was merged into its neighbour
    assert joined(clauses) == text


def test_a_first_clause_below_the_first_minimum_is_extended():
    clauses = run("जी। ठीक है, मैं बताता हूँ। फिर देखिए।", first_min_words=3, min_words=3, max_words=30)
    assert clauses[0].split()[:2] == ["जी।", "ठीक"]


def test_a_long_run_without_punctuation_is_cut_at_the_maximum_on_a_word_boundary():
    words = [f"शब्द{i}" for i in range(40)]
    clauses = run(" ".join(words), first_min_words=3, min_words=6, max_words=14)
    assert [len(c.split()) for c in clauses] == [14, 14, 12]
    assert joined(clauses) == " ".join(words)


def test_flush_speaks_the_unfinished_tail_and_nothing_is_lost_or_duplicated():
    text = "आप कौन सा course करना चाहते हैं और किस शहर में पढ़ना चाहते हैं"
    clauses = run(text)
    assert joined(clauses) == text and clauses


def test_whitespace_only_and_punctuation_only_input_produce_nothing():
    assert run("   \n  ") == [] and run("।। ,, ..") == []


# ------------------------------------------------------------------ protected spans: never split inside
@pytest.mark.parametrize("text,protected", [
    ("फ़ीस सालाना ₹1,25,000 है, hostel अलग से लगता है।", "₹1,25,000"),                # Indian-grouped amount: commas without a space
    ("आपको 5, 10, 15 और 20 में से चुनना है, फिर बताइए।", "5, 10, 15"),                  # a spaced number list
    ("last date 30 June, 2025 है, उससे पहले form भरिए।", "30 June, 2025"),             # a date with the year after the comma
    ("registration June 30, 2025 को बंद होगा, जल्दी कीजिए।", "June 30, 2025"),
    ("counselling सुबह 10:30 बजे शुरू होगी, आप तैयार रहिए।", "10:30 बजे"),               # a time
    ("आप 9876543210 या +91 98765 43210 पर call कर सकते हैं, हम मदद करेंगे।", "98765 43210"),  # phone numbers (shown here as dummies)
    ("आप www.example.com/apply पर जाइए, फिर form भरिए।", "www.example.com/apply"),      # a URL
    ("mail info@example.com पर भेजिए, हम जवाब देंगे।", "info@example.com"),
    ("फ़ीस Rs. 5,000 है, और hostel अलग है।", "Rs. 5,000"),                              # an abbreviation + amount
    ("आपके पास B.Tech या M.B.A. course का option है, चुनिए।", "M.B.A. course"),            # dotted acronyms
    ("fee 3.5 lakh है, scholarship मिलेगी।", "3.5 lakh"),                                 # a decimal
    ("फ़ीस 1 लाख 20 हज़ार रुपये है, hostel अलग है।", "1 लाख 20 हज़ार"),
])
def test_never_splits_inside_a_protected_span(text, protected):
    for min_words in (1, 2, 3, 4):
        for chunk in (1, 3, 8):
            clauses = run(text, chunk=chunk, first_min_words=1, min_words=min_words, max_words=40)
            assert joined(clauses) == text
            assert any(protected in c for c in clauses), (protected, clauses)


def test_a_forced_cut_at_the_maximum_also_avoids_protected_spans():
    text = " ".join(["शब्द"] * 12) + " ₹ 1,25,000 है और " + " ".join(["बात"] * 10)
    clauses = run(text, first_min_words=3, min_words=6, max_words=13)
    assert joined(clauses) == text
    assert any("₹ 1,25,000" in c for c in clauses)


def test_a_period_before_a_lowercase_word_or_after_an_abbreviation_is_not_a_sentence_end():
    assert any("Dr. Sharma" in c for c in run("आप Dr. Sharma से बात कर सकते हैं, वो बहुत अच्छे counsellor हैं।", first_min_words=1, min_words=2, max_words=40))
    assert any("vs. the" in c for c in run("यह B.Tech vs. the MBA के बारे में है, ध्यान से सुनिए।", first_min_words=1, min_words=2, max_words=40))


def test_english_sentences_split_normally():
    text = "You need at least 60% in 12th with Physics, Chemistry and Maths. Admission is by entrance exam. Apply online before 30 June."
    clauses = run(text, first_min_words=3, min_words=4, max_words=30)
    assert clauses[0] == "You need at least 60% in 12th with Physics," or clauses[0].startswith("You need at least 60% in 12th")
    assert joined(clauses) == text
    assert any("60%" in c for c in clauses) and any("30 June." in c for c in clauses)


def test_segmenter_never_writes_to_a_log(caplog):
    caplog.set_level(0)
    run("जी, आपका नाम क्या है? मैं आपकी मदद करूँगा।")
    assert caplog.records == []
