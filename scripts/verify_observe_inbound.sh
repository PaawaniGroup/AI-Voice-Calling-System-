#!/bin/bash
# Verifies record-only inbound handling against the REAL local FreeSWITCH `public` context (your DID dialplan) using a
# LOOPBACK call (no carrier, no cost): starts the service with MME_TEL_OBSERVE_CONTEXTS=public against the local test
# MongoDB, injects one inbound call to the DID, and checks that a call + session were recorded and that nothing was
# commanded (the service's FreeSWITCH dialplan result is untouched). Dev only.
set -uo pipefail
cd "$(dirname "$0")/.."
export PATH="${MME_VENV:-$HOME/MME_TELEPHONY/.venv}/bin:$PATH" PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1
DID="${1:-918065530418}"
bash scripts/dev_mongo.sh >/dev/null 2>&1
export MME_TEL_ENV=development MME_TEL_MONGO_URI=mongodb://127.0.0.1:27117 MME_TEL_MONGO_DB=mme_telephony_observe \
       MME_TEL_JWT_SECRET="dev-only-secret-$(date +%s)-xxxxxxxxxxxxxxxx" MME_TEL_WEBHOOK_SIGNING_MASTER="dev-only-master-xxxxxxxxxxxxxxxxxxxxxx" \
       MME_TEL_LOG_LEVEL=WARNING MME_TEL_BIND_PORT=8801 MME_TEL_OBSERVE_CONTEXTS=public
export MME_TEL_ESL_PASSWORD="$(python - <<'EOF'
import sys; sys.path.insert(0, "scripts")
from esl_check import esl_password; print(esl_password())
EOF
)"
python - <<EOF
import asyncio
from pymongo import AsyncMongoClient
async def main():
    c = AsyncMongoClient("mongodb://127.0.0.1:27117"); db = c["mme_telephony_observe"]
    await db.inbound_routes.update_one({"number": "+$DID"}, {"\$set": {"tenant_id": "mme-test", "action": "park", "enabled": True}}, upsert=True)
    await c.close()
asyncio.run(main())
EOF
uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8801 --workers 1 --no-access-log >/tmp/mme_tel_observe.log 2>&1 &
PID=$!
for i in $(seq 1 40); do curl -sf localhost:8801/health/live >/dev/null && break; sleep 0.25; done
sleep 1.5
echo "ready: $(curl -s localhost:8801/health/ready)"
UUID=$(python -c "import uuid;print(uuid.uuid4())")
python scripts/esl_check.py "originate {origination_uuid=$UUID,mme_leg=x}loopback/$DID/public &park()" | tail -1
sleep 4
TOKEN=$(python scripts/dev_token.py mme-test telephony:user)
echo "--- calls";    curl -s -H "Authorization: Bearer $TOKEN" localhost:8801/v1/calls | python -c "import sys,json; [print({k:c.get(k) for k in ('id','direction','state','from_number','to_number','session_id','hangup_cause','connected_duration_s')}) for c in json.load(sys.stdin)['items']]"
echo "--- sessions"; curl -s -H "Authorization: Bearer $TOKEN" localhost:8801/v1/sessions | python -c "import sys,json; [print({k:s.get(k) for k in ('session_id','status','end_reason','answered_at')}) for s in json.load(sys.stdin)['items']]"
echo "--- channels left: $(python scripts/esl_check.py 'show channels count' | tail -1)"
kill -TERM $PID; wait $PID 2>/dev/null
python scripts/esl_check.py "hupall NORMAL_CLEARING mme_leg x" >/dev/null
python - <<'EOF'
import asyncio
from pymongo import AsyncMongoClient
async def main():
    c = AsyncMongoClient("mongodb://127.0.0.1:27117"); await c.drop_database("mme_telephony_observe"); await c.close()
asyncio.run(main())
EOF
