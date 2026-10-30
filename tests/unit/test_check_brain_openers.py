"""tools/check_brain_openers.py: the pure analysis and the refusals. SIMULATED: no Brain call, no network. (The tool itself is never run by a test.)"""
import argparse
import pathlib

from tools import check_brain_openers as t

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_twenty_questions_cover_the_required_topics_and_languages():
    assert len(t.QUESTIONS) == 20 and len({q[0] for q in t.QUESTIONS}) == 20
    topics = {q[2] for q in t.QUESTIONS}
    assert {"fees", "eligibility", "scholarship", "hostel", "placements", "abroad", "callback", "greeting", "garbled", "repeat"} <= topics
    assert {q[1] for q in t.QUESTIONS} == {"hi", "hg", "en"}
    assert any("New Zealand" in q[3] for q in t.QUESTIONS)


def test_a_reply_that_obeys_the_rule():
    r = t.analyze("देखिए, इस बारे में, यह कोर्स चार साल का होता है।", "hi")
    assert r["obeyed"] == 1 and r["forbidden"] == 0 and r["opener_lang_ok"] == 1 and r["fee"] == 0 and r["cutoff"] == 0 and r["date"] == 0


def test_forbidden_opening_word_and_a_missing_opener_are_counted():
    assert t.analyze("बिल्कुल, मैं बताता हूँ।", "hi")["forbidden"] == 1
    assert t.analyze("Sure, I can help you with that today.", "en")["forbidden"] == 1
    r = t.analyze("यह कोर्स चार साल का होता है और इसमें कई विषय पढ़ाए जाते हैं", "hi")
    assert r["obeyed"] == 0                                           # no boundary in the first words: cut at the maximum, not an opener


def test_language_match_of_the_opener():
    assert t.analyze("On that point, the course is four years.", "en")["opener_lang_ok"] == 1
    assert t.analyze("On that point, the course is four years.", "hi")["opener_lang_ok"] == 0
    assert t.analyze("आपके प्रश्न पर, the course is four years.", "en")["opener_lang_ok"] == 0


def test_statements_of_a_fee_a_cutoff_or_a_date_are_counted_without_keeping_the_text():
    assert t.analyze("आपके प्रश्न पर, फीस ₹1,25,000 है।", "hi")["fee"] == 1
    assert t.analyze("On that point, the fee is 5 lakh per year.", "en")["fee"] == 1
    assert t.analyze("On that point, the cut-off is 60 percent.", "en")["cutoff"] == 1
    assert t.analyze("On that point, apply before 30 June.", "en")["date"] == 1
    assert t.analyze("आप 15 नवंबर तक आवेदन कर सकते हैं।", "hi")["date"] == 1                  # a Devanagari month name counts too
    assert all(isinstance(v, int) for v in t.analyze("On that point, hello.", "en").values())


def _args(**kw):
    base = dict(approved="id-1", questions=20, reply_file="/tmp/replies.jsonl")
    base.update(kw)
    return argparse.Namespace(**base)


def test_the_tool_refuses_without_an_approval_id_inside_the_repo_or_with_too_many_questions():
    assert "no --approved" in t.refuse_reason(_args(approved=""), ROOT)
    assert "OUTSIDE the repository" in t.refuse_reason(_args(reply_file=str(ROOT / "replies.jsonl")), ROOT)
    assert "1 to 20" in t.refuse_reason(_args(questions=21), ROOT)
    assert t.refuse_reason(_args(), ROOT) is None
