# Implementation plan and checklist

Legend: [x] done and tested, [~] done, partly verified, [ ] not done / blocked (reason).

## Milestone 1 — audit & architecture
- [x] Environment audit (ENVIRONMENT_AUDIT.md); architecture (ARCHITECTURE.md)
## Milestone 2 — foundation
- [x] Typed settings + validation + docs generator; JSON logging with redaction; health/readiness; Mongo persistence, indexes, schema version; JWT auth, roles, tenant isolation
## Milestone 3 — FreeSWITCH
- [x] ESL parser, event client (single subscription per connect, bounded queue, idle detection, backoff+jitter), command client (serialised, timeouts, teardown on timeout), UUID correlation, reconciliation — verified against local FreeSWITCH 1.11.4-dev
## Milestone 4 — lifecycle
- [x] State machine, idempotent event processing, dispatcher with queue/CPS/concurrency, cancel/hangup/transfer, timeouts, recovery
- [x] Inbound routing (`mme_inbound` context + `_on_inbound`) exercised on real FreeSWITCH via loopback legs — [~] **no real inbound SIP/PSTN call was made**
## Milestone 5 — provider
- [x] Provider interface, FreeSWITCH-gateway adapter, mock; Plivo adapter + V3 signature verification/replay protection — [ ] real Plivo originate/hangup/inbound **BLOCKED: no account/credentials**
## Milestone 6 — SaaS APIs
- [x] Calls, history, usage, analytics (+CSV), admin policy/routes, webhook endpoints/deliveries/replay, audit
- [~] Recording: control/metadata/upload implemented; real FreeSWITCH recording + verified upload to the local backend tested — [ ] S3/KMS/presigned URLs **BLOCKED: no AWS config**
## Milestone 7 — security/resilience
- [x] Cross-tenant, auth, injection, SSRF, idempotency races, duplicate/out-of-order events, DB down, FS down, reconnect, restart recovery, uncertain origination
## Milestone 8 — load/deploy
- [x] Synthetic load harness; results in CAPACITY_REPORT.md; systemd unit; backup/restore scripts
- [ ] Production host, TLS, firewall, real-carrier load **BLOCKED: external**
## Milestone 9 — acceptance
- [x] PHASE_2_ACCEPTANCE.md with PASS/BLOCKED per criterion

## Not in scope / deferred
Phase 3 AI integration (no STT/Brain/TTS code), browser calling (no `mod_verto` in this build), multi-node HA, Dockerfile (Docker unavailable), live MME Express/Atlas integration.
