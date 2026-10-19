# Data model (MongoDB database `mme_telephony`)

Schema version is stored in `_schema` (`{_id:"version", v:2}`). Index creation is idempotent at startup (`MME_TEL_MONGO_INIT_INDEXES`); the service refuses to start against a database whose version is *newer* than the code (downgrade protection). Changes are additive/forward-only; a breaking change needs a numbered script in `migrations/`.

| Collection | Purpose | Key fields | Indexes |
|---|---|---|---|
| `calls` | one document per call (also the dispatch queue: `state=queued`) | `_id` call id, `tenant_id`, `direction`, `provider`, `provider_call_id`, `fs_uuid`, `fs_uuids[]`, `job_uuid`, `from_number`, `to_number`, `bridge_to`, `state`, `state_version`, `queued_at/initiated_at/ringing_at/answered_at/bridged_at/ended_at`, `ring_duration_s`, `connected_duration_s`, `hangup_cause`, `failure_reason`, `end_reason`, `cancel_requested`, `origination_uncertain`, `transfer{current,attempts[]}`, `recording_ids[]`, `idempotency_key`, `correlation_id`, `max_duration_s`, `metadata`, `clock_anomaly`, `purge_at` | `(tenant_id, created_at↓)`, `(tenant_id,state,created_at↓)`, `(state,queued_at)`, `fs_uuid`, `fs_uuids`, `job_uuid`, `(provider,provider_call_id)`, reporting `(created_at,tenant_id,state)`, TTL `purge_at` |
| `call_events` | append-only lifecycle/audit trail | `call_id`, `tenant_id`, `type`, `ts`, `source` (api/esl/plivo/controller/reconcile), `dedup_id`, `correlation_id`, sanitized `payload`, `purge_at` | unique `(call_id,dedup_id)`, `(tenant_id,call_id,ts)`, TTL |
| `idempotency` | request replay protection | `tenant_id`, `operation`, `key`, `fingerprint` (sha256 of normalized body), `call_id`, `purge_at` (48 h) | unique `(tenant_id,operation,key)`, TTL |
| `recordings` | metadata only (audio lives in object storage) | `call_id`, `tenant_id`, `status` (recording/uploading/available/failed/upload_failed/deleted), `storage`, `storage_key`, `consent_mode`, `size_bytes`, `duration_s`, `retention_until`, `deleted` | `(tenant_id,call_id)`, `(status,created_at)` |
| `audit` | security-sensitive operations (policy, routes, webhooks, call control, recording access) | `tenant_id`, `principal`, `action`, `target`, `details`, `request_id`, `ts`, `purge_at` | `(tenant_id,ts↓)`, TTL |
| `tenant_policies` | per-tenant calling policy (admin API) | `enabled` (default deny), `max_active_calls`, `max_duration_s`, `allowed_countries`, `deny_prefixes`, `allow_prefixes`, `caller_ids`, `transfer_destinations`, `recording` | unique `tenant_id` |
| `inbound_routes` | DID → tenant routing | `number` (E.164, unique), `tenant_id`, `action` (park/bridge/hangup), `target`, `enabled` | unique `number`, `tenant_id` |
| `webhook_endpoints` | outbound consumers | `tenant_id`, `url`, `events[]`, `enabled`, `secret_version` (secret is derived, never stored) | `tenant_id` |
| `webhook_deliveries` | delivery outbox + attempt history | `endpoint_id`, `event_id`, `payload`, `status` (pending/sending/retrying/delivered/dead), `attempts[]`, `attempt_count`, `next_attempt_at` | `(status,next_attempt_at)`, unique `(endpoint_id,event_id)`, `(tenant_id,created_at↓)`, TTL |
| `provider_dedup` | provider callback nonce store (replay protection) | `key`, `purge_at` (72 h) | unique `key`, TTL |

## Tenants and identity
No tenant or user collection is created: `tenant_id` is an opaque string taken from the verified token. `tenant_policies` is keyed by that id. Linking it to the existing MME tenant collection is a Phase-2 integration item (see SECURITY.md).

## Privacy and retention
* Numbers are stored in E.164 (needed to dial and to report). They are masked in logs (`+91*******10`), webhook payloads (`to_masked`) and audit details.
* Retention via TTL indexes (`purge_at`): calls 730 d, events 180 d, idempotency 48 h, audit 365 d, provider nonces 72 h, webhook deliveries 180 d; recordings 90 d (`retention_until`, enforced by housekeeping which also deletes the object).
* Usage figures come only from `calls` (answered→ended). Carrier invoices are never stored here; `estimated_charges` is clearly labelled and null unless a rate is configured.

## Consistency
No multi-document transactions are used. Guarantees: (1) idempotency key unique index claimed *before* the call insert (released if the insert fails); (2) event dedup unique index; (3) CAS on `state_version` for transitions; (4) queue claim by CAS; (5) reconciliation repairs gaps (crash between event insert and state update).

## Backup / restore
`scripts/backup_mongo.sh` / `scripts/restore_mongo.sh` wrap `mongodump`/`mongorestore --gzip` for this database only; see DEPLOYMENT.md for the verification status.
