#!/usr/bin/env python3
"""Script to remove plaintext API keys from tenant ai_config document.

UNSETS:
  - keys
  - stt_api_key
  - brain_api_key
  - tts_api_key

Safety guard: Refuses to strip plaintext keys unless indirect key_refs are ALREADY configured and verified.
Default is dry-run (--apply required to execute).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pymongo import AsyncMongoClient
from config.settings import Settings
from scripts.backup_ai_config import backup_tenant_ai_config


async def strip_plaintext_keys(tenant_id: str, apply: bool = False, mongo_uri: str | None = None, db_name: str | None = None) -> dict:
    s = Settings()
    raw_uri = s.mongo_uri.get_secret_value() if hasattr(s.mongo_uri, "get_secret_value") else str(s.mongo_uri)
    uri = mongo_uri or raw_uri or "mongodb://127.0.0.1:27117"
    dbname = db_name or s.mongo_db or "mme_telephony"
    client = AsyncMongoClient(uri)
    db = client[dbname]

    try:
        doc = await db["ai_config"].find_one({"tenant_id": tenant_id})
        if not doc:
            raise RuntimeError(f"No ai_config document found for tenant '{tenant_id}' in db '{dbname}'")

        # 1. Safety verification: key_refs MUST exist and be populated
        key_refs = doc.get("key_refs")
        if not isinstance(key_refs, dict) or not all(k in key_refs for k in ("stt", "brain", "tts")):
            raise RuntimeError(
                f"SAFETY REFUSAL: Cannot strip plaintext keys for tenant '{tenant_id}'. "
                f"indirect key_refs are missing or incomplete: {key_refs}. "
                "Apply key_refs first."
            )

        fields_to_unset = {}
        if "keys" in doc:
            fields_to_unset["keys"] = ""
        for k in ("stt_api_key", "brain_api_key", "tts_api_key"):
            if k in doc:
                fields_to_unset[k] = ""

        print(f"Plaintext fields found to remove: {list(fields_to_unset.keys())}")
        if not fields_to_unset:
            print(f"Tenant '{tenant_id}' already has no plaintext key fields.")
            return {"status": "no_op", "removed": []}

        if not apply:
            print("[DRY-RUN] Would backup and remove plaintext key fields. Run with --apply to execute.")
            return {"status": "dry_run", "would_remove": list(fields_to_unset.keys())}

        # 2. Pre-change backup
        backup_path = await backup_tenant_ai_config(tenant_id, mongo_uri=uri, db_name=dbname)
        print(f"Pre-change backup saved to: {backup_path}")

        # 3. Strip plaintext keys
        now = datetime.now(timezone.utc).isoformat()
        res = await db["ai_config"].update_one(
            {"tenant_id": tenant_id},
            {
                "$unset": fields_to_unset,
                "$set": {"plaintext_keys_stripped_at": now}
            }
        )
        print(f"Strip completed: {res.modified_count} document(s) modified.")
        return {"status": "stripped", "removed": list(fields_to_unset.keys()), "backup": str(backup_path)}
    finally:
        await client.close()


async def main():
    parser = argparse.ArgumentParser(description="Strip plaintext keys from tenant ai_config after key_refs are active")
    parser.add_argument("--tenant", default="mme-test", help="Tenant ID")
    parser.add_argument("--apply", action="store_true", help="Execute the deletion (default is dry-run)")
    parser.add_argument("--mongo-uri", help="Optional MongoDB URI override")
    parser.add_argument("--db-name", help="Optional MongoDB database name override")
    args = parser.parse_args()

    result = await strip_plaintext_keys(args.tenant, apply=args.apply, mongo_uri=args.mongo_uri, db_name=args.db_name)
    if result["status"] == "stripped":
        print("Plaintext keys successfully stripped from tenant document.")


if __name__ == "__main__":
    asyncio.run(main())
