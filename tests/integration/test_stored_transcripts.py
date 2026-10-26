"""Verifies that when ai_store_transcripts=True (the default), conversation turns store heard/reply
in ai_sessions.transcript, live flow, AI session view, activity feed, and /calls/{id}/transcript."""
import json
from types import SimpleNamespace

import pytest

from tests.integration.test_live_api import ADMIN, VIEWER, seed_call

CALLER = "CALLER-SAID-ALPHA-4711"
ASSISTANT = "ASSISTANT-SAID-BRAVO-4711"


def _rt(call_id="call_w1", sid="s_w1"):
    return SimpleNamespace(session_id=sid, tenant_id="t1", call_id=call_id,
                           conversation=SimpleNamespace(turns=[{"heard": CALLER, "reply": ASSISTANT}]))


async def _record_a_turn(env, call_id="call_w1", sid="s_w1"):
    await env.db["ai_sessions"].insert_one({"session_id": sid, "tenant_id": "t1", "state": "active", "language": "hi", "turns": 0, "timings_ms": {},
                                            "media": {"connected": True}})
    rt = _rt(call_id, sid)
    await env.app.state.ai.record_turn(rt, {"stt": 300.0, "brain": 700.0, "tts": 400.0, "turn_ms": 1400.0, "heard": CALLER}, heard=CALLER, reply=ASSISTANT, trace_id="tr1", turn_num=1)
    await env.app.state.ai.finalize_turn(rt, "tr1", {"t_speech_end": 0.0, "t_turn_end": 1500.0}, reply=ASSISTANT)
    return rt


async def test_the_stored_session_record_holds_transcripts(env):
    await _record_a_turn(env)
    doc = await env.db["ai_sessions"].find_one({"session_id": "s_w1"})
    assert doc is not None
    entry = doc["transcript"][0]
    assert entry["turn"] == 1
    assert entry["heard"] == CALLER
    assert entry["reply"] == ASSISTANT
    assert entry["heard_chars"] == len(CALLER)
    assert entry["reply_chars"] == len(ASSISTANT)
    assert doc["last_exchange"]["heard"] == CALLER
    assert doc["last_exchange"]["reply"] == ASSISTANT


@pytest.mark.parametrize("headers", [VIEWER, ADMIN], ids=["viewer", "admin"])
async def test_ai_session_view_carries_transcripts(env, headers):
    s, _ = await env.app.state.sessions.create("t1", call_ref="ref-w3", caller="+919876543210", destination="+911140000000")
    sid = s["_id"]
    await _record_a_turn(env, "call_w3", sid)
    r = await env.client.get(f"/v1/ai/sessions/{sid}", headers=headers())
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["transcript"][0]["heard"] == CALLER
    assert data["transcript"][0]["reply"] == ASSISTANT
    assert data["last_exchange"]["heard"] == CALLER
    assert data["last_exchange"]["reply"] == ASSISTANT


@pytest.mark.parametrize("headers", [VIEWER, ADMIN], ids=["viewer", "admin"])
async def test_call_flow_carries_transcripts(env, headers):
    sid = "s_w2"
    await seed_call(env, "t1", "call_w2", state="answered", session_id=sid, ai_enabled=True)
    await _record_a_turn(env, "call_w2", sid)
    flow = await env.client.get("/v1/live/calls/call_w2/flow", headers=headers())
    assert flow.status_code == 200
    data = flow.json()
    assert data["transcript"][0]["heard"] == CALLER
    assert data["transcript"][0]["reply"] == ASSISTANT
    assert data["last_exchange"]["heard"] == CALLER
    assert data["last_exchange"]["reply"] == ASSISTANT


async def test_calls_transcript_endpoint_returns_transcripts(env):
    sid = "s_w4"
    await seed_call(env, "t1", "call_w4", state="answered", session_id=sid, ai_enabled=True)
    await _record_a_turn(env, "call_w4", sid)
    r = await env.client.get("/v1/calls/call_w4/transcript", headers=ADMIN())
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["call_id"] == "call_w4"
    assert data["session_id"] == sid
    assert data["transcript"][0]["heard"] == CALLER
    assert data["transcript"][0]["reply"] == ASSISTANT
