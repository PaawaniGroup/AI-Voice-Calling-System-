"""Derive the end-to-end flow of one call for the dashboard.

Pure function (no I/O): given the call row, its counselling session row and its AI session row (any of the last two may be
None) it returns the pipeline stages with a status each. Everything shown comes from stored state; a stage with no evidence is
`waiting` or `skipped`, never assumed done.

  created -> ringing -> answered -> session -> media -> ai -> ended

Status values: done | active | waiting | failed | skipped
"""
from __future__ import annotations

from typing import Any

from app.observability.activity import strip_words

PRE_ANSWER = {"queued", "initiating", "ringing"}
CONNECTED = {"answered", "bridged", "transferring"}
BAD_END = {"failed", "busy", "no_answer", "cancelled"}
TERMINAL = {"completed"} | BAD_END


def _stage(key: str, label: str, status: str, at: Any = None, detail: str | None = None) -> dict:
    return {"key": key, "label": label, "status": status, "at": at, "detail": detail}


def flow_stages(call: dict, session: dict | None = None, ai: dict | None = None) -> dict:
    state = call.get("state", "")
    answered = bool(call.get("answered_at")) or state in CONNECTED or (state == "completed" and bool(call.get("answered_at")))
    ended = state in TERMINAL
    wants_ai = bool(call.get("ai_enabled")) or ai is not None
    stages: list[dict] = []

    # 1 created
    stages.append(_stage("created", "Call created", "done", call.get("created_at"),
                         f"{call.get('direction', '?')} via {call.get('provider') or '?'}"))

    # 2 ringing
    if answered or call.get("ringing_at"):
        stages.append(_stage("ringing", "Ringing", "done", call.get("ringing_at"),
                             f"{call['ring_duration_s']:.1f}s" if call.get("ring_duration_s") is not None else None))
    elif state in PRE_ANSWER:
        stages.append(_stage("ringing", "Ringing", "active", None, state))
    elif ended:
        stages.append(_stage("ringing", "Ringing", "failed" if state in BAD_END else "skipped", None, call.get("hangup_cause")))
    else:
        stages.append(_stage("ringing", "Ringing", "waiting"))

    # 3 answered
    if answered:
        stages.append(_stage("answered", "Answered", "done", call.get("answered_at"),
                             "bridged" if call.get("bridged_at") else None))
    elif ended and state in BAD_END:
        stages.append(_stage("answered", "Answered", "failed", None, call.get("failure_reason") or call.get("hangup_cause") or state))
    else:
        stages.append(_stage("answered", "Answered", "waiting"))

    # 4 session
    if session:
        st = session.get("status")
        stages.append(_stage("session", "Session", "failed" if st == "failed" else ("done" if st in ("answered", "active", "ended") else "active"),
                             session.get("created_at"), st))
    elif ended:
        stages.append(_stage("session", "Session", "skipped"))
    else:
        stages.append(_stage("session", "Session", "waiting"))

    # 5 media stream (FreeSWITCH media module or Plivo stream)
    ai_state = (ai or {}).get("state")
    media_up = bool(call.get("stream_connected")) or bool(((ai or {}).get("media") or {}).get("connected")) or ai_state in ("active", "stopped")
    if not wants_ai:
        stages.append(_stage("media", "Media stream", "skipped", None, "AI not requested"))
    elif ai_state == "failed" and not media_up:
        stages.append(_stage("media", "Media stream", "failed", None, (ai or {}).get("stop_reason") or (ai or {}).get("error")))
    elif media_up and ai_state != "media_disconnected":
        stages.append(_stage("media", "Media stream", "done", call.get("stream_connected_at") or (ai or {}).get("started_at"),
                             "plivo stream" if call.get("stream_connected") else "freeswitch module"))
    elif ai_state == "media_disconnected":
        stages.append(_stage("media", "Media stream", "active", None, "reconnecting"))
    elif ai_state == "starting" or (answered and not ended):
        stages.append(_stage("media", "Media stream", "active", None, "waiting for the stream to connect"))
    else:
        stages.append(_stage("media", "Media stream", "waiting"))

    # 6 AI conversation
    if not wants_ai:
        stages.append(_stage("ai", "AI conversation", "skipped"))
    elif ai is None:
        stages.append(_stage("ai", "AI conversation", "failed" if ended else "waiting", None,
                             "no AI session was started" if ended else None))
    else:
        turns = ai.get("turns", 0)
        tm = ai.get("timings_ms") or {}
        last = f", last turn {tm['turn_ms']:.0f} ms" if tm.get("turn_ms") is not None else ""
        det = f"{turns} turn(s){last}"
        if ai_state == "failed":
            stages.append(_stage("ai", "AI conversation", "failed", ai.get("stopped_at"), f"{ai.get('stop_reason') or ai.get('error')}; {det}"))
        elif ai_state == "stopped":
            stages.append(_stage("ai", "AI conversation", "done", ai.get("stopped_at"), f"{ai.get('stop_reason') or 'stopped'}; {det}"))
        else:
            stages.append(_stage("ai", "AI conversation", "active", ai.get("started_at"), f"{ai_state}; {det}"))

    # 7 ended
    if ended:
        stages.append(_stage("ended", "Ended", "failed" if state in BAD_END else "done", call.get("ended_at"),
                             " · ".join(x for x in (state, call.get("hangup_cause"),
                                                    f"{call['connected_duration_s']:.0f}s connected" if call.get("connected_duration_s") else None) if x)))
    else:
        stages.append(_stage("ended", "Ended", "waiting"))

    overall = "failed" if state in BAD_END else ("ended" if state == "completed" else "live")
    return {
        "overall": overall,
        "state": state,
        "stages": stages,
        "ai_timings": strip_words((ai or {}).get("timings_ms", {})),
        "last_exchange": strip_words((ai or {}).get("last_exchange")),   # timings only: no words live
        "transcript": strip_words((ai or {}).get("transcript", []))
    }
