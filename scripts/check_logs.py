import asyncio
import httpx
import json

async def main():
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8800") as client:
        r = await client.post("/v1/auth/login", json={"username": "admin", "password": "AdminPass1234!"})
        token = r.json()["access_token"]
        logs = await client.get("/v1/logs/errors?limit=50", headers={"Authorization": f"Bearer {token}"})
        print("ERRORS / WARNINGS:")
        for x in logs.json().get("items", []):
            print(f"[{x.get('ts')}] {x.get('level')} ({x.get('logger')}): {x.get('msg')}")

if __name__ == "__main__":
    asyncio.run(main())
