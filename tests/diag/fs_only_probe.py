"""Control experiment (dev tool, NOT a test): is FreeSWITCH's media timing stable on this host WITHOUT the app, the module,
the WebSocket or MongoDB?

Each iteration originates the same loopback "caller" as the AI tests (extension 9010 in the isolated mme_test context: answer,
record_session, sleep 1.5 s, playback of a 1.2 s tone) over a bare ESL connection, then measures how long FreeSWITCH says the tone
playback took, on FreeSWITCH's OWN event clock (PLAYBACK_START -> PLAYBACK_STOP). Healthy = about 1.2 s. It also samples this
process's event-loop lag, system CPU and the wall-vs-monotonic clock offset. No carrier, no models, no module.

Usage: python tests/diag/fs_only_probe.py --n 100 --out /tmp/fsmedia_diag/fs_only.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import psutil  # noqa: E402

from app.telephony.esl import EslCommandClient  # noqa: E402
from esl_check import esl_password  # noqa: E402
from tests.diag.fs_media_trace import Tap, Trace, write_caller_wav  # noqa: E402


async def lag_sampler(stats: dict, stop: asyncio.Event):
    while not stop.is_set():
        t = time.monotonic()
        await asyncio.sleep(0.01)
        stats["max_lag_ms"] = max(stats.get("max_lag_ms", 0.0), (time.monotonic() - t - 0.01) * 1000)


async def one(idx: int, cmd: EslCommandClient) -> dict:
    tr = Trace()
    tap = Tap(tr)
    await tap.start()
    stats: dict = {}
    stop = asyncio.Event()
    sampler = asyncio.create_task(lag_sampler(stats, stop))
    psutil.cpu_percent(None)
    off0 = time.time() - time.monotonic()
    u = str(uuid.uuid4())
    out = {"idx": idx}
    try:
        await cmd.api(f"originate {{origination_uuid={u},mme_leg=a}}loopback/9010/mme_test &park()")
        end = time.monotonic() + 8
        while time.monotonic() < end:
            if any(r["ev"] == "PLAYBACK_STOP" and "9010-b" in r.get("chan", "") for r in tr.rows):
                break
            await asyncio.sleep(0.05)
        ps = [r for r in tr.rows if r["ev"] == "PLAYBACK_START" and "9010-b" in r.get("chan", "")]
        pe = [r for r in tr.rows if r["ev"] == "PLAYBACK_STOP" and "9010-b" in r.get("chan", "")]
        ans = [r for r in tr.rows if r["ev"] == "CHANNEL_ANSWER" and "9010-b" in r.get("chan", "")]
        if ps and pe:
            out["fs_clock_playback_s"] = round((int(pe[0]["fsts"]) - int(ps[0]["fsts"])) / 1e6, 3)
            out["arrival_playback_s"] = round(pe[0]["t"] - ps[0]["t"], 3)
        if ans and ps:
            out["fs_clock_answer_to_tone_s"] = round((int(ps[0]["fsts"]) - int(ans[0]["fsts"])) / 1e6, 3)  # dialplan says 1.5 s
        out["complete"] = bool(ps and pe)
    finally:
        out["max_loop_lag_ms"] = round(stats.get("max_lag_ms", 0.0), 1)
        out["cpu_percent"] = psutil.cpu_percent(None)
        out["load1"] = round(psutil.getloadavg()[0], 2)
        out["clock_offset_changed"] = round((time.time() - time.monotonic()) - off0, 2)
        stop.set()
        await sampler
        try:
            await cmd.api(f"uuid_kill {u}")
        except Exception:
            pass
        await tap.stop()
        await asyncio.sleep(0.3)
    return out


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--out", default="/tmp/fsmedia_diag/fs_only.json")
    a = ap.parse_args()
    write_caller_wav()
    cmd = EslCommandClient("127.0.0.1", 8021, esl_password(), connect_timeout=5, reconnect_initial=0.5, reconnect_max=2, jitter=0.1)
    rows = []
    for i in range(a.n):
        r = await one(i, cmd)
        rows.append(r)
        bad = (not r.get("complete")) or r.get("fs_clock_playback_s", 0) > 1.6
        print(f"iter {i:03d} fs_playback_s={r.get('fs_clock_playback_s')} to_tone_s={r.get('fs_clock_answer_to_tone_s')} lag_ms={r['max_loop_lag_ms']} "
              f"cpu={r['cpu_percent']} {'STALL' if bad else ''}", flush=True)
    await cmd.stop()
    stalls = [r for r in rows if (not r.get("complete")) or r.get("fs_clock_playback_s", 0) > 1.6]
    summ = {"runs": len(rows), "stalls": len(stalls), "stall_rate": round(len(stalls) / len(rows), 3) if rows else None,
            "stall_idx": [r["idx"] for r in stalls]}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps({"summary": summ, "rows": rows}, indent=1))
    print(json.dumps(summ))


if __name__ == "__main__":
    asyncio.run(main())
