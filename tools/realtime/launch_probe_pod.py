"""Gate G1 (round trip to an always-on server on RunPod): start ONE throw-away Pod running the no-model probe server (deploy/rt_workers/probe_server.py), run the probe against it
over TLS (certificate PINNED), print the numbers, DELETE the Pod. CHARGEABLE: needs the operator's "APPROVED: run G1 probe, cap $0.10, key file <path>". NOT RUN YET.

    python tools/realtime/launch_probe_pod.py --check                          FREE: Pods listed (read-only), balance and GPU prices (read-only GraphQL)
    python tools/realtime/launch_probe_pod.py --dry-run                        FREE: prints what would be sent (no key, no network)
    python tools/realtime/launch_probe_pod.py --declared-from pc|cloud --cap 0.10 --max-minutes 7 --key-file <file OUTSIDE the repo> --out <result.json OUTSIDE the repo> --yes

Reuses tools/phase3/launch_pod.py: the account key from RUNPOD_ADMIN_KEY or --key-file (never printed, never put into the Pod), balance margin, cheapest 24 GB-class GPU, --max-price-hr,
hard deadline = min(--max-minutes, cap/price x 0.9), the Pod deleted in EVERY path and the deletion verified (REST, then GraphQL), best-effort self-delete timer inside the Pod.
Added here: Pods are LISTED (read-only) at the start and at the end and the counts printed; the probe secret is generated per run, passed to the Pod as an environment variable and
to the probe in this process only (never printed); the Pod's certificate fingerprint is read from its status file and PINNED (a fingerprint is public); the address (public IP and
the external TCP port, which RunPod changes whenever a Pod resets) is read from the Pod's own details: if it cannot be read the run stops and deletes the Pod.
If a Pod might still run, this prints "ACTION NEEDED: DELETE POD NOW".
Minimum key permissions: Pod create, list, read and delete (nothing on serverless endpoints, billing or templates). Revoke the key afterwards.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import secrets
import sys
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from tools.phase3.launch_pod import BALANCE_MARGIN, RunPod, Refused, deadline_seconds, pick_gpu   # noqa: E402

IMAGE = "python:3.12-slim"                       # no GPU work happens: the Pod is only the other end of the connection (UNVERIFIED that this public image starts on every GPU host)
PORT_WS, PORT_STATUS = 8001, 8000
NAME = "mme-g1-probe"
FILES = (("rt_protocol.py", ROOT / "app" / "ai" / "rt" / "protocol.py"), ("common.py", ROOT / "deploy" / "rt_workers" / "common.py"),
         ("probe_server.py", ROOT / "deploy" / "rt_workers" / "probe_server.py"))


def pack(text: str) -> str:
    return base64.b64encode(zlib.compress(text.encode("utf-8"), 9)).decode("ascii")


def start_command(token: str, hard_stop_s: int) -> list:
    script = f"""set -u
OUT=/workspace/out/{token}; mkdir -p $OUT /workspace/rt
python3 - <<'PY'
import os, base64, zlib
for name in ("rt_protocol.py", "common.py", "probe_server.py"):
    open("/workspace/rt/" + name, "wb").write(zlib.decompress(base64.b64decode(os.environ["FILE_" + name.split(".")[0].upper()])))
PY
echo '{{"state": "installing"}}' > $OUT/status.json
pip install -q websockets cryptography > $OUT/install.log 2>&1
( cd /workspace/out && python3 -m http.server {PORT_STATUS} >/dev/null 2>&1 ) &
( sleep {hard_stop_s}; runpodctl remove pod $RUNPOD_POD_ID >/dev/null 2>&1 || true ) &
RT_BIND=0.0.0.0 PORT={PORT_WS} STATUS_DIR=$OUT python3 /workspace/rt/probe_server.py
"""
    return ["bash", "-lc", script]


def build_body(gpu_id: str, cloud: str, token: str, secret: str, hard_stop_s: int) -> dict:
    env = {"RT_SECRET": secret, "RESULT_TOKEN": token, "PYTHONUNBUFFERED": "1", "HF_HUB_DISABLE_TELEMETRY": "1"}
    for name, path in FILES:
        env["FILE_" + name.split(".")[0].upper()] = pack(path.read_text(encoding="utf-8"))
    return {"name": NAME, "imageName": IMAGE, "gpuTypeIds": [gpu_id], "gpuCount": 1, "cloudType": cloud, "containerDiskInGb": 10, "volumeInGb": 0,
            "ports": [f"{PORT_STATUS}/http", f"{PORT_WS}/tcp"], "env": env, "dockerStartCmd": start_command(token, hard_stop_s), "interruptible": False}


def describe(body: dict) -> dict:
    """The request with every value summarised (sizes, not contents): safe to print."""
    b = dict(body)
    b["env"] = {k: f"<{len(v)} chars>" for k, v in body["env"].items()}
    b["dockerStartCmd"] = [body["dockerStartCmd"][0], body["dockerStartCmd"][1], f"<script of {len(body['dockerStartCmd'][2])} chars>"]
    return b


def endpoint_from_pod(pod: dict | None, port: int = PORT_WS) -> tuple[str, int] | None:
    """(public IP, external port) of the Pod's TCP port, or None. The field names are RunPod REST's `publicIp` and `portMappings` {"<internal>": <external>} (UNVERIFIED: if they
    are not there the run stops and deletes the Pod, it never guesses an address)."""
    if not isinstance(pod, dict):
        return None
    ip, maps = pod.get("publicIp"), pod.get("portMappings")
    ext = maps.get(str(port)) if isinstance(maps, dict) else None
    return (ip, int(ext)) if isinstance(ip, str) and ip and isinstance(ext, (int, str)) and str(ext).isdigit() else None


def wss_url(ip: str, port: int) -> str:
    return f"wss://{ip}:{port}/ws"


def status_url(pod_id: str, token: str) -> str:
    return f"https://{pod_id}-{PORT_STATUS}.proxy.runpod.net/{token}/status.json"


class ProbeRunPod(RunPod):
    async def list_pods(self) -> list:
        """Read-only: the ids of the account's Pods."""
        r = await self.http.get("https://rest.runpod.io/v1/pods", headers=self._h, timeout=30)
        if r.status_code in (401, 403):
            raise Refused("RunPod refused this key for listing Pods (HTTP %d): it needs Pod read permission." % r.status_code)
        r.raise_for_status()
        data = r.json()
        return [p.get("id") for p in (data if isinstance(data, list) else data.get("pods", [])) if isinstance(p, dict)]


async def list_pod_ids(rp) -> list:
    return await rp.list_pods()


async def run(a, http, key: str, probe, rp=None, sleep=asyncio.sleep, clock=time.monotonic, log=print):
    """`probe(url, secret_bytes, pin) -> summary dict` runs the round-trip probe. Returns a result dict."""
    rp = rp or ProbeRunPod(http, key)
    before = await list_pod_ids(rp)
    log(f"Pods listed (read-only) at the start: {len(before)}")
    acct = await rp.account()
    balance = (acct.get("myself") or {}).get("clientBalance")
    if not isinstance(balance, (int, float)):
        raise Refused("the account balance could not be read: nothing is started")
    if balance < a.cap + BALANCE_MARGIN:
        raise Refused(f"balance ${balance:.2f} is below the cap ${a.cap:.2f} + margin ${BALANCE_MARGIN:.2f}: nothing is started")
    gpu_id, price = pick_gpu(acct.get("gpuTypes"), cloud=a.cloud)
    if price > a.max_price_hr:
        raise Refused(f"the cheapest candidate costs ${price:.2f}/h, above --max-price-hr ${a.max_price_hr:.2f}: nothing is started")
    limit = deadline_seconds(a.cap, price, a.max_minutes)
    log(f"balance ${balance:.2f} | GPU {gpu_id} at ${price:.3f}/h ({a.cloud}) | hard deadline {limit / 60:.1f} min = at most ${price * limit / 3600:.3f} (cap ${a.cap:.2f})")
    token, secret = secrets.token_urlsafe(12), secrets.token_urlsafe(24)
    body = build_body(gpu_id, a.cloud, token, secret, int(limit) + 60)
    pod_id, t_created, result = None, None, {"ok": False}
    try:
        pod = await rp.create(body)
        pod_id = pod.get("id")
        t_created = clock()
        cost_hr = pod.get("costPerHr")
        log(f"pod {pod_id} created" + (f" (reported cost ${cost_hr}/h)" if cost_hr is not None else ""))
        if isinstance(cost_hr, (int, float)) and cost_hr > a.max_price_hr:
            raise Refused(f"the pod reports ${cost_hr}/h, above --max-price-hr: deleting it")
        pin = addr = None
        while clock() - t_created < limit:
            await sleep(10)
            try:
                if pin is None:
                    r = await http.get(status_url(pod_id, token), timeout=20)
                    if r.status_code == 200 and r.json().get("state") == "serving":
                        pin = r.json().get("cert_sha256")
                if addr is None:
                    addr = endpoint_from_pod(await rp.get(pod_id))
            except Exception:
                pass
            if pin and addr:
                break
        else:
            log(f"hard deadline reached after {limit / 60:.1f} min without a reachable server")
        if not (pin and addr):
            result.update(state="no_server", reason="address_or_certificate_unavailable")
        else:
            log(f"server reachable (certificate pinned; address read from the Pod's details, not printed)")
            summary = await probe(wss_url(*addr), secret.encode(), pin)
            result.update(ok=True, state="done", summary=summary)
    finally:
        if pod_id:
            deleted = await rp.delete(pod_id)
            spent = (clock() - t_created) / 3600 * price if t_created else 0.0
            result.update(pod_id=pod_id, deleted=deleted, est_cost_usd=round(spent, 4))
            try:
                after = await list_pod_ids(rp)
                result["pods_after"] = len(after)
                log(f"Pods listed (read-only) at the end: {len(after)} (before: {len(before)})")
            except Exception:
                result["pods_after"] = None
            if deleted:
                log(f"pod {pod_id} DELETED and verified. Estimated cost ${spent:.4f} (up to the delete call; Billing is authoritative).")
            else:
                log("ACTION NEEDED: DELETE POD NOW\n" + "!" * 78 + f"\n!! THE POD {pod_id} COULD NOT BE VERIFIED AS DELETED. REMOVE IT in the RunPod console (Pods). It bills by the hour.\n" + "!" * 78)
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cap", type=float, default=0.10)
    ap.add_argument("--max-minutes", type=float, default=7)
    ap.add_argument("--max-price-hr", type=float, default=1.00)
    ap.add_argument("--cloud", choices=("SECURE", "COMMUNITY"), default="SECURE", help="SECURE: the public IP is stable (docs, 2026-10-27)")
    ap.add_argument("--key-file", default="")
    ap.add_argument("--declared-from", choices=("pc", "cloud"), default="")
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--out", default="")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args(argv)
    if a.cap > 0.10:
        raise SystemExit("refused: the cap of this request is 0.10 USD")
    if a.dry_run:
        print(json.dumps(describe(build_body("<gpu>", a.cloud, "<token>", "<secret>", 480)), indent=1))
        return 0
    key = os.environ.get("RUNPOD_ADMIN_KEY", "").strip()
    if not key and a.key_file:
        kf = Path(a.key_file).resolve()
        if ROOT in kf.parents:
            raise SystemExit("--key-file must be OUTSIDE the repository")
        key = kf.read_text(encoding="utf-8").strip()
    if not key:
        raise SystemExit("no key: set RUNPOD_ADMIN_KEY or pass --key-file (a file outside the repo holding an account key with Pod permissions)")
    import httpx

    async def go() -> int:
        async with httpx.AsyncClient() as http:
            rp = ProbeRunPod(http, key)
            if a.check:
                acct = await rp.account()
                print(f"Pods listed (read-only): {len(await list_pod_ids(rp))} | balance ${acct['myself']['clientBalance']:.2f}")
                for cloud in ("SECURE", "COMMUNITY"):
                    try:
                        g, p = pick_gpu(acct["gpuTypes"], cloud=cloud)
                        print(f"cheapest candidate on {cloud}: {g} ${p:.3f}/h")
                    except Refused as e:
                        print(cloud, "->", e)
                return 0
            if not a.yes or not a.declared_from:
                raise SystemExit("--yes and --declared-from pc|cloud are required: chargeable")
            if not a.out or ROOT in Path(a.out).resolve().parents:
                raise SystemExit("--out is required and must be OUTSIDE the repository")
            from app.ai.rt.connection import make_websockets_connector
            from tools.realtime import rt_rtt_probe as RP

            async def probe(url, secret, pin):
                return await RP.probe(make_websockets_connector(pin), url, secret, a.n, 0.05)
            res = await run(a, http, key, probe)
            ev = RP.evidence("MEASURED", res.get("summary") or {}, a.declared_from)
            Path(a.out).write_text(json.dumps({**ev, "run": {k: v for k, v in res.items() if k != "summary"}}, indent=1), encoding="utf-8")
            print(json.dumps({k: v for k, v in res.items()}))
            return 0 if res.get("ok") and res.get("deleted") else 1
    return asyncio.run(go())


if __name__ == "__main__":
    sys.exit(main())
