# Acceptance report — MME AI Voice Counselling platform (calls, sessions, dashboard, AI contracts)

Run: 2026-10-19, WSL2 Ubuntu 24.04, Python 3.12.3. Full suite: **164 passed, 1 skipped** (the skip is the opt-in real-Plivo test). Command: `bash scripts/run.sh pytest -q` (needs local `mongod` and FreeSWITCH; Chromium for 3 tests).
Labels — **Mock**: mock model servers / mock carrier adapter / synthetic client. **Real-FS**: the actual local FreeSWITCH 1.11.4-dev over ESL (loopback legs, no carrier). **Real-browser**: headless Chromium. **Real-carrier / real-model**: never done.

| # | Requirement | Result | Evidence (test / script) | Used |
|---|---|---|---|---|
| 1 | Health and readiness | PASS | `test_health`, `test_readiness_reports_database_down`, `smoke_local.sh` | Real-FS + real mongod |
| 2 | Call creation and validation | PASS | `test_policy_rejections`, `test_invalid_bodies`, `test_create_and_dispatch`, FS `test_outbound_answer_correlate_hangup` | Mock carrier; Real-FS loopback |
| 3 | Inbound call event processing | PASS (loopback) | FS `test_inbound_routed_call…`, `…rejected_when_tenant_disabled_or_over_capacity`, `test_observed_inbound_creates_records…`, `verify_observe_inbound.sh` (your real `public` DID dialplan) | Real-FS, no carrier |
| 4 | Session creation and retrieval | PASS | `test_session_api_idempotent_validated_and_tenant_isolated`, `test_prototype_sequence_…` | real mongod |
| 5 | Answer-state transitions | PASS | `test_session_created_with_call_answered_and_ended_by_events` | events simulated; also Real-FS observe script |
| 6 | Hangup and final session state | PASS | same + FS `test_outbound_answer_correlate_hangup`; observe script shows `ended / normal_clearing / 1.0 s` | Real-FS |
| 7 | Duplicate event delivery | PASS | `test_full_lifecycle_duplicates_and_out_of_order`, `test_failed_call…duplicates…`, observed CREATE+PARK duplicate | simulated + Real-FS |
| 8 | Out-of-order events | PASS | `test_full_lifecycle_duplicates_and_out_of_order`, Plivo `test_valid_callbacks_progress_dedupe_and_replay` | simulated |
| 9 | API / database outages | PASS | `test_database_unavailable_returns_503_without_leaking`, `test_readiness_reports_database_down`, FS `test_freeswitch_unavailable_holds_queue_durably` | injected failures |
| 10 | Listener restart and session recovery | PASS (1 flaky run in ~12) | FS `test_recovery_after_controller_restart`, `test_state_survives_service_restart_and_idempotency_persists`, reconcile tests. One full-suite run failed this test while the WSL VM was stalling; it passed in 5 later runs | Real-FS |
| 11 | Duplicate outbound call requests | PASS | `test_idempotency`, `test_concurrent_duplicate_requests_create_one_call` | mock carrier |
| 12 | Authentication and authorization | PASS | `test_auth_required_and_validated` (incl. `alg=none`), `test_login_roles_and_failures`, `test_platform_role_only_from_cli_not_http` | — |
| 13 | Tenant isolation | PASS | `test_cross_tenant_access_denied`, session/AI-config/overview isolation, AI WS tenant check | — |
| 14 | Webhook signatures and retries | PASS | `test_webhook_delivery_signing_retry_and_replay`, `…dead_letter…`, `test_session_and_call_webhooks_emitted` | consumer mocked |
| 15 | Dashboard data loading | PASS | 3 Real-browser tests: login, overview, live SSE, call detail, hangup with confirm, sessions, AI banner, logs, viewer masking/permissions, logout; plus API/static/CSP tests | Real-browser, mock carrier |
| 16 | Call-control authorization | PASS | viewer 403 on `POST /v1/calls`, operator vs admin routes, browser viewer test | — |
| 17 | AI adapter timeouts | PASS | `test_adapter_timeout_and_unreachable_are_classified`, `test_model_timeout_is_nonfatal_error_event` | **Mock models** |
| 18 | AI session cancellation on hangup | PASS | `test_hangup_cancels_ai_and_closes_media_no_stale_audio` | **Mock models, synthetic client** |
| 19 | Real audio transport | **NOT DONE (blocked)** | no media-fork module installed; protocol + server side tested with a synthetic client only (`test_full_turn…`, `test_barge_in…`, reconnect, backpressure path) | Synthetic |
| 20 | End-to-end lifecycle with FreeSWITCH | PARTIAL | full lifecycle on real FreeSWITCH with loopback legs (answer, bridge/transfer, hangup, busy, cancel, recording, reconnect, recovery); **no real carrier leg** | Real-FS |

## Not verified / blocked
* **Real carrier**: no real Plivo call was made (inbound or outbound); trunk limits and callback fields unconfirmed.
* **Real audio and models**: no media-fork module (`mod_audio_fork`/`mod_audio_stream`), no model servers reachable, no GPU; real-time latency, Hindi/Hinglish quality, interruption behaviour on a phone call are **unknown**. AI stays disabled by default and refuses to start without a ready bridge.
* **Transcripts, summaries**: not stored; endpoints answer 404 (`transcript_not_available`, `summary_not_available`); `transcript.ready` / `summary.ready` webhooks are never emitted.
* **Recording** on S3, consent announcement audio, retention enforcement on real data: untested (local storage path tested).
* **MME identity integration**, MongoDB Atlas, production host/TLS/firewall, compliance review (DoT/TRAI, recording consent): not done.
* Dashboard: no browser matrix beyond Chromium; no UI for tenant policy / inbound routes / webhooks (API only); no accessibility audit; users page minimal. The log view is an in-memory ring (resets on restart).
* Single worker process; rate limits and queues are per-process.

## Defects found and fixed during this work
1. Concurrent `CHANNEL_CREATE` + `CHANNEL_PARK` could create two inbound call records → in-memory guard + unique partial index.
2. **FreeSWITCH event timestamps were 2.5 days behind the host clock**; the max-duration sweeper then hung up a live call. Fixed: stored times use the service clock, FreeSWITCH timestamps only as same-domain deltas.
3. Log redaction missed inline `password=…` in messages.
4. Session search missed external references; AI start leaked policy state across tenants before the 404.
5. Dashboard stale-render race and a noisy 404 on the call page.
6. Reconciliation could kill/finalize a still-originating call (earlier phase).

## Load re-check after these changes
`--mode fs --calls 300 --concurrency 30 --hold 5`: 300/300 completed, 0 errors, peak 30, 0 channels left; API latency p95 1.25 s (was 0.71 s before sessions/webhooks were added). `docs/load/fs_300x30_hold5_v3.json`. Same caveats as `CAPACITY_REPORT.md`.
