# Test report

Run date 2026-10-15, WSL2 Ubuntu 24.04, Python 3.12.3, FastAPI 0.115.6, pymongo 4.10.1, pytest 8.3.4, FreeSWITCH 1.11.4-dev (local), MongoDB 7.0.14 (local, `scripts/dev_mongo.sh`).

```
bash scripts/dev_mongo.sh
bash scripts/run.sh pytest -q            # full suite
```
**Result: 134 passed, 0 failed, 0 skipped (74.9 s).** Pyflakes: clean for `app/`, `config/`, `scripts/` (a few cosmetic warnings remain in tests). `.env.example` loads and validates (3 development warnings).

| File | Tests | Covers |
|---|---|---|
| `tests/unit/test_state_machine.py` | 24 | transitions, rejection, out-of-order, terminal immutability, hangup-cause mapping |
| `tests/unit/test_core.py` | 15 | phone normalisation/policy, dial-string injection resistance, webhook signing, SSRF blocklist, log redaction, backoff bounds, ESL parser limits |
| `tests/unit/test_configuration.py` | 32 | defaults safe, production secret enforcement, invalid ranges, ESL privacy rule, recording/Plivo/AI/WebRTC guards, secret non-leakage, `validate_config.py` exit codes |
| `tests/integration/test_api_calls.py` | 29 | auth (incl. alg=none, expiry, wrong secret), health, tenant disabled by default, create/dispatch, policy rejections, invalid bodies/413, idempotency (+concurrent), **cross-tenant isolation**, full lifecycle with duplicate/out-of-order events, failure classification, hangup/cancel/transfer, provider rejection/uncertain, capacity/queue/per-tenant dispatch, rate limit, pagination/usage/analytics, webhook signing/retry/replay/dead-letter |
| `tests/integration/test_plivo_callbacks.py` | 6 | Plivo V3 signature accept/reject, nonce replay, event dedup, out-of-order, status mapping, inbound routing/capacity |
| `tests/integration/test_freeswitch.py` | 15 | **real ESL/FreeSWITCH**: auth failure, connect + subscribe once, outbound answer/hangup + UUID correlation, busy, unroutable, cancel while ringing, transfer (via `mme_transfer` context), ESL drop and reconnect without duplicate subscription, **restart recovery**, uncertain-origination reconciliation, young-call protection, orphan-channel cleanup, inbound routed/unrouted/disabled/over-capacity via `mme_inbound`, FreeSWITCH-unavailable holds queue durably |
| `tests/integration/test_recordings.py` | 5 | policy matrix (disabled by default), local-storage path safety and no public URL, download authorization, **real FreeSWITCH recording, upload verification, metadata**, no recording without policy |
| `tests/resilience/test_resilience.py` | 8 | DB down gives 503 without leakage and idempotency key not burned, readiness vs liveness, fail-fast startup, restart + idempotency persistence (exactly one origination), event race gives a single terminal state, malformed events, queue timeout, max-duration backstop |

## Other verifications (commands run)
| Check | Command | Result |
|---|---|---|
| Config validation | `python scripts/validate_config.py` | `config OK` with 3 development warnings (JWT secret empty, provider ceiling, real calls off) |
| Real service start | `bash scripts/smoke_local.sh` | uvicorn started; `/health/live` ok; `/health/ready` reports database ok and freeswitch ok; unauthenticated status 401; authenticated status shows ESL connected, `ai_enabled:false`; OpenAPI exposes 25 paths; `/metrics` 200 on loopback; SIGTERM shutdown (exit 143 = uvicorn re-raising the signal), 0 error log lines |
| ESL | `python scripts/esl_check.py status` | auth + `api` succeed against 127.0.0.1:8021 (password never printed) |
| Backup/restore | `bash scripts/verify_backup_restore.sh` | mongodump then mongorestore into a second DB: calls 50/50, call_events 50/50, policies 1/1, schema 1/1, **index names identical**; `BACKUP/RESTORE VERIFIED` (local mongod only; Atlas auth/network not exercised) |
| Load | see `docs/CAPACITY_REPORT.md` | 3 profiles, 0 errors |

## Bugs found by testing and fixed
* Reconciliation could finalize an originating call (load test): grace rule + orphan cleanup added.
* Inline `uuid_transfer ... bridge:` to a loopback target killed the call: replaced with the controller-owned `mme_transfer` dialplan context (verified).
* Missing `call.initiated` webhook (dispatcher claim bypassed emission): fixed, tested.
* Negative durations on wall-clock steps: clamped, with `clock_anomaly`.
* Stale transfer attempt on a call that ended mid-transfer: closed as `call_ended`.

## Not tested (see PHASE_2_ACCEPTANCE.md)
Real Plivo (REST, callbacks from Plivo, inbound PSTN), real SIP gateway/carrier, S3 upload/presign/KMS, consent announcement audio, MongoDB Atlas, TLS/firewall/production host, WebRTC, multi-hour soak, provider limits. The opt-in real-provider suite exists (`tests/integration/test_real_provider.py`, marker `real_provider`) but has **never been run**: it skips unless `MME_TEL_REAL_PROVIDER_TESTS=1` plus Plivo credentials, a public callback URL, an approved test destination and a caller ID are set. It places one paid call.

Note: the FreeSWITCH tests skip automatically when ESL is unreachable. FreeSWITCH has no supervisor on the dev host and did not survive a WSL restart on 2026-10-16 (a run then showed 117 passed, 18 skipped); after restarting it the suite gave **134 passed, 1 skipped**.

## Plivo Voice Application integration (2026-10-21)
Run: `bash scripts/run.sh pytest -q` -> **238 passed, 1 skipped** (the opt-in real-Plivo test), 0 leftover FreeSWITCH channels; `pyflakes` clean for `app/`, `config/`, `scripts/`.
New: `tests/unit/test_plivo_stream.py` (25) and `tests/integration/test_plivo_voice.py` (44). **Mocked:** Plivo (SDK-signed requests, fake token, test WebSocket client), the STT/Brain/TTS servers, the Plivo REST client. **Real:** the service, MongoDB, HTTP/WebSocket server, conversation loop. No real Plivo call, no real model. Existing suites (169 + 1 skipped, including real-FreeSWITCH and headless-browser tests) pass unchanged. Details: `docs/PLIVO_VOICE_APPLICATION.md` §12.
