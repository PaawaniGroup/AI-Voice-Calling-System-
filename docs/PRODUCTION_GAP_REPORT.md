# Production gap report

Audit date: 2026-10-21. Scope: repo B (MME_Telephony) as run from WSL. Evidence rules: a mock, synthetic client or local loopback is **never** counted as live evidence. Where nothing real could be run, the item is marked unverified.

## 1. Defects found and fixed in this phase (each has a test; mutation-checked where noted)

| # | Finding | Severity | Fix | Test |
|---|---|---|---|---|
| 1 | A malformed model reply (non-JSON STT/Brain/RAG body, wrong shape) surfaced as an unclassified exception and could kill the conversation worker | High | Parse failures become `AiError(..., "bad_response")`; the worker catches any exception per turn, counts it, continues | `test_production_hardening.py` (24 tests; 17 failed against the pre-fix code) |
| 2 | TTS audio was trusted: stereo, odd sample rates, empty, truncated or very long WAVs could reach the caller | High | `wav_to_pcm16` validates channels, width, rate, length, truncation; `bad_audio` error, nothing played | same file |
| 3 | A failed/cancelled/empty turn left half a turn in the conversation history (the next reply saw the orphaned user message) | Medium | History rolled back to the turn-start mark on cancel, `AiError`, unexpected exception, empty reply | same file |
| 4 | Service restart orphaned live state: `ai_sessions` left `active`, Plivo streams with no owner | High | `recover_on_startup()` (AI sessions) + `recover_provider_streams()` (hang up orphaned Plivo calls), failures caught so startup never blocks | same file |
| 5 | `SIGTERM` dropped calls with no drain | High | `app.state.drain`: stop accepting (Plivo answer → busy, readiness `draining`), wait `DRAIN_TIMEOUT_S`, stop AI runtimes, provider hangups. systemd `TimeoutStopSec` raised 20 → 45 s | same file |
| 6 | Duplicate inbound Plivo callbacks could create two calls | High | Unique partial index `uniq_inbound_provider_call` (schema v4); duplicates and replays re-describe the existing call | `test_plivo_voice.py` |
| 7 | Concurrent sweepers could hang up the same orphan Plivo stream twice | Medium | Atomic `find_one_and_update` claim on `stream_watch_done` | `test_plivo_voice.py` |
| 8 | No per-turn latency or error visibility | Medium | `AI_TURN_SECONDS`, `AI_TURN_ERRORS{reason}`, `AI_RUNTIMES`, `DRAINING`, `RECOVERY`, `PLIVO_*` metrics (bounded labels) | `test_production_hardening.py` |
| 9 | Dependency vulnerabilities in the unpinned set | High | Upgraded in a dedicated venv, `pip-audit` clean for the pinned set, exact pins in `requirements.txt`, full freeze in `requirements.lock.txt` | full suite on the new venv |
| 10 | Restart-persistence test raced the origination (test bug, product correct) | Low | Test waits for the origination itself, then confirms no duplicate; 15/15 on both venvs | `test_resilience.py` |

Result of the full suite on the audited venv after all changes: **269 passed, 1 skipped** (the skip is the environment-gated test, unchanged). pyflakes on `app config scripts` is clean.

## 2. Gaps that remain open (not fixable from this machine)

| Gap | Why it matters | What closes it |
|---|---|---|
| No Plivo credentials loaded into repo B; no Application pointed at it | Every Plivo behaviour is verified against a **mock** only | Operator loads `PLIVO_AUTH_ID`/`PLIVO_AUTH_TOKEN` into repo B's environment; creates an Application; supplies a dedicated test number |
| No public HTTPS/WSS endpoint | Plivo cannot reach answer/status/stream URLs | Reverse proxy with TLS (or an approved tunnel) in front of 127.0.0.1; see `docs/PLIVO_VOICE_APPLICATION.md` |
| Three protocol details are unverified against real Plivo | (a) `playAudio` `contentType` spelling, (b) which URI form Plivo signs on the WebSocket handshake (the code accepts the `wss://` form and its `https://` twin), (c) whether `extraHeaders` is echoed in the `start` event | Step 6 of `LIVE_VERIFICATION_PLAN.md`; capture one real `start` event and one real handshake |
| Model endpoints unreachable; no GPU | STT/Brain/TTS request/response **shapes** are unverified against the real Oriserve / Qwen servers; latency unknown | Operator provides URLs (+ key); run `scripts/ai_bench.py`; then step 8 |
| No production host, no Atlas | Backup/restore verified on a local mongod only; TLS, firewall, systemd unit untested on the target | Production host access; see runbook |
| `mod_audio_stream` load-on-restart not exercised | A FreeSWITCH restart might not reload the module | Approved FreeSWITCH restart in a maintenance window |
| Backup restore test seeds `calls`, `call_events`, `tenant_policies`, `_schema` only | `sessions`, `ai_sessions`, `idempotency`, `recordings` are not compared | Extend the seed (small change) before relying on RPO claims for those collections |
| Recording storage on S3 untested | `boto3` is not pinned/audited | Pin and audit when S3 is enabled |

## 3. Security findings

* **Repo B**: secrets only via environment; redaction in logs verified (including inline `password=`); ESL stays on 127.0.0.1; stream tickets are purpose-limited HMAC with replay protection; Plivo callbacks require V3 signatures. Rate limits exist per tenant/route. Not yet verified: TLS termination, firewall on the real host.
* **Third codebase `~/mme-plivo/app.py` (the user's test app, read only, not modified):** binds `0.0.0.0` with `--reload`; authenticates with a query-string token instead of signature verification; logs that token; exposes `/outbound`. **Action for the operator:** treat that token and the Plivo auth token as exposed, rotate both in the Plivo console, and bind to 127.0.0.1 or retire the app. I did not read or print any credentials.

## 4. Things deliberately not done

No chargeable call, DID change, tunnel exposure, paid load test, destructive migration or FreeSWITCH restart was performed.
