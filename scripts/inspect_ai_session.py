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
    # Check audit logs
    audits = await db["audit_logs"].find({"entity_id": "call_9e558f459c9846a99a6a773d"}).to_list(100)
    print("AUDIT LOGS FOR CALL:")
    for a in audits:
        print(a)
    
    # Check session
    sess = await db["sessions"].find_one({"_id": "9b9deaf6-d785-4470-8ede-0a56f0e7d5c5"})
    print("\nSESSION:")
    print(sess)

    # Check ai_sessions
    ai_sess = await db["ai_sessions"].find_one({"session_id": "9b9deaf6-d785-4470-8ede-0a56f0e7d5c5"})
    print("\nAI SESSION:")
    print(ai_sess)

    # Check provider_dedup
    dedups = await db["provider_dedup"].find({"key": {"$regex": "7d39edbe"}}).to_list(100)
    print("\nDEDUPS:")
    for d in dedups:
        print(d)

    await db.close()

if __name__ == "__main__":
    asyncio.run(main())
