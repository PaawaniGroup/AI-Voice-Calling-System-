# MME_TELEPHONY: complete architecture, routes and end-to-end flows

Scope: Phase 2 (standalone telephony). Phase 3 (AI voice agent) is **not** included. "Verified" below means exercised by the tests in `docs/TEST_REPORT.md`; anything touching a real carrier, Plivo, Atlas or S3 is **unverified** (see `PHASE_2_ACCEPTANCE.md`).

## 1. System context

```
 MME Express / Next.js (existing)                    Plivo (carrier / Voice API)
        │ HTTPS + Bearer JWT                             ▲  │ signed webhooks (V3)
        ▼                                                │  ▼
 ┌─────────────────────────────── MME_TELEPHONY (FastAPI, 1 process) ───────────────────────────────┐
 │  API layer ── security ── services ── controller ── providers ── webhook emitter/worker          │
 │      │                         │          │  ▲                                                   │
 │      │                         │          │  └── ESL event client (subscribe, queue, dedupe)      │
 │      │                         │          └───── ESL command client (api / bgapi)                │
 └──────┼─────────────────────────┼──────────────────┬──────────────────────────────────────────────┘
        ▼                         ▼                  ▼ 127.0.0.1:8021 (control + events, no audio)
   MongoDB `mme_telephony`   Object storage      FreeSWITCH ── SIP/RTP ──► carrier gateway / PSTN
   (calls, events, …)        (recordings)         contexts: mme_inbound, mme_transfer
        │
        └──► outbound lifecycle webhooks ──► MME consumers (HMAC-signed, retried)
```
Media (audio) never passes through this service. ESL is control and events only.

## 2. Components

| Layer | Module | Responsibility |
|---|---|---|
| API | `app/api/routes.py`, `schemas.py`, `main.py` | routes, strict request schemas, error envelope, request/correlation IDs, body limit, CORS, DB-error → 503 |
| Security | `app/security/auth.py` | JWT verify (HS256), roles, tenant from token, per-principal rate limiter |
| Services | `services/calls.py` | tenant policy, destination/caller-ID checks, admission control, idempotency, durable enqueue |
| | `services/recordings.py` | recording policy, start via FreeSWITCH, upload + verify, presigned access, retention |
| Controller | `telephony/controller.py` | event → state transitions, dispatcher, hangup/cancel/transfer, timeouts, reconciliation, inbound routing |
| | `telephony/state_machine.py` | pure transition table + hangup-cause mapping |
| | `telephony/esl.py` | ESL protocol, event client, command client, backoff |
| | `telephony/dialstrings.py` | allowlist-validated FreeSWITCH command builders |
| Providers | `providers/base.py` | `ProviderAdapter` interface, FreeSWITCH-gateway adapter, mock (dev/test only) |
| | `providers/plivo.py` | Plivo REST originate/hangup, V3 signature verify, status mapping |
| Events | `events/webhooks.py` | emitter (DB outbox), signer, SSRF guard, retrying worker |
| Persistence | `persistence/db.py`, `repos.py` | indexes, schema version, CAS transitions, dedup events, idempotency |
| Observability | `core/logging.py`, `observability/metrics.py` | JSON logs with redaction, Prometheus metrics |
| Config | `config/settings.py`, `telephony.yaml` | 116 validated parameters; FreeSWITCH dialplan in `config/freeswitch/` |

Background tasks started at boot: ESL event reader + 8 handlers, controller loops (dispatch, timeouts every 2 s, reconcile every 30 s), webhook worker, housekeeping (recording upload/purge, stuck webhook claims). MongoDB TTL indexes handle retention.

## 3. Complete route table (27 routes)

Auth: **none** = public; **user** = `telephony:user`/admin/platform; **admin** = `telephony:admin`/platform (own tenant); **platform** = `platform:admin`; **signature** = Plivo V3. Every `/v1` route except the provider callbacks requires `Authorization: Bearer <JWT>`; the tenant always comes from the token.

| # | Method | Path | Auth | Purpose |
|---|---|---|---|---|
| 1 | GET | `/health/live` | none | liveness |
| 2 | GET | `/health/ready` | none | readiness: database + FreeSWITCH event stream (200/503) |
| 3 | GET | `/metrics` | token or loopback | Prometheus metrics |
| 4 | GET | `/v1/telephony/status` | user | active/queued counts, connection flags |
| 5 | POST | `/v1/calls` | user | create outbound call (needs `Idempotency-Key`) → 202, or 200 on replay |
| 6 | GET | `/v1/calls` | user | history; filters `state, direction, start, end`; cursor pagination |
| 7 | GET | `/v1/calls/{id}` | user | call detail (other tenant → 404) |
| 8 | POST | `/v1/calls/{id}/hangup` | user | terminate (idempotent; final state from the hangup event) |
| 9 | POST | `/v1/calls/{id}/cancel` | user | cancel before connect (409 once answered) |
| 10 | POST | `/v1/calls/{id}/transfer` | user | transfer a connected call to an authorized destination → 202 |
| 11 | GET | `/v1/calls/{id}/events` | user | ordered lifecycle events |
| 12 | GET | `/v1/calls/{id}/recordings` | user | recording metadata |
| 13 | GET | `/v1/recordings/{id}/download-url` | user | short-lived presigned URL (S3 only), audited |
| 14 | GET | `/v1/usage` | user | measured usage (+ labelled estimate); cross-tenant needs platform |
| 15 | GET | `/v1/analytics/calls` | user | outcome counts, durations; `format=csv` |
| 16 | GET | `/v1/admin/policy` | admin | own tenant policy |
| 17 | PUT | `/v1/admin/tenants/{tenant}/policy` | platform | enable tenant, caller IDs, limits, deny/allow prefixes, transfer targets, recording |
| 18 | PUT | `/v1/admin/tenants/{tenant}/routes` | platform | map inbound number → tenant + action (`park`/`bridge`/`hangup`) |
| 19 | GET | `/v1/admin/routes` | admin | own tenant routes |
| 20 | DELETE | `/v1/admin/tenants/{tenant}/routes/{number}` | platform | remove route |
| 21 | POST | `/v1/webhooks/endpoints` | admin | register consumer (SSRF-checked); returns derived signing secret |
| 22 | GET | `/v1/webhooks/endpoints` | admin | list |
| 23 | DELETE | `/v1/webhooks/endpoints/{id}` | admin | remove |
| 24 | GET | `/v1/webhooks/deliveries` | admin | delivery + attempt history (`status` filter) |
| 25 | POST | `/v1/webhooks/deliveries/{id}/replay` | admin | re-send same event id |
| 26 | POST | `/v1/providers/plivo/status` | signature | Plivo ring/hangup callbacks (only if `provider=plivo_api`) |
| 27 | POST | `/v1/providers/plivo/answer` | signature | Plivo answer URL: inbound notification / outbound answer → returns Plivo XML |

Also: `/docs`, `/openapi.json` when `DOCS_ENABLED=true`. Error envelope: `{"error":{"code","message","details","request_id"}}`. Key codes: 401 `unauthorized`, 403 `forbidden`/`tenant_calling_disabled`/`real_calls_disabled`, 404 `not_found`, 409 `idempotency_key_reuse`/`already_connected`/`not_transferable`, 413, 422 (`invalid_number`, `destination_denied`, `destination_country_denied`, `caller_id_not_approved`, `transfer_not_permitted`), 429 `capacity_exceeded`/`rate_limited`, 502 `hangup_failed`/`transfer_failed`, 503 `database_unavailable`/`freeswitch_unavailable`.

## 4. Call state machine

`queued → initiating → ringing → answered → bridged ⇄ transferring → completed`; early exits `busy | no_answer | cancelled | failed`; terminal states are immutable. Full table: `CALL_STATE_MACHINE.md`.

## 5. End-to-end flows

### 5.1 One-time setup (per tenant)
1. Platform admin: `PUT /v1/admin/tenants/{t}/policy` (`enabled`, `caller_ids`, `max_active_calls`, country/prefix rules, `transfer_destinations`, `recording`). Tenants are **denied until enabled**.
2. Optional: `PUT …/routes` for inbound numbers; tenant admin: `POST /v1/webhooks/endpoints` (store the returned secret).
3. Operator: `MME_TEL_REAL_CALLS_ENABLED=true` (only for PSTN-capable paths).

### 5.2 Outbound call (FreeSWITCH-gateway path)
1. **Client** `POST /v1/calls {to, from_number}` + `Idempotency-Key`.
2. **API**: verify JWT → rate limit → tenant policy (enabled?) → real-calls gate → normalise numbers (E.164) → destination allow/deny + country → caller ID approved → fingerprint request.
3. **Idempotency**: claim `(tenant, op, key)` by unique index. Replay → return the original call (200, `Idempotent-Replay`); same key + different body → 409.
4. **Admission**: count active + queued vs ceilings (global, provider, tenant). Over capacity → queue (if policy `queue` and room) else 429.
5. **Persist** call as `queued` (this row is the durable queue) + `state.queued` event + audit. **Response 202 with call id.**
6. **Dispatcher** (≈4×/s): needs FreeSWITCH event stream connected, capacity below `min(global, provider)` ceiling, a token from the CPS bucket, and tenant below its cap. CAS-claims `queued → initiating`, emits `call.initiated`.
7. **Originate**: generate `fs_uuid` + `job_uuid`, **persist them first**, then `bgapi originate {origination_uuid=…,mme_call_id=…,…}<gateway dial> &park()` (or `&bridge(second party)` if `bridge_to`). `ProviderUncertain` (timeout/lost reply) → mark `origination_uncertain`, **never retry**; `ProviderError` → `failed`.
8. **FreeSWITCH events** (ESL stream) arrive: `CHANNEL_PROGRESS` → `ringing`; `CHANNEL_ANSWER` → `answered` (recording may start); `CHANNEL_BRIDGE` → `bridged` (records B-leg uuid); `BACKGROUND_JOB -ERR cause` or `CHANNEL_HANGUP` → terminal state via cause mapping.
9. Each event: dedup insert into `call_events` → CAS transition → metrics → webhook outbox row (`call.ringing`, `call.answered`, …).
10. **Webhook worker** signs and POSTs each delivery (retry with backoff, dead-letter after max attempts); consumers dedupe on `Idempotency-Key`.
11. **Timeouts**: ring timeout in FreeSWITCH; queue wait, stuck `initiating`, max duration (`sched_hangup` + controller backstop), transfer timeout in the sweeper.
12. **Finish**: terminal state sets `ended_at`, `connected_duration_s`, `ring_duration_s`, `hangup_cause`, `failure_reason`; recording uploaded and verified; call appears in history, usage and analytics.

### 5.3 Outbound call (Plivo REST path, `provider=plivo_api`)
Steps 1–6 identical. Step 7: Plivo `calls.create(from, to, answer_url, ring_url, hangup_url, ring_timeout, time_limit)` → `request_uuid` stored as `provider_call_id`. Step 8: Plivo posts to routes 26/27; each is signature-verified (V3 over the public URL), nonce-replay checked, mapped (`ringing`, `in-progress`, `completed`, `busy`, `no-answer`, `cancel`, `failed`), deduped by `CallUUID+status+event`, and applied through the same state machine. Unknown calls are acknowledged and ignored. *(Unverified against a live account.)*

### 5.4 Inbound call (SIP via FreeSWITCH)
1. Carrier INVITE lands in context `mme_inbound` → dialplan `ring_ready` + `park`.
2. `CHANNEL_CREATE/PARK` with context `mme_inbound` and no known call → controller: normalise dialed number → look up `inbound_routes` → tenant policy enabled? → global/tenant capacity?
3. Reject (kill with `UNALLOCATED_NUMBER` / `CALL_REJECTED` / `NORMAL_CIRCUIT_CONGESTION`) without creating a record, or create an inbound call (`initiating → ringing`).
4. `uuid_answer`, then route action: `park` (hold for a future agent), `bridge` (set dial var + transfer into `mme_transfer`), or `hangup`. Events, state, webhooks and finalisation as in 5.2.
Plivo inbound (`provider=plivo_api`): route 27 does the same decision and answers with `<Wait>`, `<Dial>` or `<Hangup>`.

### 5.5 Hangup and cancel
`POST …/hangup`: tenant-scoped lookup → terminal? return as is. `queued` (or claimed but not yet dispatched) → cancelled directly. Otherwise `uuid_kill` (`ORIGINATOR_CANCEL` pre-connect, `NORMAL_CLEARING` after) and the **response may still show the old state**; the final state arrives only with the hangup event (or reconciliation). `…/cancel` is the same but 409 after answer.

### 5.6 Transfer
API validates destination against the tenant's `transfer_destinations` → state must be `answered/bridged` → state `transferring` (attempt recorded) → set validated dial var → `uuid_transfer` into fixed context `mme_transfer` → success = `CHANNEL_BRIDGE` (state `bridged`, attempt `completed`); failure = `CUSTOM mme::transfer_failed` or timeout (attempt `rejected`/`timeout`, state reverts to `answered`); call ending mid-transfer closes the attempt as `call_ended`.

### 5.7 Recording
Needs `RECORDING_ENABLED` + consent mode + storage backend + tenant `recording=on`. On answer: `uuid_record start` into a spool path, metadata `recording`. After the call ends, housekeeping uploads, **verifies size**, marks `available`, deletes the local file. Access via route 13 returns a presigned URL (S3 only), audited; retention purge deletes object and marks `deleted`.

### 5.8 Recovery and reconciliation
On every ESL (re)connect and every 30 s: list channels (`show channels as json`); if FreeSWITCH cannot be queried, change nothing. Calls with no channel are finalised only after their grace period (`ring_timeout + 10 s` for `initiating`/uncertain); live channels belonging to already-terminal calls are hung up (orphans); missed answered/ringing state is caught up. Restarts never re-originate: UUIDs were persisted before dispatch.

### 5.9 Failure behaviour summary
| Failure | Behaviour |
|---|---|
| FreeSWITCH down | readiness 503; accepted calls stay `queued` (durable); reconnect with backoff; reconcile on reconnect |
| MongoDB down | API 503 `database_unavailable` (no driver text); liveness stays 200; idempotency key released on failed create |
| Provider reject | call `failed` with reason; uncertain → left for reconciliation, never retried |
| Duplicate / late / out-of-order events | recorded, state unchanged |
| Webhook consumer down | retries with backoff, then `dead`; replay available |
| Process restart | state is in MongoDB; reconcile on boot; idempotency persists |

## 6. Data stores
MongoDB (`calls`, `call_events`, `idempotency`, `recordings`, `audit`, `tenant_policies`, `inbound_routes`, `webhook_endpoints`, `webhook_deliveries`, `provider_dedup`, `_schema`) → `DATA_MODEL.md`. Audio lives only in object storage (or the local spool in dev).

## 7. Security boundaries
Public/proxy edge (TLS, only `/v1/providers/plivo/*` needs internet reach) → API (JWT, roles, tenant scoping, rate limits) → controller (validated command builders only) → FreeSWITCH (ESL loopback only, owned dialplan contexts) → carrier (firewall to carrier IPs). Details and tests: `SECURITY.md`.

## 8. Deployment topology (target, not yet provisioned)
One Linux host: nginx/Caddy (TLS) → uvicorn (1 worker, systemd) + FreeSWITCH (same host, ESL on loopback) → MongoDB Atlas, S3, Plivo. Single-node by design; horizontal scale needs leader election and shared rate limiting (not built). `DEPLOYMENT.md`, `deploy/mme-telephony.service`.

## 9. Not included
AI voice agent and any media bridge (Phase 3), browser/WebRTC calling (`mod_verto` absent), multi-node HA, Dockerfile, live MME identity integration, real carrier/Plivo/S3/Atlas verification.

## 10. Phase 3 attach points (inactive)
Parked answered legs (`&park()`, inbound `park`), stable `call_id` ↔ `fs_uuid` correlation (`mme_call_id`, `mme_tenant`, `mme_leg`), hangup/transfer controls, lifecycle webhooks. Audio transport must be chosen in Phase 3 (WebSocket media fork on FreeSWITCH, or Plivo streaming on the Plivo path).
