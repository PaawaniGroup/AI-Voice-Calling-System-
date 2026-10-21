"""Collect the persisted evidence for ONE real call into a JSON file (for docs/evidence/).

    MME_TEL_MONGO_URI=... MME_TEL_MONGO_DB=mme_telephony python scripts/collect_call_evidence.py <call_id> [--out FILE] [--full-numbers]

Reads (never writes) the call, its session, AI session (state, turns, per-stage timings), lifecycle events with timestamps,
and recording metadata. Phone numbers are masked unless --full-numbers is given. Nothing is fabricated: a missing record is
reported as missing. The output is evidence only if the call was a real one (check `call.provider` / how it was placed).
"""
import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.logging import mask_phone  # noqa: E402
from app.persistence.db import Database  # noqa: E402
from config.settings import Settings  # noqa: E402

DROP = {"purge_at"}


def clean(doc, full: bool):
    if isinstance(doc, dict):
        out = {}
        for k, v in doc.items():
            if k in DROP:
                continue
            if not full and k in ("from_number", "to_number", "bridge_to", "caller", "destination") and isinstance(v, str):
                v = mask_phone(v)
            out[k] = clean(v, full)
        return out
    if isinstance(doc, list):
        return [clean(x, full) for x in doc]
    if isinstance(doc, datetime):
        return doc.astimezone(timezone.utc).isoformat()
    return doc


async def collect(db, call_id: str, full: bool) -> dict:
    call = await db["calls"].find_one({"_id": call_id})
    if not call:
        return {"call_id": call_id, "error": "call not found"}
    sid = call.get("session_id")
    out = {"collected_at": datetime.now(timezone.utc).isoformat(), "call": call,
           "session": await db["sessions"].find_one({"_id": sid}) if sid else None,
           "ai_session": await db["ai_sessions"].find_one({"session_id": sid}) if sid else None,
           "events": await db["call_events"].find({"call_id": call_id}).sort("ts", 1).to_list(1000),
           "recordings": await db["recordings"].find({"call_id": call_id}).to_list(20)}
    out["missing"] = [k for k in ("session", "ai_session") if not out[k]]
    return clean(out, full)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("call_id"), ap.add_argument("--out"), ap.add_argument("--full-numbers", action="store_true")
    a = ap.parse_args()
    db = Database(Settings(_env_file=None, mongo_init_indexes=False))
    await db.connect()
    try:
        res = await collect(db, a.call_id, a.full_numbers)
    finally:
        await db.close()
    text = json.dumps(res, indent=2, default=str, ensure_ascii=False)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(text, encoding="utf-8")
    print(text)
    return 1 if res.get("error") else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
