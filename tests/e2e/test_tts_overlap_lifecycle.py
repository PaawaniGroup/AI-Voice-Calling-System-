"""SIMULATED (scripted mock models, no GPU, no real Plivo): the turn lifecycle with `tts_overlap` ON.

Root cause of the "2 of 10 turns lost" seen with overlap on in test_simulated_10_turn_cold_warm.py (traced, not assumed):
the server emits ONE checkpoint per clause. With overlap, clause 2 of turn N is ready as soon as clause 1, so its checkpoint is
already queued when the harness has read the first one. That harness treats "the first checkpoint" as "the turn is over": it reads
the leftover checkpoint of turn N as the answer to turn N+1 and speaks turn N+2 while turn N+1 has produced no audio yet. The
server then does what it should: the newer caller speech supersedes (barge-in cancels) the turn that was still pre-audio, and a turn
cancelled before its first audio chunk is never recorded. Nothing is dropped by the pipeline itself.

Here the caller waits for the turn to be FINALIZED (every clause handed to the transport, `t_turn_end` stored) before it talks again.
"""
from __future__ import annotations

import asyncio
import base64
import json

from app.ai import codec  # noqa: F401  (keeps the import graph identical to the sibling e2e test)
from tests.e2e.test_simulated_10_turn_cold_warm import ADMIN, CFG, pv_simulated  # noqa: F401  (fixture re-used)
from tests.integration.test_plivo_voice import ai_doc, answered_stream, connect, post_cb, send_media, until, utterance

TURNS = 5


async def _drain(ws, quiet_s: float = 0.25) -> list[dict]:
    """Everything the server has sent so far, until the socket is quiet."""
    out = []
    while True:
        try:
            raw = await asyncio.wait_for(ws.recv(), quiet_s)
        except asyncio.TimeoutError:
            return out
        out.append(json.loads(raw))


async def _finalized(env, sid: str, count: int):
    d = await ai_doc(env, sid)
    t = (d or {}).get("transcript") or []
    if len(t) >= count and all("t_turn_end" in (e.get("timings_ms") or {}) for e in t[:count]):
        return t[:count]
    return None


async def test_overlap_on_records_every_turn_when_the_caller_waits_for_the_turn_to_finish(pv_simulated):
    env = pv_simulated
    res = await env.c.put("/v1/ai/config", json={**CFG, "tts_overlap": True}, headers=ADMIN())
    assert res.status_code == 200, res.text

    a = await answered_stream(env, call_uuid="SIM-OVERLAP-LIFECYCLE")
    ws = await connect(env, a)
    await post_cb(env, "stream-status", {"CallUUID": "SIM-OVERLAP-LIFECYCLE", "Event": "started", "StreamID": a.stream_id})
    n_chunk = 1
    checkpoints: list[str] = []
    plays_per_turn: list[int] = []
    for turn_idx in range(1, TURNS + 1):
        env.models.current_turn = turn_idx - 1
        env.models.tts_calls_in_turn = 0
        n_chunk = await send_media(ws, utterance(speech_ms=400, silence_ms=800), first_chunk=n_chunk)
        # lifecycle event 5: the turn is finalized (all audio handed to the transport and the trace stored)
        done = await until(lambda i=turn_idx: _finalized(env, a.sid, i), timeout=15)
        assert done is not None, f"turn {turn_idx} was never finalized"
        if turn_idx == 1:   # the tenant setting really reached the Conversation (the config API used to reject the key)
            assert env.ai.runtime[a.sid].conversation.tts_overlap is True
        msgs = await _drain(ws)
        plays = [m for m in msgs if m.get("event") == "playAudio"]
        plays_per_turn.append(len(plays))
        checkpoints += [m["name"] for m in msgs if m.get("event") == "checkpoint"]
        assert plays and all(base64.b64decode(p["media"]["payload"]) for p in plays), f"turn {turn_idx}: no audio"

    transcript = await _finalized(env, a.sid, TURNS)
    assert transcript is not None and len(transcript) == TURNS     # none lost
    assert all(e.get("reply") for e in transcript)
    # one checkpoint per clause, numbered without gaps or repeats across the whole call = audio ordered and none dropped
    assert checkpoints == [f"utt-{i}" for i in range(1, len(checkpoints) + 1)]
    assert min(plays_per_turn) >= 1 and max(plays_per_turn) >= 2   # the multi-clause (overlap) path really ran

    await post_cb(env, "hangup", {"CallUUID": "SIM-OVERLAP-LIFECYCLE", "CallStatus": "completed", "HangupCause": "NORMAL_CLEARING"})
    await ws.close()
