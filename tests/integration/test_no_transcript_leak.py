"""No role sees conversation words live: the activity feed, the call flow, the snapshot and the AI session view carry timings and
counts, never `heard` / `reply` text, and the stored session record holds lengths and timings, not text (until the operator decides
otherwise). Model services and the provider are MOCKS."""
import json
from types import SimpleNamespace

import pytest

from tests.integration.test_live_api import ADMIN, VIEWER, seed_call

CALLER = "CALLER-SAID-ALPHA-4711"
ASSISTANT = "ASSISTANT-SAID-BRAVO-4711"


@pytest.fixture(autouse=True)
def privacy_mode(env):
    env.s.ai_store_transcripts = False
    env.app.state.settings.ai_store_transcripts = False
    env.app.state.activity.keep_words = False


def _rt(call_id="call_w1", sid="s_w1"):
    return SimpleNamespace(session_id=sid, tenant_id="t1", call_id=call_id,
                           conversation=SimpleNamespace(turns=[{"heard": CALLER, "reply": ASSISTANT}]))


async def _record_a_turn(env, call_id="call_w1", sid="s_w1"):
    await env.db["ai_sessions"].insert_one({"session_id": sid, "tenant_id": "t1", "state": "active", "language": "hi", "turns": 0, "timings_ms": {},
                                            "media": {"connected": True}})
    rt = _rt(call_id, sid)
    await env.app.state.ai.record_turn(rt, {"stt": 300.0, "brain": 700.0, "tts": 400.0, "turn_ms": 1400.0, "heard": CALLER}, heard=CALLER, reply=ASSISTANT, trace_id="tr1")
    await env.app.state.ai.finalize_turn(rt, "tr1", {"t_speech_end": 0.0, "t_turn_end": 1500.0})   # the second write of a turn
    env.app.state.ai.note_error(rt, "tts_unreachable")
    return rt


def _assert_no_words(text: str, where: str):
    assert CALLER not in text and ASSISTANT not in text, f"conversation words leaked in {where}"


async def test_the_stored_session_record_holds_lengths_and_timings_not_text(env):
    await _record_a_turn(env)
    doc = await env.db["ai_sessions"].find_one({"session_id": "s_w1"})
    _assert_no_words(json.dumps(doc, default=str), "the ai_sessions document in MongoDB")
    entry = doc["transcript"][0]
    assert "heard" not in entry and "reply" not in entry
    assert entry["heard_chars"] == len(CALLER) and entry["reply_chars"] == len(ASSISTANT)       # sizes are kept (useful, not sensitive)
    assert entry["timings_ms"]["tts"] == 400.0 and entry["timings_ms"]["t_turn_end"] == 1500.0  # and so are the timings
    assert doc["last_exchange"]["reply_chars"] == len(ASSISTANT) and "reply" not in doc["last_exchange"]


@pytest.mark.parametrize("headers", [VIEWER, ADMIN], ids=["viewer", "admin"])
async def test_live_activity_feed_carries_timings_not_words(env, headers):
    await _record_a_turn(env)
    r = await env.client.get("/v1/live/activity", headers=headers())
    _assert_no_words(r.text, "/v1/live/activity")
    items = r.json()["items"]
    turn = next(i for i in items if i["kind"] == "ai.turn")
    assert turn["data"]["turn_ms"] == 1400.0 and turn["data"]["stt_ms"] == 300.0     # the useful numbers are still there
    assert "heard" not in turn["data"] and "reply" not in turn["data"]
    assert any(i["kind"] == "ai.turn_latency" for i in items)


async def test_the_bus_itself_strips_words_whoever_publishes(env):
    """Defence in depth: even a future publisher that passes words (here: nested) cannot put them on the feed."""
    env.app.state.activity.publish("t1", "ai.turn", data={"turn": 1, "heard": CALLER, "nested": {"reply": ASSISTANT, "ok": 1}, "rows": [{"heard": CALLER, "n": 2}]})
    text = json.dumps(env.app.state.activity.recent("t1"))
    _assert_no_words(text, "ActivityBus.recent")
    assert '"ok": 1' in text and '"n": 2' in text


@pytest.mark.parametrize("headers", [VIEWER, ADMIN], ids=["viewer", "admin"])
async def test_call_flow_and_snapshot_carry_no_words(env, headers):
    sid = "s_w2"
    await seed_call(env, "t1", "call_w2", state="answered", session_id=sid, ai_enabled=True)
    await _record_a_turn(env, "call_w2", sid)
    flow = await env.client.get("/v1/live/calls/call_w2/flow", headers=headers())
    _assert_no_words(flow.text, "/v1/live/calls/{id}/flow")
    assert flow.json()["last_exchange"]["timings_ms"]["tts"] == 400.0 and flow.json()["transcript"][0]["turn"] == 1   # timings survive
    snap = await env.client.get("/v1/live/snapshot", headers=headers())
    _assert_no_words(snap.text, "/v1/live/snapshot")


@pytest.mark.parametrize("headers", [VIEWER, ADMIN], ids=["viewer", "admin"])
async def test_ai_session_view_carries_no_words(env, headers):
    s, _ = await env.app.state.sessions.create("t1", call_ref="ref-w3", caller="+919876543210", destination="+911140000000")
    sid = s["_id"]
    await _record_a_turn(env, "call_w3", sid)
    r = await env.client.get(f"/v1/ai/sessions/{sid}", headers=headers())
    assert r.status_code == 200, r.text
    _assert_no_words(r.text, "/v1/ai/sessions/{id}")
    assert r.json()["turns"] == 1
