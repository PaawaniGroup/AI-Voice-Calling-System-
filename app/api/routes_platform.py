"""Sessions, auth/users, system status, dashboard data, SSE stream, AI contracts, logs."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request, WebSocket
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.ai.ws import handle_media
from app.api.schemas import call_out
from app.core.errors import Forbidden, NotFound, RateLimited, Unauthorized, Unprocessable
from app.core.ids import utcnow
from app.core.logging import mask_phone
from app.observability import metrics as m
from app.security import users as userlib
from app.security.auth import Principal, require_admin, require_user, require_viewer
from app.telephony import state_machine as sm

router = APIRouter(prefix="/v1")
ws_router = APIRouter()


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _can_see_numbers(request: Request, p: Principal) -> bool:
    s = request.app.state.settings
    return any(p.has(r) for r in (s.role_user, s.role_admin, s.role_platform))


def session_out(sess: dict, full: bool) -> dict:
    mk = (lambda n: n) if full else (lambda n: mask_phone(n) if n else n)
    return {"session_id": sess["_id"], "tenant_id": sess["tenant_id"], "call_id": sess.get("call_id"),
            "caller": mk(sess.get("caller")), "destination": mk(sess.get("destination")), "language": sess.get("language"),
            "status": sess["status"], "created_at": sess["created_at"], "answered_at": sess.get("answered_at"),
            "ended_at": sess.get("ended_at"), "end_reason": sess.get("end_reason"), "source": sess.get("source"),
            "metadata": sess.get("metadata") or {}, "history": sess.get("history", [])[-20:]}


# ================================================================== auth
class LoginIn(Strict):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


@router.post("/auth/login")
async def login(request: Request, body: LoginIn):
    s = request.app.state.settings
    if not s.login_enabled:
        raise NotFound("login disabled")
    ip = request.client.host if request.client else "?"
    request.app.state.limiter.check(f"login:{ip}", s.login_rate_limit_per_min)
    try:
        user = await userlib.authenticate(request.app.state.db, body.username, body.password)
    except Unauthorized:
        m.LOGINS.labels("failed").inc()
        await request.app.state.repos.audit(None, body.username[:64], "auth.login_failed", "-", {"ip": ip}, request.state.request_id)
        raise
    m.LOGINS.labels("ok").inc()
    token, exp = userlib.issue_token(s, user)
    await request.app.state.repos.audit(user["tenant_id"], user["username"], "auth.login", "-", {}, request.state.request_id)
    return {"access_token": token, "token_type": "bearer", "expires_at": exp,
            "user": {"username": user["username"], "tenant_id": user["tenant_id"], "role": user["role"]}}


@router.get("/auth/me")
async def me(p: Principal = Depends(require_viewer)):
    return {"sub": p.sub, "tenant_id": p.tenant_id, "roles": sorted(p.roles)}


class UserIn(Strict):
    username: str
    password: str = Field(min_length=12, max_length=256)
    role: Literal["viewer", "operator", "admin"] = "viewer"
    tenant_id: str | None = None


@router.post("/admin/users", status_code=201)
async def create_user(request: Request, body: UserIn, p: Principal = Depends(require_admin)):
    s = request.app.state.settings
    tenant = body.tenant_id or p.tenant_id
    if tenant != p.tenant_id and not p.has(s.role_platform):
        raise Forbidden("cannot create users in another tenant")
    u = await userlib.create_user(request.app.state.db, body.username, body.password, tenant, body.role)
    await request.app.state.repos.audit(tenant, p.sub, "user.create", u["username"], {"role": body.role}, request.state.request_id)
    return {"username": u["username"], "tenant_id": tenant, "role": body.role}


@router.get("/admin/users")
async def list_users(request: Request, p: Principal = Depends(require_admin)):
    rows = await request.app.state.db["users"].find({"tenant_id": p.tenant_id}).to_list(500)
    return {"items": [{"username": r["username"], "role": r["role"], "disabled": r.get("disabled", False),
                       "last_login_at": r.get("last_login_at")} for r in rows]}


@router.post("/admin/users/{username}/disable")
async def disable_user(request: Request, username: str, p: Principal = Depends(require_admin)):
    r = await request.app.state.db["users"].update_one({"username": username.lower(), "tenant_id": p.tenant_id}, {"$set": {"disabled": True}})
    if not r.matched_count:
        raise NotFound("user not found")
    await request.app.state.repos.audit(p.tenant_id, p.sub, "user.disable", username, {}, request.state.request_id)
    return {"username": username, "disabled": True}


# ================================================================== sessions
class SessionCreate(Strict):
    tenant_id: str = Field(min_length=1, max_length=100)
    caller_id: str | None = Field(None, max_length=50)
    call_id: str | None = Field(None, max_length=150)
    language: str = Field("auto", max_length=30)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionEnd(Strict):
    reason: str = Field("completed", max_length=100)


@router.post("/sessions", status_code=201)
async def create_session(request: Request, body: SessionCreate, p: Principal = Depends(require_user)):
    if body.tenant_id != p.tenant_id:
        raise Forbidden("tenant_id does not match the authenticated tenant", code="tenant_mismatch")
    if len(json.dumps(body.metadata, default=str)) > 4000:
        raise Unprocessable("metadata too large", code="metadata_too_large")
    svc = request.app.state.sessions
    call = await request.app.state.repos.get_call(body.call_id, p.tenant_id) if body.call_id else None
    if call is None and body.call_id:  # the reference may be a FreeSWITCH UUID of a call we already track
        call = await request.app.state.db["calls"].find_one({"tenant_id": p.tenant_id, "fs_uuid": body.call_id})
    sess, created = await svc.create(p.tenant_id, call_ref=body.call_id, caller=body.caller_id or (call or {}).get("from_number"),
                                     destination=(call or {}).get("to_number"), language=body.language, metadata=body.metadata,
                                     source="external", call_id=call["_id"] if call else None)
    if created:
        await request.app.state.repos.audit(p.tenant_id, p.sub, "session.create", sess["_id"], {}, request.state.request_id)
    return session_out(sess, True)


@router.get("/sessions")
async def list_sessions(request: Request, status: str | None = None, q: str | None = Query(None, max_length=60),
                        limit: int = Query(50, ge=1, le=200), cursor: str | None = None, p: Principal = Depends(require_viewer)):
    try:
        rows, nxt = await request.app.state.sessions.list(p.tenant_id, status=status, q=q, limit=limit, cursor=cursor)
    except ValueError:
        raise Unprocessable("invalid cursor", code="invalid_cursor") from None
    full = _can_see_numbers(request, p)
    return {"items": [session_out(r, full) for r in rows], "next_cursor": nxt}


@router.get("/sessions/{session_id}")
async def get_session(request: Request, session_id: str, p: Principal = Depends(require_viewer)):
    return session_out(await request.app.state.sessions.get(p.tenant_id, session_id), _can_see_numbers(request, p))


@router.post("/sessions/{session_id}/answer")
async def answer_session(request: Request, session_id: str, p: Principal = Depends(require_user)):
    s = await request.app.state.sessions.answer(p.tenant_id, session_id)
    await request.app.state.repos.audit(p.tenant_id, p.sub, "session.answer", session_id, {}, request.state.request_id)
    return session_out(s, True)


@router.post("/sessions/{session_id}/end")
async def end_session(request: Request, session_id: str, body: SessionEnd, p: Principal = Depends(require_user)):
    s = await request.app.state.sessions.end(p.tenant_id, session_id, body.reason)
    await request.app.state.repos.audit(p.tenant_id, p.sub, "session.end", session_id, {"reason": body.reason}, request.state.request_id)
    return session_out(s, True)


@router.get("/calls/{call_id}/session")
async def call_session(request: Request, call_id: str, p: Principal = Depends(require_viewer)):
    sess = await request.app.state.sessions.get_for_call(p.tenant_id, call_id)
    if not sess:
        raise NotFound("no session for this call")
    return session_out(sess, _can_see_numbers(request, p))


# ================================================================== transcripts / recording / summary
@router.get("/calls/{call_id}/transcript")
async def transcript(request: Request, call_id: str, p: Principal = Depends(require_user)):
    if not await request.app.state.repos.get_call(call_id, p.tenant_id):
        raise NotFound("call not found")
    raise NotFound("no transcript is stored for this call: transcripts are produced only by an AI session, "
                   "and persistence of transcripts is not implemented (consent/retention policy pending)", code="transcript_not_available")


@router.get("/calls/{call_id}/summary")
async def summary(request: Request, call_id: str, p: Principal = Depends(require_user)):
    if not await request.app.state.repos.get_call(call_id, p.tenant_id):
        raise NotFound("call not found")
    raise NotFound("no summary exists for this call (AI summarisation is not implemented)", code="summary_not_available")


@router.get("/recordings")
async def list_recordings(request: Request, limit: int = Query(100, ge=1, le=200), p: Principal = Depends(require_viewer)):
    rows = await request.app.state.db["recordings"].find({"tenant_id": p.tenant_id}).sort("created_at", -1).limit(limit).to_list(limit)
    return {"items": [{"id": r["_id"], "call_id": r["call_id"], "status": r["status"], "duration_s": r.get("duration_s"),
                       "size_bytes": r.get("size_bytes"), "created_at": r["created_at"], "retention_until": r.get("retention_until"),
                       "consent_mode": r.get("consent_mode"), "deleted": bool(r.get("deleted"))} for r in rows]}


@router.get("/calls/{call_id}/recording")
async def recording(request: Request, call_id: str, p: Principal = Depends(require_user)):
    if not await request.app.state.repos.get_call(call_id, p.tenant_id):
        raise NotFound("call not found")
    rows = await request.app.state.db["recordings"].find({"call_id": call_id, "tenant_id": p.tenant_id}).sort("created_at", -1).to_list(20)
    if not rows:
        raise NotFound("no recording for this call (recording is disabled unless enabled globally, by tenant policy and with storage)",
                       code="recording_not_available")
    return {"items": [{"id": r["_id"], "status": r["status"], "duration_s": r.get("duration_s"), "size_bytes": r.get("size_bytes"),
                       "retention_until": r.get("retention_until"), "download": f"/v1/recordings/{r['_id']}/download-url"} for r in rows]}


# ================================================================== system status / dashboard
async def collect_health(app) -> dict:
    """Live checks (never hardcoded). `last_ok_at` records the last time each component was seen healthy."""
    st = app.state
    s, now = st.settings, time.time()
    ok_at = st.last_ok
    comp: dict[str, dict] = {}
    try:
        ms = await st.db.ping()
        ok_at["database"] = now
        comp["database"] = {"status": "up", "latency_ms": round(ms, 1)}
    except Exception:
        comp["database"] = {"status": "down"}
    ev, cmd = st.controller.esl_events, st.controller.esl_cmd
    if s.fs_enabled and s.provider == "freeswitch_gateway":
        fresh = bool(ev and ev.connected and ev.last_event_at and now - ev.last_event_at <= s.ready_max_event_age_s)
        comp["freeswitch_events"] = {"status": "up" if fresh else "down", "reconnects": ev.reconnect_count if ev else None,
                                     "queue_depth": ev.queue.qsize() if ev else None, "dropped": ev.events_dropped if ev else None,
                                     "last_event_age_s": round(now - ev.last_event_at, 1) if ev and ev.last_event_at else None}
        comp["freeswitch_commands"] = {"status": "up" if cmd and cmd.connected else "idle_or_down",
                                       "failures": cmd.command_failures if cmd else None}
        if fresh:
            ok_at["freeswitch"] = now
    else:
        comp["freeswitch_events"] = {"status": "not_applicable", "provider": s.provider}
    ai = st.ai
    ready, reason = await ai.bridge.ready()
    comp["ai"] = {"status": "enabled" if s.ai_enabled else "disabled", "media_bridge": {"name": ai.bridge.name, "ready": ready, "reason": reason},
                  "last_service_check": ai.last_health}
    comp["workers"] = {"controller_loops": len(st.controller._tasks), "webhook_worker": bool(st.webhook_worker._task and not st.webhook_worker._task.done())}
    comp["checked_at"] = utcnow().isoformat()
    from datetime import datetime, timezone
    comp["last_ok_at"] = {k: datetime.fromtimestamp(v, tz=timezone.utc).isoformat() for k, v in ok_at.items()}
    return comp


@router.get("/system/status")
async def system_status(request: Request, p: Principal = Depends(require_viewer)):
    s = request.app.state.settings
    return {"service": s.app_name, "version": s.app_version, "env": s.env, "provider": s.provider,
            "components": await collect_health(request.app),
            "limits": {"max_active_global": s.max_active_global, "max_queued": s.max_queued,
                       "provider_max_concurrent": s.provider_max_concurrent, "provider_max_cps": s.provider_max_cps},
            "real_calls_enabled": s.real_calls_enabled, "recording_enabled": s.recording_enabled}


@router.get("/system/queues")
async def system_queues(request: Request, p: Principal = Depends(require_viewer)):
    db, repos = request.app.state.db, request.app.state.repos
    deliveries: dict[str, int] = {}
    async for r in await db["webhook_deliveries"].aggregate([{"$match": {"tenant_id": p.tenant_id}}, {"$group": {"_id": "$status", "n": {"$sum": 1}}}]):
        deliveries[r["_id"]] = r["n"]
    return {"queued_calls": await repos.count_queued(), "active_calls": await repos.count_active(),
            "uncertain_originations": await db["calls"].count_documents({"tenant_id": p.tenant_id, "origination_uncertain": True,
                                                                         "state": {"$in": ["initiating"]}}),
            "webhook_deliveries": deliveries, "esl_event_queue": (request.app.state.controller.esl_events.queue.qsize()
                                                                 if request.app.state.controller.esl_events else None)}


@router.get("/dashboard/overview")
async def overview(request: Request, p: Principal = Depends(require_viewer)):
    db, repos = request.app.state.db, request.app.state.repos
    start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    by_state: dict[str, int] = {}
    dur_sum = dur_n = 0.0
    async for r in await db["calls"].aggregate([
            {"$match": {"tenant_id": p.tenant_id, "created_at": {"$gte": start}}},
            {"$group": {"_id": "$state", "n": {"$sum": 1}, "dur": {"$sum": {"$ifNull": ["$connected_duration_s", 0]}},
                        "dn": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$connected_duration_s", 0]}, 0]}, 1, 0]}}}}]):
        by_state[r["_id"]] = r["n"]
        dur_sum += r["dur"]
        dur_n += r["dn"]
    active_sessions = await db["sessions"].count_documents({"tenant_id": p.tenant_id, "status": {"$in": ["answered", "active"]}})
    ai_active = await db["ai_sessions"].count_documents({"tenant_id": p.tenant_id, "state": "active"})
    errs = [e for e in request.app.state.errlog.snapshot(20)]
    return {"as_of": utcnow().isoformat(), "since": start.isoformat(), "health": await collect_health(request.app),
            "calls_today": sum(by_state.values()), "by_state": by_state,
            "answered_today": sum(by_state.get(k, 0) for k in ("answered", "bridged", "transferring", "completed")),
            "missed_today": by_state.get("no_answer", 0) + by_state.get("busy", 0),
            "failed_today": by_state.get("failed", 0), "completed_today": by_state.get("completed", 0),
            "avg_connected_seconds": round(dur_sum / dur_n, 1) if dur_n else None,
            "active_calls": await repos.count_active(p.tenant_id), "active_sessions": active_sessions, "active_ai_sessions": ai_active,
            "recent_errors": errs if p.has(request.app.state.settings.role_admin) or p.has(request.app.state.settings.role_platform) else []}


@router.get("/logs/errors")
async def error_logs(request: Request, level: str | None = None, q: str | None = Query(None, max_length=60),
                     limit: int = Query(100, ge=1, le=500), p: Principal = Depends(require_admin)):
    items = request.app.state.errlog.snapshot(limit, level=level, q=q)
    return {"items": items, "note": "in-memory ring of this process; redacted; resets on restart"}


# ================================================================== SSE live stream
_sse_counts: dict[str, int] = {}


@router.get("/stream/events")
async def stream(request: Request, p: Principal = Depends(require_viewer)):
    """Server-Sent Events: a snapshot of live calls, pushed when it changes (poll of MongoDB every second).
    Use fetch() with the Authorization header (EventSource cannot set headers). Max 5 streams per tenant."""
    if _sse_counts.get(p.tenant_id, 0) >= 5:
        raise RateLimited("too many live streams for this tenant", code="too_many_streams")
    db = request.app.state.db
    full = _can_see_numbers(request, p)

    async def gen():
        _sse_counts[p.tenant_id] = _sse_counts.get(p.tenant_id, 0) + 1
        last, last_beat, t_end = None, 0.0, time.time() + 3600
        try:
            yield "retry: 3000\n\n"
            while time.time() < t_end and not await request.is_disconnected():
                cutoff = utcnow() - timedelta(seconds=120)
                rows = await db["calls"].find({"tenant_id": p.tenant_id, "$or": [
                    {"state": {"$in": [x.value for x in (sm.ACTIVE | {sm.S.QUEUED})]}}, {"updated_at": {"$gte": cutoff}}]}) \
                    .sort("created_at", -1).limit(200).to_list(200)
                snap = json.dumps([call_out(r, mask=not full) for r in rows], default=str, sort_keys=True)
                h = hashlib.sha1(snap.encode()).hexdigest()
                if h != last:
                    last = h
                    yield f"event: calls\ndata: {snap}\n\n"
                elif time.time() - last_beat > 15:
                    last_beat = time.time()
                    yield ": keep-alive\n\n"
                await asyncio.sleep(1.0)
        finally:
            _sse_counts[p.tenant_id] = max(0, _sse_counts.get(p.tenant_id, 1) - 1)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# ================================================================== AI contracts
class AiConfigIn(Strict):
    enabled: bool | None = None
    stt_url: str | None = Field(None, max_length=300)
    brain_url: str | None = Field(None, max_length=300)
    tts_url: str | None = Field(None, max_length=300)
    rag_url: str | None = Field(None, max_length=300)
    default_language: str | None = None
    languages: list[str] | None = None
    models: dict[str, str] | None = None
    timeouts: dict[str, float] | None = None
    paths: dict[str, str] | None = None
    devanagari_hindi: bool | None = None   # Devanagari replies for Hindi/Hinglish callers (prompt + reply_mode tag + reply check/retry)
    runpod_mode: Literal["run", "runsync"] | None = None
    streaming_pipeline: bool | None = None   # token-streaming voice pipeline (Plivo calls); default OFF per tenant
    full_duplex_input: bool | None = None    # listen while the assistant speaks (barge-in minimum, echo guard, kept speech); default OFF per tenant
    duplex_min_voiced_ms: int | None = Field(None, ge=100, le=1000)
    duplex_dropout_ms: int | None = Field(None, ge=0, le=500)
    duplex_echo_ratio: float | None = Field(None, ge=0.0, le=2.0)
    duplex_resume_ms: int | None = Field(None, ge=0, le=300)
    duplex_pre_roll_ms: int | None = Field(None, ge=0, le=1000)
    tts_ulaw8k: bool | None = None           # RunPod TTS worker v3: voice by name + 8 kHz mu-law answer, no reference sent; default OFF per tenant
    tts_voice: str | None = Field(None, max_length=40)   # the worker's voice name (validated in the service: letters, digits, _ and -)
    stream_first_min_words: int | None = Field(None, ge=1, le=8)
    stream_min_words: int | None = Field(None, ge=1, le=20)
    stream_max_words: int | None = Field(None, ge=6, le=40)
    stream_prefetch_depth: int | None = Field(None, ge=1, le=4)
    stream_prebuffer_ms: int | None = Field(None, ge=0, le=400)
    runpod_poll_cap_s: float | None = Field(None, ge=0.03, le=1.0)
    glossary_correction: bool | None = None  # phonetic normalisation of the caller's text before the Brain (quality-work); default OFF per tenant
    strict_grounding: bool | None = None    # strict grounding rules appended to the system prompt (quality-work); default OFF per tenant
    ack_enabled: bool | None = None          # cached neutral acknowledgement after the speech-to-text final (needs MME_TEL_AI_ACK_DIR); default OFF per tenant
    ack_after_ms: int | None = Field(None, ge=0, le=3000)   # wait after the speech-to-text final before it plays (default 1500)
    stream_ramp: bool | None = None          # clause-size ramp (short first clause, Brain opening rule); needs streaming_pipeline; default OFF per tenant
    tts_adaptive_poll: bool | None = None    # voice jobs: overlapping status polls from the predicted end (saves at most ~0.15 s per job, ESTIMATE); default OFF per tenant
    rt_transport: dict[str, str] | None = None     # per service (stt, tts, brain): "queue" (default) or "persistent" (always-on server over a standing connection); default: all queue
    rt_endpoints: dict[str, str] | None = None     # wss:// URL of the STT and voice servers, https:// root of the Brain server (host allowlist as for the queue URLs)
    rt_pins: dict[str, str] | None = None          # service -> SHA-256 (hex) of the server certificate to trust (a self-signed certificate on a Pod's TCP port)
    rt_secret_env: str | None = Field(None, max_length=64)   # NAME of the environment variable holding the signing secret (never the secret itself)
    stt_raw_pcm: bool | None = None         # send the caller's audio to the STT worker as ONE raw PCM16 field + sample rate (needs a worker that reads audio_pcm16_b64); default OFF per tenant
    stream_ramp_first_min_words: int | None = Field(None, ge=1, le=4)
    stream_first_max_words: int | None = Field(None, ge=2, le=8)
    stream_second_min_words: int | None = Field(None, ge=2, le=20)
    stream_second_max_words: int | None = Field(None, ge=4, le=24)
    trace_evidence: bool | None = None      # numbers-only turn evidence line per turn (also needs MME_TEL_AI_TRACE_EVIDENCE_PATH); default OFF per tenant


@router.get("/ai/config")
async def get_ai_config(request: Request, p: Principal = Depends(require_viewer)):
    ai = request.app.state.ai
    detail = p.has(request.app.state.settings.role_admin) or p.has(request.app.state.settings.role_platform)
    return ai.public_config(await ai.get_config(p.tenant_id), detail)


@router.put("/ai/config")
async def put_ai_config(request: Request, body: AiConfigIn, p: Principal = Depends(require_admin)):
    ai = request.app.state.ai
    cfg = await ai.put_config(p.tenant_id, body.model_dump(exclude_none=True), p.sub)
    await request.app.state.repos.audit(p.tenant_id, p.sub, "ai.config.put", p.tenant_id,
                                        {"fields": sorted(body.model_dump(exclude_none=True))}, request.state.request_id)
    return ai.public_config(cfg, True)


@router.get("/ai/health")
async def ai_health(request: Request, p: Principal = Depends(require_viewer)):
    return await request.app.state.ai.health(p.tenant_id)


class AiStart(Strict):
    language: str | None = None


@router.post("/ai/sessions/{session_id}/start", status_code=202)
async def ai_start(request: Request, session_id: str, body: AiStart | None = None, p: Principal = Depends(require_user)):
    out = await request.app.state.ai.start(p.tenant_id, session_id, body.language if body else None)
    await request.app.state.repos.audit(p.tenant_id, p.sub, "ai.start", session_id, {}, request.state.request_id)
    return {"session_id": session_id, "state": out["state"], "note": "state becomes 'active' only when the media client connects"}


@router.post("/ai/sessions/{session_id}/stop")
async def ai_stop(request: Request, session_id: str, p: Principal = Depends(require_user)):
    out = await request.app.state.ai.stop(p.tenant_id, session_id)
    await request.app.state.repos.audit(p.tenant_id, p.sub, "ai.stop", session_id, {}, request.state.request_id)
    return {"session_id": session_id, "state": out["state"], "stop_reason": out.get("stop_reason")}


@router.get("/ai/sessions/{session_id}")
async def ai_get(request: Request, session_id: str, p: Principal = Depends(require_viewer)):
    detail = p.has(request.app.state.settings.role_admin) or p.has(request.app.state.settings.role_platform)
    return await request.app.state.ai.get(p.tenant_id, session_id, detail)


@ws_router.websocket("/v1/ai/media/fs")
async def ai_media_fs(ws: WebSocket):
    from app.ai.ws import handle_fs_media
    await handle_fs_media(ws, ws.app)


@ws_router.websocket("/v1/ai/sessions/{session_id}/media")
async def ai_media(ws: WebSocket, session_id: str):
    await handle_media(ws, ws.app, session_id)


@ws_router.websocket("/v1/providers/plivo/stream")
async def plivo_media(ws: WebSocket):
    """Plivo bidirectional <Stream>: authenticated by Plivo's handshake signature + the signed ticket in the start event."""
    from app.ai.ws import handle_plivo_media
    await handle_plivo_media(ws, ws.app)
