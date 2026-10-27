"""Gate G1 probe: round-trip time over a persistent connection to an always-on server (the echo/worker protocol of deploy/rt_workers/).

  python tools/realtime/rt_rtt_probe.py --selftest                                        FREE, offline: an in-memory server (SIMULATED: proves the probe, says nothing about a network)
  python tools/realtime/rt_rtt_probe.py --url wss://HOST/ws --secret-env RT_SECRET --n 200 --interval-ms 50 \
        --declared-from pc|cloud --approved "<id>" --evidence out.json                     REAL: needs a server that is running (a Pod: a chargeable REQUEST first)

Two series on ONE connection (hello and token included once): `ping` (the bare round trip) and `warmup` requests (the whole client request path: sequence numbers, queues, the
reader task). Evidence (numbers only): summary.rt_roundtrip_ms = the ping series, summary.warmup_request_ms = the request series; median, p90, n. The place the probe ran
from is DECLARED by the operator (`pc` = the PC behind ngrok, `cloud` = a host near the GPUs): G1 is about the path the service will really use. The secret comes from the
environment variable named by --secret-env and is never printed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ai.rt import protocol as P                                       # noqa: E402
from app.ai.rt.connection import PersistentConnection, RtConfig          # noqa: E402
from tools.realtime import stages as S                                    # noqa: E402


def stats(values: list[float]) -> dict:
    v = sorted(values)
    if not v:
        return {"median": None, "p90": None, "n": 0}
    mid = len(v) // 2
    return {"median": round(v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2, 2), "p90": round(S.pctl(v, 90), 2), "min": round(v[0], 2), "max": round(v[-1], 2), "n": len(v)}


async def probe(connector, url: str, secret: bytes, n: int, interval_s: float, call_id: str = "g1-probe") -> dict:
    conn = PersistentConnection("tts", url, call_id, lambda: secret, connector, RtConfig(hb_interval_s=30.0, hb_timeout_s=90.0, request_timeout_s=10.0))
    await conn.ensure_ready()
    pings, warm = [], []
    ws = conn._ws
    for i in range(n):
        t0 = time.perf_counter()
        # the bare round trip: a ping answered by a pong; the connection's own reader sets _last_pong when it arrives
        before = conn._last_pong
        await ws.send(P.dumps({"type": "ping", "n": i}))
        while conn._last_pong == before:
            await asyncio.sleep(0)
            if time.perf_counter() - t0 > 5.0:
                break
        pings.append((time.perf_counter() - t0) * 1000.0)
        t1 = time.perf_counter()
        async for _ in conn.request({"type": "warmup"}):
            pass
        warm.append((time.perf_counter() - t1) * 1000.0)
        await asyncio.sleep(interval_s)
    await conn.close()
    return {"rt_roundtrip_ms": stats(pings), "warmup_request_ms": stats(warm)}


def evidence(tag: str, summary: dict, declared_from: str) -> dict:
    return {"schema": 1, "tag": tag, "scenario": {"mode": "rtt_probe", "measured_from": declared_from}, "n_utterances": 0, "n_errors": 0, "summary": summary, "utterances": []}


async def run_selftest(n: int) -> dict:
    import importlib.util
    from app.ai.rt.testing import make_pair
    spec = importlib.util.spec_from_file_location("rt_worker_common", ROOT / "deploy" / "rt_workers" / "common.py")
    W = importlib.util.module_from_spec(spec)
    sys.modules["rt_worker_common"] = W          # a dataclass with postponed annotations looks its module up in sys.modules
    spec.loader.exec_module(W)
    secret = b"selftest"

    class NoEngine:
        async def synth_stream(self, *a, **k):
            if False:
                yield b""

    async def connector(url):
        a, b = make_pair()
        asyncio.ensure_future(W.serve_connection(b, service="tts", engine=NoEngine(), secret=secret))
        return a
    return evidence("SIMULATED", await probe(connector, "ws://selftest", secret, n, 0.0), "selftest")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--url", default="")
    ap.add_argument("--secret-env", default="RT_SECRET")
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--interval-ms", type=float, default=50.0)
    ap.add_argument("--declared-from", choices=("pc", "cloud"), default="")
    ap.add_argument("--approved", default="")
    ap.add_argument("--evidence", default="")
    a = ap.parse_args(argv)
    if a.selftest:
        ev = asyncio.run(run_selftest(min(a.n, 100)))
    else:
        if not a.approved.strip():
            print("refused: no --approved id (the probe talks to a running server; the server is a chargeable Pod)")
            return 2
        if not a.url.startswith(("wss://", "ws://")) or not a.declared_from:
            print("refused: --url wss://... and --declared-from pc|cloud are required")
            return 2
        secret = os.environ.get(a.secret_env, "").strip().encode()
        if not secret:
            print("refused: the secret environment variable is empty")
            return 2
        from app.ai.rt.connection import websockets_connector
        ev = evidence("MEASURED", asyncio.run(probe(websockets_connector, a.url, secret, a.n, a.interval_ms / 1000.0)), a.declared_from)
    if a.evidence:
        Path(a.evidence).write_text(json.dumps(ev, indent=1), encoding="utf-8")
    print(json.dumps({"tag": ev["tag"], "measured_from": ev["scenario"]["measured_from"], "summary": ev["summary"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
