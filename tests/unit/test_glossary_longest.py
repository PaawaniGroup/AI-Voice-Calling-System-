"""Longest match wins in Glossary.correct, whatever the order of the file, and the single helper both turn paths use. SIMULATED / unit level; NOT RUN.
Generic course terms only; the operator's real said/heard pairs never enter a test."""
from __future__ import annotations

import inspect

import pytest

from app.ai.glossary_correction import Glossary, apply_glossary, correct_glossary, correct_glossary_counted


def load(tmp_path, lines):
    p = tmp_path / "pairs.tsv"
    p.write_text("\n".join(lines), encoding="utf-8")
    g, why = Glossary.load(str(p), "t1")
    assert why == "ok"
    return g


def test_a_longer_guarded_pair_beats_the_shorter_always_on_mapping(tmp_path):
    g = load(tmp_path, ["BCA\tbi si e"])
    assert g.correct("bi si e course ke liye") == ("BCA course ke liye", 1)                       # the file pair (7 characters) wins over the `bi si` hint (5)


def test_without_a_cue_the_guarded_pair_is_off_and_the_always_on_mapping_applies_as_before(tmp_path):
    g = load(tmp_path, ["BCA\tbi si e"])
    out, n = g.correct("bi si e")                                                                  # no cue, not a question: the guarded pair is off
    assert out == correct_glossary("bi si e") and n == 1


def test_bi_si_alone_still_maps_exactly_as_before(tmp_path):
    for g in (Glossary(), load(tmp_path, ["BCA\tbi si e"])):
        for text in ("mujhe bi si ke baare mein janna hai", "bi si", "bi si ka cutoff kya hai"):
            assert g.correct(text) == correct_glossary_counted(text), text


def test_a_longer_always_on_mapping_beats_a_shorter_guarded_pair(tmp_path):
    g = load(tmp_path, ["X\tee men"])                                                              # inside the always-on `je ee men` (9 characters against 6)
    assert g.correct("je ee men ka cutoff batao") == ("JEE Main ka cutoff batao", 1)


def test_at_equal_length_the_operators_pair_wins_over_the_always_on_mapping_when_it_applies(tmp_path):
    g = load(tmp_path, ["Z\tbeetaik"])
    assert g.correct("beetaik fees batao") == ("Z fees batao", 1)                                  # a cue: the operator's pair
    assert g.correct("beetaik") == ("B.Tech", 1)                                                   # no cue: the pair is off, the always-on mapping applies


@pytest.mark.parametrize("lines", [["A\tbi si e", "B\tbi si"], ["B\tbi si", "A\tbi si e"]])
def test_two_overlapping_file_pairs_the_longer_wins_whatever_the_file_order(tmp_path, lines):
    g = load(tmp_path, lines)
    assert g.correct("bi si e course batao") == ("A course batao", 1)
    assert g.correct("bi si course batao") == ("B course batao", 1)                                # the shorter pair still applies where the longer does not match


def test_the_result_does_not_depend_on_the_order_of_the_file_lines(tmp_path):
    lines = ["A\tem bi e", "B\tem bi", "C\tbi e", "D\tbi si e", "E\tbi si"]
    texts = ["em bi e course", "bi si e fees", "bi e admission", "em bi branch", "bi si cutoff"]
    base = load(tmp_path, lines)
    for perm in (lines[::-1], lines[2:] + lines[:2]):
        other = load(tmp_path, perm)
        for t in texts:
            assert other.correct(t) == base.correct(t), t


def test_replacements_are_applied_together_and_never_scanned_again(tmp_path):
    g = load(tmp_path, ["bitech\tbi si e"])                                                    # a replacement that is itself a sound-alike of another entry
    assert g.correct("bitech course aur bi si e course") == ("B.Tech course aur bitech course", 2)


def test_the_count_is_the_number_of_replacements():
    assert Glossary().correct("edm course aur edm fees") == ("B.Tech course aur B.Tech fees", 2)
    assert Glossary().correct("") == ("", 0) and Glossary().correct(None) == ("", 0)


def test_apply_glossary_is_the_single_helper_of_both_paths():
    from app.ai.pipeline import Conversation
    from app.ai.streaming import live
    assert "apply_glossary" in inspect.getsource(Conversation.stream_turn)                        # the legacy path
    assert "apply_glossary" in inspect.getsource(live.run_streaming_turn)                         # the streaming path
    conv = type("C", (), {"glossary": Glossary()})()
    assert apply_glossary(conv, "edm course") == ("B.Tech course", 1)
    assert apply_glossary(type("C", (), {})(), "edm course") == correct_glossary_counted("edm course")        # no Glossary on the call: the always-on mappings only
