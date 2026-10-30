"""Turn the live service's numbers-only trace lines (MME_TEL_AI_TRACE_EVIDENCE_PATH + tenant flag trace_evidence: one JSON line per finished turn) into harness evidence, so
a REAL call can be judged by tools/realtime/gate.py and tools/realtime/table.py exactly like a harness run.

  python tools/realtime/from_trace.py LINES.jsonl --voice-workers 2 --clause-plan ramp [--skip-first 1] --out call.json

Everything comes from the trace marks of that turn (relative to the last voiced frame, `t_speech_end`) and its job records; the metric `to_first_audio_ms` is the turn's
`t_first_frame_to_plivo`, i.e. last voiced frame -> first audio frame handed to the transport. The tag is MEASURED because the lines come from a real call; the operator
DECLARES the scenario the call was made in (voice workers, clause plan): a trace cannot see the console. Numbers and enumerations only: the lines contain no words.
`--skip-first N` drops the first N turns (the operator reports medians from turn 2 on; turn 1 is the cold turn).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.realtime import stages as S      # noqa: E402


def _d(m: dict, a: str, b: str):
    x, y = m.get(a), m.get(b)
    return None if x is None or y is None else round(y - x, 1)


def utterance_from_line(line: dict) -> dict | None:
    """One stage record from one trace line (None when the turn never produced a first frame)."""
    m = line.get("marks_ms") or {}
    if m.get("t_first_frame_to_plivo") is None:
        return None
    jobs = line.get("jobs") or []
    stt = next((j for j in jobs if j.get("service") == "stt"), {})
    tts = [j for j in jobs if j.get("service") == "tts" and j.get("status") == "ok" and j.get("clause_idx") is not None]
    by_idx = {}
    for j in tts:
        by_idx.setdefault(j["clause_idx"], j)                         # one record per clause (a retried clause counts once)
    first = by_idx.get(0, {})
    st = line.get("stream") or {}
    return {"end_of_turn_ms": m.get("t_endpoint"), "stt_ms": _d(m, "t_stt_sent", "t_stt_final"),
            "brain_first_token_ms": _d(m, "t_llm_sent", "t_llm_first_token"), "brain_first_clause_ms": _d(m, "t_llm_sent", "t_llm_first_clause"),
            "voice_first_audio_ms": _d(m, "t_tts_sent", "t_tts_first_chunk"), "playout_start_ms": _d(m, "t_tts_first_chunk", "t_first_frame_to_plivo"),
            "to_first_audio_ms": m["t_first_frame_to_plivo"],
            "reply_words": sum(j.get("words") or 0 for j in by_idx.values()), "first_clause_words": first.get("words"), "first_clause_stop_reason": first.get("stop_reason"),
            "stt_queue_ms": stt.get("delay_ms"), "stt_exec_ms": stt.get("exec_ms"), "stt_remainder_ms": stt.get("remainder_ms"), "stt_polls": stt.get("polls"),
            "stt_request_bytes": stt.get("request_bytes"), "voice_queue_ms": first.get("delay_ms"), "voice_exec_ms": first.get("exec_ms"), "voice_wall_ms": first.get("total_ms"),
            "voice_polls": first.get("polls"), "voice_poll_ms": first.get("poll_ms"), "voice_remainder_ms": first.get("remainder_ms"),
            "voice_gap_since_prev_ms": first.get("gap_since_prev_ms"), "silence_ms": st.get("silence_ms"), "underruns": st.get("underruns"),
            "gaps": len(st.get("gaps") or []) if isinstance(st.get("gaps"), list) else None, "tts_errors": st.get("tts_errors"),
            "brain_calls": len(line.get("brain_calls") or []), "voice_clauses": len(by_idx),
            "voice_jobs": sum(1 for j in jobs if j.get("service") == "tts")}       # every voice job of the turn, failed and retried ones included (what is billed)


EXTRA_KEYS = ("stt_queue_ms", "stt_exec_ms", "stt_remainder_ms", "stt_polls", "voice_queue_ms", "voice_exec_ms", "voice_wall_ms", "voice_polls", "voice_poll_ms",
              "voice_remainder_ms", "voice_gap_since_prev_ms")


def build_evidence(lines: list[dict], voice_workers: int | None, clause_plan: str, skip_first: int = 0, flags: dict | None = None) -> dict:
    utts = [u for u in (utterance_from_line(x) for x in lines[skip_first:]) if u is not None]
    summary = S.summarize(utts)
    for k in EXTRA_KEYS:                                                   # extra numbers for the report (medians only)
        vals = sorted(u[k] for u in utts if isinstance(u.get(k), (int, float)))
        if vals:
            summary[k] = {"median": vals[len(vals) // 2] if len(vals) % 2 else round((vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2, 1),
                          "p90": S.pctl(vals, 90), "n": len(vals)}
    return {"schema": 1, "tag": "MEASURED", "scenario": {"mode": "trace", "clause_plan": clause_plan, "voice_workers": voice_workers, "skipped_first_turns": skip_first,
                                                          "tenant_flags": flags or {}},
            "n_utterances": len(utts), "n_errors": 0, "summary": summary, "utterances": utts}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("lines")
    ap.add_argument("--voice-workers", type=int, required=True, help="the voice workers the operator had set in the console during the call (declared, not observable)")
    ap.add_argument("--clause-plan", choices=("today", "ramp"), required=True)
    ap.add_argument("--skip-first", type=int, default=0)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    lines = []
    for raw in Path(a.lines).read_text(encoding="utf-8").splitlines():
        if raw.strip():
            lines.append(json.loads(raw))
    ev = build_evidence(lines, a.voice_workers, a.clause_plan, a.skip_first)
    Path(a.out).write_text(json.dumps(ev, indent=1), encoding="utf-8")
    print(json.dumps({"tag": "MEASURED", "turns": ev["n_utterances"], "summary": {k: ev["summary"][k] for k in ("to_first_audio_ms", "end_of_turn_ms", "stt_ms") if k in ev["summary"]}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
