# App-to-RunPod network, per-request overhead, and what to measure

Tags: MEASURED (a command output of 26 Oct 2026 from the WSL host that runs the service), ESTIMATE, UNVERIFIED, UNKNOWN.

## Hops of one turn

Caller phone -> Plivo -> (WebSocket media) -> ngrok tunnel -> the service (WSL on the operator's PC) -> internet -> `api.runpod.ai` (Cloudflare edge) -> RunPod gateway -> worker (STT, Brain, voice) and back.
Per turn the service makes at least: STT 1 job (POST /run + status polls), Brain 1 streamed request, voice 1 job per clause (POST /run + status polls).
Where the service, ngrok, Plivo's media servers and the three RunPod endpoints sit (region/datacenter) is UNKNOWN to me.

## Evidence 1: the path to the RunPod API (MEASURED, no key, no job, free)

- `api.runpod.ai` resolves to Cloudflare addresses only (IPv6 2606:4700::...): the API is behind Cloudflare.
- ICMP ping, 5 packets: min 13.4 ms, average 57.1 ms, max 117.2 ms. So the nearest edge is close (tens of ms).
- curl, 5 requests to a non-existent endpoint id (HTTP 404 answered by the gateway): DNS 17-81 ms, TCP connect 34-226 ms, TLS done at 78-256 ms (first request 889 ms), first byte at **294-312 ms for the 3 typical requests** (1.32 s and 0.71 s for the first two).
- `tools/health_timing.py`, 10 requests 1 s apart per endpoint, NO key (HTTP 401, answered by the gateway; the key-less numbers show the path and the gateway, not the worker):

| endpoint | series | connect (TCP+TLS) ms min/median/max | first byte ms min/median/max | total ms min/median/max |
|---|---|---|---|---|
| speech (STT) | fresh connection | 42 / 122 / 342 | 266 / 340 / 562 | 267 / 342 / 565 |
| speech (STT) | reused connection | 0 / 0 / 63 | 210 / 219 / 309 | 211 / 221 / 311 |
| Brain | fresh | 23 / 42 / 232 | 239 / 256 / 474 | 240 / 258 / 476 |
| Brain | reused | 0 / 0 / 295 | 208 / 215 / 523 | 210 / 218 / 525 |
| voice (TTS) | fresh | 27 / 109 / 210 | 249 / 335 / 646 | 251 / 338 / 648 |
| voice (TTS) | reused | 0 / 0 / 48 | 208 / 226 / 428 | 210 / 228 / 429 |

Not measured: the same GET with each endpoint's own key (the keys are not available to the assistant; `tools/health_timing.py` uses `RUNPOD_STT_KEY`, `RUNPOD_BRAIN_KEY`, `RUNPOD_TTS_KEY` when the operator runs it). An authenticated /health may take longer than a 401.

## What it implies (ESTIMATE / reasoning, to be confirmed with the per-job records of WP1)

1. **About 0.2 s per request is fixed gateway time, not our distance.** On a reused connection a request that never reaches a worker still needs about 0.22 s to its first byte, while the edge is about 15-60 ms away. Most of the 0.22 s is behind the edge (UNVERIFIED which part). Moving the service closer to RunPod would therefore save only the small part (tens of ms per request), not the 0.22 s.
2. **Every RunPod job costs at least two such requests**: POST /run (about 0.22 s + the upload) and the final status GET that returns the result (about 0.22 s), plus the time to notice that the job finished. This explains the two numbers the operator asked about:
   - the **0.3 s submit** is the 0.22 s gateway floor plus the upload of the body (the triple-copy body of about 190 KB for 3 s of audio adds the rest; `stt_raw_pcm` cuts it to about a third);
   - the **0.7 s STT remainder** (MEASURED median 695 ms: client time minus RunPod queue minus RunPod run) is about the submit (0.3 s) + the last status request (0.22-0.3 s) + the wait until the next poll + the WAV/base64 work. Little of it is payload.
3. **Polling is slower than it looks.** The poll loop waits 60-150 ms and then waits for each status request to return (about 0.22 s), so a poll starts every 0.28-0.37 s; a job that finishes just after a poll is noticed one period later. Cheaper than more requests: send the first poll near the predicted end, then start a new poll every 0.1 s WITHOUT waiting for the previous one (overlapping polls, at most 10 per second per job, far below RunPod's `/status` limit of 2000 per 10 s per endpoint, docs.runpod.io/docs/using-your-api). This is WP3 (`tts_adaptive_poll`, voice jobs only, default OFF): a draft with mock tests, not committed. **Expected saving: at most about 0.15 s per voice job** (the time to notice a finished job drops from about half a poll period, 0.14-0.18 s, to about 0.05 s; ESTIMATE, not measured). It does not touch the fixed 0.22 s per request, and the first poll near the predicted end saves requests, not time.
4. **`/runsync` makes one request instead of two or more**, which would save one 0.22 s request if the job finishes inside the call. The operator's Phase 0 showed higher overhead for `/runsync` than for `/run`, so no saving is claimed; the comparison below decides. `/runsync` cannot be cancelled mid-job.
5. **Connection reuse is mostly in place** (one shared client, keep-alive). Fresh connections cost 40-120 ms (median) and sometimes more; the REUSED series still shows a max connect of 295 ms (Brain) and 63 ms (STT), i.e. an occasional reconnect. A keep-alive expiry of 120 s is set in the client; whether Cloudflare closes idle connections earlier is UNKNOWN.
6. HTTP/2 is not available (the `h2` package is not installed) and was not requested.

## The floor for queue-based jobs, and the only ways around it

**Floor.** A queue-based serverless job (`/run` + `/status`) costs two gateway requests of about 0.22 s each (MEASURED first byte of a request that never reaches a worker, reused connection): about **0.44 s per job that no code of ours can remove**, plus the time to notice the result (WP3 trims at most about 0.15 s of that), plus the upload. This is the floor of the queue-based design; `/runsync` makes it one request but is not shown to be faster (Phase 0), and it cannot be cancelled mid-job.

The only ways to get below it:

| way | what it is | status of the evidence |
|---|---|---|
| a persistent connection to a Pod | run the voice (or STT) handler as our own server on a Pod and keep ONE connection (WebSocket or HTTP keep-alive) open for the whole call: no queue, no gateway request per job, no polling | needs a code change of the worker and an always-on Pod (hourly price, UNVERIFIED); the only way to remove the per-request cost completely |
| a RunPod "load balancing endpoint" | docs.runpod.io/serverless/load-balancing/overview (read 26 Oct 2026): requests go "directly to workers without queuing" to the worker's own HTTP server at `https://ENDPOINT_ID.api.runpod.ai/<our path>`; "Lower (single-hop)" latency than queue-based; 30 MB payload; request timeout 2 min when no worker is available, 5.5 min processing; workers must answer a health check (`/ping`, 200 = healthy, 204 = initializing); no built-in retries, requests are dropped when overloaded | the page gives no latency numbers and does not mention WebSocket. UNVERIFIED whether the 0.22 s per request disappears (it is still `api.runpod.ai`, behind the same edge). Needs our own HTTP server in the worker image (a rewrite of the handler). Worth ONE capped measurement before any build |
| a streaming route for queue-based endpoints | the operations table lists `/stream` ("receive incremental results as they become available", rate limit 2000 per 10 s per endpoint) | it is still an HTTP request per read through the same gateway, so it does not remove the per-request cost; it helps only for incremental output. For the Brain, the `/openai/*` route (the app already streams over one connection) is the route that avoids polling. UNVERIFIED how `/stream` reads are issued (the page does not say) |
| WebSocket to a serverless endpoint | not mentioned in the documentation pages read | UNVERIFIED / no evidence it exists |

### When a load-balancing endpoint or a Pod would change the 0.44 s floor (rough effort and monthly cost; everything priced here is UNVERIFIED unless a source is named)

Prices: the operator's figure for the voice GPU is $0.00019 per second (REAL, operator); the earlier assumption was $0.0003 per second. A worker (or Pod) that runs all month costs 2,628,000 s x price: **about $499 at $0.00019 and $788 at $0.0003**. Only Billing is authoritative.

| case | what changes on the critical path | effort (ESTIMATE) | monthly cost (UNVERIFIED) |
|---|---|---|---|
| today: queue-based serverless (`/run` + `/status`) | 2 gateway requests per job (0.44 s) + noticing the result (WP3 trims at most about 0.15 s) | - | pay per second of worker time; Active workers 0 |
| load-balancing endpoint, one per worker type | ONE request per job (the worker answers in the same HTTP response): the floor falls from about 0.44 s to about 0.22 s per job, and the polling lag disappears. On the first-sound path there are two jobs (STT and the first voice clause): at most about 0.2 s + 0.2 s + the polling lag, i.e. up to roughly 0.5-0.7 s, if the 0.22 s per request really is per request and not per job (UNVERIFIED, needs ONE measurement) | each worker image becomes an HTTP server (for example FastAPI) with a `/ping` health route and our own route; a "direct" client mode (POST, read the response; cancel = close the connection; no job id, no `/cancel`); canary endpoints; about 3-6 working days per worker with tests | the documentation page names no separate price, so the same per-second worker billing as today is assumed; the same Active-worker rule applies (an always-warm worker is about $499-$788 per month). No built-in retries and requests are dropped when overloaded (documented): the client must retry |
| a Pod with one persistent connection (WebSocket or keep-alive HTTP) per call | no per-request gateway cost at all after the handshake; the cost left is the network round trip to the Pod (tens of ms) and the model's own run time | a new server (protocol, reconnect, health, logging, security), no autoscaling, a fallback to serverless when the Pod is down; about 5-10 working days | a Pod runs all month: hourly price x 730 h; at the voice figure of $0.00019 per second that is about $499 per month per GPU, so roughly $500-$1,000 per month for STT + voice (they may share one larger GPU: UNVERIFIED). Whether the Pod's HTTP proxy allows long WebSockets is UNVERIFIED |
| neither (Brain) | the Brain already streams over one connection (`/openai/*`): nothing to gain | - | - |

Reading: the cheap levers (idle timeout, Active workers, WP3 polls, the ramp with two warm voice workers) come first. A load-balancing endpoint is the only documented route that could take about 0.2 s off each job without leaving serverless; it should be tried with ONE capped measurement of a minimal echo endpoint (a REQUEST, a few cents) before any worker is rewritten. A Pod is the only way to remove the per-request cost completely and costs about as much as one always-on worker.

Nothing else changes the floor: a closer host saves tens of ms per request, HTTP/2 was not requested (and `h2` is not installed), and a bigger or faster GPU shortens the run, not the gateway.

## What to measure next (free unless stated)

| what | how | cost |
|---|---|---|
| per-job breakdown on a real call | WP1 job records (submit ms, request bytes, polls, poll ms, last GET ms, response bytes, RunPod delay/execution, remainder) with `MME_TEL_AI_TRACE_EVIDENCE_PATH` set and the tenant flag `trace_evidence` on | part of the supervised call |
| authenticated /health | `RUNPOD_STT_KEY=... RUNPOD_BRAIN_KEY=... RUNPOD_TTS_KEY=... python tools/health_timing.py --tenant mme-test` (operator runs it, or gives the assistant the key file paths) | free |
| upload speed of this PC | any speed test (operator); compare with `submit_ms` against `request_bytes` in the job records | free |
| service-to-Plivo path | ngrok region setting and Plivo media region (console facts, UNKNOWN to the assistant); the "path to the ear" of about 0.2 s in the trace | free |
| where the three endpoints run | RunPod console: datacenter of each endpoint (the operator) | free |
| a service host in another region | only if the evidence shows a distance problem; not indicated by the numbers above | n/a |

## REQUEST (not run): STT-only /run versus /runsync

Action: the same recorded 3-5 s clip (the operator's own file, not in the repository), 10 jobs per mode, alternating /run (+ status polls) and /runsync, 5 s apart, STT endpoint only, first job of each mode discarded; record client total, submit/POST time, polls, RunPod delay, execution, remainder. Numbers only.
Endpoints that must be warm: the speech endpoint (idle 300 s) - run it inside the warm window of a supervised call so only the 20 jobs are extra.
Cost (ESTIMATE, GPU price UNVERIFIED, assumed $0.0003 per GPU-second): 20 jobs x about 0.7 s of execution = 14 s = about $0.004 when the worker is already warm; if the worker must be started first, add the cold start and the 300 s idle tail (about $0.12 more).
Proposed cap: $0.03 for the 20 jobs on a warm worker (the idle tail is not part of the test). No claim is made that /runsync saves time.
