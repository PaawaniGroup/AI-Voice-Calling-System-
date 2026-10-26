#!/usr/bin/env python3
"""Rollback script for tenant key_refs configuration.

Removes the key_refs mapping from the tenant's ai_config document, returning the tenant
to direct key resolution if needed.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pymongo import AsyncMongoClient
from config.settings import Settings


async def rollback_key_refs(tenant_id: str, apply: bool = False, mongo_uri: str | None = None, db_name: str | None = None) -> dict:
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

        key_refs = doc.get("key_refs")
        print(f"Current key_refs for tenant '{tenant_id}': {key_refs}")

        if not apply:
            print("[DRY-RUN] Would remove key_refs from document. Run with --apply to execute.")
            return {"status": "dry_run", "current": key_refs}

        res = await db["ai_config"].update_one(
            {"tenant_id": tenant_id},
            {
                "$unset": {
                    "key_refs": "",
                    "key_refs_updated_at": ""
                }
            }
        )
        print(f"Rollback completed: {res.modified_count} document(s) modified.")
        return {"status": "rolled_back", "modified_count": res.modified_count}
    finally:
        await client.close()


async def main():
    parser = argparse.ArgumentParser(description="Rollback indirect key_refs for tenant ai_config")
    parser.add_argument("--tenant", default="mme-test", help="Tenant ID")
    parser.add_argument("--apply", action="store_true", help="Execute the rollback (default is dry-run)")
    parser.add_argument("--mongo-uri", help="Optional MongoDB URI override")
    parser.add_argument("--db-name", help="Optional MongoDB database name override")
    args = parser.parse_args()

    result = await rollback_key_refs(args.tenant, apply=args.apply, mongo_uri=args.mongo_uri, db_name=args.db_name)
    if result["status"] == "rolled_back":
        print("key_refs successfully rolled back.")


if __name__ == "__main__":
    asyncio.run(main())
