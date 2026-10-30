#!/usr/bin/env python3
"""Script to apply key_refs indirect environment variable names to tenant ai_config.

Sets:
  key_refs: {
      "stt": "RUNPOD_STT_KEY",
      "brain": "RUNPOD_BRAIN_KEY",
      "tts": "RUNPOD_TTS_KEY"
  }
No secret values are written. Only standard variable names.
Performs an automatic backup of the ai_config document before updating.
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

STANDARD_KEY_REFS = {
    "stt": "RUNPOD_STT_KEY",
    "brain": "RUNPOD_BRAIN_KEY",
    "tts": "RUNPOD_TTS_KEY"
}


async def apply_key_refs(tenant_id: str, apply: bool = False, mongo_uri: str | None = None, db_name: str | None = None) -> dict:
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

        current_key_refs = doc.get("key_refs")
        print(f"Current key_refs for tenant '{tenant_id}': {current_key_refs}")
        print(f"Proposed key_refs: {STANDARD_KEY_REFS}")

        if not apply:
            print("[DRY-RUN] Would perform backup and update document. Run with --apply to execute.")
            return {"status": "dry_run", "proposed": STANDARD_KEY_REFS}

        # 1. Automatic pre-change backup
        backup_path = await backup_tenant_ai_config(tenant_id, mongo_uri=uri, db_name=dbname)
        print(f"Automatic backup saved to: {backup_path}")

        # 2. Update document with indirect variable names only
        now = datetime.now(timezone.utc).isoformat()
        res = await db["ai_config"].update_one(
            {"tenant_id": tenant_id},
            {
                "$set": {
                    "key_refs": STANDARD_KEY_REFS,
                    "key_refs_updated_at": now
                }
            }
        )
        print(f"Update completed: {res.modified_count} document(s) modified.")
        return {"status": "applied", "modified_count": res.modified_count, "backup": str(backup_path)}
    finally:
        await client.close()


async def main():
    parser = argparse.ArgumentParser(description="Configure indirect key_refs for tenant ai_config")
    parser.add_argument("--tenant", default="mme-test", help="Tenant ID")
    parser.add_argument("--apply", action="store_true", help="Execute the update (default is dry-run)")
    parser.add_argument("--mongo-uri", help="Optional MongoDB URI override")
    parser.add_argument("--db-name", help="Optional MongoDB database name override")
    args = parser.parse_args()

    result = await apply_key_refs(args.tenant, apply=args.apply, mongo_uri=args.mongo_uri, db_name=args.db_name)
    if result["status"] == "applied":
        print("key_refs configuration successfully applied.")


if __name__ == "__main__":
    asyncio.run(main())
