# Dashboard: Live operations

Open `/dashboard/` and sign in (see `PRODUCTION_RUNBOOK.md` §4 for users). The console has a **Live operations** page (`#/ops`) that shows every flow as it happens, plus the existing Overview, Live calls, Sessions, Recordings, AI configuration, Logs and Users pages.

Status of this feature: implemented and tested locally (unit, API, SSE over a real server, headless Chromium). It has **not** been exercised with a real Plivo call or real models, so everything it shows today is the real state of this service (real MongoDB, real FreeSWITCH connection health) with no live traffic.

## What the page shows

| Section | Source | Updates |
|---|---|---|
| **System flow**: tiles for Plivo, FreeSWITCH events and commands, media bridge, call controller, MongoDB, webhook worker, STT, Brain, TTS, RAG, grouped Entry -> Telephony -> Platform -> AI models, each with a real status and detail | live checks (`collect_health`, Plivo stream prerequisites, model probes at most every 30 s per tenant, only when AI is on and configured) | every 2 s |
| **Gauges**: active calls, queued, active sessions, AI conversations live, calls today, completed, failed, missed | MongoDB (tenant-scoped) | every 2 s |
| **Admin only**: AI turn p50/p95, AI turns, AI turn errors, Plivo streams open, ESL reconnects | the process's Prometheus metrics (whole process, all tenants, labelled as such) | every 2 s |
| **Sparklines**: active calls and live AI conversations over the last ~5 minutes | kept in the browser from the snapshots (nothing stored on the server) | every 2 s |
| **Calls and their pipeline**: one card per active call (up to 50) plus the 10 most recent ended, each with the stages `Call created -> Ringing -> Answered -> Session -> Media stream -> AI conversation -> Ended`, each stage `done / active / waiting / failed / skipped` | derived only from stored state (`app/services/flow.py`); a stage without evidence is `waiting` or `skipped`, never assumed done | every 2 s |
| **Live event feed**: call state changes, provider callbacks, media events, session changes, AI session and per-turn timings, webhook deliveries; filter by text, category, level; pause/resume | in-process activity bus | pushed as it happens |
| **Recent warnings and errors** (admin) | in-memory log ring, redacted | every 2 s |

Each call card links to the call page, which now has a **Flow** section with the same stages next to the existing AI pipeline and lifecycle timeline.

## How it works

* `GET /v1/live/stream` (Server-Sent Events, viewer role): `hello`, `backlog` (last 50 entries), `activity` (new entries, immediately), `snapshot` (every 2 s). Fallback: the page polls `GET /v1/live/snapshot` and `GET /v1/live/activity?after=<seq>` every 3 s and retries the stream every 15 s. `GET /v1/live/calls/{id}/flow` gives one call's stages.
* Producers publish to an **in-process `ActivityBus`** (a bounded ring of 2,000 entries) next to the action they describe: call events (`Repos.add_event`, only genuinely new ones), session changes, AI session start/stop/failure, one `ai.turn` per conversation turn with stage timings, `ai.turn_error`, and webhook delivery results. Publishing never raises into a call path.
* The feed is ordered by a monotonic sequence number, not timestamps, so it does not depend on the host's wall clock (this dev host's clock steps by about 51.7 s) and adds no MongoDB polling.
* Limits: 5 streams per tenant (6th gets 429 `too_many_streams`); a stream ends after an hour and the page reconnects; a reader that falls behind the ring is told how many entries it missed. The ring is per process and resets on restart: durable per-call history stays in `call_events` (call page, timeline).

## Privacy and access

* Tenant isolation everywhere: entries are returned only to the tenant that owns them; another tenant gets 401/404/empty.
* Phone numbers inside feed entries are always masked; in call cards they are masked for read-only (viewer) users, as elsewhere in the console.
* **What callers say is never published.** `ai.turn` carries stage timings (STT, RAG, Brain, TTS, total) and a turn counter only. Transcripts and summaries are still not stored by this release; showing them live would need a consent and retention decision first.
* Secret-looking keys are masked and long values are cut before an entry is stored. The page renders with text nodes only and the strict CSP is unchanged (no inline script or style, no `unsafe-eval`).
* Process-wide metrics and error logs are admin/platform only.

## Not covered / limits

* No history beyond the ring (use call pages, `/metrics` and Prometheus for history). No per-tenant AI latency distribution (turn latency is process-wide, labelled as such).
* No alert delivery from the dashboard; alert rules live in `PRODUCTION_RUNBOOK.md` §5 and are not loaded anywhere yet.
* The Repo A prototype (`:8090`) is not shown: this console covers Repo B only.
* Real traffic has not been seen by this page. Expect to tune it (for example which events are noise) after the first real calls.

## Tests

`tests/unit/test_live_activity_flow.py` (bus ordering, tenant scope, redaction, bounds, wake-up, histogram quantiles, every flow state), `tests/integration/test_live_api.py` (auth, tenant isolation, masking, no words in `ai.turn`, duplicates not announced, SSE hello/backlog/snapshot/live activity over a real server, stream limit and slot release), `tests/e2e/test_dashboard_browser.py` (headless Chromium: page goes live, tiles and pipeline chips, new events pushed without reload, filter, pause/resume, drill-down, viewer masking).
