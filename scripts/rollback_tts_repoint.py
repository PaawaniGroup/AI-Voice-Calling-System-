#!/usr/bin/env python3
"""Rollback script for tenant voice endpoint repoint.

Safely restores the tenant's TTS endpoint configuration to the previous state.
Supports --db-name for automated testing against throwaway databases.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.persistence.db import Database
from config.settings import Settings


async def rollback_tts(
    tenant_id: str,
    original_url: str = "https://api.runpod.ai/v2/93tqgnxb5dyhvx/runsync",
    original_model: str = "chatterbox",
    original_timeout: float = 60.0,
    db_name: str | None = None,
) -> bool:
    s = Settings()
    if db_name:
        s.mongo_db = db_name

    db = Database(s)
    await db.connect()

    try:
        coll = db.db["ai_config"]
        # Restore original endpoint URL, model, path, and timeout
        # Remove any repointed key_refs.tts
        update = {
            "tts_url": original_url,
            "models.tts": original_model,
            "paths.tts": "/runsync",
            "timeouts.tts": original_timeout,
        }
        res = await coll.update_one(
            {"tenant_id": tenant_id},
            {
                "$set": update,
                "$unset": {"key_refs.tts": ""}
            }
        )
        return res.acknowledged
    finally:
        await db.close()


async def main():
    parser = argparse.ArgumentParser(description="Rollback tenant voice endpoint repoint")
    parser.add_argument("--tenant", default="mme-test", help="Tenant ID")
    parser.add_argument("--db-name", help="Database name override (for testing)")
    args = parser.parse_args()

    ok = await rollback_tts(args.tenant, db_name=args.db_name)
    if ok:
        print(f"Successfully rolled back TTS configuration for tenant '{args.tenant}'.")
    else:
        sys.exit(f"Rollback failed for tenant '{args.tenant}'.")


if __name__ == "__main__":
    asyncio.run(main())
