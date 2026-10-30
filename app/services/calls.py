"""Call creation service: policy, admission control, idempotency, durable enqueue."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.core import phone
from app.core.errors import ApiError, CapacityExceeded, Conflict, Forbidden, RateLimited
from app.core.ids import new_id, utcnow
from app.core.logging import correlation_id_var
from app.core.logging import event as log_event
from app.observability import metrics as m
from app.persistence.repos import Repos, fingerprint
from app.security.auth import Principal
from app.telephony.state_machine import S
from config.settings import Settings, load_policy_file


def effective_policy(s: Settings, tenant_policy: dict | None) -> tuple[dict, phone.DestinationPolicy]:
    file_pol = load_policy_file()
    tp = tenant_policy or {}
    merged = {
        "enabled": tp.get("enabled", False),  # tenants are denied until explicitly enabled
        "max_active_calls": min(tp.get("max_active_calls", s.max_active_per_tenant), s.max_active_global),
        "max_duration_s": min(tp.get("max_duration_s", s.max_call_duration_s), s.max_call_duration_s),
        "recording": tp.get("recording", "off"),
        # outbound AI: fail-closed (a tenant must opt in, and may only dial the exact numbers listed)
        "outbound_ai_enabled": tp.get("outbound_ai_enabled") is True,
        "outbound_ai_destinations": list(tp.get("outbound_ai_destinations") or []),
        "outbound_ai_daily_quota": tp.get("outbound_ai_daily_quota"),
        "outbound_ai_max_duration_s": tp.get("outbound_ai_max_duration_s"),
    }
    deny =s.csv(s.default_deny_prefixes) + list(file_pol.get("deny_prefixes", [])) + list(tp.get("deny_prefixes", []))
    dp = phone.DestinationPolicy(
        allowed_countries=tp.get("allowed_countries") or s.csv(s.default_allowed_countries),
        deny_prefixes=deny, allow_prefixes=list(tp.get("allow_prefixes", [])),
        caller_ids=list(tp.get("caller_ids", [])), transfer_destinations=list(tp.get("transfer_destinations", [])))
    return merged, dp


def pstn_path(s: Settings) -> bool:
    """True if origination would reach the real telephone network."""
    return s.provider == "plivo_api" or (s.provider == "freeswitch_gateway" and "{destination}" in s.fs_dial_template)


def outbound_ai_requested(s: Settings, body: dict[str, Any]) -> bool:
    """An outbound call that asks for an AI conversation AND goes through Plivo (the only provider whose answer handler can open the
    AI media stream). Other providers keep storing `ai_enabled` without starting anything (FreeSWITCH outbound AI is not built)."""
    return bool(body.get("ai_enabled")) and s.provider == "plivo_api"


log = logging.getLogger("mme_telephony.calls")


async def _outbound_ai_gate(s: Settings, repos: Repos, tenant: str, pol: dict, to: str, bridge_to: str | None, ai) -> None:
    """Every precondition of an outbound AI call that does not need capacity or quota. Raises ApiError; the caller audits it. Order: the
    global pause first (cheapest and the emergency stop), then tenant opt-in, the exact destination allowlist, then AI readiness."""
    if await repos.outbound_ai_paused():
        raise Forbidden("outbound AI calls are paused", code="outbound_ai_paused")
    if not pol["outbound_ai_enabled"]:
        raise Forbidden("outbound AI calls are not enabled for this tenant", code="outbound_ai_disabled")
    if to not in pol["outbound_ai_destinations"]:   # exact string match of normalised E.164: no prefixes, no wildcards, an empty list allows nothing
        raise Forbidden("destination is not on this tenant's outbound AI allowlist", code="outbound_ai_destination_not_allowed")
    if bridge_to:
        raise phone.Unprocessable("an AI call cannot also bridge to a second party", code="ai_bridge_unsupported")
    from app.ai.media import PlivoStreamBridge
    ok, why = await ai.preflight(tenant, bridge=PlivoStreamBridge(s)) if ai is not None else (False, "ai_unavailable")
    if not ok:
        raise Conflict(f"AI is not ready for this tenant ({why})", code="ai_not_ready", details={"reason": why})


async def _audit_ai_rejection(repos: Repos, p: Principal, e: ApiError, to: str | None = None) -> None:
    """Structured record of a refused outbound AI call (never the destination number: only its length class). Best effort."""
    action = "outbound_ai.quota_exhausted" if e.code == "outbound_ai_quota_exhausted" else "outbound_ai.rejected"
    log_event(log, action, tenant=p.tenant_id, code=e.code)
    try:
        await repos.audit(p.tenant_id, p.sub, action, "call", {"code": e.code}, "-")
    except Exception:
        log.exception("could not record the refusal of an outbound AI call")


async def create_call(s: Settings, repos: Repos, p: Principal, body: dict[str, Any], idem_key: str, ai=None) -> tuple[dict, bool]:
    """Returns (call, created). Tenant comes from the authenticated principal only.
    `ai` (AiService): needed to preflight outbound AI calls; without it an outbound AI request is refused."""
    tenant = p.tenant_id
    pol, dp = effective_policy(s, await repos.get_policy(tenant))
    if not pol["enabled"]:
        raise Forbidden("calling is not enabled for this tenant", code="tenant_calling_disabled")
    if pstn_path(s) and not s.real_calls_enabled:
        raise Forbidden("real PSTN calls are disabled (MME_TEL_REAL_CALLS_ENABLED=false)", code="real_calls_disabled")
    from app.services.sessions import LANGUAGES
    if body.get("tenant_id") not in (None, tenant):
        raise Forbidden("tenant_id does not match the authenticated tenant", code="tenant_mismatch")
    if body.get("webhook_url"):
        raise phone.Unprocessable("per-call webhook_url is not supported; register /v1/webhooks/endpoints", code="webhook_url_unsupported")
    language = body.get("language", "auto")
    if language not in LANGUAGES:
        raise phone.Unprocessable("unsupported language", code="invalid_language", details={"supported": sorted(LANGUAGES)})
    to = phone.normalize(body["to"], s.default_region)
    frm = phone.normalize(body["from_number"], s.default_region)
    dp.check_destination(to)
    dp.check_caller_id(frm)
    bridge_to = None
    if body.get("bridge_to"):
        bridge_to = phone.normalize(body["bridge_to"], s.default_region)
        dp.check_transfer(bridge_to)
    max_dur = min(body.get("max_duration_s") or pol["max_duration_s"], pol["max_duration_s"])
    ai_outbound = outbound_ai_requested(s, body)
    if ai_outbound:
        # Same preconditions as an inbound AI call (`plivo_calls.handle_answer`) plus the outbound-only controls: never queue a call that must not start.
        try:
            await _outbound_ai_gate(s, repos, tenant, pol, to, bridge_to, ai)
        except ApiError as e:
            await _audit_ai_rejection(repos, p, e)
            raise
        max_dur = min(max_dur, pol["outbound_ai_max_duration_s"] or s.outbound_ai_max_duration_s)   # hard cap for one AI call

    fp = fingerprint({"to": to, "from": frm, "bridge_to": bridge_to, "max_duration_s": body.get("max_duration_s"),
                      "metadata": body.get("metadata"), "ai_enabled": bool(body.get("ai_enabled")), "language": language})
    call_id = new_id("call")
    existing = await repos.claim_idempotency(tenant, "create_call", idem_key, fp, call_id)
    if existing is not None:
        for _ in range(10):  # the original request may still be inserting
            c = await repos.get_call(existing["call_id"], tenant)
            if c:
                return c, False
            await asyncio.sleep(0.1)
        raise Conflict("original request still in progress", code="idempotency_in_progress")
    quota_tenant = None
    try:
        # Admission control (hard ceilings; never silently dropped).
        active_total = await repos.count_active()
        ceiling = min(s.max_active_global, s.provider_max_concurrent)
        queued = await repos.count_queued()
        tenant_in_flight = await repos.count_in_flight(tenant)
        over_capacity = active_total + queued >= ceiling or tenant_in_flight >= pol["max_active_calls"]
        if over_capacity and (s.queue_policy == "reject" or queued >= s.max_queued or
                              tenant_in_flight >= pol["max_active_calls"] + max(1, s.max_queued // 10)):
            raise CapacityExceeded("call capacity exhausted", details={"retry_after_s": 5})
        if ai_outbound:
            # Daily quota, taken atomically (see Repos.reserve_outbound_ai_quota) only after everything else has passed, and given back below
            # if the call cannot be stored. Counts ACCEPTED creations (a call that later fails to dial still used its slot).
            quota = pol["outbound_ai_daily_quota"] if pol["outbound_ai_daily_quota"] is not None else s.outbound_ai_daily_quota
            if not await repos.reserve_outbound_ai_quota(tenant, quota):
                e = RateLimited("the daily outbound AI call quota is used up", code="outbound_ai_quota_exhausted", details={"quota": quota})
                await _audit_ai_rejection(repos, p, e)
                raise e
            quota_tenant = tenant
        now = utcnow()
        call = {
            "_id": call_id, "tenant_id": tenant, "direction": "outbound", "provider": s.provider,
            "from_number": frm, "to_number": to, "bridge_to": bridge_to, "state": S.QUEUED.value, "state_version": 0,
            "queued_at": now, "created_at": now, "idempotency_key": idem_key, "created_by": p.sub,
            "correlation_id": correlation_id_var.get() if correlation_id_var.get() != "-" else new_id("cor"),
            "max_duration_s": max_dur, "metadata": body.get("metadata") or {},
            "recording_policy": pol["recording"], "ai_enabled": bool(body.get("ai_enabled")), "language": language,
        }
        if ai_outbound:
            call["ai_outbound"] = True  # read by the Plivo Answer handler: this answered call gets the AI media stream, not <Wait>
        await repos.insert_call(call)
        await repos.add_event(call, "state.queued", "api", f"created:{call_id}", {})
    except Exception:
        if quota_tenant:
            await repos.release_outbound_ai_quota(quota_tenant)
        await repos.release_idempotency(tenant, "create_call", idem_key)
        raise
    m.CALLS_CREATED.labels("outbound").inc()
    return call, True
