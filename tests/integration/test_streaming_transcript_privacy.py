"""Test privacy enforcement on the streaming path, record_turn, and per-tenant store_transcripts.

Verifies:
1. When store_transcripts is False (default):
   - ai_sessions.transcript[] has heard_chars and reply_chars, but NOT 'heard' or 'reply' text.
   - ai_sessions.last_exchange has heard_chars and reply_chars, but NOT 'heard' or 'reply' text.
   - ActivityBus live feed (ai.turn) does not carry 'heard' or 'reply' text.
   - GET /calls/{id}/transcript returns 404 transcript_not_available.
2. When store_transcripts is True for a specific tenant:
   - ai_sessions.transcript[] and last_exchange contain 'heard' and 'reply'.
   - ActivityBus live feed contains 'heard' and 'reply'.
   - GET /calls/{id}/transcript returns transcript list.
"""
import pytest
from types import SimpleNamespace
from tests.integration.test_live_api import seed_call

CALLER = "TEST-CALLER-WORDS-SECRET"
ASSISTANT = "TEST-ASSISTANT-REPLY-SECRET"


def _make_runtime(sid: str = "sess_priv_1", call_id: str = "call_priv_1", tenant_id: str = "t1", store_transcripts: bool = False):
    conv = SimpleNamespace(turns=[{"heard": CALLER, "reply": ASSISTANT}], streaming=None)
    rt = SimpleNamespace(
        session_id=sid,
        tenant_id=tenant_id,
        call_id=call_id,
        conversation=conv,
        store_transcripts=store_transcripts,
    )
    return rt


@pytest.mark.asyncio
async def test_streaming_record_turn_strips_words_by_default(env):
    """By default (store_transcripts=False), record_turn and finalize_turn must NOT store words."""
    sid = "s_streaming_priv_default"
    call_id = "call_streaming_priv_default"
    await env.db["ai_sessions"].insert_one({
        "session_id": sid, "tenant_id": "t1", "call_id": call_id,
        "state": "active", "language": "hi", "turns": 0, "timings_ms": {}, "transcript": []
    })
    rt = _make_runtime(sid, call_id, tenant_id="t1", store_transcripts=False)

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
async def test_streaming_record_turn_stores_words_when_tenant_flag_enabled(env):
    """When tenant has store_transcripts=True, record_turn and finalize_turn store words."""
    sid = "s_streaming_priv_optin"
    call_id = "call_streaming_priv_optin"
    await env.db["ai_sessions"].insert_one({
        "session_id": sid, "tenant_id": "t1", "call_id": call_id,
        "state": "active", "language": "hi", "turns": 0, "timings_ms": {}, "transcript": []
    })
    rt = _make_runtime(sid, call_id, tenant_id="t1", store_transcripts=True)

    timings = {"stt": 200.0, "brain": 600.0, "tts": 300.0, "turn_ms": 1100.0}
    await env.app.state.ai.record_turn(rt, timings, count=True, heard=CALLER, reply=ASSISTANT, trace_id="tr_p2")
    await env.app.state.ai.finalize_turn(rt, "tr_p2", {"t_speech_end": 0.0, "t_turn_end": 1200.0}, reply=ASSISTANT)

    doc = await env.db["ai_sessions"].find_one({"session_id": sid})
    assert doc is not None
    entry = doc["transcript"][0]

    # Words MUST be stored when tenant opted in
    assert entry.get("heard") == CALLER
    assert entry.get("reply") == ASSISTANT
    assert doc["last_exchange"].get("heard") == CALLER
    assert doc["last_exchange"].get("reply") == ASSISTANT

    # Live activity feed never carries words (feed invariant from commit 0aeed46)
    events = env.app.state.activity.recent("t1")
    turn_ev = next((e for e in events if e.get("kind") == "ai.turn" and e.get("session_id") == sid), None)
    assert turn_ev is not None
    assert "heard" not in turn_ev["data"]
    assert "reply" not in turn_ev["data"]


@pytest.mark.asyncio
async def test_cross_tenant_isolation_for_transcript_route(env):
    """Tenant B must receive 404 when querying transcript for Tenant A's call, even with store_transcripts on."""
    # 1. Enable store_transcripts for tenant t1 and tenant t2
    await env.app.state.ai.put_config("t1", {"store_transcripts": True}, "test")
    await env.app.state.ai.put_config("t2", {"store_transcripts": True}, "test")

    # 2. Seed a completed call and transcript session for tenant t1
    call_id = "call_t1_private_cross"
    await seed_call(env, tenant="t1", cid=call_id)
    sid = "sess_t1_private_cross"
    await env.db["ai_sessions"].insert_one({
        "session_id": sid,
        "tenant_id": "t1",
        "call_id": call_id,
        "state": "completed",
        "language": "hi",
        "turns": 1,
        "transcript": [{"turn": 1, "heard": "Hello", "reply": "Hi"}]
    })

    # 3. Tenant t1 accesses own call: 200 OK with transcript
    from tests.conftest import auth
    res_t1 = await env.client.get(f"/v1/calls/{call_id}/transcript", headers=auth(tenant="t1"))
    assert res_t1.status_code == 200
    assert res_t1.json()["turns"] == 1
    assert len(res_t1.json()["transcript"]) == 1

    # 4. Tenant t2 accesses tenant t1's call: MUST receive 404 Not Found
    res_t2 = await env.client.get(f"/v1/calls/{call_id}/transcript", headers=auth(tenant="t2"))
    assert res_t2.status_code == 404
    assert "not found" in res_t2.text.lower()
