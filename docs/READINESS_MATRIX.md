# Readiness matrix

Status vocabulary (strongest evidence achieved, nothing inferred):

* **IMPL+LOCAL**: implemented and covered by automated tests on this host (mocks / loopback / local mongod / local FreeSWITCH)
* **REAL-SVC**: verified against a real external service (Plivo, real model servers)
* **REAL-CALL**: verified end to end on a real telephone call
* **LOAD**: load-tested at the stated level
* **UNVERIFIED**: not yet verified
* **BLOCKED**: cannot be verified without an input listed in `LIVE_VERIFICATION_PLAN.md`

As of 2026-10-21: suite 269 passed / 1 skipped. **No row below has REAL-SVC or REAL-CALL status.** That is the honest state, not an omission.

| Feature | Status | Evidence / note |
|---|---|---|
| Call state machine, CAS on `state_version`, idempotency | IMPL+LOCAL | integration + resilience tests, real mongod |
| Outbound call via FreeSWITCH (mock provider) | IMPL+LOCAL, LOAD (control plane) | `CAPACITY_REPORT.md` profiles A–C: 30 concurrent x 180 s on real FreeSWITCH loopback, 0 errors. Not a carrier test |
| Outbound call via Plivo | UNVERIFIED / BLOCKED | needs credentials, number, approval |
| Inbound Plivo (answer/status/hangup callbacks, V3 signature, replay) | IMPL+LOCAL | mock Plivo with SDK-computed signatures. Real Plivo: BLOCKED |
| Plivo bidirectional stream (`<Stream>`, ticket, media, barge-in, checkpoints) | IMPL+LOCAL | mock WebSocket client. Protocol spellings in the gap report are unverified: BLOCKED |
| FreeSWITCH media-fork AI path (`mod_audio_stream`) | IMPL+LOCAL | real FreeSWITCH + real module on loopback with synthetic audio, mock models. Real carrier audio: BLOCKED |
| STT / Brain / TTS pipeline | IMPL+LOCAL | mock model servers only. Real models: BLOCKED (no endpoints/GPU) |
| Malformed model reply / bad TTS audio robustness | IMPL+LOCAL | mutation-checked |
| Barge-in, endpointing, bounded queue | IMPL+LOCAL | synthetic tones; tuning against real speech: UNVERIFIED |
| Startup recovery | IMPL+LOCAL | tests; real crash on production host: UNVERIFIED |
| Graceful drain on SIGTERM | IMPL+LOCAL | app-level drain tested; a real `systemctl stop` with live calls: UNVERIFIED |
| FreeSWITCH inbound AI auto-start (`AI_FS_AUTOSTART`, route action `ai`; replaces prototype `maybe_start`) | IMPL+LOCAL | decisions tested with a fake bridge/ESL (9 tests); `uuid_getvar` guard format checked against the real local FreeSWITCH (loopback). Not exercised end to end with a real inbound DID: BLOCKED (needs dialplan change + approved call). See `REPO_A_B_INTEGRATION.md` |
| Sweepers / reconciliation / orphan backstops | IMPL+LOCAL | real mongod + FreeSWITCH loopback |
| MongoDB indexes, TTL retention, schema v4 | IMPL+LOCAL | |
| Backup + restore | IMPL+LOCAL | `verify_backup_restore.sh`: counts and index names equal for 4 collections on local mongod. Atlas: BLOCKED. RPO/RTO measured: UNVERIFIED |
| Secrets handling, log redaction | IMPL+LOCAL | tests; rotation of exposed credentials is an operator action (see gap report §3) |
| Dependency audit | IMPL+LOCAL | `pip-audit` clean for the pinned set on 2026-10-21; re-run before each release |
| TLS / firewall / public exposure | UNVERIFIED / BLOCKED | no public host |
| Rate limits, concurrency caps | IMPL+LOCAL | tests |
| Prometheus metrics, JSON logs | IMPL+LOCAL | scrape tested locally; alert rules written in `OPERATIONS_RUNBOOK.md`, not wired to a real Alertmanager: UNVERIFIED |
| Dashboard | IMPL+LOCAL | headless Chromium e2e |
| AI capacity (concurrent AI calls, GPU) | UNVERIFIED / BLOCKED | `scripts/ai_bench.py` ready; needs endpoints |
| Cost per call/minute | UNVERIFIED | method in `CAPACITY_AND_COST.md`; no invented prices |
| 12-step live test program | UNVERIFIED / BLOCKED | `LIVE_VERIFICATION_PLAN.md`; step 1 is the only one runnable now |
| 12-month operations plan | Documented | `OPERATIONS_PLAN_12_MONTHS.md`; nothing operated yet |

## Verdict

Not yet verified for production with real callers. The code is hardened and locally tested; the remaining risk is entirely in behaviour that only real Plivo, real models and a real host can show. Do not move a live DID until steps 1–9 of the verification plan have evidence.
