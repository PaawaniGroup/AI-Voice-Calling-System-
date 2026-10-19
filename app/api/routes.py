from __future__ import annotations

import csv
import io
import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from app.api.schemas import CallCreate, PolicyIn, RouteIn, TransferRequest, WebhookEndpointIn, call_out
from app.core import phone
from app.core.errors import ApiError, Conflict, Forbidden, NotFound, Unauthorized, Unprocessable
from app.core.ids import new_id, utcnow
from app.events import webhooks as wh
from app.observability import metrics as m
from app.persistence.repos import Repos
from app.providers import plivo as plivo_mod
from app.security.auth import Principal, require_admin, require_platform, require_user
from app.services import calls as call_svc
from app.services import recordings as rec_svc
from app.telephony.state_machine import CONNECTED, TERMINAL, S

health = APIRouter(tags=["health"])
v1 = APIRouter(prefix="/v1")


def _ctx(request: Request):
    st = request.app.state
    return st.settings, st.repos, st.controller


async def _own_call(request: Request, p: Principal, call_id: str) -> dict:
    _, repos, _ = _ctx(request)
    c = await repos.get_call(call_id, p.tenant_id)  # tenant scoping is part of the lookup => 404, not 403
    if not c:
        raise NotFound("call not found")
    return c


# ------------------------------------------------------------------ health
@health.get("/health/live")
async def live():
    return {"status": "ok"}


@health.get("/health/ready")
async def ready(request: Request):
    s, repos, ctrl = _ctx(request)
    checks: dict[str, str] = {}
    try:
        await repos.db.ping()
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "unavailable"
    if s.fs_enabled and s.provider == "freeswitch_gateway":
        ev = ctrl.esl_events
        fresh = bool(ev and ev.connected and ev.last_event_at and time.time() - ev.last_event_at <= s.ready_max_event_age_s)
        checks["freeswitch"] = "ok" if fresh else "unavailable"
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse({"status": "ready" if ok else "not_ready", "checks": checks}, status_code=200 if ok else 503)


@health.get("/metrics")
async def metrics(request: Request):
    s, repos, ctrl = _ctx(request)
    token = s.metrics_token.get_secret_value()
    auth = request.headers.get("authorization", "")
    client = request.client.host if request.client else ""
    if token:
        import hmac

        if not hmac.compare_digest(auth, f"Bearer {token}"):
            raise Unauthorized("metrics token required")
    elif client not in ("127.0.0.1", "::1"):
        raise Forbidden("metrics restricted to loopback")
    ev = ctrl.esl_events
    if ev:
        m.ESL_CONNECTED.set(1 if ev.connected else 0)
        m.ESL_RECONNECTS.set(ev.reconnect_count)
        m.ESL_EVENT_LAG.set(ev.last_lag_s)
        m.ESL_QUEUE_DEPTH.set(ev.queue.qsize())
        m.ESL_EVENTS_DROPPED.set(ev.events_dropped)
    if ctrl.esl_cmd:
        m.ESL_CMD_FAILURES.set(ctrl.esl_cmd.command_failures)
    m.DB_FAILURES.set(repos.db.failures)
    if repos.db.last_ping_ms is not None:
        m.DB_PING_MS.set(repos.db.last_ping_ms)
    body, ct = m.render()
    return Response(body, media_type=ct)


@v1.get("/telephony/status")
async def status(request: Request):
    s, repos, ctrl = _ctx(request)
    if s.status_requires_auth:
        require_user(request)
    ev = ctrl.esl_events
    return {
        "service": s.app_name, "version": s.app_version, "env": s.env, "provider": s.provider,
        "freeswitch": {"event_stream_connected": bool(ev and ev.connected),
                       "command_channel_connected": bool(ctrl.esl_cmd and ctrl.esl_cmd.connected)},
        "active_calls": await repos.count_active(), "queued_calls": await repos.count_queued(),
        "real_calls_enabled": s.real_calls_enabled, "recording_enabled": s.recording_enabled,
        "ai_enabled": False,
    }


# ------------------------------------------------------------------ calls
@v1.post("/calls", status_code=202)
async def create_call(request: Request, body: CallCreate, response: Response,
                      idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=128),
                      p: Principal = Depends(require_user)):
    s, repos, _ = _ctx(request)
    request.app.state.limiter.check(f"create:{p.tenant_id}", s.rate_limit_call_create_per_min)
    call, created = await call_svc.create_call(s, repos, p, body.model_dump(exclude_none=True), idempotency_key)
    if created:
        await repos.audit(p.tenant_id, p.sub, "call.create", call["_id"], {"direction": "outbound"},
                          request.state.request_id)
    else:
        response.headers["Idempotent-Replay"] = "true"
        response.status_code = 200
    return call_out(call)


@v1.get("/calls")
async def list_calls(request: Request, state: str | None = None, direction: str | None = None,
                     start: datetime | None = None, end: datetime | None = None,
                     limit: int = Query(50, ge=1, le=200), cursor: str | None = None,
                     p: Principal = Depends(require_user)):
    _, repos, _ = _ctx(request)
    if state and state not in {x.value for x in S}:
        raise Unprocessable("unknown state", code="invalid_state")
    try:
        rows, nxt = await repos.list_calls(p.tenant_id, state=state, direction=direction, start=start, end=end,
                                           limit=limit, cursor=cursor)
    except ValueError:
        raise Unprocessable("invalid cursor", code="invalid_cursor") from None
    return {"items": [call_out(c) for c in rows], "next_cursor": nxt}


@v1.get("/calls/{call_id}")
async def get_call(request: Request, call_id: str, p: Principal = Depends(require_user)):
    return call_out(await _own_call(request, p, call_id))


@v1.post("/calls/{call_id}/hangup")
async def hangup(request: Request, call_id: str, p: Principal = Depends(require_user)):
    _, repos, ctrl = _ctx(request)
    c = await _own_call(request, p, call_id)
    c = await ctrl.hangup(c)
    await repos.audit(p.tenant_id, p.sub, "call.hangup", call_id, {"state": c["state"]}, request.state.request_id)
    return call_out(c)


@v1.post("/calls/{call_id}/cancel")
async def cancel(request: Request, call_id: str, p: Principal = Depends(require_user)):
    _, repos, ctrl = _ctx(request)
    c = await _own_call(request, p, call_id)
    st = S(c["state"])
    if st in CONNECTED:
        raise Conflict("call already connected; use hangup", code="already_connected")
    if st in TERMINAL:
        return call_out(c)  # idempotent
    c = await ctrl.hangup(c)
    await repos.audit(p.tenant_id, p.sub, "call.cancel", call_id, {}, request.state.request_id)
    return call_out(c)


@v1.post("/calls/{call_id}/transfer", status_code=202)
async def transfer(request: Request, call_id: str, body: TransferRequest, p: Principal = Depends(require_user)):
    s, repos, ctrl = _ctx(request)
    c = await _own_call(request, p, call_id)
    _, dp = call_svc.effective_policy(s, await repos.get_policy(p.tenant_id))
    dest = phone.normalize(body.destination, s.default_region)
    dp.check_transfer(dest)
    c = await ctrl.transfer(c, dest)
    await repos.audit(p.tenant_id, p.sub, "call.transfer", call_id, {"to": dest[:3] + "***"}, request.state.request_id)
    return call_out(c)


@v1.get("/calls/{call_id}/events")
async def call_events(request: Request, call_id: str, p: Principal = Depends(require_user)):
    _, repos, _ = _ctx(request)
    await _own_call(request, p, call_id)
    evs = await repos.list_events(call_id, p.tenant_id)
    return {"items": [{"id": e["_id"], "type": e["type"], "ts": e["ts"], "source": e["source"],
                       "payload": e.get("payload", {})} for e in evs]}


# ------------------------------------------------------------------ recordings
@v1.get("/calls/{call_id}/recordings")
async def call_recordings(request: Request, call_id: str, p: Principal = Depends(require_user)):
    _, repos, _ = _ctx(request)
    await _own_call(request, p, call_id)
    rows = await repos.db["recordings"].find({"call_id": call_id, "tenant_id": p.tenant_id}).to_list(50)
    return {"items": [{"id": r["_id"], "status": r["status"], "duration_s": r.get("duration_s"),
                       "size_bytes": r.get("size_bytes"), "created_at": r["created_at"]} for r in rows]}


@v1.get("/recordings/{recording_id}/download-url")
async def recording_url(request: Request, recording_id: str, p: Principal = Depends(require_user)):
    s, repos, _ = _ctx(request)
    url = await rec_svc.download_url(repos, request.app.state.storage, s, p.tenant_id, recording_id)
    await repos.audit(p.tenant_id, p.sub, "recording.access", recording_id, {}, request.state.request_id)
    return {"url": url, "expires_in_s": s.recording_url_ttl_s}


# ------------------------------------------------------------------ usage / analytics
def _range(start: datetime | None, end: datetime | None) -> dict:
    q: dict[str, Any] = {}
    if start or end:
        q["created_at"] = {k: v for k, v in (("$gte", start), ("$lt", end)) if v}
    return q


async def _aggregate(repos: Repos, tenant: str | None, start, end, group_by: list[str]) -> list[dict]:
    match = {**_range(start, end)}
    if tenant:
        match["tenant_id"] = tenant
    gid = {k: f"${k}" for k in group_by}
    gid["state"] = "$state"
    pipeline = [{"$match": match},
                {"$group": {"_id": gid, "calls": {"$sum": 1}, "connected_s": {"$sum": {"$ifNull": ["$connected_duration_s", 0]}},
                            "ring_s": {"$sum": {"$ifNull": ["$ring_duration_s", 0]}},
                            "billable_min_rounded": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$connected_duration_s", 0]}, 0]},
                                                                           {"$ceil": {"$divide": ["$connected_duration_s", 60]}}, 0]}}}}]
    return [r async for r in await repos.db["calls"].aggregate(pipeline)]


@v1.get("/usage")
async def usage(request: Request, start: datetime | None = None, end: datetime | None = None,
                tenant_id: str | None = None, p: Principal = Depends(require_user)):
    s, repos, _ = _ctx(request)
    tenant = p.tenant_id
    if tenant_id and tenant_id != p.tenant_id:
        if not p.has(s.role_platform):
            raise Forbidden("cross-tenant access denied")
        tenant = tenant_id
    rows = await _aggregate(repos, tenant, start, end, ["provider"])
    by_provider: dict[str, dict] = {}
    for r in rows:
        pr = by_provider.setdefault(r["_id"]["provider"], {"calls": 0, "connected_seconds": 0.0, "billable_minutes_rounded_up": 0})
        pr["calls"] += r["calls"]
        pr["connected_seconds"] += r["connected_s"]
        pr["billable_minutes_rounded_up"] += int(r["billable_min_rounded"])
    total_s = sum(v["connected_seconds"] for v in by_provider.values())
    out = {"tenant_id": tenant, "range": {"start": start, "end": end},
           "measured": {"total_calls": sum(v["calls"] for v in by_provider.values()), "connected_seconds": total_s,
                        "source": "persisted call records (answered_at -> ended_at)", "by_provider": by_provider},
           "estimated_charges": None}
    if s.estimate_rate_per_min > 0:
        mins = sum(v["billable_minutes_rounded_up"] for v in by_provider.values())
        out["estimated_charges"] = {"amount": round(mins * s.estimate_rate_per_min, 4), "basis": "ESTIMATE: configured rate x rounded-up minutes; not a carrier invoice"}
    return out


@v1.get("/analytics/calls")
async def analytics(request: Request, start: datetime | None = None, end: datetime | None = None,
                    format: str = Query("json", pattern="^(json|csv)$"), p: Principal = Depends(require_user)):
    _, repos, _ = _ctx(request)
    rows = await _aggregate(repos, p.tenant_id, start, end, ["direction"])
    outcomes: dict[str, int] = {}
    conn = ring = 0.0
    total = 0
    for r in rows:
        st = r["_id"]["state"]
        outcomes[st] = outcomes.get(st, 0) + r["calls"]
        conn += r["connected_s"]
        ring += r["ring_s"]
        total += r["calls"]
    answered = sum(outcomes.get(k, 0) for k in ("completed", "answered", "bridged", "transferring"))
    data = {"total": total, "outcomes": outcomes, "failed": outcomes.get("failed", 0), "busy": outcomes.get("busy", 0),
            "no_answer": outcomes.get("no_answer", 0), "completed": outcomes.get("completed", 0),
            "connected_seconds": conn, "avg_connected_seconds": (conn / answered) if answered else 0.0,
            "avg_ring_seconds": (ring / total) if total else 0.0}
    if format == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["metric", "value"])
        for k, v in data.items():
            if k != "outcomes":
                w.writerow([k, v])
        for k, v in outcomes.items():
            w.writerow([f"outcome_{k}", v])
        return PlainTextResponse(buf.getvalue(), media_type="text/csv")
    return data


# ------------------------------------------------------------------ admin: policy, routes
@v1.get("/admin/policy")
async def get_policy(request: Request, p: Principal = Depends(require_admin)):
    _, repos, _ = _ctx(request)
    pol = await repos.get_policy(p.tenant_id) or {}
    pol.pop("_id", None)
    return pol


@v1.put("/admin/tenants/{tenant_id}/policy")
async def put_policy(request: Request, tenant_id: str, body: PolicyIn, p: Principal = Depends(require_platform)):
    s, repos, _ = _ctx(request)
    data = body.model_dump(exclude_none=True)
    data["caller_ids"] = [phone.normalize(n, s.default_region) for n in data.get("caller_ids", [])]
    data["transfer_destinations"] = [x if x.endswith("*") else phone.normalize(x, s.default_region)
                                     for x in data.get("transfer_destinations", [])]
    res = await repos.put_policy(tenant_id, data)
    await repos.audit(tenant_id, p.sub, "policy.put", tenant_id, {"enabled": data["enabled"]}, request.state.request_id)
    res.pop("_id", None)
    return res


@v1.put("/admin/tenants/{tenant_id}/routes")
async def put_route(request: Request, tenant_id: str, body: RouteIn, p: Principal = Depends(require_platform)):
    s, repos, _ = _ctx(request)
    number = phone.normalize(body.number, s.default_region)
    target = phone.normalize(body.target, s.default_region) if body.target else None
    if body.action == "bridge" and not target:
        raise Unprocessable("bridge route needs a target", code="target_required")
    existing = await repos.db["inbound_routes"].find_one({"number": number})
    if existing and existing["tenant_id"] != tenant_id:
        raise Conflict("number already assigned to another tenant", code="number_in_use")
    await repos.db["inbound_routes"].update_one(
        {"number": number}, {"$set": {"tenant_id": tenant_id, "action": body.action, "target": target, "enabled": True,
                                      "updated_at": utcnow()}}, upsert=True)
    await repos.audit(tenant_id, p.sub, "route.put", number[:3] + "***", {"action": body.action}, request.state.request_id)
    return {"number": number, "tenant_id": tenant_id, "action": body.action, "target": target}


@v1.get("/admin/routes")
async def list_routes(request: Request, p: Principal = Depends(require_admin)):
    _, repos, _ = _ctx(request)
    rows = await repos.db["inbound_routes"].find({"tenant_id": p.tenant_id}).to_list(200)
    return {"items": [{"number": r["number"], "action": r["action"], "target": r.get("target"), "enabled": r["enabled"]} for r in rows]}


@v1.delete("/admin/tenants/{tenant_id}/routes/{number}", status_code=204)
async def delete_route(request: Request, tenant_id: str, number: str, p: Principal = Depends(require_platform)):
    s, repos, _ = _ctx(request)
    n = phone.normalize(number, s.default_region)
    r = await repos.db["inbound_routes"].delete_one({"number": n, "tenant_id": tenant_id})
    if not r.deleted_count:
        raise NotFound("route not found")
    await repos.audit(tenant_id, p.sub, "route.delete", n[:3] + "***", {}, request.state.request_id)


# ------------------------------------------------------------------ webhook endpoints
@v1.post("/webhooks/endpoints", status_code=201)
async def create_endpoint(request: Request, body: WebhookEndpointIn, p: Principal = Depends(require_admin)):
    s, repos, _ = _ctx(request)
    await wh.validate_destination(body.url, s)
    bad = [e for e in body.events if e != "*" and e not in wh.EVENT_TYPES]
    if bad:
        raise Unprocessable("unknown event types", code="invalid_events")
    eid = new_id("whe")
    await repos.db["webhook_endpoints"].insert_one({"_id": eid, "tenant_id": p.tenant_id, "url": body.url, "events": body.events,
                                                    "enabled": body.enabled, "secret_version": 1, "created_at": utcnow()})
    secret = wh.derive_secret(s.webhook_signing_master.get_secret_value(), eid, 1) if s.webhook_signing_master.get_secret_value() else None
    await repos.audit(p.tenant_id, p.sub, "webhook.create", eid, {}, request.state.request_id)
    return {"id": eid, "url": body.url, "events": body.events, "signing_secret": secret,
            "note": "signing_secret is derived from the server master key and can be re-derived by rotation only"}


@v1.get("/webhooks/endpoints")
async def list_endpoints(request: Request, p: Principal = Depends(require_admin)):
    _, repos, _ = _ctx(request)
    rows = await repos.db["webhook_endpoints"].find({"tenant_id": p.tenant_id}).to_list(100)
    return {"items": [{"id": r["_id"], "url": r["url"], "events": r["events"], "enabled": r["enabled"]} for r in rows]}


@v1.delete("/webhooks/endpoints/{endpoint_id}", status_code=204)
async def delete_endpoint(request: Request, endpoint_id: str, p: Principal = Depends(require_admin)):
    _, repos, _ = _ctx(request)
    r = await repos.db["webhook_endpoints"].delete_one({"_id": endpoint_id, "tenant_id": p.tenant_id})
    if not r.deleted_count:
        raise NotFound("endpoint not found")
    await repos.audit(p.tenant_id, p.sub, "webhook.delete", endpoint_id, {}, request.state.request_id)


@v1.get("/webhooks/deliveries")
async def list_deliveries(request: Request, status: str | None = None, limit: int = Query(50, ge=1, le=200),
                          p: Principal = Depends(require_admin)):
    _, repos, _ = _ctx(request)
    q: dict[str, Any] = {"tenant_id": p.tenant_id}
    if status:
        q["status"] = status
    rows = await repos.db["webhook_deliveries"].find(q).sort("created_at", -1).limit(limit).to_list(limit)
    return {"items": [{"id": r["_id"], "endpoint_id": r["endpoint_id"], "event_id": r["event_id"], "type": r["event_type"],
                       "status": r["status"], "attempt_count": r["attempt_count"], "attempts": r.get("attempts", [])} for r in rows]}


@v1.post("/webhooks/deliveries/{delivery_id}/replay", status_code=202)
async def replay_delivery(request: Request, delivery_id: str, p: Principal = Depends(require_admin)):
    worker = request.app.state.webhook_worker
    if not await worker.replay(delivery_id, p.tenant_id):
        raise NotFound("delivery not found or not replayable")
    _, repos, _ = _ctx(request)
    await repos.audit(p.tenant_id, p.sub, "webhook.replay", delivery_id, {}, request.state.request_id)
    return {"status": "queued"}


# ------------------------------------------------------------------ Plivo provider callbacks
async def _verified_form(request: Request) -> tuple[dict[str, str], str]:
    s, repos, _ = _ctx(request)
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    base = s.webhook_public_base_url.rstrip("/")
    url = base + request.url.path  # signature is computed over the public URL Plivo called
    nonce = request.headers.get("x-plivo-signature-v3-nonce", "")
    sig = request.headers.get("x-plivo-signature-v3", "")
    if not plivo_mod.verify_signature(s, method="POST", url=url, params=params, nonce=nonce, signature=sig):
        raise Unauthorized("invalid provider signature", code="invalid_signature")
    ts = params.get("Timestamp")  # not always sent; replay defence is the nonce store
    _ = ts
    if await repos.seen_provider_key(f"plivo:nonce:{nonce}"):
        raise ApiError("replayed callback", code="replay", status=200)
    return params, nonce


@v1.post("/providers/plivo/status")
async def plivo_status(request: Request):
    s, repos, ctrl = _ctx(request)
    if s.provider != "plivo_api":
        raise NotFound("provider not enabled")
    try:
        params, _ = await _verified_form(request)
    except ApiError as e:
        if e.code == "replay":
            return {"status": "duplicate"}
        raise
    uuid_ = params.get("CallUUID", "")
    call = await repos.find_call_by_provider_id("plivo_api", uuid_)
    if not call and params.get("RequestUUID"):
        call = await repos.find_call_by_provider_id("plivo_api", params["RequestUUID"])
        if call:
            await repos.update_call(call["_id"], {"provider_call_id": uuid_})
    if not call:
        return {"status": "ignored"}  # unknown call: acknowledge so Plivo does not retry forever
    mapped = plivo_mod.map_status(params, bool(call.get("answered_at")))
    if not mapped:
        return {"status": "ignored"}
    target, reason = mapped
    key = f"plivo:{uuid_}:{params.get('CallStatus','')}:{params.get('Event','')}"
    fields = {}
    if target in TERMINAL:
        fields = {"hangup_cause": params.get("HangupCause", "")[:60], "failure_reason": reason if target != S.COMPLETED else None}
    outcome, _c = await ctrl.apply(call, target, source="plivo", dedup_id=key, fields=fields, payload={"status": params.get("CallStatus")})
    return {"status": outcome}


@v1.post("/providers/plivo/answer")
async def plivo_answer(request: Request):
    s, repos, ctrl = _ctx(request)
    if s.provider != "plivo_api":
        raise NotFound("provider not enabled")
    try:
        params, _ = await _verified_form(request)
    except ApiError as e:
        if e.code == "replay":
            return Response('<Response><Hangup reason="rejected"/></Response>', media_type="application/xml")
        raise
    xml = lambda b: Response(f"<Response>{b}</Response>", media_type="application/xml")  # noqa: E731
    uuid_ = params.get("CallUUID", "")
    call = await repos.find_call_by_provider_id("plivo_api", uuid_)
    if call is None and params.get("RequestUUID"):
        call = await repos.find_call_by_provider_id("plivo_api", params["RequestUUID"])
        if call:
            await repos.update_call(call["_id"], {"provider_call_id": uuid_})
    if call is None and params.get("Direction", "").lower() == "inbound":
        try:
            to = phone.normalize("+" + params.get("To", "").lstrip("+"))
        except ApiError:
            return xml('<Hangup reason="rejected"/>')
        route = await repos.get_route(to)
        pol = await repos.get_policy(route["tenant_id"]) if route else None
        if not route or (pol or {}).get("enabled") is not True:
            return xml('<Hangup reason="rejected"/>')
        if await repos.count_active() >= ctrl.max_active or await repos.count_active(route["tenant_id"]) >= (pol or {}).get("max_active_calls", s.max_active_per_tenant):
            return xml('<Hangup reason="busy"/>')
        call = {"_id": new_id("call"), "tenant_id": route["tenant_id"], "direction": "inbound", "provider": "plivo_api",
                "provider_call_id": uuid_, "from_number": params.get("From", "")[:20], "to_number": to, "state": S.INITIATING.value,
                "initiated_at": utcnow(), "correlation_id": new_id("cor"), "max_duration_s": s.max_call_duration_s}
        await repos.insert_call(call)
        m.CALLS_CREATED.labels("inbound").inc()
        action, target = route.get("action", "park"), route.get("target")
    elif call is None:
        return xml('<Hangup reason="rejected"/>')
    else:
        action, target = ("bridge", call.get("bridge_to")) if call.get("bridge_to") else ("park", None)
    await ctrl.apply(call, S.ANSWERED, source="plivo", dedup_id=f"plivo:{uuid_}:answer")
    if action == "hangup":
        return xml('<Hangup/>')
    if action == "bridge" and target:
        return xml(f'<Dial timeLimit="{call.get("max_duration_s", s.max_call_duration_s)}"><Number>{target.lstrip("+")}</Number></Dial>')
    return xml(f'<Wait length="{min(call.get("max_duration_s", s.max_call_duration_s), 3600)}"/>')
