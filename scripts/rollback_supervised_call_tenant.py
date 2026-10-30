#!/usr/bin/env python3
"""Rollback script for tenant supervised call flags.

Restores the exact previous field states recorded in the pre-change backup
(or unsets them if they were not present).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pymongo import AsyncMongoClient
from scripts.backup_ai_config import get_default_backup_dir

TENANT_ID = "mme-test"
SUPERVISED_KEYS = ("tts_ulaw8k", "devanagari_hindi", "streaming_pipeline", "full_duplex_input", "tts_voice",
                   "stream_ramp", "tts_adaptive_poll", "trace_evidence", "glossary_correction", "strict_grounding")

# Corrected one-line rollback command restoring previous unconfigured state (ONLY for a tenant that had none of these fields before; the script above restores the
# previous values from the backup):
ONE_LINE_ROLLBACK = (
    "python -c \"import asyncio; from pymongo import AsyncMongoClient; "
    "asyncio.run(AsyncMongoClient('mongodb://127.0.0.1:27117')['mme_telephony']['ai_config']"
    ".update_one({'tenant_id': 'mme-test'}, "
    "{'$unset': {'tts_ulaw8k': '', 'devanagari_hindi': '', 'streaming_pipeline': '', "
    "'full_duplex_input': '', 'tts_voice': '', 'stream_ramp': '', 'tts_adaptive_poll': '', 'trace_evidence': '', "
    "'glossary_correction': '', 'strict_grounding': ''}}))\""
)


def find_latest_backup(backup_dir: Path, tenant_id: str) -> Path | None:
    if not backup_dir.exists():
        return None
    matches = sorted(backup_dir.glob(f"ai_config_{tenant_id}_*.json"))
    return matches[-1] if matches else None


async def rollback_supervised_call(backup_file: Path | None = None, mongo_uri: str | None = None, db_name: str | None = None) -> dict:
    uri = mongo_uri or "mongodb://127.0.0.1:27117"
    dbname = db_name or "mme_telephony"
    client = AsyncMongoClient(uri)
    db = client[dbname]

    try:
        # Load previous values from backup if available
        bp = backup_file or find_latest_backup(get_default_backup_dir(), TENANT_ID)
        prev_cfg = {}
        if bp and bp.exists():
            data = json.loads(bp.read_text(encoding="utf-8"))
            prev_cfg = data.get("config", {})
            print(f"Reading previous state from backup: {bp}")
        else:
            print("No backup file found; falling back to unsetting all supervised call keys.")

        to_set = {}
        to_unset = {}

        for k in SUPERVISED_KEYS:
            if k in prev_cfg:
                to_set[k] = prev_cfg[k]
            else:
                to_unset[k] = ""

        update_op = {}
        if to_set:
            update_op["$set"] = to_set
        if to_unset:
            update_op["$unset"] = to_unset

        print("Rollback operations:")
        if to_set:
            print(f"  $set:   {to_set}")
        if to_unset:
            print(f"  $unset: {list(to_unset.keys())}")

        res = await db["ai_config"].update_one(
            {"tenant_id": TENANT_ID},
            update_op
        )
        print(f"Rollback completed: {res.modified_count} document modified.")
        # READ-ONLY check (this script never changes the policy): is the tenant disarmed? Disarming is `plivo_testcall.py disarm --yes` (policy.enabled = False).
        try:
            pol = await db["tenant_policies"].find_one({"tenant_id": TENANT_ID})
            policy_enabled = bool(pol.get("enabled")) if isinstance(pol, dict) else None
        except Exception:
            policy_enabled = None
        state = {True: "ENABLED (STILL ARMED: run `python scripts/plivo_testcall.py disarm --yes`)", False: "DISABLED (disarmed)", None: "UNKNOWN (no policy document readable)"}[policy_enabled]
        print(f"Tenant policy after the rollback: {state}")
        return {"status": "rolled_back", "modified_count": res.modified_count, "backup_used": str(bp) if bp else None, "policy_enabled": policy_enabled}
    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser(description="Rollback tenant supervised call configuration")
    parser.add_argument("--backup", help="Path to specific backup JSON file")
    parser.add_argument("--mongo-uri", help="Optional MongoDB URI override")
    parser.add_argument("--db-name", help="Optional MongoDB database name override")
    args = parser.parse_args()

    bp = Path(args.backup) if args.backup else None
    res = asyncio.run(rollback_supervised_call(backup_file=bp, mongo_uri=args.mongo_uri, db_name=args.db_name))
    if res.get("policy_enabled") is True:
        sys.exit(2)                                  # the flags are rolled back, but the tenant is still armed: not a clean end state


if __name__ == "__main__":
    main()
