#!/bin/bash
# Proves backup+restore against the LOCAL test MongoDB: seeds a scratch DB, dumps it, restores into a second DB,
# compares document counts and index names. Needs MongoDB Database Tools (set MONGODUMP/MONGORESTORE or put on PATH).
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="${MME_VENV:-$HOME/MME_TELEPHONY/.venv}/bin:$HOME/mongo-tools/bin:$PATH" PYTHONPATH=.
bash scripts/dev_mongo.sh >/dev/null 2>&1
export MME_TEL_MONGO_URI=mongodb://127.0.0.1:27117 MME_TEL_MONGO_DB=mme_tel_bk_src MME_TEL_RESTORE_DB=mme_tel_bk_dst
python - <<'EOF'
import asyncio, os, sys
sys.path.insert(0, ".")
from tests.conftest import make_settings
from app.persistence.db import Database
from app.persistence.repos import Repos
from app.core.ids import utcnow
async def main():
    s = make_settings(mongo_db="mme_tel_bk_src"); d = Database(s); await d.connect(); r = Repos(d, s)
    for i in range(50):
        await r.insert_call({"_id": f"call_bk{i}", "tenant_id": "t1", "direction": "outbound", "provider": "mock", "from_number": "+911140000000",
                             "to_number": "+919876543210", "state": "completed", "state_version": 4, "queued_at": utcnow()})
        await r.add_event({"_id": f"call_bk{i}", "tenant_id": "t1"}, "state.completed", "test", f"d{i}")
    await r.put_policy("t1", {"enabled": True})
    await d.close()
asyncio.run(main())
EOF
OUT="$(mktemp -d)"
bash scripts/backup_mongo.sh "$OUT"
ARCH="$(ls "$OUT"/*.archive.gz)"
bash scripts/restore_mongo.sh "$ARCH"
python - <<'EOF'
import asyncio
from pymongo import AsyncMongoClient
async def main():
    c = AsyncMongoClient("mongodb://127.0.0.1:27117"); ok = True
    for coll in ("calls", "call_events", "tenant_policies", "_schema"):
        a = await c["mme_tel_bk_src"][coll].count_documents({}); b = await c["mme_tel_bk_dst"][coll].count_documents({})
        ia = sorted((await c["mme_tel_bk_src"][coll].index_information()).keys()); ib = sorted((await c["mme_tel_bk_dst"][coll].index_information()).keys())
        print(f"{coll:16s} src={a:3d} restored={b:3d} indexes_equal={ia == ib}"); ok &= (a == b and ia == ib)
    await c.drop_database("mme_tel_bk_src"); await c.drop_database("mme_tel_bk_dst"); await c.close()
    print("BACKUP/RESTORE VERIFIED" if ok else "MISMATCH"); raise SystemExit(0 if ok else 1)
asyncio.run(main())
EOF
rm -rf "$OUT"
