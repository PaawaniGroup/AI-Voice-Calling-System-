"""tools/realtime/from_trace.py: the arithmetic from a numbers-only trace line to the stage record. The lines below are SYNTHETIC test data (not a real call)."""
from __future__ import annotations

import json

from tools.realtime import from_trace as F
from tools.realtime import gate as G


def line(first_frame=6000.0, **over):
    base = {"trace_id": "t1",
            "marks_ms": {"t_speech_end": 0.0, "t_endpoint": 700.0, "t_stt_sent": 705.0, "t_stt_final": 2150.0, "t_llm_sent": 2160.0, "t_llm_first_token": 2900.0,
                         "t_llm_first_clause": 3140.0, "t_tts_sent": 3150.0, "t_tts_first_chunk": 5900.0, "t_first_frame_to_plivo": first_frame, "voice_to_voice": first_frame},
            "jobs": [{"service": "stt", "status": "ok", "delay_ms": 139.0, "exec_ms": 612.0, "remainder_ms": 695.0, "polls": 4, "request_bytes": 60000},
                     {"service": "tts", "status": "ok", "clause_idx": 0, "stop_reason": "punct", "words": 3, "delay_ms": 1200.0, "exec_ms": 1700.0, "total_ms": 2750.0, "polls": 9,
                      "poll_ms": 900.0, "remainder_ms": 100.0},
                     {"service": "tts", "status": "ok", "clause_idx": 1, "stop_reason": "max_words", "words": 8, "total_ms": 3400.0},
                     {"service": "tts", "status": "error:http_error", "clause_idx": 2, "words": 12},
                     {"service": "tts", "status": "ok", "clause_idx": 1, "words": 8, "total_ms": 3500.0}],     # a retried clause counts once
            "brain_calls": [{"call": 0, "messages": 3}], "stream": {"silence_ms": 120.0, "underruns": 1, "gaps": [120.0]}}
    base.update(over)
    return base


def test_stage_numbers_come_from_the_marks_and_the_job_records():
    u = F.utterance_from_line(line())
    assert u["end_of_turn_ms"] == 700.0 and u["stt_ms"] == 1445.0 and u["brain_first_token_ms"] == 740.0 and u["brain_first_clause_ms"] == 980.0
    assert u["voice_first_audio_ms"] == 2750.0 and u["playout_start_ms"] == 100.0 and u["to_first_audio_ms"] == 6000.0
    assert u["reply_words"] == 11 and u["first_clause_words"] == 3 and u["first_clause_stop_reason"] == "punct" and u["voice_clauses"] == 2     # clause 2 failed, clause 1 counts once
    assert u["voice_jobs"] == 4                                                                          # but every job is counted (the billed ones)
    assert (u["stt_queue_ms"], u["stt_exec_ms"], u["stt_remainder_ms"], u["stt_polls"]) == (139.0, 612.0, 695.0, 4)
    assert (u["voice_queue_ms"], u["voice_exec_ms"], u["voice_wall_ms"], u["voice_polls"]) == (1200.0, 1700.0, 2750.0, 9)
    assert u["gaps"] == 1 and u["underruns"] == 1 and u["brain_calls"] == 1


def test_a_turn_without_a_first_frame_is_left_out_and_the_first_turns_can_be_skipped():
    assert F.utterance_from_line({"marks_ms": {"t_endpoint": 700.0}}) is None
    lines = [line(first_frame=36000.0)] + [line(first_frame=2000.0 + 100 * i) for i in range(5)]
    ev = F.build_evidence(lines, 2, "ramp", skip_first=1)
    assert ev["n_utterances"] == 5 and ev["summary"]["to_first_audio_ms"]["median"] == 2200.0           # the cold first turn is not in the median
    assert F.build_evidence(lines, 2, "ramp")["summary"]["to_first_audio_ms"]["n"] == 6


def test_the_evidence_is_measured_declares_the_scenario_and_feeds_the_gate():
    lines = [line(first_frame=1900.0 + 10 * i) for i in range(12)]
    ev = F.build_evidence(lines, 2, "ramp", flags={"stream_ramp": True})
    assert ev["tag"] == "MEASURED" and ev["scenario"]["voice_workers"] == 2 and ev["scenario"]["clause_plan"] == "ramp"
    # reply words of this synthetic data are 11: outside the stated 20-26, so the goal gate must refuse to judge it
    res = {r["id"]: r for r in G.evaluate(ev, json.loads(G.DEFAULT_GATES.read_text(encoding="utf-8")))}
    assert res["A-median"]["status"] == "NOT_EVALUATED" and "reply_words" in res["A-median"]["reason"]


def test_the_cli_writes_evidence_and_prints_only_numbers(tmp_path, capsys):
    p = tmp_path / "lines.jsonl"
    p.write_text("\n".join(json.dumps(line(first_frame=2000.0 + i)) for i in range(4)) + "\n", encoding="utf-8")
    out = tmp_path / "ev.json"
    assert F.main([str(p), "--voice-workers", "2", "--clause-plan", "ramp", "--skip-first", "1", "--out", str(out)]) == 0
    ev = json.loads(out.read_text(encoding="utf-8"))
    assert ev["n_utterances"] == 3 and json.loads(capsys.readouterr().out)["turns"] == 3
