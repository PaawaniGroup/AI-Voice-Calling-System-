"""Plivo Voice-Application call handling: Answer, status/hangup and stream-status callbacks.

Everything here runs AFTER the route has verified Plivo's V3 signature. It reuses the existing durable lifecycle:
`Repos` (calls, events), `Controller.apply` (state machine, dedup events, webhooks, session sync, metrics),
tenant policy / inbound routes / capacity, and `AiService` (readiness). Nothing is bypassed to make a demo work.

Inbound flow (route action `ai`):
  Answer POST -> route + tenant policy + capacity + AI preflight -> durable call (unique per Plivo CallUUID) -> ANSWERED
  (session is created/linked by the controller) -> `<Stream bidirectional>` XML carrying a signed ticket -> Plivo opens the
  media WebSocket (app/ai/ws.py::handle_plivo_media) -> AiService.start/attach -> shared conversation loop.
Other route actions keep their existing behaviour: park -> `<Wait>`, bridge -> `<Dial>`, hangup -> `<Hangup>`.
Duplicate Answer callbacks (Plivo retries, races) return the same decision for the same call and never create a second call.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import timedelta
from xml.sax.saxutils import escape, quoteattr

from pymongo.errors import DuplicateKeyError

from app.core import phone
from app.core.errors import ApiError
from app.core.ids import new_id, utcnow
from app.observability import metrics as m
from app.providers import plivo as plivo_mod
from app.security import tickets
from app.telephony.state_machine import TERMINAL, S

log = logging.getLogger("mme_telephony.plivo_calls")

CALL_UUID = re.compile(r"^[0-9A-Za-z._-]{1,64}$")
_CALLER = re.compile(r"[^0-9A-Za-z+*#@._-]")
REJECT = '<Response><Hangup reason="rejected"/></Response>'
BUSY = '<Response><Hangup reason="busy"/></Response>'
ENDED_EVENTS = {"stopped", "failed", "completed", "hangup", "busy", "no-answer", "cancel"}


def _xml(body: str) -> str:
    return f"<Response>{body}</Response>"


def stream_xml(s, ticket: str, greeting: str | None = None) -> str:
    """Plivo `<Stream>`: bidirectional, keep the call alive after the stream, mu-law 8 kHz, signed ticket echoed back in `start`,
    stream lifecycle callbacks to our signed stream-status route. Body = the public wss:// URL (<= 2048 chars)."""
    wss = plivo_mod.stream_wss_url(s)
    status_cb = plivo_mod.callback_url(s, f"{s.api_prefix}/providers/plivo/stream-status")
    text = greeting if greeting is not None else getattr(s, "ai_greeting", "Welcome to MME AI Voice Calling Agent.")
    speak_part = f"<Speak>{escape(text)}</Speak>" if text else ""
    return _xml(
        f"{speak_part}"
        f'<Stream bidirectional="true" keepCallAlive="true" contentType="audio/x-mulaw;rate=8000" '
        f'extraHeaders={quoteattr("mme_ticket=" + ticket)} statusCallbackUrl={quoteattr(status_cb)} statusCallbackMethod="POST">'
        f"{escape(wss)}</Stream>")


def valid_call_uuid(v: str) -> bool:
    return bool(CALL_UUID.match(v or ""))


async def _by_provider_id(repos, uuid_: str, request_uuid: str | None):
    """Our call for this Plivo CallUUID (outbound calls are first known only by the REST RequestUUID)."""
    call = await repos.find_call_by_provider_id("plivo_api", uuid_)
    if call is None and request_uuid:
        call = await repos.find_call_by_provider_id("plivo_api", request_uuid)
        if call:
            await repos.update_call(call["_id"], {"provider_call_id": uuid_})
            call["provider_call_id"] = uuid_
    return call


async def _respond(app, call: dict) -> str:
    """XML for a call that exists (first answer or a duplicate callback): the decision is a pure function of the stored call."""
    s, repos = app.state.settings, app.state.repos
    if call["state"] in {x.value for x in TERMINAL}:
        return REJECT
    cap = min(call.get("max_duration_s", s.max_call_duration_s), 3600)
    if call.get("direction") != "inbound":  # calls we placed through the REST API keep their existing behaviour
        if call.get("bridge_to"):
            return _xml(f'<Dial timeLimit="{call.get("max_duration_s", s.max_call_duration_s)}"><Number>{call["bridge_to"].lstrip("+")}</Number></Dial>')
        return _xml(f'<Wait length="{cap}"/>')
    action, target = call.get("route_action", "park"), call.get("route_target")
    if action == "hangup":
        return _xml("<Hangup/>")
    if action == "bridge" and target:
        return _xml(f'<Dial timeLimit="{call.get("max_duration_s", s.max_call_duration_s)}"><Number>{target.lstrip("+")}</Number></Dial>')
    if action == "ai":
        sid = call.get("session_id")
        for _ in range(10):  # a duplicate Answer can race the first one's session creation: wait briefly, never hang up on it
            if sid:
                break
            await asyncio.sleep(0.1)
            sid = ((await repos.get_call(call["_id"])) or {}).get("session_id")
        if not sid:
            return REJECT
        call = {**call, "session_id": sid}
        if not call.get("stream_connected"):  # a late duplicate keeps the watchdog honest
            await repos.update_call(call["_id"], {"stream_expected_by": utcnow() + timedelta(seconds=s.plivo_stream_connect_s + 15),
                                                  "stream_watch_done": False})
        ticket = tickets.mint(s, session_id=sid, tenant_id=call["tenant_id"], call_id=call["provider_call_id"])
        return stream_xml(s, ticket, greeting=getattr(s, "ai_greeting", "Welcome to MME AI Voice Calling Agent."))
    return _xml(f'<Wait length="{cap}"/>')


async def handle_answer(app, params: dict[str, str], *, replayed: bool = False) -> str:
    """Process a signature-verified Answer callback. `replayed` = the same signed request (nonce) was already seen: only an
    existing call may be re-described, nothing new is created."""
    s, repos, ctrl, ai = app.state.settings, app.state.repos, app.state.controller, app.state.ai
    uuid_ = params.get("CallUUID", "")
    if not valid_call_uuid(uuid_):
        m.PLIVO_CALLBACKS.labels("answer", "invalid").inc()
        raise ApiError("missing or invalid CallUUID", code="invalid_payload", status=400)

    call = await _by_provider_id(repos, uuid_, params.get("RequestUUID"))
    if call is not None:
        # Known call: either one we placed (this is its first Answer) or a duplicate/retry. ANSWERED is idempotent (dedup id).
        if call["state"] not in {x.value for x in TERMINAL}:
            await ctrl.apply(call, S.ANSWERED, source="plivo", dedup_id=f"plivo:{uuid_}:answer")
            call = await repos.get_call(call["_id"]) or call
        m.PLIVO_CALLBACKS.labels("answer", "duplicate" if call.get("direction") == "inbound" else "outbound").inc()
        return await _respond(app, call)
    if replayed or (params.get("Direction") or "").lower() != "inbound":
        m.PLIVO_CALLBACKS.labels("answer", "ignored").inc()
        return REJECT  # unknown outbound call, or a replay with nothing to describe

    if getattr(app.state, "draining", False):  # shutting down: do not take a call we cannot serve
        m.PLIVO_CALLBACKS.labels("answer", "draining").inc()
        return BUSY

    # ---- inbound: tenant routing and policy (existing rules, unchanged)
    try:
        to = phone.normalize("+" + params.get("To", "").lstrip("+"))
    except ApiError:
        m.PLIVO_CALLBACKS.labels("answer", "rejected_number").inc()
        return REJECT
    route = await repos.get_route(to)
    pol = await repos.get_policy(route["tenant_id"]) if route else None
    if not route or (pol or {}).get("enabled") is not True:
        m.PLIVO_CALLBACKS.labels("answer", "rejected_route").inc()
        return REJECT
    tenant = route["tenant_id"]
    allowed = (pol or {}).get("allowed_callers") or []
    if allowed:  # opt-in caller allowlist (controlled test calls): only these numbers reach the AI, others are rejected before a call exists
        caller = phone.normalize_caller(params.get("From"), s.default_region)   # +digits first; national 0XXXXXXXXXX only as a fallback
        if caller is None or caller not in allowed:
            m.PLIVO_CALLBACKS.labels("answer", "rejected_caller").inc()
            log.warning("plivo inbound call rejected: caller is not in the tenant's allowed_callers")
            return REJECT
    if await repos.count_active() >= ctrl.max_active or await repos.count_active(tenant) >= (pol or {}).get("max_active_calls", s.max_active_per_tenant):
        m.PLIVO_CALLBACKS.labels("answer", "busy").inc()
        return BUSY

    action = route.get("action", "park")
    language, ai_ok, ai_why = "auto", False, ""
    if action == "ai":
        ai_ok, ai_why = await ai.preflight(tenant, bridge=_plivo_bridge(s))
        if ai_ok:
            language = (await ai.get_config(tenant)).get("default_language", "auto")
    now = utcnow()
    call = {
        "_id": new_id("call"), "tenant_id": tenant, "direction": "inbound", "provider": "plivo_api", "provider_call_id": uuid_,
        "from_number": _CALLER.sub("", params.get("From", ""))[:20], "to_number": to, "state": S.INITIATING.value,
        "initiated_at": now, "correlation_id": new_id("cor"), "max_duration_s": min((pol or {}).get("max_duration_s", s.max_call_duration_s), s.max_call_duration_s),
        "route_action": action, "route_target": route.get("target"), "ai_enabled": action == "ai" and ai_ok, "language": language,
    }
    if call["ai_enabled"]:
        call["stream_expected_by"] = now + timedelta(seconds=s.plivo_stream_connect_s + 15)
    try:
        await repos.insert_call(call)
    except DuplicateKeyError:  # a concurrent duplicate Answer won the race: describe THAT call
        m.PLIVO_CALLBACKS.labels("answer", "duplicate").inc()
        existing = await repos.find_call_by_provider_id("plivo_api", uuid_)
        return await _respond(app, existing) if existing else REJECT
    m.CALLS_CREATED.labels("inbound").inc()
    m.PLIVO_CALLBACKS.labels("answer", "accepted").inc()

    if action == "ai" and not ai_ok:
        # Recorded, never silently dropped: the call fails with a visible reason and the caller is rejected, not left on a dead stream.
        log.warning("plivo inbound call rejected: AI not ready (%s)", ai_why)
        await ctrl.apply(call, S.FAILED, source="plivo", dedup_id=f"plivo:{uuid_}:ai-unavailable",
                         fields={"failure_reason": f"ai_unavailable:{ai_why}"[:80]})
        return REJECT

    await ctrl.apply(call, S.ANSWERED, source="plivo", dedup_id=f"plivo:{uuid_}:answer")  # also creates/links the session
    call = await repos.get_call(call["_id"]) or call
    if call.get("ai_enabled"):
        asyncio.create_task(ai.warmup(tenant))
    return await _respond(app, call)


def _plivo_bridge(s):
    from app.ai.media import PlivoStreamBridge
    return PlivoStreamBridge(s)


async def handle_status(app, params: dict[str, str]) -> dict:
    """Ring/answer/hangup callbacks (the Application's Answer, Ring and Hangup URLs share this). Unknown calls are
    acknowledged and ignored so Plivo does not retry forever."""
    repos, ctrl = app.state.repos, app.state.controller
    uuid_ = params.get("CallUUID", "")
    if not valid_call_uuid(uuid_):
        raise ApiError("missing or invalid CallUUID", code="invalid_payload", status=400)
    call = await _by_provider_id(repos, uuid_, params.get("RequestUUID"))
    if not call:
        return {"status": "ignored"}
    mapped = plivo_mod.map_status(params, bool(call.get("answered_at")))
    if not mapped:
        return {"status": "ignored"}
    target, reason = mapped
    key = f"plivo:{uuid_}:{params.get('CallStatus', '')}:{params.get('Event', '')}"
    fields = {}
    if target in TERMINAL:
        fields = {"hangup_cause": params.get("HangupCause", "")[:60], "failure_reason": reason if target != S.COMPLETED else None}
    outcome, _ = await ctrl.apply(call, target, source="plivo", dedup_id=key, fields=fields, payload={"status": params.get("CallStatus")})
    return {"status": outcome}


async def handle_stream_status(app, params: dict[str, str]) -> dict:
    """`statusCallbackUrl` of the Stream element: Event in started | stopped | failed (+ StatusReason).
    A stream event is NOT a call event: it never changes the call state, but a failed stream ends the AI conversation and
    asks Plivo to hang the call up (the final state still comes from the Hangup callback)."""
    repos, ctrl, ai = app.state.repos, app.state.controller, app.state.ai
    uuid_ = params.get("CallUUID", "")
    if not valid_call_uuid(uuid_):
        raise ApiError("missing or invalid CallUUID", code="invalid_payload", status=400)
    event = (params.get("Event") or "").lower()
    if event not in ("started", "stopped", "failed"):
        return {"status": "ignored"}
    call = await repos.find_call_by_provider_id("plivo_api", uuid_)
    if not call:
        return {"status": "ignored"}
    stream_id = _CALLER.sub("", params.get("StreamID", ""))[:64]
    fresh = await repos.add_event(call, f"stream.{event}", "plivo", f"plivo-stream:{stream_id}:{event}",
                                  {"reason": _CALLER.sub("", params.get("StatusReason", ""))[:60]})
    if not fresh:
        return {"status": "duplicate"}
    if event == "failed":
        m.PLIVO_STREAMS.labels("provider_failed").inc()
        sid = call.get("session_id")
        if sid:
            try:
                await ai.stop(call["tenant_id"], sid, "stream_failed")
            except ApiError:
                pass
        if call["state"] not in {x.value for x in TERMINAL}:
            try:
                await ctrl.hangup(call, source="plivo")
            except Exception as e:
                log.warning("hangup after failed stream not executed: %s", type(e).__name__)
    return {"status": "recorded"}

