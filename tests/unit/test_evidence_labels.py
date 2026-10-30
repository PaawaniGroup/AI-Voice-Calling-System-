"""The evidence line's labels and counts (turn_trace.note_turn / add_turn_count), the glossary correction counter, and the acknowledgement overlap/cut-off numbers.
SIMULATED: virtual time and mock models; nothing here measures real latency. Numbers and enumerated labels only: never a caller or reply word."""
import asyncio
import json

import pytest

from app.ai import turn_trace
from app.ai.glossary_correction import correct_glossary, correct_glossary_counted
from app.ai.streaming.turn import ACK_IDX
from tests.fakeclock import FakeClock
from tests.unit.test_ack_turn import Q, build


@pytest.fixture(autouse=True)
def _no_leaked_trace():
    tok = turn_trace._current.set(None)
    yield
    turn_trace._current.reset(tok)


def test_enumerated_labels_survive_the_numbers_only_filter_and_free_strings_do_not():
    tr = turn_trace.TurnTrace(t_speech_end=1.0)
    turn_trace.start(tr)
    turn_trace.note_turn("endpoint_reason", "cap")
    turn_trace.note_turn("cancel_reason", "barge_in")
    turn_trace.note_turn("outcome", "interrupted")
    turn_trace.note_turn("utterance_ms", 7340)
    turn_trace.note_turn("free_text", "caller words must never appear")           # not an enumerated key: dropped
    turn_trace.add_turn_count("glossary_corrections", 2)
    turn_trace.add_turn_count("glossary_corrections", 1)
    ev = tr.evidence()
    assert ev["turn"] == {"endpoint_reason": "cap", "cancel_reason": "barge_in", "outcome": "interrupted", "utterance_ms": 7340, "free_text": None, "glossary_corrections": 3}
    assert "caller words" not in json.dumps(ev)


def test_labels_are_task_local_and_do_nothing_without_a_trace():
    turn_trace.note_turn("outcome", "done")                                         # no trace started: no error
    turn_trace.add_turn_count("glossary_corrections", 1)


@pytest.mark.parametrize("text,n", [("mujhe beetaik karna hai", 1), ("beetaik, parsentaail?", 2), ("je ee men aur nyoo jeelaind", 2), ("bi si ke baare mein", 1),
                                    ("kuch nahi badlega", 0), ("", 0)])
def test_the_counter_counts_substitutions_and_the_text_is_exactly_what_correct_glossary_returns(text, n):
    out, count = correct_glossary_counted(text)
    assert count == n and out == correct_glossary(text)


def test_the_counter_handles_none_like_the_original():
    assert correct_glossary_counted(None) == ("", 0)


async def test_a_completed_turn_reports_no_overlap_no_cut_and_the_pause_between_the_phrase_and_the_answer():
    clock = FakeClock()
    t, _ = build(clock)
    out = await clock.run(t.run(Q))
    s = out.stream
    assert s["ack_cut_ms"] == 0.0 and s["ack_overlap_ms"] == 0.0                    # the whole phrase went out; the answer started after it, never over it
    assert s["ack_to_answer_ms"] == pytest.approx(s["first_frame_ms"] - s["ack_at_ms"] - s["ack_ms"], abs=1.0) and s["ack_to_answer_ms"] > 0


async def test_without_an_acknowledgement_the_new_numbers_are_none():
    clock = FakeClock()
    t, _ = build(clock, ack=False)
    s = (await clock.run(t.run(Q))).stream
    assert s["ack_cut_ms"] is None and s["ack_overlap_ms"] is None and s["ack_to_answer_ms"] is None


async def test_a_barge_in_during_the_phrase_reports_how_much_of_it_was_never_sent():
    clock = FakeClock()
    t, sink = build(clock)
    seen = {}

    async def scenario():
        task = asyncio.ensure_future(t.run(Q))
        while t.playout.t_first is None:
            await clock.sleep(0.01)
        await clock.sleep(0.5)
        await t.interrupt("barge_in")
        try:
            await task
        except asyncio.CancelledError:
            pass
        seen["m"] = t._metrics(t.outcome.playout)
    await clock.run(scenario())
    m = seen["m"]
    assert 0 < m["ack_cut_ms"] < 1000 and m["ack_overlap_ms"] is None and m["ack_to_answer_ms"] is None      # cut part of a 1 s phrase; the answer never started
    assert ACK_IDX in t.playout.accounts


# ---- the outcome label of a cancelled turn: "interrupted" only when ANSWER audio was sent; the evidence writer keeps the label
def test_cancel_outcome_interrupted_only_when_answer_audio_was_sent():
    from types import SimpleNamespace as NS
    from app.ai.streaming.live import cancel_outcome
    assert cancel_outcome(NS(playout=NS(t_first=5.0, t_first_real=5.0))) == "interrupted"                     # audio sent, then cancelled
    assert cancel_outcome(NS(playout=NS(t_first=None, t_first_real=None))) == "cancelled_before_audio"       # nothing sent
    assert cancel_outcome(NS(playout=NS(t_first=4.0, t_first_real=None))) == "cancelled_before_audio"        # only the cached acknowledgement was sent: not the answer


def test_the_evidence_writer_keeps_the_new_label_and_the_old_one_is_not_produced(tmp_path):
    tr = turn_trace.TurnTrace(t_speech_end=1.0)
    tr.turn.update(outcome="cancelled_before_audio", cancel_reason="merged", endpoint_reason="silence", utterance_ms=1200, pieces_merged=1)
    f = tmp_path / "ev.jsonl"
    turn_trace.append_evidence(str(f), tr)
    line = json.loads(f.read_text(encoding="utf-8"))
    assert line["turn"]["outcome"] == "cancelled_before_audio" and line["turn"]["cancel_reason"] == "merged"
    assert "not_recorded" not in json.dumps(line) and len("cancelled_before_audio") <= 40               # inside the 40-character limit of the enumerated strings