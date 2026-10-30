#!/usr/bin/env python3
"""Prepares tenant configuration for supervised call.

Sets ONLY these ai_config fields:
  - tts_ulaw8k, devanagari_hindi, streaming_pipeline, full_duplex_input: True; tts_voice: "utkarsh"   (as before)
  - latency work:  stream_ramp, tts_adaptive_poll, trace_evidence: True
  - quality work:  glossary_correction, strict_grounding: True

NOT set (listed in every run with their current values): stt_raw_pcm, faq_grounding, ack_enabled, keepalive_enabled (all default OFF), and reply_guard, which has
NO tenant flag in this tree (reply_guard.py is an unwired file; there is nothing to switch).
Every flag set here is in the allowlist of AiService.get_config (tests/unit/test_tenant_key_scripts.py verifies it): a flag missing from that allowlist would be silently
dropped before it reaches the runtime, which is exactly how key_refs was lost once.

Does NOT set allowed_callers or max_duration_s (those live in tenant policy).
Performs automatic timestamped backup prior to applying.
Default is dry-run (--apply required to execute).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pymongo import AsyncMongoClient
from scripts.backup_ai_config import backup_tenant_ai_config

TENANT_ID = "mme-test"
SUPERVISED_FLAGS = {
    "tts_ulaw8k": True,
    "devanagari_hindi": True,
    "streaming_pipeline": True,
    "full_duplex_input": True,
    "tts_voice": "utkarsh",
    "stream_ramp": True,
    "tts_adaptive_poll": True,
    "trace_evidence": True,
    "glossary_correction": True,
    "strict_grounding": True,
}
# Flags that stay as they are: shown, never written. reply_guard has no flag (module not wired).
LISTED_NOT_SET = {"stt_raw_pcm": "default OFF", "faq_grounding": "default OFF (module not in this tree)", "ack_enabled": "default OFF (module not in this tree)",
                  "keepalive_enabled": "default OFF (module not in this tree)", "reply_guard": "no tenant flag exists; nothing to set"}


async def prepare_supervised_call(apply: bool = False, mongo_uri: str | None = None, db_name: str | None = None) -> dict:
    uri = mongo_uri or "mongodb://127.0.0.1:27117"
    dbname = db_name or "mme_telephony"
    client = AsyncMongoClient(uri)
    db = client[dbname]

    try:
        doc = await db["ai_config"].find_one({"tenant_id": TENANT_ID})
        if not doc:
            raise RuntimeError(f"Tenant '{TENANT_ID}' document not found in ai_config")

        current = {k: doc.get(k) for k in SUPERVISED_FLAGS}
        print("=" * 65)
        print(f"TENANT SUPERVISED CALL PREPARATION ({'APPLY' if apply else 'DRY-RUN'})")
        print("=" * 65)
        print(f"Tenant:         {TENANT_ID}")
        print(f"Database:       {dbname}")
        print("Target Flags (ai_config ONLY):")
        for k, v in SUPERVISED_FLAGS.items():
            print(f"  {k:22s}: current={current.get(k)!r} -> proposed={v!r}")
        print("Listed, NOT set:")
        for k, why in LISTED_NOT_SET.items():
            print(f"  {k:22s}: current={doc.get(k)!r}  ({why})")

        if not apply:
            print("\n[DRY-RUN] No database modifications made.")
            print("Run with --apply to execute.")
            return {"status": "dry_run", "current": current, "proposed": SUPERVISED_FLAGS}

        # 1. Automatic pre-change backup
        backup_path = await backup_tenant_ai_config(TENANT_ID, mongo_uri=uri, db_name=dbname)
        print(f"\nPre-change backup saved to:\n  {backup_path}")

        # 2. Update tenant document
        res = await db["ai_config"].update_one(
            {"tenant_id": TENANT_ID},
            {"$set": SUPERVISED_FLAGS}
        )
        print(f"\nApplied successfully: {res.modified_count} document modified.")
        return {"status": "applied", "backup": str(backup_path), "proposed": SUPERVISED_FLAGS}
    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser(description="Prepare tenant ai_config for supervised call (Dry-run by default)")
    parser.add_argument("--apply", action="store_true", help="Apply the configuration (default is dry-run)")
    parser.add_argument("--mongo-uri", help="Optional MongoDB URI override")
    parser.add_argument("--db-name", help="Optional MongoDB database name override")
    args = parser.parse_args()

    asyncio.run(prepare_supervised_call(apply=args.apply, mongo_uri=args.mongo_uri, db_name=args.db_name))


if __name__ == "__main__":
    main()
