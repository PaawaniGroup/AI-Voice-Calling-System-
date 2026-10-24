"""Live operations API for the dashboard: one stream + one snapshot that show every flow as it happens.

  GET /v1/live/snapshot            current picture (components, gauges, active/recent call flows)         viewer
  GET /v1/live/activity?after=N    polling fallback: activity entries newer than seq N                    viewer
  GET /v1/live/stream              Server-Sent Events: hello, backlog, activity (as it happens), snapshot (every 2 s)
  GET /v1/live/calls/{id}/flow     the pipeline stages of one call                                         viewer

Everything is tenant-scoped; phone numbers are masked for read-only viewers (as elsewhere) and always masked inside activity
entries; what callers say is never part of any of these payloads (AI turns carry stage timings only).
"""
from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from app.api.routes_platform import _can_see_numbers, collect_health
from app.api.schemas import call_out
from app.core.errors import NotFound, RateLimited
from app.core.ids import utcnow
from app.observability import metrics as m
from app.security.auth import Principal, require_viewer
from app.services.flow import flow_stages
from app.telephony import state_machine as sm

router = APIRouter(prefix="/v1/live")

_streams: dict[str, int] = {}
MAX_STREAMS_PER_TENANT = 5
MODEL_CHECK_EVERY_S = 30.0
SNAPSHOT_EVERY_S = 2.0


# ---------------------------------------------------------------------------------------------- process metrics (admin only)
def _samples(name: str):
    for fam in m.REGISTRY.collect():
        for s in fam.samples:
            if s.name == name:
                yield s


def counter_total(name: str) -> float:
    return float(sum(s.value for s in _samples(name)))


def hist_quantile(base: str, q: float) -> float | None:
    """Quantile estimate from a Prometheus histogram's cumulative buckets (linear inside the bucket), None without observations."""
    buckets = sorted(((float(s.labels["le"]), s.value) for s in _samples(base + "_bucket") if s.labels.get("le") not in (None, "+Inf")))
    total = next((s.value for s in _samples(base + "_count")), 0.0)
    if not total or not buckets:
        return None
    target, prev_le, prev_c = q * total, 0.0, 0.0
    for le, c in buckets:
        if c >= target:
            return round(prev_le + (le - prev_le) * ((target - prev_c) / (c - prev_c) if c > prev_c else 1.0), 3)
        prev_le, prev_c = le, c
    return round(buckets[-1][0], 3)  # beyond the last finite bucket: report the bound, not an invented value


def process_metrics() -> dict:
    return {"scope": "whole process (all tenants)",
            "ai_turn_seconds_p50": hist_quantile("mme_tel_ai_turn_seconds", 0.5), "ai_turn_seconds_p95": hist_quantile("mme_tel_ai_turn_seconds", 0.95),
            "ai_turns": counter_total("mme_tel_ai_turn_seconds_count"), "ai_turn_errors": counter_total("mme_tel_ai_turn_errors_total"),
            "plivo_streams_active": next((s.value for s in _samples("mme_tel_plivo_streams_active")), 0.0),
            "ai_runtimes": next((s.value for s in _samples("mme_tel_ai_runtimes")), 0.0),
            "esl_reconnects": next((s.value for s in _samples("mme_tel_esl_reconnects")), 0.0),
            "esl_events_dropped": next((s.value for s in _samples("mme_tel_esl_events_dropped")), 0.0),
            "rate_limited": counter_total("mme_tel_rate_limited_total"),
            "webhook_dead": sum(s.value for s in _samples("mme_tel_webhook_deliveries_total") if s.labels.get("result") == "dead")}


# ---------------------------------------------------------------------------------------------- components (topology)
def _c(key: str, label: str, group: str, status: str, detail: str = "") -> dict:
    return {"key": key, "label": label, "group": group, "status": status, "detail": detail}


async def _model_health(app, tenant: str) -> tuple[dict | None, str]:
    """(services, note). Models are probed at most every 30 s per tenant; never when AI is off or not configured."""
    s, ai = app.state.settings, app.state.ai
    if not s.ai_enabled:
        return None, "disabled"
    cfg = await ai.get_config(tenant)
    if not (cfg.get("stt_url") and cfg.get("brain_url") and cfg.get("tts_url")):
        return None, "not_configured"
    cache = getattr(app.state, "live_models", None)
    if cache is None:
        cache = app.state.live_models = {}
    hit = cache.get(tenant)
    if hit and time.monotonic() - hit[0] < MODEL_CHECK_EVERY_S:
        return hit[1], "cached"
    try:
        res = (await ai.health(tenant))["services"]
    except Exception:
        res = None
    cache[tenant] = (time.monotonic(), res)
    return res, "checked"


async def build_components(request: Request, p: Principal, health: dict) -> list[dict]:
    app, s = request.app, request.app.state.settings
    st, ai = app.state, app.state.ai
    comps: list[dict] = []
    # edge: how calls arrive
    plivo_on = s.plivo_inbound_enabled or s.provider == "plivo_api"
    if plivo_on:
        from app.ai.media import PlivoStreamBridge
        ok, why = await PlivoStreamBridge(s).ready()
        comps.append(_c("plivo", "Plivo (voice + stream)", "edge", "up" if ok else "not_configured", why))
    else:
        comps.append(_c("plivo", "Plivo (voice + stream)", "edge", "disabled", "inbound/streams off"))
    # telephony
    fe = health.get("freeswitch_events", {})
    if fe.get("status") == "not_applicable":
        comps.append(_c("fs_events", "FreeSWITCH events (ESL)", "telephony", "disabled", "provider is not freeswitch_gateway"))
    else:
        comps.append(_c("fs_events", "FreeSWITCH events (ESL)", "telephony", fe.get("status", "down"),
                        f"reconnects {fe.get('reconnects')}, queue {fe.get('queue_depth')}, dropped {fe.get('dropped')}, last event {fe.get('last_event_age_s')}s ago"))
        fc = health.get("freeswitch_commands", {})
        comps.append(_c("fs_commands", "FreeSWITCH commands (ESL)", "telephony", "up" if fc.get("status") == "up" else "idle_or_down", f"failures {fc.get('failures')}"))
    mb = health.get("ai", {}).get("media_bridge") or {}
    comps.append(_c("media_bridge", "Media bridge", "telephony", "up" if mb.get("ready") else ("disabled" if not s.ai_enabled else "down"),
                    f"{mb.get('name')}: {mb.get('reason')}"))
    # core
    comps.append(_c("controller", "Call controller", "core", "draining" if st.draining else "up", f"loops {health.get('workers', {}).get('controller_loops')}"))
    db = health.get("database", {})
    comps.append(_c("database", "MongoDB", "core", db.get("status", "down"), f"ping {db.get('latency_ms')} ms" if db.get("latency_ms") is not None else ""))
    comps.append(_c("webhooks", "Webhook worker", "core", "up" if health.get("workers", {}).get("webhook_worker") else "down",
                    "enabled" if s.webhooks_enabled else "webhooks disabled"))
    # models
    svc, note = await _model_health(app, p.tenant_id)
    for key, label in (("stt", "STT (speech to text)"), ("brain", "Brain (LLM)"), ("tts", "TTS (text to speech)"), ("rag", "RAG (knowledge)")):
        if svc is None:
            comps.append(_c(key, label, "models", "disabled" if note == "disabled" else "not_configured", "AI is off" if note == "disabled" else "no endpoint configured"))
        else:
            r = svc.get(key, {})
            comps.append(_c(key, label, "models", r.get("status", "unchecked"), f"{r.get('latency_ms')} ms" if r.get("latency_ms") is not None else ""))
    return comps


# ---------------------------------------------------------------------------------------------- flows + gauges
async def build_flows(request: Request, p: Principal) -> list[dict]:
    db, t = request.app.state.db, p.tenant_id
    full = _can_see_numbers(request, p)
    live_states = [x.value for x in (sm.ACTIVE | {sm.S.QUEUED})]
    act = await db["calls"].find({"tenant_id": t, "state": {"$in": live_states}}).sort("created_at", -1).limit(50).to_list(50)
    rec = await db["calls"].find({"tenant_id": t, "state": {"$nin": live_states}}).sort("created_at", -1).limit(10).to_list(10)
    calls = act + rec
    sids = [c["session_id"] for c in calls if c.get("session_id")]
    sess = {x["_id"]: x async for x in db["sessions"].find({"_id": {"$in": sids}, "tenant_id": t})} if sids else {}
    ais = {x["session_id"]: x async for x in db["ai_sessions"].find({"session_id": {"$in": sids}, "tenant_id": t})} if sids else {}
    out = []
    for c in calls:
        co = call_out(c, mask=not full)
        out.append({"call": {k: co.get(k) for k in ("id", "direction", "state", "provider", "from_number", "to_number", "language", "ai_enabled",
                                                       "session_id", "created_at", "answered_at", "ended_at")},
                    "flow": flow_stages(c, sess.get(c.get("session_id")), ais.get(c.get("session_id"))),
                    "active": c["state"] in live_states})
    return out


async def build_snapshot(request: Request, p: Principal) -> dict:
    app, s = request.app, request.app.state.settings
    db, repos, t = app.state.db, app.state.repos, p.tenant_id
    health = await collect_health(app)
    start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    by_state: dict[str, int] = {}
    async for r in await db["calls"].aggregate([{"$match": {"tenant_id": t, "created_at": {"$gte": start}}}, {"$group": {"_id": "$state", "n": {"$sum": 1}}}]):
        by_state[r["_id"]] = r["n"]
    gauges = {"active_calls": await repos.count_active(t), "queued_calls": await repos.count_in_flight(t) - await repos.count_active(t),
              "active_sessions": await db["sessions"].count_documents({"tenant_id": t, "status": {"$in": ["answered", "active"]}}),
              "active_ai_sessions": await db["ai_sessions"].count_documents({"tenant_id": t, "state": "active"}),
              "calls_today": sum(by_state.values()), "completed_today": by_state.get("completed", 0), "failed_today": by_state.get("failed", 0),
              "missed_today": by_state.get("no_answer", 0) + by_state.get("busy", 0)}
    admin = p.has(s.role_admin) or p.has(s.role_platform)
    snap = {"as_of": utcnow().isoformat(), "service": {"name": s.app_name, "version": s.app_version, "env": s.env, "draining": bool(app.state.draining),
                                                       "real_calls_enabled": s.real_calls_enabled, "ai_enabled": s.ai_enabled},
            "components": await build_components(request, p, health), "gauges": gauges, "flows": await build_flows(request, p),
            "last_seq": app.state.activity.last_seq}
    if admin:
        snap["process"] = process_metrics()
        snap["recent_errors"] = app.state.errlog.snapshot(10)
    return snap


@router.get("/snapshot")
async def snapshot(request: Request, p: Principal = Depends(require_viewer)):
    return await build_snapshot(request, p)


@router.get("/activity")
async def activity(request: Request, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), p: Principal = Depends(require_viewer)):
    items, lost = request.app.state.activity.since(after, p.tenant_id, limit)
    return {"items": items, "lost": lost, "last_seq": request.app.state.activity.last_seq}


@router.get("/calls/{call_id}/flow")
async def call_flow(request: Request, call_id: str, p: Principal = Depends(require_viewer)):
    db = request.app.state.db
    c = await request.app.state.repos.get_call(call_id, p.tenant_id)  # tenant-scoped lookup: other tenants get 404
    if not c:
        raise NotFound("call not found")
    sid = c.get("session_id")
    sess = await db["sessions"].find_one({"_id": sid, "tenant_id": p.tenant_id}) if sid else None
    ai = await db["ai_sessions"].find_one({"session_id": sid, "tenant_id": p.tenant_id}) if sid else None
    return flow_stages(c, sess, ai)


@router.get("/stream")
async def stream(request: Request, p: Principal = Depends(require_viewer)):
    """SSE. Events: `hello` (config), `backlog` (last 50 activity entries), `activity` (list, as it happens), `snapshot` (every 2 s).
    Use fetch() with the Authorization header (EventSource cannot set headers). At most 5 streams per tenant; the server ends a stream
    after an hour and the client reconnects."""
    if _streams.get(p.tenant_id, 0) >= MAX_STREAMS_PER_TENANT:
        raise RateLimited("too many live streams for this tenant", code="too_many_streams")
    bus = request.app.state.activity

    def sse(event: str, data) -> str:
        return f"event: {event}\ndata: {json.dumps(data, default=str, separators=(',', ':'))}\n\n"

    async def gen():
        _streams[p.tenant_id] = _streams.get(p.tenant_id, 0) + 1
        try:
            yield "retry: 3000\n\n"
            seq = bus.last_seq
            yield sse("hello", {"last_seq": seq, "snapshot_every_s": SNAPSHOT_EVERY_S, "ring": "in-process; history resets on restart"})
            yield sse("backlog", bus.recent(p.tenant_id, 50))
            t_end, next_snap = time.monotonic() + 3600, 0.0  # monotonic: immune to wall-clock steps
            while time.monotonic() < t_end and not await request.is_disconnected():
                if time.monotonic() >= next_snap:
                    next_snap = time.monotonic() + SNAPSHOT_EVERY_S
                    try:
                        yield sse("snapshot", await build_snapshot(request, p))
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:  # a failing probe must not end the stream: say so and carry on
                        yield sse("snapshot_error", {"error": type(e).__name__})
                await bus.wait(seq, max(0.05, min(1.0, next_snap - time.monotonic())))
                items, lost = bus.since(seq, p.tenant_id, 200)
                seq = bus.last_seq if (lost or len(items) < 200) else items[-1]["seq"]
                if items or lost:  # (an idle stream still writes a snapshot every 2 s, which doubles as the keep-alive)
                    yield sse("activity", {"items": items, "lost": lost})
        finally:
            _streams[p.tenant_id] = max(0, _streams.get(p.tenant_id, 1) - 1)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
