"""Outbound AI calls through Plivo: POST /v1/calls (ai_enabled) -> dispatch -> signed Answer -> <Stream> -> media WebSocket -> the SAME
conversation pipeline as inbound -> hangup cleanup.

MOCKED, exactly like tests/integration/test_plivo_voice.py: the Plivo REST adapter is a fake (no call is placed), Plivo's own requests are signed
with the SDK primitives and a fake token, the media client is a test WebSocket, and STT/Brain/TTS are httpx mocks. The service (routes,
controller, MongoDB test database, AiService, serve_loop) is real. Nothing here proves behaviour with real Plivo, a real phone or real models.
"""
import asyncio
import json

import httpx
import pytest
import pytest_asyncio
import websockets

from app.core.ids import utcnow
from app.main import create_app
from app.providers.base import OriginateResult, ProviderAdapter, ProviderUncertain
from tests.conftest import auth, idem, make_settings
from tests.helpers import live_server, pcm_tone
from tests.integration.test_plivo_voice import (ADMIN, BASE, CFG, RATE, TOKEN, Models, _active, _stopped, ai_doc, connect, media_frames,
                                                post_answer, post_cb, recv_until, send_media, start_msg, stream_of, until, utterance)

CALLER_ID = "+911140000000"
DEST = "+919876543210"
OTHER = "+919876543211"
# The outbound-AI opt-in a tenant needs (fail-closed by default): enabled, the EXACT numbers it may dial, a daily quota.
AI_POLICY = {"outbound_ai_enabled": True, "outbound_ai_destinations": [DEST], "outbound_ai_daily_quota": 50}
USER = lambda: auth("t1", ("telephony:user",))  # noqa: E731


class FakeOutPlivo(ProviderAdapter):
    """Stands in for the Plivo REST client: never dials. `delay` holds the originate response back (the answer-before-id race),
    `uncertain` makes it time out the way a lost REST response does."""
    name = "plivo_api"

    def __init__(self):
        self.originated = []
        self.hangups = []
        self.delay = 0.0
        self.uncertain = False

    async def originate(self, req):
        self.originated.append(req)
        if self.uncertain:
            raise ProviderUncertain("plivo_timeout")
        if self.delay:
            await asyncio.sleep(self.delay)
        return OriginateResult(provider_call_id=f"REQ-{req.call_id}", events_via_esl=False)

    async def hangup(self, call, cause):
        self.hangups.append((call["_id"], cause))


@pytest_asyncio.fixture
async def ov(db):
    s = make_settings(mongo_db=db.s.mongo_db, provider="plivo_api", plivo_auth_id="AUTHID-test", plivo_auth_token=TOKEN,
                      webhook_public_base_url=BASE, real_calls_enabled=True, ai_enabled=True,
                      ai_allowed_hosts="stt.test,brain.test,tts.test,rag.test", ai_api_key="test-model-key-123456",
                      plivo_orphan_grace_s=30, plivo_stream_connect_s=20)
    models, fake = Models(), FakeOutPlivo()
    app = create_app(s, db=db, provider=fake, ai_transport=models.transport())
    async with live_server(app) as host:
        async with httpx.AsyncClient(base_url=f"http://{host}", timeout=15) as c:
            e = type("OV", (), {})()
            e.s, e.app, e.c, e.host, e.models, e.fake, e.db = s, app, c, host, models, fake, db
            e.repos, e.ctrl, e.ai = app.state.repos, app.state.controller, app.state.ai
            await e.repos.put_policy("t1", {"enabled": True, "caller_ids": [CALLER_ID], "max_active_calls": 5, **AI_POLICY})
            assert (await c.put("/v1/ai/config", json=CFG, headers=ADMIN())).status_code == 200
            yield e


async def _usage(ov, call_id):
    c = await ov.db["calls"].find_one({"_id": call_id})
    return c if c and c.get("ai_usage") else None


PLATFORM = lambda: auth("t1", ("platform:admin",))  # noqa: E731


async def replace_policy(ov, **kw):
    """Replace the tenant policy document outright (Repos.put_policy merges with $set, so it would keep an earlier opt-in)."""
    await ov.db["tenant_policies"].replace_one({"tenant_id": "t1"}, {"tenant_id": "t1", "enabled": True, "caller_ids": [CALLER_ID], **kw}, upsert=True)


async def audit_actions(ov):
    return [r["action"] for r in await ov.db["audit"].find({}).to_list(100)]


async def place(ov, **kw):
    body = {"to": DEST, "from_number": CALLER_ID, **kw}
    return await ov.c.post("/v1/calls", json=body, headers={**USER(), **idem()})


async def dispatched(ov, call_id):
    """Wait until the controller originated the call and the RequestUUID is stored."""
    async def stored():
        c = await ov.db["calls"].find_one({"_id": call_id})
        return c if c and c.get("provider_call_id") else None
    c = await until(stored)
    assert c, "call was not dispatched"
    return c


def answer_params(call, call_uuid, direction="outbound"):
    return {"CallUUID": call_uuid, "RequestUUID": call["provider_call_id"], "Direction": direction, "To": DEST.lstrip("+"),
            "From": CALLER_ID.lstrip("+")}


async def answered_outbound(ov, call_uuid="OUT-1", **kw):
    r = await place(ov, ai_enabled=True, language="hinglish", **kw)
    assert r.status_code == 202, r.text
    call = await dispatched(ov, r.json()["id"])
    ans = await post_answer(ov, answer_params(call, call_uuid))
    assert ans.status_code == 200, ans.text
    _, st = stream_of(ans.text)
    assert st is not None, ans.text
    call = await ov.db["calls"].find_one({"_id": call["_id"]})
    return type("A", (), {"extra": st.get("extraHeaders"), "call": call, "uuid": call_uuid, "sid": call["session_id"],
                          "stream_id": "stream-" + call_uuid, "xml": ans.text})()


# =================================================================================== creation
async def test_outbound_ai_call_is_marked_and_has_its_own_session(ov):
    r = await place(ov, ai_enabled=True, language="hinglish")
    assert r.status_code == 202, r.text
    call = await ov.db["calls"].find_one({"_id": r.json()["id"]})
    assert call["ai_enabled"] is True and call["ai_outbound"] is True and call["provider"] == "plivo_api" and call["direction"] == "outbound"
    sess = await ov.db["sessions"].find_one({"_id": call["session_id"]})
    assert sess["call_id"] == call["_id"] and sess["tenant_id"] == "t1"
    assert ov.fake.hangups == [] and await ov.db["ai_sessions"].count_documents({}) == 0   # nothing starts before the recipient answers


async def test_ordinary_outbound_call_is_unchanged_and_gets_wait_not_a_stream(ov):
    r = await place(ov)
    call = await dispatched(ov, r.json()["id"])
    assert "ai_outbound" not in call and call["ai_enabled"] is False
    ans = await post_answer(ov, answer_params(call, "ORD-1"))
    assert "<Wait" in ans.text and "Stream" not in ans.text
    assert await ov.db["ai_sessions"].count_documents({}) == 0


async def test_ai_call_is_refused_when_ai_is_not_ready_and_nothing_is_queued(ov):
    assert (await ov.c.put("/v1/ai/config", json={"enabled": False}, headers=ADMIN())).status_code == 200
    r = await place(ov, ai_enabled=True)
    assert r.status_code == 409 and r.json()["error"]["code"] == "ai_not_ready", r.text
    assert await ov.db["calls"].count_documents({}) == 0 and await ov.db["idempotency"].count_documents({}) == 0
    assert (await place(ov)).status_code == 202   # a plain call is not affected by the AI switch


async def test_ai_call_cannot_also_bridge(ov):
    await ov.repos.put_policy("t1", {"enabled": True, "caller_ids": [CALLER_ID], "transfer_destinations": ["+919999999999"], **AI_POLICY})   # the transfer itself is permitted
    r = await place(ov, ai_enabled=True, bridge_to="+919999999999")
    assert r.status_code == 422 and r.json()["error"]["code"] == "ai_bridge_unsupported", r.text


async def test_other_providers_keep_storing_ai_enabled_without_starting_anything(db):
    from app.providers.base import MockProvider
    s = make_settings(mongo_db=db.s.mongo_db, provider="mock")
    app = create_app(s, db=db, provider=MockProvider())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            await app.state.repos.put_policy("t1", {"enabled": True, "caller_ids": [CALLER_ID]})
            r = await c.post("/v1/calls", json={"to": DEST, "from_number": CALLER_ID, "ai_enabled": True}, headers={**USER(), **idem()})
            assert r.status_code == 202
            call = await db["calls"].find_one({"_id": r.json()["id"]})
            assert call["ai_enabled"] is True and "ai_outbound" not in call


# =================================================================================== answer and stream
async def test_answer_returns_a_ticketed_bidirectional_stream_for_this_call(ov):
    a = await answered_outbound(ov, "OUT-XML")
    _, st = stream_of(a.xml)
    assert st.get("bidirectional") == "true" and st.get("contentType") == "audio/x-mulaw;rate=8000"
    assert a.extra.startswith("mme_ticket=")
    assert a.call["state"] == "answered" and a.call["provider_call_id"] == "OUT-XML"        # RequestUUID rewritten to the CallUUID
    assert a.call["stream_expected_by"] is not None and a.call["session_id"]
    assert (await ov.db["sessions"].find_one({"_id": a.sid}))["status"] == "answered"
    dup = await post_answer(ov, answer_params(a.call, "OUT-XML"))                         # Plivo retry: same decision, no second session
    assert dup.status_code == 200 and "Stream" in dup.text
    assert await ov.db["sessions"].count_documents({}) == 1


async def test_answer_that_arrives_before_the_request_uuid_is_stored_is_not_rejected(ov):
    ov.fake.delay = 0.8                                                                   # the originate response (and the stored id) is late
    r = await place(ov, ai_enabled=True)
    cid = r.json()["id"]

    async def originating():
        c = await ov.db["calls"].find_one({"_id": cid})
        return c if c and c.get("originate_started_at") else None
    assert await until(originating)
    c0 = await ov.db["calls"].find_one({"_id": cid})
    assert not c0.get("provider_call_id")
    ans = await post_answer(ov, {"CallUUID": "RACE-1", "RequestUUID": f"REQ-{cid}", "Direction": "outbound", "To": DEST.lstrip("+")})
    assert "Stream" in ans.text and "Hangup" not in ans.text, ans.text
    assert (await ov.db["calls"].find_one({"_id": cid}))["state"] == "answered"


async def test_unknown_outbound_answer_is_still_rejected(ov):
    ans = await post_answer(ov, {"CallUUID": "NOBODY-1", "Direction": "outbound", "To": DEST.lstrip("+")})
    assert ans.text.count("Hangup") == 1 and "Stream" not in ans.text


async def test_outbound_stream_needs_the_ticket_and_the_right_call(ov):
    a = await answered_outbound(ov, "TK-1")
    b = await answered_outbound(ov, "TK-2")
    ws = await connect(ov, a, send_start=False)
    msg = start_msg(a)
    del msg["extra_headers"]                                                              # no ticket at all
    await ws.send(json.dumps(msg))
    with pytest.raises(websockets.ConnectionClosed) as ei:
        await asyncio.wait_for(ws.recv(), 4)
    assert ei.value.rcvd.code == 1008
    ws2 = await connect(ov, a, send_start=False)                                          # call A's ticket, claiming call B
    await ws2.send(json.dumps({**start_msg(a, start={"callId": b.uuid}), "extra_headers": a.extra}))
    with pytest.raises(websockets.ConnectionClosed):
        await asyncio.wait_for(ws2.recv(), 4)
    assert await ov.db["ai_sessions"].count_documents({}) == 0


# =================================================================================== the whole mocked lifecycle
async def test_full_outbound_ai_conversation_then_hangup_cleans_everything_up(ov):
    a = await answered_outbound(ov, "FULL-1")
    ws = await connect(ov, a)
    doc = await until(lambda: _active(ov, a.sid))
    assert doc["media"]["hello"]["flavor"] == "plivo_stream" and doc["call_id"] == a.call["_id"]
    await send_media(ws, utterance())
    msgs = await recv_until(ws, lambda m: m.get("event") == "checkpoint")
    assert [m for m in msgs if m["event"] == "playAudio"]
    assert [k for k, _ in ov.models.calls][:4] == ["stt", "rag", "brain", "tts"]
    ai = await ai_doc(ov, a.sid)
    assert ai["turns"] == 1 and {"stt", "brain", "tts", "turn_ms"} <= set(ai["timings_ms"])
    call = await ov.db["calls"].find_one({"_id": a.call["_id"]})
    assert call["stream_connected"] is True
    done = {"CallUUID": "FULL-1", "CallStatus": "completed", "HangupCause": "NORMAL_CLEARING", "Duration": "7"}
    assert (await post_cb(ov, "hangup", done)).json()["status"] == "applied"
    call = await ov.db["calls"].find_one({"_id": a.call["_id"]})
    assert call["state"] == "completed" and (await ov.db["sessions"].find_one({"_id": a.sid}))["status"] == "ended"
    ai = await until(lambda: _stopped(ov, a.sid))
    assert ai["stop_reason"] == "call_ended" and a.sid not in ov.ai.runtime
    usage = (await until(lambda: _usage(ov, a.call["_id"])))["ai_usage"]                 # usage accounting written once when the AI session ended
    assert usage["turns"] == 1 and usage["ai_seconds"] > 0 and usage["stop_reason"] == "call_ended"
    assert usage["jobs"]["stt"] == 1 and usage["jobs"]["brain"] == 1 and usage["jobs"]["tts"] >= 1 and usage["jobs"]["rag"] == 1
    assert [e["type"] for e in await ov.db["call_events"].find({"call_id": call["_id"]}).to_list(80)].count("ai.usage") == 1
    with pytest.raises(websockets.ConnectionClosed):
        await asyncio.wait_for(ws.recv(), 3)
    types = [e["type"] for e in await ov.db["call_events"].find({"call_id": call["_id"]}).to_list(50)]
    assert "stream.connected" in types and "stream.ended" in types


async def test_outbound_stream_that_never_connects_gets_the_call_hung_up_once(ov):
    a = await answered_outbound(ov, "NEVER-1")
    await ov.db["calls"].update_one({"_id": a.call["_id"]}, {"$set": {"stream_expected_by": utcnow().replace(year=2020)}})
    await ov.ctrl.sweep_timeouts()
    await ov.ctrl.sweep_timeouts()
    assert ov.fake.hangups == [(a.call["_id"], "NORMAL_CLEARING")]


# =================================================================================== origination uncertainty
async def test_uncertain_plivo_origination_does_not_hold_capacity_forever(ov):
    ov.fake.uncertain = True
    r = await place(ov, ai_enabled=True)
    cid = r.json()["id"]

    async def uncertain():
        c = await ov.db["calls"].find_one({"_id": cid})
        return c if c and c.get("origination_uncertain") else None
    c = await until(uncertain)
    assert c and c["state"] == "initiating" and await ov.repos.count_active() == 1
    await ov.ctrl.sweep_timeouts()
    assert (await ov.db["calls"].find_one({"_id": cid}))["state"] == "initiating"         # too early: Plivo may still be ringing
    await ov.db["calls"].update_one({"_id": cid}, {"$set": {"initiated_at": utcnow().replace(year=2020)}})
    await ov.ctrl.sweep_timeouts()
    c = await ov.db["calls"].find_one({"_id": cid})
    assert c["state"] == "failed" and c["failure_reason"] == "origination_uncertain_no_channel"
    assert await ov.repos.count_active() == 0
    late = await post_answer(ov, {"CallUUID": "LATE-1", "RequestUUID": f"REQ-{cid}", "Direction": "outbound", "To": DEST.lstrip("+")})
    assert "Stream" not in late.text and "Hangup" in late.text                           # a late answer for the failed call is not served


# =================================================================================== security and lifecycle (phase 2)
async def test_clients_cannot_set_the_outbound_ai_marker_themselves(ov):
    r = await place(ov, ai_outbound=True)
    assert r.status_code == 422                                                           # CallCreate forbids unknown fields: only the server marks a call
    r = await place(ov)                                                                   # and an ordinary call never carries it
    assert "ai_outbound" not in (await ov.db["calls"].find_one({"_id": r.json()["id"]}))


@pytest.mark.parametrize("case", ["garbage", "tampered", "expired", "other_session", "other_tenant"])
async def test_outbound_ticket_variants_are_rejected_and_start_no_ai(ov, case):
    import hashlib
    import hmac
    import time
    from app.security import tickets as T
    a = await answered_outbound(ov, "TV-1")
    b = await answered_outbound(ov, "TV-2")
    msg = start_msg(a)
    if case == "garbage":
        msg["extra_headers"] = "mme_ticket=not-a-ticket"
    elif case == "tampered":
        body, sig = a.extra.split("=", 1)[1].split(".")
        msg["extra_headers"] = f"mme_ticket={body}.{'A' * len(sig)}"
    elif case == "expired":
        body = T._b64(json.dumps({"s": a.sid, "t": "t1", "c": a.uuid, "e": int(time.time()) - 5}, separators=(",", ":")).encode())
        msg["extra_headers"] = f"mme_ticket={body}.{T._b64(hmac.new(T._key(ov.s), body.encode(), hashlib.sha256).digest())}"
    elif case == "other_session":                                                         # a genuine ticket, but minted for call B's session
        msg["extra_headers"] = f"mme_ticket={T.mint(ov.s, session_id=b.sid, tenant_id='t1', call_id=a.uuid)}"
    elif case == "other_tenant":
        msg["extra_headers"] = f"mme_ticket={T.mint(ov.s, session_id=a.sid, tenant_id='t2', call_id=a.uuid)}"
    ws = await connect(ov, a, send_start=False)
    await ws.send(json.dumps(msg))
    with pytest.raises(websockets.ConnectionClosed) as ei:
        await asyncio.wait_for(ws.recv(), 4)
    assert ei.value.rcvd.code in (1003, 1008)
    assert await ov.db["ai_sessions"].count_documents({}) == 0
    assert (await ov.db["calls"].find_one({"_id": a.call["_id"]}))["state"] == "answered"


async def test_a_stream_after_the_call_ended_is_refused_and_a_late_answer_is_not_served(ov):
    a = await answered_outbound(ov, "END-1")
    done = {"CallUUID": "END-1", "CallStatus": "completed", "HangupCause": "NORMAL_CLEARING"}
    assert (await post_cb(ov, "hangup", done)).json()["status"] == "applied"
    ws = await connect(ov, a)                                                             # hang-up landed during AI start-up: the stream must not attach
    with pytest.raises(websockets.ConnectionClosed):
        await asyncio.wait_for(ws.recv(), 4)
    assert await ov.db["ai_sessions"].count_documents({}) == 0
    late = await post_answer(ov, answer_params(a.call, "END-1"))
    assert "Stream" not in late.text and "Hangup" in late.text


async def test_reordered_and_duplicate_callbacks_cannot_move_or_reattach_the_call(ov):
    a = await answered_outbound(ov, "ORD-9")
    late_ring = {"CallUUID": "ORD-9", "CallStatus": "ringing", "Event": "Ring"}
    assert (await post_cb(ov, "status", late_ring)).json()["status"] in ("out_of_order", "duplicate", "ignored")
    assert (await ov.db["calls"].find_one({"_id": a.call["_id"]}))["state"] == "answered"
    done = {"CallUUID": "ORD-9", "CallStatus": "completed", "HangupCause": "NORMAL_CLEARING"}
    first = await post_cb(ov, "hangup", done)
    assert first.json()["status"] == "applied"
    assert (await post_cb(ov, "hangup", done)).json()["status"] == "duplicate"           # a second hangup does nothing
    assert (await post_cb(ov, "status", {**done, "CallStatus": "ringing"})).json()["status"] in ("terminal_ignored", "ignored", "duplicate")
    types = [e["type"] for e in await ov.db["call_events"].find({"call_id": a.call["_id"]}).to_list(80)]
    assert types.count("state.completed") == 1
    assert await ov.repos.count_active() == 0                                             # capacity is derived from the state: released exactly once


async def test_replayed_signed_answer_gives_the_same_decision_and_no_second_session(ov):
    a = await answered_outbound(ov, "RPL-1")
    p = answer_params(a.call, "RPL-1")
    r1 = await post_answer(ov, p, nonce="fixed-nonce-1")
    r2 = await post_answer(ov, p, nonce="fixed-nonce-1")                                  # the very same signed request again
    assert "Stream" in r1.text and "Stream" in r2.text
    assert await ov.db["sessions"].count_documents({}) == 1 and await ov.db["calls"].count_documents({}) == 1


async def test_active_or_answered_call_is_never_failed_by_the_uncertain_origination_rule(ov):
    r = await place(ov, ai_enabled=True)
    call = await dispatched(ov, r.json()["id"])
    ans = await post_answer(ov, answer_params(call, "ACT-1"))
    assert "Stream" in ans.text
    await ov.db["calls"].update_one({"_id": call["_id"]}, {"$set": {"origination_uncertain": True, "initiated_at": utcnow().replace(year=2020)}})
    await ov.ctrl.sweep_timeouts()
    assert (await ov.db["calls"].find_one({"_id": call["_id"]}))["state"] == "answered"   # only calls still `initiating` are failed


async def test_hangup_during_a_turn_releases_the_session_and_the_stream_registry(ov):
    from app.ai import ws as ws_mod
    a = await answered_outbound(ov, "CUT-1")
    ws = await connect(ov, a)
    await until(lambda: _active(ov, a.sid))
    await send_media(ws, utterance())
    done = {"CallUUID": "CUT-1", "CallStatus": "completed", "HangupCause": "NORMAL_CLEARING"}
    await post_cb(ov, "hangup", done)
    ai = await until(lambda: _stopped(ov, a.sid))
    assert ai["stop_reason"] == "call_ended" and a.sid not in ov.ai.runtime

    async def released():
        return a.sid not in ws_mod._plivo_streaming
    assert await until(released)


# =================================================================================== safety controls (phase 3)
async def test_nothing_is_dialled_for_any_refused_outbound_ai_call(ov):
    refusals = []
    # tenant not opted in
    await replace_policy(ov, outbound_ai_destinations=[DEST])
    refusals.append((await place(ov, ai_enabled=True), "outbound_ai_disabled"))
    # opted in but the destination is not on the exact-number list (and an empty list allows nothing)
    await replace_policy(ov, **{**AI_POLICY, "outbound_ai_destinations": [OTHER]})
    refusals.append((await place(ov, ai_enabled=True), "outbound_ai_destination_not_allowed"))
    await replace_policy(ov, **{**AI_POLICY, "outbound_ai_destinations": []})
    refusals.append((await place(ov, ai_enabled=True), "outbound_ai_destination_not_allowed"))
    # quota 0 blocks
    await replace_policy(ov, **{**AI_POLICY, "outbound_ai_daily_quota": 0})
    refusals.append((await place(ov, ai_enabled=True), "outbound_ai_quota_exhausted"))
    # global pause
    await replace_policy(ov, **AI_POLICY)
    await ov.repos.set_outbound_ai_pause(True, "test", "drill")
    refusals.append((await place(ov, ai_enabled=True), "outbound_ai_paused"))
    for r, code in refusals:
        assert r.status_code in (403, 429) and r.json()["error"]["code"] == code, (code, r.text)
    await asyncio.sleep(0.4)                                                              # give the dispatcher every chance to act on anything queued
    assert ov.fake.originated == [] and await ov.db["calls"].count_documents({}) == 0
    assert await ov.db["idempotency"].count_documents({}) == 0 and await ov.repos.outbound_ai_quota_used("t1") == 0
    acts = await audit_actions(ov)
    assert acts.count("outbound_ai.rejected") == 4 and acts.count("outbound_ai.quota_exhausted") == 1


async def test_pause_blocks_only_outbound_ai_and_the_runtime_switch_needs_the_platform_role(ov):
    assert (await ov.c.put("/v1/admin/outbound-ai/pause", json={"paused": True}, headers=ADMIN())).status_code == 403
    r = await ov.c.put("/v1/admin/outbound-ai/pause", json={"paused": True, "reason": "drill"}, headers=PLATFORM())
    assert r.status_code == 200 and r.json()["paused"] is True
    g = await ov.c.get("/v1/admin/outbound-ai/pause", headers=ADMIN())
    assert g.json()["paused"] is True and g.json()["runtime_flag"]["reason"] == "drill"
    assert (await place(ov, ai_enabled=True)).status_code == 403
    assert (await place(ov)).status_code == 202                                           # ordinary outbound calls are not paused
    r = await ov.c.put("/v1/admin/outbound-ai/pause", json={"paused": False}, headers=PLATFORM())
    assert r.json()["paused"] is False and (await place(ov, ai_enabled=True)).status_code == 202
    acts = await audit_actions(ov)
    assert "outbound_ai.pause" in acts and "outbound_ai.resume" in acts


async def test_the_config_switch_also_pauses(ov):
    ov.s.outbound_ai_paused = True
    r = await place(ov, ai_enabled=True)
    assert r.status_code == 403 and r.json()["error"]["code"] == "outbound_ai_paused" and ov.fake.originated == []


async def test_daily_quota_is_enforced_and_idempotent_replays_do_not_consume_it(ov):
    await ov.repos.put_policy("t1", {"enabled": True, "caller_ids": [CALLER_ID], **{**AI_POLICY, "outbound_ai_daily_quota": 2}})
    key = {"Idempotency-Key": "same-key-1234"}
    body = {"to": DEST, "from_number": CALLER_ID, "ai_enabled": True}
    first = await ov.c.post("/v1/calls", json=body, headers={**USER(), **key})
    replay = await ov.c.post("/v1/calls", json=body, headers={**USER(), **key})
    assert first.status_code == 202 and replay.status_code == 200 and replay.json()["id"] == first.json()["id"]
    assert await ov.repos.outbound_ai_quota_used("t1") == 1
    assert (await place(ov, ai_enabled=True)).status_code == 202
    third = await place(ov, ai_enabled=True)
    assert third.status_code == 429 and third.json()["error"]["code"] == "outbound_ai_quota_exhausted"
    assert await ov.repos.outbound_ai_quota_used("t1") == 2 and await ov.db["calls"].count_documents({}) == 2
    assert (await place(ov)).status_code == 202                                           # ordinary calls do not use the AI quota


async def test_quota_holds_under_concurrent_requests(ov):
    await ov.repos.put_policy("t1", {"enabled": True, "caller_ids": [CALLER_ID], "max_active_calls": 50, **{**AI_POLICY, "outbound_ai_daily_quota": 3}})
    rs = await asyncio.gather(*[place(ov, ai_enabled=True) for _ in range(10)])
    codes = sorted(r.status_code for r in rs)
    assert codes == [202] * 3 + [429] * 7, codes
    assert await ov.repos.outbound_ai_quota_used("t1") == 3 and await ov.db["calls"].count_documents({}) == 3


async def test_a_reserved_quota_slot_is_given_back_when_the_call_cannot_be_stored(ov, monkeypatch):
    async def boom(doc):
        raise RuntimeError("storage down")
    monkeypatch.setattr(ov.repos, "insert_call", boom)
    r = await place(ov, ai_enabled=True)
    assert r.status_code == 500
    assert await ov.repos.outbound_ai_quota_used("t1") == 0 and await ov.db["idempotency"].count_documents({}) == 0 and ov.fake.originated == []


async def test_ai_call_duration_is_capped_by_service_policy_and_request(ov):
    r = await place(ov, ai_enabled=True, max_duration_s=900)                              # asks for more than the 300 s service default
    assert (await ov.db["calls"].find_one({"_id": r.json()["id"]}))["max_duration_s"] == 300
    await ov.repos.put_policy("t1", {"enabled": True, "caller_ids": [CALLER_ID], **{**AI_POLICY, "outbound_ai_max_duration_s": 120}})
    r = await place(ov, ai_enabled=True)
    assert (await ov.db["calls"].find_one({"_id": r.json()["id"]}))["max_duration_s"] == 120
    r = await place(ov, ai_enabled=True, max_duration_s=60)                               # a shorter request is honoured
    assert (await ov.db["calls"].find_one({"_id": r.json()["id"]}))["max_duration_s"] == 60
    r = await place(ov)                                                                   # an ordinary call keeps the ordinary cap
    assert (await ov.db["calls"].find_one({"_id": r.json()["id"]}))["max_duration_s"] == ov.s.max_call_duration_s


async def test_policy_api_normalises_the_exact_destination_list_and_defaults_to_closed(ov):
    r = await ov.c.put("/v1/admin/tenants/t1/policy", json={"enabled": True, "caller_ids": [CALLER_ID]}, headers=PLATFORM())
    assert r.status_code == 200 and r.json()["outbound_ai_enabled"] is False and r.json()["outbound_ai_destinations"] == []   # omitted = closed
    r = await ov.c.put("/v1/admin/tenants/t1/policy", headers=PLATFORM(),
                       json={"enabled": True, "caller_ids": [CALLER_ID], "outbound_ai_enabled": True, "outbound_ai_destinations": ["98765 43210"]})
    assert r.status_code == 200 and r.json()["outbound_ai_destinations"] == [DEST]
    bad = await ov.c.put("/v1/admin/tenants/t1/policy", headers=PLATFORM(), json={"enabled": True, "outbound_ai_destinations": ["+91*"]})
    assert bad.status_code == 422                                                         # wildcards are not E.164
