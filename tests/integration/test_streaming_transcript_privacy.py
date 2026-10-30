"""Test privacy enforcement on the streaming path and record_turn.

Verifies:
1. When ai_store_transcripts is False (default):
   - ai_sessions.transcript[] has heard_chars and reply_chars, but NOT 'heard' or 'reply' text.
   - ai_sessions.last_exchange has heard_chars and reply_chars, but NOT 'heard' or 'reply' text.
   - ActivityBus live feed (ai.turn) does not carry 'heard' or 'reply' text.
2. When ai_store_transcripts is True:
   - ai_sessions.transcript[] and last_exchange contain 'heard' and 'reply'.
"""
import pytest
from types import SimpleNamespace
from tests.integration.test_live_api import seed_call

CALLER = "TEST-CALLER-WORDS-SECRET"
ASSISTANT = "TEST-ASSISTANT-REPLY-SECRET"


def _make_runtime(sid: str = "sess_priv_1", call_id: str = "call_priv_1", tenant_id: str = "t1"):
    conv = SimpleNamespace(turns=[{"heard": CALLER, "reply": ASSISTANT}], streaming=None)
    rt = SimpleNamespace(
        session_id=sid,
        tenant_id=tenant_id,
        call_id=call_id,
        conversation=conv,
    )
    return rt


@pytest.mark.asyncio
async def test_streaming_record_turn_strips_words_by_default(env):
    """By default (ai_store_transcripts=False), record_turn and finalize_turn must NOT store words."""
    sid = "s_streaming_priv_default"
    call_id = "call_streaming_priv_default"
    await env.db["ai_sessions"].insert_one({
        "session_id": sid, "tenant_id": "t1", "call_id": call_id,
        "state": "active", "language": "hi", "turns": 0, "timings_ms": {}, "transcript": []
    })
    rt = _make_runtime(sid, call_id)

    # Simulate real record_turn and finalize_turn as invoked by the streaming pipeline
    timings = {"stt": 200.0, "brain": 600.0, "tts": 300.0, "turn_ms": 1100.0}
    await env.app.state.ai.record_turn(rt, timings, count=True, heard=CALLER, reply=ASSISTANT, trace_id="tr_p1")
    await env.app.state.ai.finalize_turn(rt, "tr_p1", {"t_speech_end": 0.0, "t_turn_end": 1200.0}, reply=ASSISTANT)

    # Check MongoDB document
    doc = await env.db["ai_sessions"].find_one({"session_id": sid})
    assert doc is not None
    entry = doc["transcript"][0]

    # Must preserve sizes and timings
    assert entry["heard_chars"] == len(CALLER)
    assert entry["reply_chars"] == len(ASSISTANT)
    assert entry["timings_ms"]["turn_ms"] == 1100.0

    # Words MUST NOT be stored
    assert "heard" not in entry, f"heard leaked into transcript: {entry.get('heard')}"
    assert "reply" not in entry, f"reply leaked into transcript: {entry.get('reply')}"
    assert "heard" not in doc["last_exchange"], f"heard leaked into last_exchange"
    assert "reply" not in doc["last_exchange"], f"reply leaked into last_exchange"

    # Check live activity feed
    events = env.app.state.activity.recent("t1")
    turn_ev = next((e for e in events if e.get("kind") == "ai.turn" and e.get("session_id") == sid), None)
    if turn_ev:
        assert "heard" not in turn_ev["data"], "heard leaked into activity feed"
        assert "reply" not in turn_ev["data"], "reply leaked into activity feed"


@pytest.mark.asyncio
async def test_streaming_record_turn_stores_words_when_flag_enabled(env):
    """When ai_store_transcripts=True, record_turn and finalize_turn store words."""
    sid = "s_streaming_priv_optin"
    call_id = "call_streaming_priv_optin"
    await env.db["ai_sessions"].insert_one({
        "session_id": sid, "tenant_id": "t1", "call_id": call_id,
        "state": "active", "language": "hi", "turns": 0, "timings_ms": {}, "transcript": []
    })
    rt = _make_runtime(sid, call_id)

    # Enable flag on settings
    env.app.state.settings.ai_store_transcripts = True
    try:
        timings = {"stt": 200.0, "brain": 600.0, "tts": 300.0, "turn_ms": 1100.0}
        await env.app.state.ai.record_turn(rt, timings, count=True, heard=CALLER, reply=ASSISTANT, trace_id="tr_p2")
        await env.app.state.ai.finalize_turn(rt, "tr_p2", {"t_speech_end": 0.0, "t_turn_end": 1200.0}, reply=ASSISTANT)

        doc = await env.db["ai_sessions"].find_one({"session_id": sid})
        assert doc is not None
        entry = doc["transcript"][0]

        # Words MUST be stored when opted in
        assert entry.get("heard") == CALLER
        assert entry.get("reply") == ASSISTANT
        assert doc["last_exchange"].get("heard") == CALLER
        assert doc["last_exchange"].get("reply") == ASSISTANT
    finally:
        env.app.state.settings.ai_store_transcripts = False
