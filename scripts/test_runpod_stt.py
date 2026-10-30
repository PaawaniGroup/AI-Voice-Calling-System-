import httpx
import json
import time
import sys
sys.path.insert(0, '/mnt/c/Users/Admin/Desktop/MME_Telephony')
from config.settings import Settings
from app.persistence.db import Database
import asyncio

async def test_all():
    s = Settings()
    db = Database(s)
    await db.connect()
    c = await db['ai_config'].find_one({'tenant_id': 'mme-test'})
    keys = c.get('keys') or {}
    
    endpoints = {
        'stt': (c.get('stt_url'), keys.get('stt')),
        'brain': (c.get('brain_url'), keys.get('brain')),
        'tts': (c.get('tts_url'), keys.get('tts')),
    }
    
    for svc, (url, key) in endpoints.items():
        print(f"\n--- Testing {svc.upper()} ---")
        print("URL:", url)
        print("Key:", (key[:8] + '...') if key else 'None')
        if not url or not key:
            print("Missing URL or key!")
            continue
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        body = {"input": {"prompt": "Hello"}}
        try:
            with httpx.Client(timeout=10.0) as client:
                r = client.post(url, json=body, headers=headers)
                print("Status code:", r.status_code)
                print("Response:\n", r.text[:300])
        except Exception as e:
            print("Error:", type(e).__name__, str(e))

asyncio.run(test_all())
