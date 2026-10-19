#!/bin/bash
# Local smoke test: starts the real service (uvicorn) against the local test MongoDB and local FreeSWITCH,
# checks health/readiness/docs/auth, then stops it. No calls are placed. Dev only.
set -uo pipefail
cd "$(dirname "$0")/.."
export PATH="${MME_VENV:-$HOME/MME_TELEPHONY/.venv}/bin:$PATH" PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1
bash scripts/dev_mongo.sh >/dev/null 2>&1
export MME_TEL_ENV=development MME_TEL_MONGO_URI=mongodb://127.0.0.1:27117 MME_TEL_MONGO_DB=mme_telephony_smoke \
       MME_TEL_JWT_SECRET="dev-only-secret-$(date +%s)-xxxxxxxxxxxxxxxx" MME_TEL_WEBHOOK_SIGNING_MASTER="dev-only-master-xxxxxxxxxxxxxxxxxxxxxx" \
       MME_TEL_LOG_LEVEL=WARNING MME_TEL_BIND_PORT=8800
export MME_TEL_ESL_PASSWORD="$(python - <<'EOF'
import sys; sys.path.insert(0, "scripts")
from esl_check import esl_password; print(esl_password())
EOF
)"
python scripts/validate_config.py | tail -1
uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8800 --workers 1 --no-access-log >/tmp/mme_tel_smoke.log 2>&1 &
PID=$!
for i in $(seq 1 40); do curl -sf localhost:8800/health/live >/dev/null && break; sleep 0.25; done
echo "live:   $(curl -s localhost:8800/health/live)"
sleep 1
echo "ready:  $(curl -s localhost:8800/health/ready)"
echo "status (no token): HTTP $(curl -s -o /dev/null -w '%{http_code}' localhost:8800/v1/telephony/status)"
TOKEN=$(python scripts/dev_token.py smoke-tenant telephony:user)
echo "status (token):    $(curl -s -H "Authorization: Bearer $TOKEN" localhost:8800/v1/telephony/status)"
echo "openapi paths:     $(curl -s localhost:8800/openapi.json | python -c 'import sys,json; print(len(json.load(sys.stdin)["paths"]))')"
echo "metrics (loopback): HTTP $(curl -s -o /dev/null -w '%{http_code}' localhost:8800/metrics)"
kill -TERM $PID; wait $PID 2>/dev/null; echo "exit after SIGTERM: $?"
grep -ci -E 'error|traceback' /tmp/mme_tel_smoke.log | sed 's/^/log error lines: /'
python - <<'EOF'
import asyncio
from pymongo import AsyncMongoClient
async def main():
    c = AsyncMongoClient("mongodb://127.0.0.1:27117"); await c.drop_database("mme_telephony_smoke"); await c.close()
asyncio.run(main())
EOF
