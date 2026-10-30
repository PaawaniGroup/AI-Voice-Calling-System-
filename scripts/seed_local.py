"""Seed initial local MongoDB data for development/testing."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.persistence.db import Database
from config.settings import Settings


async def main():
    s = Settings()
    db = Database(s)
    await db.connect()
    
    # 1. Ensure Tenant Policy for mme-test
    await db["tenant_policies"].update_one(
        {"tenant_id": "mme-test"},
        {"$set": {
            "tenant_id": "mme-test",
            "enabled": True,
            "max_active_calls": 10,
            "max_duration_s": 1800,
            "caller_ids": ["+918065530418"],
            "allowed_countries": ["IN"],
            "recording": "off"
        }},
        upsert=True
    )
    print("Policy for 'mme-test' seeded (enabled: true, caller_id: +918065530418).")

    # 2. Ensure Inbound Route for +918065530418
    await db["inbound_routes"].update_one(
        {"number": "+918065530418"},
        {"$set": {
            "number": "+918065530418",
            "tenant_id": "mme-test",
            "action": "ai",
            "enabled": True
        }},
        upsert=True
    )
    print("Inbound route for '+918065530418' seeded (tenant: mme-test, action: ai).")

    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
