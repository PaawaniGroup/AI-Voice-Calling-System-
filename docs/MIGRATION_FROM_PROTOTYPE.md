# Moving from the prototype (`~/MME_TELEPHONY/ai_counselling` + `mme_telephony`) to this service

Nothing in `~/MME_TELEPHONY` was modified. The prototype keeps running on port 8090 exactly as before; its tarball backup is `~/backups/MME_TELEPHONY-proto-*.tgz`. This service is a separate project (`C:\Users\Admin\Desktop\MME_Telephony`, reachable in WSL at `/mnt/c/Users/Admin/Desktop/MME_Telephony`) listening on **8800**.

## What the prototype lacked and where it lives now
| Prototype gap | Now |
|---|---|
| in-memory sessions (lost on restart) | MongoDB `sessions`, unique per call, state machine, history |
| API errors dropped (`print`) with no retry; end-session could be lost | the service consumes ESL itself with a bounded queue, CAS transitions, reconciliation after reconnect/restart |
| sync socket reader doing HTTP inside the loop | async ESL client; slow DB/webhooks never block the reader |
| no authentication, no tenants | JWT + roles + tenant isolation; dashboard login |
| `datetime.now()` of the listener as call time | controller clock + FreeSWITCH-clock deltas for duration (FreeSWITCH timestamps were observed 2.5 days off this host's clock, so absolute FS timestamps are never compared with wall time) |

## Option A (recommended): replace the listener, keep your dialplan
1. Create the first admin on the host: `MME_TEL_NEW_USER_PASSWORD='…' python scripts/create_user.py <you> mme platform` (uses `MME_TEL_MONGO_URI`).
2. Create `.env` from `.env.example`: `MONGO_URI`, `JWT_SECRET`, `WEBHOOK_SIGNING_MASTER`, `ESL_PASSWORD`, `FS_GATEWAY=plivo-mme`, **`OBSERVE_CONTEXTS=public`**, `REAL_CALLS_ENABLED=false`.
3. Start: `uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8800 --workers 1`.
4. In `/dashboard/` (or the API) set the tenant policy and the inbound route for `+918065530418` (see API_INTEGRATION_GUIDE §2).
5. **Stop the old listener** (`python -m mme_telephony.esl_events`) — otherwise both record the call. If you must run both temporarily, the new API de-duplicates sessions by FreeSWITCH UUID (`POST /v1/sessions` with `call_id=<uuid>` returns the existing session), so you still get one session per call.
6. Call the DID; the call and session appear in *Live calls* and *Sessions*. (Verified on loopback with `scripts/verify_observe_inbound.sh`; **not verified with a real Plivo call**.)

## Option B: keep the old listener, point it at the new API
Set `MME_COUNSELLING_API_URL=http://127.0.0.1:8800` and add an `Authorization: Bearer <token>` header in `mme_telephony/counselling_api.py::_request` (one line; you apply it). Differences: a new session starts in status `created` (prototype: `active`); `answered` / `ended` are unchanged; `tenant_id` must match the token; `404`/`403` instead of open access. This keeps the old listener's weaknesses, so Option A is preferred.

## Cut-over checklist
- [ ] Backup exists (`~/backups`), old API still up on 8090
- [ ] New service healthy: `curl localhost:8800/health/ready` → `{"status":"ready"}`
- [ ] Test call recorded (calls + sessions) and visible in the dashboard
- [ ] Old listener stopped, no duplicate sessions
- [ ] Rotate the trunk password in `plivo-mme.xml` if it has ever been shared outside the host
