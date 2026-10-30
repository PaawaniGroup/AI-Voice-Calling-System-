import asyncio
import sys
import json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.persistence.db import Database
from config.settings import Settings

async def main():
    s = Settings()
    db = Database(s)
    await db.connect()
    call_id = sys.argv[1] if len(sys.argv) > 1 else None
    if call_id:
        call = await db["calls"].find_one({"_id": call_id})
    else:
        call = await db["calls"].find_one(sort=[("created_at", -1)])
    if call:
        call_id = call["_id"]
        call["_id"] = str(call["_id"])
        # convert datetimes
        for k, v in list(call.items()):
            if hasattr(v, "isoformat"):
                call[k] = v.isoformat()
        print(json.dumps(call, indent=2))
        ai = await db["ai_sessions"].find_one({"session_id": call.get("session_id")})
        if ai:
            for k, v in list(ai.items()):
                if hasattr(v, "isoformat"):
                    ai[k] = v.isoformat()
            print("\nAI SESSION:")
            print(json.dumps(ai, indent=2))
    else:
        print("Call not found!")

    events = await db["call_events"].find({"call_id": call_id}).to_list(100)
    print("\nEVENTS:")
    for e in events:
        print(f"[{e.get('ts')}] {e.get('type')} ({e.get('source')}): {e.get('payload')}")

if __name__ == "__main__":
    asyncio.run(main())
