# Live verification plan (12 steps)

Rules: every step that places a chargeable call, touches a DID or changes a production system needs **explicit operator approval for that step**. Evidence goes in `docs/evidence/<date>-step<N>/` using `docs/evidence/TEMPLATE.md`; secrets and full phone numbers are masked. A failed step is recorded as failed; it is never converted to a skip or a weaker assertion. Mock output is not evidence.

Current state: **step 1 only is runnable without new inputs.** Everything else is blocked on the inputs in the right-hand column.

| # | Step | Pass criteria | Needs (from operator) | Cost / approval |
|---|---|---|---|---|
| 1 | Local gate: full suite, pyflakes, `pip-audit`, `validate_config.py` on the release commit | all green | nothing | none. **Done 2026-10-21: 269 passed, 1 skipped** |
| 2 | Plivo credentials check (read-only API: account details, number list) | 200 from Plivo; no secret in output | `PLIVO_AUTH_ID`/`PLIVO_AUTH_TOKEN` in repo B's environment | none |
| 3 | Public reachability: `GET /health/live` over HTTPS from outside; TLS chain valid | 200, valid cert | public HTTPS URL (`WEBHOOK_PUBLIC_BASE_URL`) | none |
| 4 | Signature negative tests against the public URL (unsigned, wrong token, replayed nonce) | 403 / replay handled; no call created | step 3 | none |
| 5 | Plivo Application configured (answer/hangup/status URLs) on a **dedicated test number**, not a live DID | Console shows the URLs; no change to any production number | test number, Console access | none |
| 6 | One inbound call, no AI (`park`/`bridge` route): call record created, state transitions, hangup recorded; capture real callback payloads | state machine ends `completed`; events timestamped | step 5; a phone to dial from | one inbound call (approve) |
| 7 | Plivo stream handshake: one inbound call with the AI route and models mocked **off** (stream connects, `start` event captured, handshake signature form recorded, `extraHeaders` echo recorded, `playAudio` contentType accepted) | the three unverified protocol items are settled with captured evidence; code adjusted if reality differs | step 6 | one call (approve) |
| 8 | Real models: `scripts/ai_bench.py` single-call latency against the three real endpoints with a real speech WAV; verify request/response shapes | no `bad_response`; p95 recorded | model URLs + key, GPU, a real speech recording | GPU time |
| 9 | One real AI call end to end (inbound): conversation of >=3 turns, one barge-in, clean hangup from each side (two calls) | `collect_call_evidence.py` output shows turns, per-stage timings, no errors; operator confirms audio quality by listening | steps 7, 8 | 2-3 calls (approve) |
| 10 | Outbound call to the operator's own phone | provider ids recorded, state `completed`, billing duration matches | outbound caller-id/number allowed on the account | one call (approve) |
| 11 | Failure drills on the production-like host: model endpoint down mid-call, `systemctl restart` with 1 live call (recovery + drain), Mongo restart, kill -9 | per runbook expectations: call ends cleanly or is recovered, no orphan Plivo call, no stuck state | production-like host, maintenance window | disruptive: approve |
| 12 | Capacity ramp: 1, 2, 5, 10 simultaneous AI calls (stop at first SLO breach); 24 h soak at low load | p95 turn latency and error rate within the SLOs in the operations plan | budget approval, model capacity | paid: approve each level |

After step 12, the readiness matrix is updated row by row from the evidence, and only then is a DID migration proposed (separately approved).

## Evidence collection

* `python scripts/collect_call_evidence.py <call_id> --out docs/evidence/<dir>/call.json` (numbers masked by default)
* `python scripts/ai_bench.py ... --out docs/evidence/<dir>/ai_bench.json`
* Plivo-side: call detail record screenshot or API response (masked), Console configuration screenshot
* Host: `journalctl -u mme-telephony --since ...` excerpt (redacted), `/metrics` snapshot
