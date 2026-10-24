"""Controlled Plivo test call: arm / disarm (kill switch) / status. Makes NO call itself.

    python scripts/plivo_testcall.py status
    python scripts/plivo_testcall.py arm --caller +91XXXXXXXXXX --yes [--watch]   # whitelist ONLY that caller, 1 call, 180 s cap
    python scripts/plivo_testcall.py disarm --yes                                 # KILL SWITCH: stop new calls, hang up live ones

`arm` (tenant policy `mme-test`): enabled, allowed_callers=[caller], max_active_calls=1, max_duration_s=180, and the inbound route of the
DID is `ai`. Anyone else who dials the DID is rejected before a call exists (no AI session, no model request, no spend). With --watch the
script stays in the foreground as a dead-man switch: it disarms when the first call has ended, when that call is older than the cap + a
grace, when the window runs out, or on Ctrl+C. `disarm` works without the API service: it writes the policy straight to MongoDB
(enabled=false, so new calls are rejected) and asks Plivo's REST API to hang up every live call of the tenant. Numbers are masked in
all output; keys are read from the service settings/.env and never printed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import phone  # noqa: E402
from app.telephony import state_machine as sm  # noqa: E402

TENANT = "mme-test"
CAP_S = 180
HANGUP_GRACE_S = 20


def mask(n: str | None) -> str:
    n = n or ""
    return (n[:3] + "•" * max(0, len(n) - 7) + n[-4:]) if len(n) > 7 else "•" * len(n)


async def arm(db, tenant: str, caller: str, did: str, cap_s: int = CAP_S) -> dict:
    """Idempotent. Returns what it replaced so the operator can restore it."""
    before = await db["tenant_policies"].find_one({"tenant_id": tenant}) or {}
    before.pop("_id", None)
    await db["tenant_policies"].update_one(
        {"tenant_id": tenant},
        {"$set": {"tenant_id": tenant, "enabled": True, "allowed_callers": [caller], "max_active_calls": 1, "max_duration_s": cap_s,
                  "updated_at": datetime.now(timezone.utc)}}, upsert=True)
    route_before = await db["inbound_routes"].find_one({"number": did}) or {}
    route_before.pop("_id", None)
    await db["inbound_routes"].update_one({"number": did}, {"$set": {"number": did, "tenant_id": tenant, "action": "ai", "enabled": True}}, upsert=True)
    return {"policy_before": {k: v for k, v in before.items() if k in ("enabled", "allowed_callers", "max_active_calls", "max_duration_s")},
            "route_before": {k: v for k, v in route_before.items() if k in ("action", "enabled", "tenant_id")}}


async def live_calls(db, tenant: str) -> list[dict]:
    states = [x.value for x in sm.ACTIVE | {sm.S.QUEUED}]
    return await db["calls"].find({"tenant_id": tenant, "state": {"$in": states}}).to_list(50)


async def disarm(db, tenant: str, hangup) -> dict:
    """The kill switch. `hangup(call)` is an async callable that asks the provider to end that call. Never raises on one failure."""
    await db["tenant_policies"].update_one({"tenant_id": tenant}, {"$set": {"enabled": False, "updated_at": datetime.now(timezone.utc)}}, upsert=True)
    out = {"policy_enabled": False, "hangups": []}
    for c in await live_calls(db, tenant):
        try:
            await hangup(c)
            out["hangups"].append({"call": c["_id"], "state": c["state"], "result": "requested"})
        except Exception as e:  # reported, never swallowed silently: the operator must see a failed hangup
            out["hangups"].append({"call": c["_id"], "state": c["state"], "result": f"FAILED:{type(e).__name__}"})
    return out


async def status(db, tenant: str, did: str | None, s) -> dict:
    pol = await db["tenant_policies"].find_one({"tenant_id": tenant}) or {}
    routes = await db["inbound_routes"].find({"tenant_id": tenant}).to_list(20)
    ai = await db["ai_config"].find_one({"tenant_id": tenant}) or {}
    calls = await live_calls(db, tenant)
    ai_live = await db["ai_sessions"].count_documents({"tenant_id": tenant, "state": {"$in": ["starting", "active", "media_disconnected"]}})
    return {"tenant": tenant,
            "policy": {"enabled": pol.get("enabled"), "allowed_callers": [mask(x) for x in pol.get("allowed_callers", [])],
                       "restricted_to_allowlist": bool(pol.get("allowed_callers")), "max_active_calls": pol.get("max_active_calls"),
                       "max_duration_s": pol.get("max_duration_s")},
            "routes": [{"number": mask(r["number"]), "action": r.get("action"), "enabled": r.get("enabled")} for r in routes],
            "ai_config": {"tenant_enabled": ai.get("enabled"), "stt_url": bool(ai.get("stt_url")), "brain_url": bool(ai.get("brain_url")),
                          "tts_url": bool(ai.get("tts_url")), "rag_url": bool(ai.get("rag_url")), "keys": sorted((ai.get("keys") or {}).keys())},
            "service_flags": {"ai_enabled": s.ai_enabled, "real_calls_enabled": s.real_calls_enabled, "plivo_inbound_enabled": s.plivo_inbound_enabled,
                              "plivo_credentials_set": bool(s.plivo_auth_id.get_secret_value() and s.plivo_auth_token.get_secret_value()),
                              "public_base_url_https": s.webhook_public_base_url.startswith("https://"), "max_call_duration_s": s.max_call_duration_s},
            "live_calls": [{"id": c["_id"], "state": c["state"], "from": mask(c.get("from_number")), "ai": bool(c.get("ai_enabled"))} for c in calls],
            "live_ai_sessions": ai_live,
            "open_to_anyone": bool(pol.get("enabled")) and not pol.get("allowed_callers") and any(r.get("action") == "ai" and r.get("enabled") for r in routes)}


async def _provider_hangup(s):
    from app.providers.plivo import PlivoApiProvider
    prov = PlivoApiProvider(s)

    async def hangup(call: dict) -> None:
        await prov.hangup(call, "NORMAL_CLEARING")
    return hangup


async def watch(db, tenant: str, s, window_s: float, cap_s: int) -> str:
    """Dead-man switch: returns the reason it stopped waiting; the caller then disarms."""
    t0 = time.monotonic()
    seen: dict | None = None
    first_seen_at = None
    while True:
        await asyncio.sleep(2)
        calls = await live_calls(db, tenant)
        if calls and seen is None:
            seen, first_seen_at = calls[0], time.monotonic()
            print(f"[watch] a call arrived from {mask(seen.get('from_number'))} (state {seen['state']}); cap {cap_s}s", flush=True)
        if seen is not None:
            cur = await db["calls"].find_one({"_id": seen["_id"]})
            if cur and cur["state"] in {x.value for x in sm.TERMINAL}:
                return f"the call ended ({cur['state']})"
            if time.monotonic() - first_seen_at > cap_s + HANGUP_GRACE_S:
                return f"the call exceeded the {cap_s}s cap + {HANGUP_GRACE_S}s grace"
        if time.monotonic() - t0 > window_s:
            return f"no call within the {window_s:.0f}s window" if seen is None else "window ended"


async def amain(args) -> int:
    from app.persistence.db import Database
    from config.settings import Settings
    s = Settings()
    db = Database(s)
    await db.connect()
    try:
        if args.cmd == "status":
            print(json.dumps(await status(db, args.tenant, None, s), indent=2))
            return 0
        if not args.yes:
            print(f"refusing to change anything without --yes (command: {args.cmd})")
            return 2
        if args.cmd == "arm":
            caller = phone.normalize(args.caller, s.default_region)
            did = phone.normalize(args.did, s.default_region)
            rep = await arm(db, args.tenant, caller, did, args.cap_s)
            print(json.dumps({"armed": True, "caller": mask(caller), "did": mask(did), "cap_s": args.cap_s, **rep}, indent=2, default=str))
            print(json.dumps(await status(db, args.tenant, did, s), indent=2))
            if args.watch:
                print(f"[watch] waiting up to {args.window_s:.0f}s for the call; Ctrl+C = kill switch", flush=True)
                try:
                    why = await watch(db, args.tenant, s, args.window_s, args.cap_s)
                except (KeyboardInterrupt, asyncio.CancelledError):
                    why = "interrupted by operator"
                print(f"[watch] disarming: {why}", flush=True)
                print(json.dumps(await disarm(db, args.tenant, await _provider_hangup(s)), indent=2))
            return 0
        if args.cmd == "disarm":
            print(json.dumps(await disarm(db, args.tenant, await _provider_hangup(s)), indent=2))
            return 0
        return 2
    finally:
        await db.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("status", "arm", "disarm"):
        p = sub.add_parser(name)
        p.add_argument("--tenant", default=TENANT)
        p.add_argument("--yes", action="store_true")
        if name == "arm":
            p.add_argument("--caller", required=True, help="the ONLY number that will be answered (your own)")
            p.add_argument("--did", default="+918065530418", help="the Plivo number that routes to the AI")
            p.add_argument("--cap-s", type=int, default=CAP_S)
            p.add_argument("--watch", action="store_true", help="stay in the foreground as a dead-man switch and disarm automatically")
            p.add_argument("--window-s", type=float, default=600.0)
    args = ap.parse_args(argv)
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
