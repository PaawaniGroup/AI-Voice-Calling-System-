"""from_trace.py and gate.py accept the evidence line's turn labels (outcome: interrupted | cancelled_before_audio | no_answer; cancel_reason; endpoint_reason). SIMULATED lines, no real call.
NOT RUN yet (needs the operator's APPROVED: run)."""
import json

from tools.realtime import from_trace as F
from tools.realtime import gate as G


def line(outcome, first_frame=None, **turn):
    marks = {"t_speech_end": 0.0, "t_endpoint": 700.0, "t_stt_sent": 701.0}
    if first_frame is not None:
        marks["t_first_frame_to_plivo"] = first_frame
    return {"trace_id": "x", "marks_ms": marks, "jobs": [{"service": "stt", "status": "ok", "delay_ms": 100.0, "exec_ms": 500.0}], "brain_calls": [], "stream": {},
            "turn": {"outcome": outcome, **turn}}


def test_an_interrupted_line_with_audio_is_a_turn_and_the_no_audio_labels_are_skipped_without_error():
    assert F.utterance_from_line(line("interrupted", first_frame=5000.0, cancel_reason="barge_in", endpoint_reason="silence"))["to_first_audio_ms"] == 5000.0
    assert F.utterance_from_line(line("cancelled_before_audio", cancel_reason="merged", endpoint_reason="cap")) is None
    assert F.utterance_from_line(line("no_answer", endpoint_reason="silence")) is None


def test_the_gate_reads_a_file_built_from_lines_with_the_new_labels(tmp_path):
    lines = [line("interrupted", first_frame=5000.0), line("cancelled_before_audio", cancel_reason="merged"), line("no_answer")]
    src = tmp_path / "t.jsonl"
    src.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    out = tmp_path / "ev.json"
    assert F.main(["--voice-workers", "2", "--clause-plan", "ramp", "--skip-first", "0", "--out", str(out), str(src)]) in (0, None)
    ev = json.loads(out.read_text(encoding="utf-8"))
    assert ev["n_utterances"] == 1
    gates = json.loads(G.DEFAULT_GATES.read_text(encoding="utf-8"))
    verdicts = {r["id"]: r["status"] for r in G.evaluate(ev, gates)}
    assert "FAIL" not in verdicts.values() and verdicts["A-median"] == "NOT_EVALUATED"
