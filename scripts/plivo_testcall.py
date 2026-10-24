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
from app.core.safe_output import hide_number, safe_json  # noqa: E402
from app.telephony import state_machine as sm  # noqa: E402

TENANT = "mme-test"
CAP_S = 180
HANGUP_GRACE_S = 20


mask = hide_number   # a number is never shown, not even partially (see app/core/safe_output.py)


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


def listener_process(port: int) -> dict | None:
    """The process listening on `port` on THIS machine (WSL/Linux): pid, start time (UTC ISO) and whether it runs from this repo.
    None when nothing listens (or this is not Linux)."""
    import os
    import re
    import subprocess
    try:
        out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = next((re.search(r"pid=(\d+)", ln) for ln in out.splitlines() if re.search(rf":{port}\s", ln) and "pid=" in ln), None)
    if not m:
        return None
    pid = int(m.group(1))
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        btime = next(int(x.split()[1]) for x in Path("/proc/stat").read_text().splitlines() if x.startswith("btime"))
        started = datetime.fromtimestamp(btime + int(stat[19]) / os.sysconf("SC_CLK_TCK"), timezone.utc).isoformat()
        cwd_is_repo = Path(os.readlink(f"/proc/{pid}/cwd")).resolve() == Path(__file__).resolve().parent.parent
    except (OSError, ValueError, IndexError, StopIteration):
        started, cwd_is_repo = None, None
    return {"pid": pid, "started_at": started, "cwd_is_repo": cwd_is_repo}


def judge_running(marker: dict | None, proc: dict | None, head: dict) -> tuple[str, str]:
    """Is the running service the code at HEAD? (verdict, why). `marker` = the build stamp the service wrote at startup (newer builds)."""
    if proc is None:
        return "not_running", "nothing is listening on the service port (here)"
    if head.get("dirty"):
        return "uncertain_dirty_tree", "the working tree has uncommitted changes: the running code may differ from HEAD"
    if marker and marker.get("pid") == proc["pid"]:
        if marker.get("commit") == head.get("commit") and not marker.get("dirty"):
            return "exact_match", "the service's own build stamp equals HEAD and the tree is clean"
        return "mismatch", f"the service reports commit {marker.get('commit')} (dirty={marker.get('dirty')}) but HEAD is {head.get('commit')}: restart it"
    try:
        started = datetime.fromisoformat(proc["started_at"]) if proc.get("started_at") else None
        committed = datetime.fromisoformat(head["committed_at"]) if head.get("committed_at") else None
    except ValueError:
        started = committed = None
    if started and committed and started >= committed:
        return "inferred_match", "no build stamp (older build): the process started after the HEAD commit and the tree is clean"
    return "mismatch_or_unknown", "no build stamp and the process started before the HEAD commit (or its start time is unknown): restart it"


async def service_info(db, port: int = 8800) -> dict:
    from app.core.buildinfo import build_info
    head, proc = build_info(), listener_process(port)
    marker = await db["service_runtime"].find_one({"_id": "api"}) or None
    verdict, why = judge_running(marker, proc, head)
    return {"port": port, "head": {k: head.get(k) for k in ("commit", "dirty", "committed_at")},
            "listener": proc, "build_stamp": {k: marker.get(k) for k in ("commit", "dirty", "pid", "started_at")} if marker else None,
            "verdict": verdict, "why": why}


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
            "service": await service_info(db),
            "open_to_anyone": bool(pol.get("enabled")) and not pol.get("allowed_callers") and any(r.get("action") == "ai" and r.get("enabled") for r in routes)}


ALLOWLIST_COMMIT = "e7c2467"   # the commit that introduced policy.allowed_callers: a running build must contain it for `prove` to be safe


def includes_commit(commit: str | None, required: str) -> bool:
    """Does `commit` contain `required` in its history? (git ancestry; False when unknown)"""
    import subprocess
    if not commit or commit == "unknown":
        return False
    r = subprocess.run(["git", "-C", str(Path(__file__).resolve().parent.parent), "merge-base", "--is-ancestor", required, commit],
                       capture_output=True, timeout=5, check=False)
    return r.returncode == 0


def running_has_allowlist(info: dict, required: str = ALLOWLIST_COMMIT) -> tuple[bool, str]:
    """Is it PROVEN that the running service contains the allowlist code? Stamp first, else the process start time vs that commit's date."""
    import subprocess
    stamp = info.get("build_stamp") or {}
    listener = info.get("listener") or {}
    if listener and stamp and stamp.get("pid") == listener.get("pid"):
        ok = includes_commit(stamp.get("commit"), required)
        return ok, f"the running service's own build stamp ({stamp.get('commit')}) {'contains' if ok else 'does NOT contain'} {required}"
    if listener and listener.get("started_at"):
        r = subprocess.run(["git", "-C", str(Path(__file__).resolve().parent.parent), "log", "-1", "--format=%cI", required], capture_output=True, text=True, timeout=5, check=False)
        try:
            if r.returncode == 0 and datetime.fromisoformat(listener["started_at"]) >= datetime.fromisoformat(r.stdout.strip()):
                return True, f"no build stamp: the process started after commit {required} (inferred)"
        except ValueError:
            pass
    return False, "cannot prove the running service contains the allowlist: restart it on the current commit"


async def probe(db, *, service_url: str, public_base: str, token: str, tenant: str, did: str, from_number: str) -> dict:
    """Send ONE signed Plivo Answer callback from a number that is NOT allowed and check the service rejects it without a trace."""
    import uuid
    import xml.etree.ElementTree as ET

    import httpx
    from plivo.utils.signature_v3 import construct_post_url, get_signature_v3
    pol = await db["tenant_policies"].find_one({"tenant_id": tenant}) or {}
    frm = phone.normalize(from_number)
    if not (pol.get("enabled") and pol.get("allowed_callers")):
        return {"ok": False, "refused": "the policy is not armed with an allowlist: the probe would be ANSWERED by the AI (and spend)"}
    if frm in pol["allowed_callers"]:
        return {"ok": False, "refused": "the probe's From number is in the allowlist: it would be answered"}
    if not await db["inbound_routes"].find_one({"number": did, "enabled": True}):
        return {"ok": False, "refused": "the DID has no enabled route: a rejection would prove nothing"}
    calls0, ai0 = await db["calls"].count_documents({}), await db["ai_sessions"].count_documents({})
    call_uuid, nonce, path = str(uuid.uuid4()), uuid.uuid4().hex, "/v1/providers/plivo/answer"
    params = {"CallUUID": call_uuid, "Direction": "inbound", "To": did.lstrip("+"), "From": frm.lstrip("+")}
    sig = get_signature_v3(token.encode(), construct_post_url(public_base.rstrip("/") + path, params), nonce.encode())
    sig = sig.decode() if isinstance(sig, bytes) else sig
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(service_url.rstrip("/") + path, data=params, headers={"X-Plivo-Signature-V3": sig, "X-Plivo-Signature-V3-Nonce": nonce})
    await asyncio.sleep(0.7)   # anything created asynchronously (a warmup, a session) would show up by now
    root = ET.fromstring(r.text) if r.status_code == 200 else None
    hangup = root is not None and root.find("Hangup") is not None
    stream = root is not None and root.find("Stream") is not None
    new_calls = await db["calls"].count_documents({}) - calls0
    new_ai = await db["ai_sessions"].count_documents({}) - ai0
    same_uuid = await db["calls"].count_documents({"provider_call_id": call_uuid})
    return {"ok": r.status_code == 200 and hangup and not stream and new_calls == 0 and new_ai == 0 and same_uuid == 0,
            "http": r.status_code, "hangup_in_response": hangup, "stream_in_response": stream, "new_calls": new_calls, "new_ai_sessions": new_ai}


async def prove(db, s, *, service_url: str, port: int, did: str, dummy_caller: str, probe_from: str, tenant: str) -> dict:
    """Prove the allowlist on the LIVE service without the operator's number: gate on the running build, arm with a dummy allowed number,
    send a signed Answer from a different number, and ALWAYS disarm."""
    info = await service_info(db, port)
    proven, why = running_has_allowlist(info)
    out: dict = {"running_build": {"verdict": info["verdict"], "listener_pid_found": bool(info["listener"]), "gate": why}}
    if not proven:
        return {**out, "ok": False, "refused": "the running service is not proven to contain the allowlist: nothing was armed or sent"}
    await arm(db, tenant, phone.normalize(dummy_caller), did)
    try:
        out["probe"] = await probe(db, service_url=service_url, public_base=s.webhook_public_base_url, token=s.plivo_auth_token.get_secret_value(),
                                   tenant=tenant, did=did, from_number=probe_from)
    finally:
        out["disarmed"] = await disarm(db, tenant, await _provider_hangup(s))
    out["ok"] = bool(out["probe"].get("ok"))
    return out


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
            print(safe_json(await status(db, args.tenant, None, s)))
            return 0
        if not args.yes:
            print(f"refusing to change anything without --yes (command: {args.cmd})")
            return 2
        if args.cmd == "arm":
            caller = phone.normalize(args.caller, s.default_region)
            did = phone.normalize(args.did, s.default_region)
            rep = await arm(db, args.tenant, caller, did, args.cap_s)
            print(safe_json({"armed": True, "caller": mask(caller), "did": mask(did), "cap_s": args.cap_s, **rep}))
            print(safe_json(await status(db, args.tenant, did, s)))
            if args.watch:
                print(f"[watch] waiting up to {args.window_s:.0f}s for the call; Ctrl+C = kill switch", flush=True)
                try:
                    why = await watch(db, args.tenant, s, args.window_s, args.cap_s)
                except (KeyboardInterrupt, asyncio.CancelledError):
                    why = "interrupted by operator"
                print(f"[watch] disarming: {why}", flush=True)
                print(safe_json(await disarm(db, args.tenant, await _provider_hangup(s))))
            return 0
        if args.cmd == "disarm":
            print(safe_json(await disarm(db, args.tenant, await _provider_hangup(s))))
            return 0
        if args.cmd == "prove":
            res = await prove(db, s, service_url=args.service, port=args.port, did=phone.normalize(args.did, s.default_region),
                              dummy_caller=args.dummy_caller, probe_from=args.probe_from, tenant=args.tenant)
            print(safe_json(res))
            return 0 if res["ok"] else 1
        return 2
    finally:
        await db.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("status", "arm", "disarm", "prove"):
        p = sub.add_parser(name)
        p.add_argument("--tenant", default=TENANT)
        p.add_argument("--yes", action="store_true")
        if name == "prove":
            p.add_argument("--did", default="+918065530418", help="the Plivo number that routes to the AI")
            p.add_argument("--service", default="http://127.0.0.1:8800", help="where the LIVE service listens (the signature is computed for the public URL)")
            p.add_argument("--port", type=int, default=8800)
            p.add_argument("--dummy-caller", default="+911140000000", help="the temporary allowlist entry: NOT the operator's number")
            p.add_argument("--probe-from", default="+919000000009", help="the caller of the signed probe: must differ from --dummy-caller")
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
