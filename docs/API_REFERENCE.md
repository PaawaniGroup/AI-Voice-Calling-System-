# API reference

OpenAPI is generated at `/openapi.json` and Swagger UI at `/docs` (disabled when `MME_TEL_DOCS_ENABLED=false`). All `/v1` calls need `Authorization: Bearer <JWT>`. Errors share one envelope:

```json
{"error": {"code": "destination_denied", "message": "...", "details": {}, "request_id": "..."}}
```
Every response carries `X-Request-Id` (a client `X-Request-Id`/`X-Correlation-Id` is propagated into logs). Bodies over `MAX_BODY_BYTES` → 413. Rate limits → 429 `rate_limited`.

Roles (claim names configurable): `telephony:user` (calls), `telephony:admin` (tenant administration), `platform:admin` (cross-tenant).

## Health / ops
| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/health/live` | none | process liveness only |
| GET | `/health/ready` | none | 200/503; `checks.database`, `checks.freeswitch` (event stream connected and fresh). No hostnames/credentials |
| GET | `/v1/telephony/status` | user | counts, connection booleans, `ai_enabled:false` |
| GET | `/metrics` | `MME_TEL_METRICS_TOKEN` bearer, else loopback only | Prometheus |

## Calls
| Method | Path | Notes |
|---|---|---|
| POST | `/v1/calls` | **202** created (or **200** + `Idempotent-Replay: true`). Header `Idempotency-Key` (8-128 chars) required. Body `{to, from_number, bridge_to?, max_duration_s?, metadata?}`. 403 `tenant_calling_disabled`/`real_calls_disabled`; 422 `invalid_number`, `destination_denied`, `destination_country_denied`, `destination_not_allowed`, `caller_id_not_approved`, `transfer_not_permitted`; 409 `idempotency_key_reuse`; 429 `capacity_exceeded` |
| GET | `/v1/calls` | filters `state, direction, start, end`; cursor pagination `limit<=200, cursor` → `{items,next_cursor}` |
| GET | `/v1/calls/{id}` | 404 for other tenants |
| POST | `/v1/calls/{id}/hangup` | idempotent; the state changes only when FreeSWITCH confirms (response may still show the pre-hangup state) |
| POST | `/v1/calls/{id}/cancel` | pre-connect only; 409 `already_connected` after answer |
| POST | `/v1/calls/{id}/transfer` | body `{destination}`; call must be answered/bridged; destination must satisfy tenant `transfer_destinations`; 202; 409 `not_transferable`; 502 `transfer_failed` |
| GET | `/v1/calls/{id}/events` | ordered lifecycle events |
| GET | `/v1/calls/{id}/recordings` | metadata |
| GET | `/v1/recordings/{id}/download-url` | short-lived presigned URL (S3 only), audited |

Call object states: `queued, initiating, ringing, answered, bridged, transferring, completed, failed, cancelled, busy, no_answer`.

## Reporting
* `GET /v1/usage?start&end[&tenant_id]` → `measured` (calls, connected seconds, per provider, rounded-up billable minutes) and `estimated_charges` (null unless `MME_TEL_ESTIMATE_RATE_PER_MIN>0`; labelled ESTIMATE). `tenant_id` of another tenant requires `platform:admin`.
* `GET /v1/analytics/calls?start&end&format=json|csv` → outcome counts, failed/busy/no_answer/completed, average connected/ring seconds, CSV export.

## Administration
| Method | Path | Role |
|---|---|---|
| GET | `/v1/admin/policy` | admin (own tenant) |
| PUT | `/v1/admin/tenants/{tenant}/policy` | platform |
| PUT / DELETE | `/v1/admin/tenants/{tenant}/routes[/{number}]` | platform (number can belong to one tenant) |
| GET | `/v1/admin/routes` | admin (own tenant) |
| POST/GET/DELETE | `/v1/webhooks/endpoints` | admin; URL validated against SSRF rules; response returns the derived signing secret |
| GET | `/v1/webhooks/deliveries` | admin; attempt history |
| POST | `/v1/webhooks/deliveries/{id}/replay` | admin |

Raw SIP credentials and arbitrary FreeSWITCH commands are never exposed.

## Outbound webhooks (versioned `api_version: 2025-01`)
`POST` JSON `{id, type, api_version, created_at, data{call_id, tenant_id, direction, state, failure_reason, hangup_cause, connected_duration_s, to_masked}}`, types `call.initiated|ringing|answered|bridged|transferring|completed|failed|cancelled|busy|no_answer`.
Headers: `X-MME-Event-Id`, `X-MME-Delivery-Id`, `X-MME-Timestamp`, `X-MME-Signature: v1=<hex HMAC-SHA256(secret, "<timestamp>." + body)>`, `Idempotency-Key` (= event id; consumers must dedupe on it). Retries: exponential backoff with jitter up to `WEBHOOK_MAX_ATTEMPTS`, then `dead`; replay re-sends the **same** event id. Recording URLs are never included.

## Provider callbacks
`POST /v1/providers/plivo/answer` and `/status` (only when `provider=plivo_api`): Plivo V3 signature + nonce required (401 otherwise); replayed nonce → 200 `duplicate`; same event again → `duplicate`; unknown call → 200 `ignored`.
