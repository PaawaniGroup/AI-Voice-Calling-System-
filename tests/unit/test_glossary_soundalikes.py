"""Glossary sound-alikes with a context guard, the operator's file of pairs, and the Brain hint block (flag `glossary_hint`, default OFF). SIMULATED / unit level; NOT RUN.
The sentences here are generic course questions; the operator's real said/heard pairs live in a file OUTSIDE the repository and are never put in a test."""
from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ai.glossary_correction import (BUILTIN_ENTRIES, CONFIRM_HINT_RULE, Glossary, apply_confirm_hint, context_ok, correct_glossary, correct_glossary_counted,
                                        has_education_cue, is_short_course_question)
from config.settings import Settings

G = Glossary()


@pytest.mark.parametrize("text,expected", [
    ("mujhe edm ke liye fees batao", "mujhe B.Tech ke liye fees batao"),
    ("edm admission kab hota hai", "B.Tech admission kab hota hai"),
    ("e d m course kaisa hai", "B.Tech course kaisa hai"),
    ("bitech cutoff kitna hai", "B.Tech cutoff kitna hai"),
    ("Bee Tech branch kaun si achhi hai", "B.Tech branch kaun si achhi hai"),
    ("ई डी एम कॉलेज कौन सा है", "B.Tech कॉलेज कौन सा है"),
    ("ईडीएम के लिए योग्यता क्या है", "B.Tech के लिए योग्यता क्या है"),
    ("EDM eligibility kya hai", "B.Tech eligibility kya hai"),
    ("edm kya hai", "B.Tech kya hai"),                                         # a very short course-name question, no other cue
    ("edm?", "B.Tech?"),
])
def test_a_sound_alike_is_rewritten_when_the_utterance_has_an_education_cue_or_is_a_very_short_course_question(text, expected):
    out, n = G.correct(text)
    assert out == expected and n == 1


@pytest.mark.parametrize("text", [
    "i love edm music festivals",                  # no cue
    "edm party tonight at the club",
    "feel the edm beat",                           # "fee" inside "feel" is not the cue "fee"
    "medm bitechnology btechnical",                # the sound-alike inside a longer word
    "edm festival kab hai kya",                    # five words: not a very short question
    "my e d m playlist is long",
    "",
    "namaste aap kaise hain",
])
def test_sentences_that_must_not_change_are_returned_unchanged(text):
    out, n = G.correct(text)
    assert out == text and n == 0


def test_the_old_always_on_mappings_still_work_without_any_cue():
    assert G.correct("mujhe beetaik karna hai") == ("mujhe B.Tech karna hai", 1)                       # existing unguarded mapping, unchanged behaviour
    assert correct_glossary("mujhe beetaik karna hai") == "mujhe B.Tech karna hai"
    assert G.correct("beetaik, parsentaail?") == correct_glossary_counted("beetaik, parsentaail?")


def test_the_everyday_words_are_never_touched():
    for text in ("kisi bhi student ko admission mil sakta hai", "application fee free hai ya paid", "new session kab shuru hoga"):
        assert G.correct(text) == (text, 0)


def test_known_residual_false_positive_of_the_short_question_guard():
    """DOCUMENTED RISK, not a wish: a four-word question about the music genre that contains a question word is rewritten. The cue list and the length limit keep it rare."""
    assert G.correct("edm party kya hai")[0] == "B.Tech party kya hai"


def test_the_guards():
    assert has_education_cue("ke liye") and has_education_cue("Fees?") and not has_education_cue("feel good") and has_education_cue("कॉलेज")
    assert is_short_course_question("edm kya hai") and not is_short_course_question("edm") and not is_short_course_question("a b c d e kya")
    assert context_ok("admission") and not context_ok("music")


def test_every_builtin_entry_is_guarded():
    assert BUILTIN_ENTRIES and all(e.guard == "cue" and e.said == "B.Tech" for e in BUILTIN_ENTRIES)


# ---------------------------------------------------------------- the operator's file (tab separated, outside the repository)
def write(tmp_path, lines):
    p = tmp_path / "pairs.tsv"
    p.write_text("\n".join(lines), encoding="utf-8")
    return str(p)


def test_the_file_adds_pairs_for_this_tenant_only_and_reports_a_code(tmp_path):
    path = write(tmp_path, ["# comment", "", "BCA\tbi si e", "MBA\tem bi e\talways", "NEET\tnit\tcue\ttenant-b", "JEE\tjee mane\tcue\tt1"])
    g, why = Glossary.load(path, "t1")
    assert why == "ok" and len(g.entries) == len(BUILTIN_ENTRIES) + 3                 # the tenant-b line is skipped for t1
    assert g.correct("bi si e course ke liye")[0] == "BCA course ke liye"              # guarded: has a cue
    assert g.correct("bi si e")[0] == correct_glossary("bi si e")                      # guarded: no cue, not a question: the always-on `bi si` mapping applies as before
    assert g.correct("em bi e")[0] == "MBA"                                           # guard `always`
    assert g.correct("nit admission")[0] == "nit admission"                           # another tenant's pair
    g2, _ = Glossary.load(path, "tenant-b")
    assert g2.correct("nit admission")[0] == "NEET admission"


def test_bad_protected_and_malformed_lines_are_refused_and_counted_never_applied(tmp_path):
    path = write(tmp_path, ["B.Tech\tto", "B.Tech\ta", "only one column", "X\t" + "y" * 41, "Z\tzz\tmaybe", "BCA\tbi si e"])
    g, why = Glossary.load(path, "t1")
    assert why == "bad_lines:5" and len(g.entries) == len(BUILTIN_ENTRIES) + 1
    assert g.correct("to admission")[0] == "to admission"                              # an everyday word of the protected list can never become a pair


@pytest.mark.parametrize("path,code", [(None, "no_path"), ("", "no_path"), ("/nonexistent/pairs.tsv", "no_file")])
def test_a_missing_file_gives_the_built_in_entries_and_never_raises(path, code):
    g, why = Glossary.load(path, "t1")
    assert why == code and g.entries == BUILTIN_ENTRIES


def test_an_unreadable_file_never_raises_and_the_reason_holds_no_term(tmp_path):
    p = tmp_path / "bin.tsv"
    p.write_bytes(b"\xff\xfe\x00bad")
    g, why = Glossary.load(str(p), "t1")
    assert why == "unreadable" and "bad" not in why and g.entries == BUILTIN_ENTRIES


def test_the_number_of_pairs_is_bounded(tmp_path):
    path = write(tmp_path, [f"T{i}\theard{i}x" for i in range(600)])
    g, why = Glossary.load(path, "t1")
    assert len(g.entries) == len(BUILTIN_ENTRIES) + 500 and why == "bad_lines:100"


# ---------------------------------------------------------------- the Brain hint block (flag glossary_hint, default OFF)
def test_the_hint_is_short_and_added_once_after_the_other_blocks_and_does_not_contradict_them():
    from app.ai.pipeline import STRICT_GROUNDING_RULES
    from app.ai.streaming.ramp import FORBIDDEN_OPENER_WORDS, RAMP_BRAIN_RULE
    assert len(CONFIRM_HINT_RULE) <= 600
    conv = MagicMock()
    conv.messages = [{"role": "system", "content": "BASE" + STRICT_GROUNDING_RULES + RAMP_BRAIN_RULE}]
    assert apply_confirm_hint(conv) is True and apply_confirm_hint(conv) is False
    c = conv.messages[0]["content"]
    assert c.index(STRICT_GROUNDING_RULES) < c.index(RAMP_BRAIN_RULE) < c.index(CONFIRM_HINT_RULE) and c.count(CONFIRM_HINT_RULE) == 1
    tokens = set(re.findall(r"\w+", CONFIRM_HINT_RULE.lower()))
    assert not (tokens & {"yes", "no", "sure"}) and "of course" not in CONFIRM_HINT_RULE.lower()          # no agreeing or denying word the opening rule forbids
    assert "ONE short confirming question" in CONFIRM_HINT_RULE and "Never state fees, cut-offs or dates unless they come from the provided knowledge" in CONFIRM_HINT_RULE
    assert "opening phrase" in CONFIRM_HINT_RULE and "promise" not in CONFIRM_HINT_RULE.lower()          # it respects the OPENING RULE and promises nothing


def test_the_hint_needs_a_system_message():
    conv = MagicMock()
    conv.messages = [{"role": "user", "content": "x"}]
    assert apply_confirm_hint(conv) is False and apply_confirm_hint(MagicMock(messages=[])) is False


# ---------------------------------------------------------------- flag, setting, allowlist
def svc(doc):
    from app.ai.service import AiService
    coll = MagicMock()
    coll.find_one = AsyncMock(return_value=doc)
    db = MagicMock()
    db.__getitem__.return_value = coll
    return AiService(db, None, Settings(_env_file=None), None)


async def test_glossary_hint_defaults_off_and_survives_get_config():
    from app.ai.service import LATENCY_FLAGS
    assert "glossary_hint" in LATENCY_FLAGS
    assert (await svc(None).get_config("t"))["glossary_hint"] is False
    assert (await svc({"tenant_id": "t", "glossary_hint": True}).get_config("t"))["glossary_hint"] is True, "dropped by the allowlist"


def test_the_api_model_and_the_setting():
    from app.api.routes_platform import AiConfigIn
    assert AiConfigIn(glossary_hint=True).glossary_hint is True
    assert Settings(_env_file=None).ai_glossary_path == ""
