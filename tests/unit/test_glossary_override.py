"""An operator file pair wins over a built-in entry at equal length on the same span; the safeguards still apply to file pairs; the override count for the evidence line.
SIMULATED / unit level; NOT RUN. Generic course terms only."""
from __future__ import annotations

import json

import pytest

from app.ai import turn_trace
from app.ai.glossary_correction import BUILTIN_ENTRIES, Glossary, apply_glossary


def load(tmp_path, lines, tenant="t1"):
    p = tmp_path / "pairs.tsv"
    p.write_text("\n".join(lines), encoding="utf-8")
    return Glossary.load(str(p), tenant)


@pytest.fixture(autouse=True)
def _isolate_trace():
    tok = turn_trace._current.set(None)
    yield
    turn_trace._current.reset(tok)


def test_an_operator_pair_overriding_a_built_in_wins_at_equal_length_and_is_counted(tmp_path):
    g, why = load(tmp_path, ["Z\tedm"])                                                  # the built-in rewrites edm to B.Tech: same span, same length
    assert why == "ok"
    assert g.correct("edm admission batao") == ("Z admission batao", 1) and g.last_overrides == 1
    assert Glossary().correct("edm admission batao") == ("B.Tech admission batao", 1)    # without the file: the built-in, no override
    b = Glossary()
    b.correct("edm admission batao")
    assert b.last_overrides == 0


def test_the_override_works_whatever_the_position_in_the_file_and_with_the_always_guard(tmp_path):
    for lines in (["Z\tedm", "Y\tbitech"], ["Y\tbitech", "Z\tedm"], ["Z\tedm\talways"]):
        g, _ = load(tmp_path, lines)
        assert g.correct("edm admission")[0] == "Z admission"
    g, _ = load(tmp_path, ["Z\tedm\talways"])
    assert g.correct("edm party") == ("Z party", 1)                                      # `always`: no cue needed, and it still overrides the (guarded) built-in


def test_a_pair_that_says_the_same_as_the_built_in_is_not_counted_as_an_override(tmp_path):
    g, _ = load(tmp_path, ["B.Tech\tedm"])
    assert g.correct("edm fees") == ("B.Tech fees", 1) and g.last_overrides == 0


def test_a_longer_built_in_still_beats_a_shorter_operator_pair(tmp_path):
    g, _ = load(tmp_path, ["Z\te d"])                                                    # shorter than the built-in `e d m`
    assert g.correct("e d m admission")[0] == "B.Tech admission" and g.last_overrides == 0


@pytest.mark.parametrize("line", ["JEE\tmain", "X\tto", "X\tkisi", "X\te", "X\t ", "X\t" + "y" * 41, "edm\tedm"])
def test_the_safeguards_still_refuse_file_pairs_including_pairs_that_would_override_a_built_in(tmp_path, line):
    g, why = load(tmp_path, [line, "A\tbitech"])
    assert why == "bad_lines:1" and len(g.entries) == len(BUILTIN_ENTRIES) + 1            # only the good pair was accepted
    for protected in ("main hoon", "kisi bhi student ke liye admission"):
        assert g.correct(protected)[0] == protected                                       # an everyday word is never rewritten


def test_with_no_file_nothing_changes():
    texts = ["edm admission batao", "e d m course", "bitech cutoff", "i love edm music", "mujhe beetaik karna hai", "namaste aap kaise hain", "bi si ke baare mein", ""]
    plain = Glossary()
    for path in (None, "", "/nonexistent/pairs.tsv"):
        g, why = Glossary.load(path, "t1")
        assert why in ("no_path", "no_file") and g.entries == BUILTIN_ENTRIES
        for t in texts:
            assert g.correct(t) == plain.correct(t) and g.last_overrides == 0


def test_the_override_count_reaches_the_evidence_line_as_a_number_for_every_call_that_has_a_glossary(tmp_path):
    g, _ = load(tmp_path, ["Z\tedm"])
    conv = type("C", (), {"glossary": g})()
    tr = turn_trace.TurnTrace(t_speech_end=1.0)
    turn_trace.start(tr)
    assert apply_glossary(conv, "edm admission")[0] == "Z admission"
    ev = json.loads(json.dumps(tr.evidence()))
    assert ev["turn"]["glossary_overrides"] == 1 and isinstance(ev["turn"]["glossary_overrides"], int)
    apply_glossary(conv, "namaste")
    assert tr.evidence()["turn"]["glossary_overrides"] == 0                               # present (0) even when nothing was overridden
    no_gloss = type("C", (), {})()
    tr2 = turn_trace.TurnTrace(t_speech_end=1.0)
    turn_trace.start(tr2)
    apply_glossary(no_gloss, "edm admission")
    assert "glossary_overrides" not in tr2.evidence()["turn"]                              # no Glossary on the call: no new key
