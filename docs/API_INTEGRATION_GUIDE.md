# API integration guide: MME AI voice counselling application ⇄ telephony backend

Base URL (dev): `http://127.0.0.1:8800`. Full route list: [ROUTES.md](ROUTES.md) (generated). OpenAPI: `/docs`, `/openapi.json` (dev only).
Status column legend: **T** = covered by an automated test in this repo, **R** = also verified against the real local FreeSWITCH, **—** = implemented but untested against real infrastructure. Nothing here has been run against a real Plivo call.

## 1. Authentication
Every `/v1` call needs `Authorization: Bearer <JWT>` (HS256). The tenant comes **only** from the token (`tenant_id` claim); a `tenant_id` in a body must equal it or the call is rejected (403 `tenant_mismatch`).
Roles: `telephony:viewer` (read, numbers masked) < `telephony:user` (operator: place/hang up/transfer, AI start/stop) < `telephony:admin` (users, AI config, webhooks, logs) ; `platform:admin` is cross-tenant (tenant policy, inbound routes).

| Who | How to get a token |
|---|---|
| Dashboard user | `POST /v1/auth/login` `{"username","password"}` → `access_token` (default 8 h). **T** |
| The MME AI app (service-to-service) | MME identity signs an HS256 JWT with the shared `MME_TEL_JWT_SECRET`: claims `sub`, `tenant_id`, `roles:["telephony:user"]`, `exp` (short-lived). **Unverified against the real MME identity service.** For development: `python scripts/dev_token.py mme telephony:user 3600` |
| First user | `MME_TEL_NEW_USER_PASSWORD=... python scripts/create_user.py <name> <tenant> <viewer\|operator\|admin\|platform>` on the host |

```bash
export API=http://127.0.0.1:8800
TOKEN=$(curl -s $API/v1/auth/login -H 'content-type: application/json' -d '{"username":"admin1","password":"<password>"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
H="Authorization: Bearer $TOKEN"
```

## 2. One-time tenant setup (platform admin) — T
```bash
curl -s -X PUT $API/v1/admin/tenants/mme/policy -H "$H" -H 'content-type: application/json' -d '{
  "enabled": true, "caller_ids": ["+918065530418"], "max_active_calls": 10, "max_duration_s": 900,
  "allowed_countries": ["IN"], "transfer_destinations": ["+91XXXXXXXXXX"], "recording": "off"}'
# inbound number -> tenant (needed to record/route inbound calls)
curl -s -X PUT $API/v1/admin/tenants/mme/routes -H "$H" -H 'content-type: application/json' -d '{"number":"+918065530418","action":"park"}'
# webhook consumer (returns a signing secret, shown once)
curl -s -X POST $API/v1/webhooks/endpoints -H "$H" -H 'content-type: application/json' -d '{"url":"https://app.example.com/hooks/telephony","events":["*"]}'
```
Tenants are **denied until enabled**. Real PSTN dialing additionally needs `MME_TEL_REAL_CALLS_ENABLED=true` on the server.

## 3. Initiate an outbound call — T (mock provider), R (FreeSWITCH loopback legs); **real Plivo call: not yet run**
```bash
curl -s -X POST $API/v1/calls -H "$H" -H 'content-type: application/json' -H "Idempotency-Key: lead-123-attempt-1" -d '{
  "to": "+91XXXXXXXXXX", "from": "+918065530418", "ai_enabled": true, "language": "auto",
  "metadata": {"lead_id": "lead-123", "purpose": "student-counselling"}}'
```
```json
{"id":"call_9f3c…","tenant_id":"mme","session_id":"6d1e…","state":"queued","direction":"outbound","ai_enabled":true,"language":"auto",
 "from_number":"+918065530418","to_number":"+91XXXXXXXXXX","created_at":"2026-10-19T10:00:00Z","answered_at":null,"hangup_cause":null,…}
```
* **202** = accepted and durably queued (`queued`). It is **not** answered until a later `answered` state (from the real FreeSWITCH/Plivo event).
* Same `Idempotency-Key` + same body → **200** with `Idempotent-Replay: true` and the original call (safe to retry after a timeout). Same key + different body → **409** `idempotency_key_reuse`.
* Errors: 403 `tenant_calling_disabled` / `real_calls_disabled`, 422 `invalid_number` / `destination_denied` / `destination_country_denied` / `caller_id_not_approved`, 429 `capacity_exceeded`.
* `webhook_url` per call is **not** supported (must be null); use webhook endpoints. `bridge_to` optionally bridges a second party on answer.

## 4. Inbound calls — T/R (loopback into the real `mme_inbound` and `public` dialplan contexts)
There is no inbound API to call: FreeSWITCH receives the call and the service reacts to its events.
* `mme_inbound` context (controller-owned): the service looks up the dialled number in `inbound_routes`, enforces tenant policy and capacity, then answers/parks/bridges/rejects. Rejects (404 number, disabled tenant, over capacity) create **no** record.
* **Record-only mode** (`MME_TEL_OBSERVE_CONTEXTS=public`): for a context you already route elsewhere (your current DID dialplan), the service only **records** the call and session when the number has an inbound route. It sends no command to FreeSWITCH. Verified with `scripts/verify_observe_inbound.sh`.
* Duplicate events (CREATE+PARK) create exactly one call and one session. **T**
* Plivo Voice-API path: `POST /v1/providers/plivo/answer|status` (signature verified). **T with locally computed signatures only.**

## 5. Call id, session id, status
```bash
curl -s $API/v1/calls/$CALL_ID -H "$H"                  # call + session_id
curl -s $API/v1/calls/$CALL_ID/session -H "$H"
curl -s "$API/v1/calls?state=answered&limit=50" -H "$H"  # cursor pagination: next_cursor
curl -s $API/v1/calls/$CALL_ID/events -H "$H"            # lifecycle timeline
```
Call states: `queued → initiating → ringing → answered → bridged ⇄ transferring → completed`, or `busy | no_answer | cancelled | failed`.
Session states: `created → answered → active (AI attached) → ending → ended | failed`. Separate AI-session states: `starting → active ⇄ media_disconnected → stopped | failed`.
Observe changes by **webhook** (preferred) or by polling the GET above, or the dashboard SSE stream (`GET /v1/stream/events`, use `fetch` with the Authorization header; ≤5 streams per tenant).

## 6. Sessions API (prototype-compatible) — T
```bash
curl -s -X POST $API/v1/sessions -H "$H" -H 'content-type: application/json' -d '{"tenant_id":"mme","caller_id":"+91XXXXXXXXXX","call_id":"<fs-uuid-or-call-id>","language":"auto","metadata":{"source":"freeswitch-esl"}}'
curl -s -X POST $API/v1/sessions/$SID/answer -H "$H"
curl -s -X POST $API/v1/sessions/$SID/end -H "$H" -H 'content-type: application/json' -d '{"reason":"NORMAL_CLEARING"}'
```
Exactly one session exists per call (`call_ref` unique per tenant; a FreeSWITCH UUID or our call id both resolve to it). Terminal sessions are immutable; repeating a request is harmless.

## 7. AI session — contracts T (synthetic media client + mock model servers); **real models and real audio: NOT verified**
```bash
curl -s $API/v1/ai/health -H "$H"          # per-service healthy|unhealthy|unreachable|not_configured + media_bridge{ready,reason}
curl -s -X PUT $API/v1/ai/config -H "$H" -H 'content-type: application/json' -d '{"enabled":true,"stt_url":"http://127.0.0.1:9001","brain_url":"http://127.0.0.1:9002","tts_url":"http://127.0.0.1:9003"}'
curl -s -X POST $API/v1/ai/sessions/$SID/start -H "$H" -H 'content-type: application/json' -d '{"language":"hinglish"}'
curl -s $API/v1/ai/sessions/$SID -H "$H"
curl -s -X POST $API/v1/ai/sessions/$SID/stop -H "$H"
```
`start` returns **409** with a precise `code` until everything is true: `ai_disabled` (global switch) → `ai_disabled_for_tenant` → `session_not_answered` → `call_not_answered` → `call_not_ai_enabled` → **`media_bridge_unavailable`** (today's real answer on this host) → `ai_not_configured`. On success it returns **202 `starting`**; the state becomes `active` only when the media client connects (`docs/AI_MEDIA_ADAPTER.md`). A hangup stops the AI session and cancels in-flight inference. **T**

## 8. Hang up, transfer
```bash
curl -s -X POST $API/v1/calls/$CALL_ID/hangup -H "$H"     # idempotent; final state arrives with the FreeSWITCH hangup event
curl -s -X POST $API/v1/calls/$CALL_ID/cancel -H "$H"     # pre-connect only (409 already_connected after answer)
curl -s -X POST $API/v1/calls/$CALL_ID/transfer -H "$H" -H 'content-type: application/json' -d '{"destination":"+91XXXXXXXXXX"}'
```
Transfer needs state answered/bridged and a destination on the tenant's `transfer_destinations`. **T/R** (loopback target).

## 9. Transcript, recording, summary
`GET /v1/calls/{id}/recording` → recording metadata (404 `recording_not_available` unless recording is enabled by config, tenant policy and storage; download via `/v1/recordings/{id}/download-url`, presigned S3 only). **T (local storage), R (real FreeSWITCH recording).**
`GET /v1/calls/{id}/transcript` and `/summary` → **always 404 `*_not_available`**: transcript/summary persistence is **not implemented**; the webhook types `transcript.ready` / `summary.ready` are reserved and **never emitted**.

## 10. Webhooks — T
Types emitted: `call.created, call.initiated, call.ringing, call.answered, call.bridged, call.transferring, call.completed, call.ended, call.failed, call.cancelled, call.busy, call.no_answer, session.created, session.answered, session.ended, ai.session.started, ai.session.stopped, ai.session.failed`.
```
POST <your url>   Content-Type: application/json
X-MME-Event-Id, X-MME-Delivery-Id, X-MME-Timestamp, X-MME-Signature: v1=<hex>, Idempotency-Key: <event id>
body: {"id":"…","type":"call.answered","api_version":"2025-01","created_at":"…","data":{"call_id":"…","session_id":"…","state":"answered","to_masked":"+91*******10",…}}
```
Verify: `HMAC_SHA256(secret, "<X-MME-Timestamp>." + raw_body)` equals the hex after `v1=`; reject timestamps older than ~5 minutes; **deduplicate on `Idempotency-Key`** (delivery is at-least-once). Retries: exponential backoff + jitter, up to 8 attempts, then `dead` (see `GET /v1/webhooks/deliveries`, replay with `POST …/replay`). Payloads carry masked numbers, never recording URLs or secrets.

## 11. Failure handling for the integrating app
| Situation | What to do |
|---|---|
| Timeout / 5xx on `POST /v1/calls` | retry with the **same** `Idempotency-Key` |
| 429 `capacity_exceeded` / `rate_limited` | back off (the body has `retry_after_s` for capacity) |
| 503 `database_unavailable` / `freeswitch_unavailable` | retry later; accepted calls are never lost (queued rows are durable) |
| Call stuck `initiating` with `origination_uncertain` | do **not** re-dial automatically; check the carrier CDR; the service reconciles it |
| 409 `media_bridge_unavailable` on AI start | run the call without AI (or fall back to a human); AI cannot start on this deployment today |
| Webhook endpoint down | deliveries retry then dead-letter; replay after fixing |
