"""Free timing of GET {endpoint}/health for the STT, Brain and voice RunPod endpoints (no job is created, nothing is billed, no worker is woken).

For each endpoint, N requests ONE SECOND apart in two series: FRESH (a new connection every time: DNS + TCP + TLS + first byte) and REUSED (one kept-alive
connection: first byte only). Reports connect (TCP+TLS), time to first byte (TTFB) and total per request as min/median/max, in ms. Nothing else is printed:
no key, no full endpoint id, no response body.

Keys: each endpoint uses its OWN key from the environment (RUNPOD_STT_KEY, RUNPOD_BRAIN_KEY, RUNPOD_TTS_KEY). A missing key sends NO Authorization header:
the answer is then HTTP 401 and the numbers show the network path only (labelled "UNAUTHENTICATED").
Endpoint URLs come from the tenant's ai_config in the local test Mongo (read only): python tools/health_timing.py --tenant mme-test [--n 10]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import statistics as st
import time

import httpx

KEYS = {"stt": "RUNPOD_STT_KEY", "brain": "RUNPOD_BRAIN_KEY", "tts": "RUNPOD_TTS_KEY"}


def mask(url: str) -> str:
    parts = url.rstrip("/").split("/")
    ident = parts[-2] if parts[-1] == "health" and len(parts) > 1 else parts[-1]
    return "/".join(parts[:3] + ["..." + ident[-4:]])                           # host + the last 4 characters of the endpoint id


class Probe:
    def __init__(self):
        self.t: dict[str, float] = {}

    async def __call__(self, name: str, info) -> None:
        self.t.setdefault(name, time.perf_counter())


async def one(client: httpx.AsyncClient, url: str, headers: dict) -> dict:
    p = Probe()
    t0 = time.perf_counter()
    r = await client.get(url, headers=headers, extensions={"trace": p})
    body = await r.aread()
    t_end = time.perf_counter()
    conn_done = p.t.get("connection.start_tls.complete") or p.t.get("connection.connect_tcp.complete")
    first = p.t.get("http11.receive_response_headers.complete") or p.t.get("http2.receive_response_headers.complete")
    return {"status": r.status_code, "bytes": len(body), "connect_ms": round((conn_done - t0) * 1000, 1) if conn_done else 0.0,
            "ttfb_ms": round((first - t0) * 1000, 1) if first else None, "total_ms": round((t_end - t0) * 1000, 1)}


def summ(rows: list[dict], key: str) -> str:
    v = [r[key] for r in rows if r.get(key) is not None]
    return f"{min(v):.0f}/{st.median(v):.0f}/{max(v):.0f}" if v else "n/a"


async def series(url: str, headers: dict, n: int, fresh: bool) -> list[dict]:
    rows = []
    client = None if fresh else httpx.AsyncClient(timeout=15, http2=False)
    try:
        for i in range(n):
            if fresh:
                async with httpx.AsyncClient(timeout=15, http2=False) as c:
                    rows.append(await one(c, url, headers))
            else:
                rows.append(await one(client, url, headers))
            if i < n - 1:
                await asyncio.sleep(1.0)
    finally:
        if client is not None:
            await client.aclose()
    return rows


async def endpoints(tenant: str, mongo: str) -> dict[str, str]:
    from pymongo import AsyncMongoClient
    c = AsyncMongoClient(mongo, serverSelectionTimeoutMS=3000)
    doc = await c["mme_telephony"]["ai_config"].find_one({"tenant_id": tenant}) or {}
    out = {}
    for svc in ("stt", "brain", "tts"):
        u = (doc.get(f"{svc}_url") or "").rstrip("/")
        if u.endswith("/runsync"):
            u = u[: -len("/runsync")]
        if u:
            out[svc] = u + "/health"
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default="mme-test")
    ap.add_argument("--mongo", default="mongodb://127.0.0.1:27117")
    ap.add_argument("--n", type=int, default=10)
    a = ap.parse_args()
    eps = await endpoints(a.tenant, a.mongo)
    print(f"n={a.n} per series, 1 s apart; values are min/median/max in ms")
    for svc, url in eps.items():
        key = os.environ.get(KEYS[svc], "").strip()
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        mode = "authenticated" if key else "UNAUTHENTICATED (no key in this environment)"
        for label, fresh in (("fresh connection", True), ("reused connection", False)):
            rows = await series(url, headers, a.n, fresh)
            codes = sorted({r["status"] for r in rows})
            print(f"{svc:5s} {mask(url)}  {mode}  {label}: HTTP {codes}  connect {summ(rows, 'connect_ms')}  ttfb {summ(rows, 'ttfb_ms')}  total {summ(rows, 'total_ms')}")


if __name__ == "__main__":
    asyncio.run(main())
