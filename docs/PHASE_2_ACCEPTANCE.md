# Phase 2 acceptance

Status values: **PASS** (verified, evidence linked), **PARTIAL** (implemented, only partly verified — gap stated), **BLOCKED** (needs an external dependency — unblock steps stated), **NOT IMPLEMENTED**.
Evidence: `docs/TEST_REPORT.md` (T), `docs/CAPACITY_REPORT.md` (C). Environment: local WSL dev host only — **no real carrier, no Plivo account, no Atlas, no production host were available**.

## Core service
| Criterion | Status | Evidence / gap |
|---|---|---|
| Service starts with documented commands | PASS | `bash scripts/smoke_local.sh` (T) |
| Startup validation detects missing required configuration | PASS | `test_configuration.py` (production secrets, ranges, ESL privacy), `validate_config.py` exit codes |
| Health & readiness behave correctly | PASS | ready 200/503, liveness independent: `test_health`, `test_readiness_reports_database_down`, FS-unavailable test |
| API docs generated and accurate | PASS | `/openapi.json` 25 paths; `docs/API_REFERENCE.md` |
| Persistent call records and events | PASS | integration + restart tests |
| State machine enforces valid transitions | PASS | `test_state_machine.py` (24), lifecycle tests |
| Idempotency prevents duplicate call creation | PASS | sequential, concurrent (10-way), cross-restart tests |
| ESL connects securely and reconnects | PASS | wrong-password test, reconnect test (no duplicate subscription) vs real FreeSWITCH; loopback-only config rule |
| FreeSWITCH UUIDs correlate with app calls | PASS | `test_outbound_answer_correlate_hangup` (`uuid_exists`) |
| Inbound and outbound routing paths implemented | PARTIAL | outbound + inbound exercised against real FreeSWITCH with **loopback legs and the real `mme_inbound` dialplan**; no SIP gateway/carrier call |
| Hangup and cancellation implemented and tested | PASS | FS tests (hangup, cancel while ringing), API tests |
| Transfer implemented and tested | PARTIAL | tested on real FreeSWITCH to a loopback target via `mme_transfer`; transfer to a real PSTN target untested; failure path (`mme::transfer_failed`) is covered by simulation/timeouts, not by a failing real bridge |
| Provider callbacks verified and idempotent | PARTIAL | signature/nonce/dedup verified with **locally computed** V3 signatures; not exercised by Plivo itself |
| Tenant isolation and RBAC tested | PASS | `test_cross_tenant_access_denied` + policy/route/webhook admin checks |
| Usage and analytics derive from persisted records | PASS | `test_pagination_filters_usage`; estimates labelled and null by default |
| Operational logs and metrics available | PASS | JSON logs with redaction; `/metrics` (smoke); alert thresholds in runbook are **untuned suggestions** |
| Recovery and reconciliation tests pass | PASS | restart recovery, uncertain origination, orphan cleanup, young-call protection |
| Database backup and restore verified | PARTIAL | verified on local mongod (`verify_backup_restore.sh`); Atlas not tested |
| Deployment and rollback instructions complete | PARTIAL | `docs/DEPLOYMENT.md`, systemd unit, scripts written; **never executed on a production-like host**; no Dockerfile (Docker unavailable) |
| Automated tests reproducible | PASS | 134 tests, `scripts/run.sh pytest -q` (needs local mongod + FreeSWITCH for 20 of them; those skip if unavailable) |
| No AI model required | PASS | no model code; `ai_enabled` forced false |
| No AI integration activated | PASS | startup rejects `AI_ENABLED`; status shows `ai_enabled:false` |
| Known limitations documented | PASS | this file, SECURITY.md, PROVIDER_SETUP.md, CAPACITY_REPORT.md |

## Production-readiness gates (all external)
| Gate | Status | To unblock |
|---|---|---|
| Production host and network configured | BLOCKED | provision Linux host, public IP/DNS, firewall (SIP/RTP from carrier only, ESL loopback), chrony; follow DEPLOYMENT.md |
| Real provider credentials and routes configured securely | BLOCKED | Plivo account, gateway/trunk or Voice API credentials in secret store (PROVIDER_SETUP.md) |
| Authorized inbound test call | BLOCKED | needs a Plivo number routed to the host + an approved caller |
| Authorized outbound test call | BLOCKED | approved destination, `REAL_CALLS_ENABLED=true` on a test tenant; then run `tests/integration/test_real_provider.py` (written, never executed) |
| Actual call events/final state verified end-to-end on a real carrier | BLOCKED | the two calls above; confirm `RequestUUID`/callback fields (UNVERIFIED) |
| Production firewall, TLS, secret management verified | BLOCKED | production host |
| Recording behaviour verified | PARTIAL | real FreeSWITCH recording → verified upload to the **local** backend tested (`test_real_recording_roundtrip_on_local_freeswitch`); **S3 path, KMS, presigned URLs, consent announcement audio untested** (needs AWS bucket + legal review) |
| Provider concurrency/initiation limits confirmed | BLOCKED | read from the Plivo account; set `PROVIDER_MAX_CONCURRENT/CPS` |
| Load results support the initial workload | PARTIAL | control plane handled 30 concurrent for 180 s and 1,000/300 compressed calls (C); no carrier/RTP/day-long soak; Atlas latency unknown |
| Telecom and privacy requirements reviewed | BLOCKED | DoT/TRAI/DLT, call-recording consent, data-retention counsel review |
| Existing MME identity/tenant integration | BLOCKED | MME Express repo not available; needs token format (HS256 shared secret or replace `decode_token`) and mapping of `tenant_id` to the MME tenant model |
| Browser calling (WebRTC) | NOT IMPLEMENTED | `mod_verto`/WSS not in this build; needs module + TLS cert + ICE/TURN; deliberately deferred |

## Phase 3 readiness (interfaces only; nothing integrated)
* A call can be **parked** after answer (`originate … &park()`; inbound `action: park`) — the hook point where a media session would attach.
* Correlation is stable: app `call_id` ↔ `fs_uuid` (`mme_call_id`, `mme_tenant`, `mme_leg` channel variables).
* Control surface for an agent: `hangup`, `transfer` (authorized destinations), lifecycle events via ESL/webhooks.
* **Media is not provided**: ESL carries no audio. Phase 3 must evaluate `mod_audio_fork`/`mod_audio_stream`-style WebSocket media forking, `mod_opus`/L16 negotiation, jitter/backpressure and barge-in. The `plivo_api` path would instead use Plivo's `<Stream>`. None of this exists here and `ai_enabled` stays false.
