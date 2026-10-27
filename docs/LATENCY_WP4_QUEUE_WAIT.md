# WP4: voice queue wait (target 1.1 s -> 0.3 s)

Tags: REAL = given by the operator from the console; MEASURED = from a trace or a test with job ids; ESTIMATE; SIMULATED; HYPOTHESIS / UNVERIFIED; UNKNOWN.

## Facts (REAL, operator)

| endpoint | Active workers | Max workers | idle timeout | scaling |
|---|---|---|---|---|
| voice (TTS) | 0 | 2 | 300 s since 26 Oct 2026 (was 5 s during the last supervised call) | queue delay 4 s |
| speech (STT) | 0 | 2 | 300 s | not stated |
| Brain | 0 | 2 | 300 s | not stated |

A second voice worker starts only after a job has waited 4 s, and then needs about 41 s (REAL, operator); cold starts so far: 41 to 52 s.
RunPod docs (docs.runpod.io/serverless/endpoints/endpoint-configurations, read 26 Oct 2026): queue delay "adds workers when requests wait longer than the threshold (default: 4 seconds)";
request count starts `ceil((requestsInQueue + requestsInProgress) / scalerValue)` workers ("use scaler value of 1 for max responsiveness"); "active workers incur charges continuously, including when they have no jobs";
idle timeout default 5 s and "the worker is billed while it waits"; FlashBoot "retains worker state after spin-down, allowing faster revival than fresh boots" and "is most effective on endpoints with consistent traffic".

## Readiness: what counts as warm (27 Oct 2026)

/health worker counts (idle, ready, running, init) are NOT a warm/cold signal: they behave like worker slots that add up to Max workers (UNVERIFIED). Job 1 of the 27 Oct test was a cold start (queue 48.9 s) while /health showed idle 2 / ready 2, which confirms it. Wake and readiness logic may use only completed jobs (real jobs or `{"warmup": true}` replies) and their delay times: under 5 s = already warm, 20 s or more = cold start. Details and every reading so far: docs/LATENCY_HEALTH_READINGS.md. Statements below that speak of "warm workers" mean workers that served a job quickly, not workers /health lists.

## What was measured about the voice queue (MEASURED)

| situation | queue (RunPod delayTime) | source |
|---|---|---|
| warm job, live app path, idle 5 s | median 1.10 s | last supervised call, warm turns 1-6 |
| warm jobs, idle 300 s, 20 s apart | steady 1.16-1.25 s (median 1.226 s, n=5) | voice_queue_test, 27 Oct 2026 (job ids in the result file) |
| back-to-back voice jobs | about 0.15 s | Phase 0b |
| speech endpoint (idle 300 s) between turns | about 0.14 s | last supervised call |
| first job after a long idle stretch | 48.9 s (cold start) | voice_queue_test job 1, 27 Oct 2026; counter 103 -> 109 over the 6 jobs |

The warm voice queue is therefore the same 1.2 s at idle 5 s and idle 300 s, much larger than back-to-back (0.15 s) and than the speech endpoint (0.14 s).

## Two hypotheses (BOTH UNVERIFIED)

- **H1, workers are restored after each job.** The voice workers are shut down and restored (FlashBoot) after every job, i.e. the effective idle timeout is still short. The 1.2 s would be the restore time. For: the queue is high after a gap of 20 s or more and low (0.15 s) back-to-back. Against: the idle timeout was raised to 300 s and the queue did not change.
- **H2, a fixed dispatch delay specific to this endpoint.** Something in the dispatch of this endpoint (its image, its worker class, its scaler) adds about 1.2 s to every job that does not directly follow another one. For: the same value at idle 5 s and 300 s. Against: nothing measured yet shows why the speech endpoint does not have it.

The gap sweep (docs/LATENCY_PAID_TESTS_REQUESTS.md, test 5) separates them by measuring the queue against the gap since the previous job's completion; the Active-workers test (test 6) separates them by removing every possible shutdown. Nothing here is a fact until one of them has run.

## Decision: tts_keepalive stays OFF; its cost corrected

The keep-alive ping (written in the other worktree, NOT ported here) costs the worker's UPTIME for the whole call, not only the pings: a worker that is kept alive is billed for as long as it is kept alive, about the GPU price times the call length. At the operator's price of $0.00019 per second that is $0.034 for a 180 s call and $0.057 for a 300 s call; at $0.0003 per second $0.054 and $0.09 (ESTIMATE; only Billing is authoritative). The operator's earlier figure of about $0.007 per call counted only the pings and is too low.
It is re-evaluated only if the sweep shows a short effective idle timeout (H1): then a keep-alive would be the cheap way to hold the worker, and its price must be compared with Active workers = 1.

## Levers (all console settings, operator-side)

| lever | effect | cost | status |
|---|---|---|---|
| idle timeout 5 s -> 300 s | worker stays up between turns; MEASURED: did NOT remove the 1.2 s queue | the worker is billed for the idle time: about (call length + 300 s) per isolated call | done 26 Oct |
| Active workers = 1 | no cold start; if H1 is true it also removes the 1.2 s; the test in docs/LATENCY_PAID_TESTS_REQUESTS.md (test 6) measures it | billed 24 h a day: 2,628,000 s a month x price = about $499 per month at $0.00019 and $788 at $0.0003 (UNVERIFIED rates; no active-worker discount is documented) | not set; a 10-minute test is designed |
| Active workers = 2 | a second voice worker that does not wait 49 s: removes the gaps of the clause ramp (SIMULATED, see below) | twice that | not set |
| scaling: request count, scaler value 1 | a second worker is started as soon as 2 requests are in the system (instead of after a 4 s wait); ready about 41-52 s later (REAL) | extra worker time while it runs | not set; helps from the next turn on, not within the same reply |
| dedicated Pod running the voice worker as an HTTP server | no cold start, no queue, no scaling | hourly price, always on (UNVERIFIED rate) | needs a code change of the worker; not built |

## SIMULATED effect on the clause ramp, with the measured queue (23-word reply; `python tools/sim_ramp_gap.py`)

Inputs MEASURED: voice wall times and audio lengths per clause (Phase 0b, back-to-back, queue 0.15 s inside them), the warm queue of 1.2 s per job, a cold start of about 49 s. Values between and beyond the measured points are interpolated: ESTIMATE. Assumes the Brain delivers the clause text faster than the voice.

| voice workers | plan | first sound (s) | gap after clause 1 (s) | all gaps (s) | reply ends (s) |
|---|---|---|---|---|---|
| 1 worker, queue 0.15 s (Phase 0b) | today 9+14 | 3.62 | 1.49 | 1.49 | 13.21 |
| 1 worker, queue 0.15 s (Phase 0b) | ramp 4+8+11 | 2.63 | 1.29 | 2.38 | 14.01 |
| 1 worker, warm queue 1.2 s | today 9+14 | 4.67 | 2.54 | 2.54 | 15.31 |
| 1 worker, warm queue 1.2 s | ramp 4+8+11 | 3.68 | 2.34 | 4.48 | 17.16 |
| 2 workers, second restored in about 1.2 s | today | 4.67 | 0.0 | 0.0 | 12.77 |
| 2 workers, second restored in about 1.2 s | ramp | 3.68 | 0.0 | 0.04 | 12.72 |
| 2 workers, second cold (about 49 s) | today | 4.67 | 2.54 | 2.54 | 15.31 |
| 2 workers, second cold (about 49 s) | ramp | 3.68 | 2.34 | 4.48 | 17.16 |

Reading:
- With the measured 1.2 s queue every plan starts about 1.05 s later than the Phase 0b numbers (first sound 4.67 s today, 3.68 s with the ramp): the ramp still saves about 1.0 s of first sound, but the target "first sound <= 2.5 s with the acknowledgement" is not reachable by clause sizes alone.
- With ONE worker the ramp creates a silence of about 2.3 s after the first clause (today's plan already has 2.5 s), because each job waits 1.2 s again. The queue, not the clause length, is the cause.
- With TWO workers and a second worker that is restored in about 1.2 s, the ramp has no silence (0.04 s total). With a second worker that needs a cold start of about 49 s, it is as bad as one worker within a reply of this length (the second worker is not there yet).
- The 15- and 28-word cases and the least-silence plans are printed by the tool (`table3`, `table2`).

## Capped tests

The original idle-timeout test (6 jobs at idle 5 s and 300 s) was reduced to the 300 s run, which ran on 27 Oct 2026 (results above). The next tests are designed, not run: the gap sweep and the Active-workers test, in docs/LATENCY_PAID_TESTS_REQUESTS.md (tests 5 and 6).
