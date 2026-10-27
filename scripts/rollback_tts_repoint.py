#!/usr/bin/env python3
"""Rollback script for tenant tts_url repoint."""
import asyncio
from pymongo import AsyncMongoClient

async def rollback():
    client = AsyncMongoClient("mongodb://127.0.0.1:27117")
    db = client["mme_telephony"]
    try:
        res = await db["ai_config"].update_one(
            {"tenant_id": "mme-test"},
            {"$set": {"tts_url": "https://api.runpod.ai/v2/93tqgnxb5dyhvx/runsync"}}
        )
        print(f"Rollback completed: reverted tts_url to https://api.runpod.ai/v2/93tqgnxb5dyhvx/runsync")
    finally:
        await client.close()

if __name__ == "__main__":
    asyncio.run(rollback())
