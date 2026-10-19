"""Call creation service: policy, admission control, idempotency, durable enqueue."""
from __future__ import annotations

import asyncio
from typing import Any

from app.core import phone
from app.core.errors import CapacityExceeded, Conflict, Forbidden
from app.core.ids import new_id, utcnow
from app.core.logging import correlation_id_var
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
    }
    deny = s.csv(s.default_deny_prefixes) + list(file_pol.get("deny_prefixes", [])) + list(tp.get("deny_prefixes", []))
    dp = phone.DestinationPolicy(
        allowed_countries=tp.get("allowed_countries") or s.csv(s.default_allowed_countries),
        deny_prefixes=deny, allow_prefixes=list(tp.get("allow_prefixes", [])),
        caller_ids=list(tp.get("caller_ids", [])), transfer_destinations=list(tp.get("transfer_destinations", [])))
    return merged, dp


def pstn_path(s: Settings) -> bool:
    """True if origination would reach the real telephone network."""
    return s.provider == "plivo_api" or (s.provider == "freeswitch_gateway" and "{destination}" in s.fs_dial_template)


async def create_call(s: Settings, repos: Repos, p: Principal, body: dict[str, Any], idem_key: str) -> tuple[dict, bool]:
    """Returns (call, created). Tenant comes from the authenticated principal only."""
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
        now = utcnow()
        call = {
            "_id": call_id, "tenant_id": tenant, "direction": "outbound", "provider": s.provider,
            "from_number": frm, "to_number": to, "bridge_to": bridge_to, "state": S.QUEUED.value, "state_version": 0,
            "queued_at": now, "created_at": now, "idempotency_key": idem_key, "created_by": p.sub,
            "correlation_id": correlation_id_var.get() if correlation_id_var.get() != "-" else new_id("cor"),
            "max_duration_s": max_dur, "metadata": body.get("metadata") or {},
            "recording_policy": pol["recording"], "ai_enabled": bool(body.get("ai_enabled")), "language": language,
        }
        await repos.insert_call(call)
        await repos.add_event(call, "state.queued", "api", f"created:{call_id}", {})
    except Exception:
        await repos.release_idempotency(tenant, "create_call", idem_key)
        raise
    m.CALLS_CREATED.labels("outbound").inc()
    return call, True
