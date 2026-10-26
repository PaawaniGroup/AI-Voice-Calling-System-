# WP4: voice queue wait (target 1.1 s -> 0.3 s)

Tags: REAL = given by the operator from the console; MEASURED = from a trace; ESTIMATE; SIMULATED; UNVERIFIED; UNKNOWN.

## Facts (REAL, operator, 26 Oct 2026)

| endpoint | Active workers | Max workers | idle timeout | scaling |
|---|---|---|---|---|
| voice (TTS) | 0 | 2 | 5 s (being changed to 300 s) | queue delay 4 s |
| speech (STT) | 0 | 2 | 300 s | not stated |
| Brain | 0 | 2 | 300 s | not stated |

A second voice worker starts only after a job has waited 4 s, and then needs about 41 s (REAL, operator). So the voice has ONE warm worker at best.
RunPod docs (docs.runpod.io/serverless/endpoints/endpoint-configurations, read 26 Oct 2026): queue delay "adds workers when requests wait longer than the threshold (default: 4 seconds)";
request count starts `ceil((requestsInQueue + requestsInProgress) / scalerValue)` workers ("use scaler value of 1 for max responsiveness"); "active workers incur charges continuously, including when they have no jobs";
idle timeout default 5 s and "the worker is billed while it waits"; FlashBoot "retains worker state after spin-down, allowing faster revival than fresh boots" and "is most effective on endpoints with consistent traffic".

## What the 1.1 s could be

MEASURED (last supervised call, warm turns 1-6, medians): voice queue 1095 ms, STT queue 139 ms. The STT endpoint has idle 300 s, the voice endpoint 5 s.
Hypothesis (NOT a fact): the voice queue of about 1 s per turn is a FlashBoot revival of a worker that went idle after 5 s (the gap between two turns is several seconds, so the worker is down at the first clause of every turn).
For: the difference between the two endpoints (about 0.96 s) matches a revival and not a long queue. Against / unknown: different endpoints, GPUs and workers; a second clause queued behind the first on the one worker also counts as queue.
The capped test below separates the two.

## Decision: tts_keepalive stays OFF and is not recommended

The keep-alive ping (written in the other worktree, NOT ported here) would bill a worker for every ping and, with idle 300 s, add no benefit over simply raising the idle timeout: any job already keeps the worker alive for 300 s.
The operator's own figure for it was about $0.007 per call (ESTIMATE, operator). Not built here.

## Levers (all console settings, operator-side)

| lever | effect | cost | status |
|---|---|---|---|
| idle timeout 5 s -> 300 s | worker stays up between turns of a call | the worker is billed for the idle time: about (call length + 300 s) per isolated call | operator is changing it |
| Active workers = 1 | no cold start or revival at all for the first call | billed 24 h a day: price per second x 86400 (GPU price UNVERIFIED; at an assumed $0.0003 per second = about $26 per day) | not set |
| Active workers = 2 | a second warm voice worker: removes the gaps of the clause ramp (SIMULATED, see below) | twice that | not set |
| scaling: request count, scaler value 1 | a second worker is started as soon as 2 requests are in the system (instead of after a 4 s wait); ready about 41 s later (REAL) | extra worker time while it runs | not set; helps from the next turn on, not within the same reply |
| dedicated Pod running the voice worker as an HTTP server | no cold start, no queue, no scaling | hourly price, always on (UNVERIFIED rate) | needs a code change of the worker; not built |

## SIMULATED effect of the worker model on the clause ramp

`python tools/sim_ramp_gap.py` (inputs MEASURED wall times and audio lengths; values between and beyond the measured points are interpolated: ESTIMATE; assumes the Brain delivers the clause text faster than the voice).

| reply words | voice workers | plan | first sound (s) | gap after clause 1 (s) | all gaps (s) | reply ends (s) |
|---|---|---|---|---|---|---|
| 23 | 1 warm | today (9+14) | 3.62 | 1.49 | 1.49 | 13.21 |
| 23 | 1 warm | ramp 4+8+11 | 2.63 | 1.29 | 2.38 | 14.01 |
| 23 | 1 warm | least silence with first sound <= 3.0 s (5+3+3+3+3+3+3) | 2.82 | 0.0 | 0.29 | 16.70 |
| 23 | 2 warm | today | 3.62 | 0.0 | 0.0 | 11.72 |
| 23 | 2 warm | ramp 4+8+11 | 2.63 | 0.0 | 0.0 | 11.63 |
| 23 | 2 warm | least silence with first sound <= 3.0 s (3+8+12) | 2.00 | 0.0 | 0.0 | 11.07 |
| 23 | 1 warm + 1 cold (45 s) | any | same as 1 warm | | | |

Reading: with ONE warm worker a short first clause buys about 1.0 s of earlier sound at the price of a silence of about 1.3 s after it (today's 9-word plan also has 1.49 s); a plan with no silence exists but means seven 3-word clauses
(choppy speech, more jobs) and the reply ends about 3.5 s later. With TWO warm workers the ramp has no silence at all. The cold second worker (ready 45 s after the trigger) does not help within one reply.
Therefore: stream_ramp pays off fully only with Active workers = 2 on the voice endpoint, or if the queue wait of the first clause is removed (idle timeout) so that clause 2 is not delayed behind a revival.

## Capped test of the idle-timeout hypothesis

UPDATE 26 Oct 2026: the operator set the voice idle timeout to 300 s (REAL) and approved ONLY the 300 s run (cap $0.15; the 5 s run is skipped because the last call's trace already gives the 1.10 s median queue at 5 s). The current procedure, key handling and stop rules are in docs/LATENCY_PAID_TESTS_REQUESTS.md; the text below is the original design.

Action: 6 real voice jobs of one fixed neutral 3-word phrase, 20 s apart, run twice: B with idle 5 s (today's setting), then A with idle 300 s (the operator changes it between the runs; B first so no settings change is needed before it).
Tool: `python tools/voice_queue_test.py --label idle_5s --jobs 6 --gap-s 20 --approved "<id>"` (it refuses to run without the approval id and never runs more than 6 jobs; the key comes from an environment variable, never printed; it prints numbers only).
Record per job: RunPod delay (queue) time, execution time, total, remainder, polls, audio seconds. Job 1 is reported but left out of the median (it may be a cold start).
Endpoints that must be warm: none for B (the point is to see the idle effect; leave the voice endpoint idle for at least 30 s before B so job 1 starts from a down worker); the voice endpoint only.
Prediction if the hypothesis holds: B median queue about 1000 ms, A median about 150 ms (the STT figure, 139 ms, is the reference). If both are about 1000 ms the queue is not a revival.
Cost (ESTIMATE, GPU price UNVERIFIED, assumed $0.0003 per GPU-second): B about 6 x (1 s revival + 2.4 s run + 5 s idle) = 50 s = $0.015 (worst case, if every job is a full 41 s cold start: about $0.09). A about 6 x 2.4 s + 5 x 20 s gaps + a 300 s tail = 415 s = $0.12 (job 1 possibly +41 s).
Proposed caps: B $0.10, A $0.15, total $0.25. The operator's billing page shows the real cost.
Needs from the operator: the APPROVED message with the cap; the idle timeout set to 300 s before A; the TTS key available as an environment variable of the process that runs the tool (or the operator runs the command).
