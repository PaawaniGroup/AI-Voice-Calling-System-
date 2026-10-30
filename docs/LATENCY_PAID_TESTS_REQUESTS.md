# Paid tests (REQUESTs; each needs an "APPROVED:" message with the action and the cap)

Status on 26 Oct 2026: test 1 is APPROVED by the operator (300 s configuration only); tests 2 and 3 are still REQUESTs and have not run.
Prices: the operator's voice figure is $0.00019 per GPU-second (REAL, operator); an older assumption was $0.0003. Both are shown. Only Billing is authoritative.
The tools read the tenant's endpoint URLs from the LOCAL test Mongo (127.0.0.1, port 27117, database `mme_telephony`, collection `ai_config`, tenant `mme-test`; a read, no write) and the key from an environment variable. They never print a key, a transcript, a reply or a file name.

## 1. Voice queue test, 300 s idle configuration only (APPROVED by the operator, cap $0.15)

| item | value |
|---|---|
| action | 6 real voice jobs of one fixed neutral phrase, 20 s apart. The 5 s run is skipped: the last call's trace already gives a 1.10 s median queue measured at 5 s (MEASURED) |
| tool | `tools/voice_queue_test.py` (refuses without `--approved`; at most 6 jobs) |
| key | the key file `/mnt/c/mme_keys/runpod_tts.txt` read into `RUNPOD_TTS_KEY` for that one process only; never printed; no other folder is searched |
| endpoint state before job 1 | NO health gate (/health worker counts are not a warm/cold signal: docs/LATENCY_HEALTH_READINGS.md). The `jobs.completed` counter and the UTC time are recorded before job 1; job 1 is classified by its own queue (20 s or more = cold start, under 5 s = already warm) and excluded from the median |
| stop rules | the cost cap, the first HTTP 401 or 403, 3 consecutive errors |
| records | per job: RunPod job id, queue time (delayTime), execution time, wall time; then the median of jobs 2-6 with its range |
| cost (ESTIMATE) | 6 x 2.4 s + 5 x 20 s of gaps + the 300 s idle tail + a cold start of up to 41 s = about 460 s = **about $0.09 at $0.00019 per second, about $0.14 at $0.0003**; cap $0.15 |
| result file | written outside the repository; not committed until the operator says so |

## 2. STT-only /run versus /runsync (still a REQUEST, cap $0.03)

| item | value |
|---|---|
| action | the same recording, 10 jobs per mode, alternating /run (+ status polls) and /runsync, 5 s apart; the first job of each mode is left out of the medians |
| tool | `tools/stt_run_vs_runsync.py` (refuses without `--approved`; 2-10 jobs per mode; the clip must be a mono 16-bit WAV of 1-8 s) |
| command | `RUNPOD_STT_KEY=<speech key> python tools/stt_run_vs_runsync.py --clip <the operator's wav> --jobs-per-mode 10 --approved "<id>"` |
| environment variable | `RUNPOD_STT_KEY` (or the path of the speech key file) |
| other input | a recording of the operator's own voice (never copied into the repository, never printed) |
| endpoint that must be warm | the speech endpoint (idle 300 s): run it inside the warm window of a supervised call or right after one job, so only the 20 jobs are extra |
| cost (ESTIMATE) | 20 jobs x about 0.7 s = 14 s = about $0.003 at $0.00019 per second, $0.004 at $0.0003 (warm worker); a cold start plus the idle tail would add about $0.09-$0.14 and is NOT part of the cap |
| cap | $0.03 |

## 3. Brain test of the opening rule (still a REQUEST, cap $0.05)

`tools/check_brain_openers.py`; details, counts and cost in docs/LATENCY_OPENERS_CHECK.md. Brain endpoint warm (idle 300 s); `RUNPOD_BRAIN_KEY` (or the path of the Brain key file); 20 calls; **$0.004-$0.036 depending on the (unknown) Brain GPU price**; cap $0.05; replies only in one local file outside the repository.

## 5. Voice gap sweep: queue against the gap since the previous job (BUILT, NOT RUN; REQUEST, proposed cap $0.20)

Question: is the 1.2 s warm queue an effect of the gap between jobs (H1: workers restored after each job) or a fixed delay (H2)? See docs/LATENCY_WP4_QUEUE_WAIT.md.

| item | value |
|---|---|
| tool | `tools/voice_gap_sweep.py` (refuses without `--approved`; result file outside the repository; no health gate; at most 13 jobs) |
| plan | 1 priming job (excluded), then gaps of 0, 2, 5, 10, 20 and 40 s measured from the previous job's COMPLETION, twice: repetition 1 ascending, repetition 2 descending (so a drift in time does not look like a gap effect) |
| records | per job: the planned and the MEASURED gap, RunPod job id, queue (delayTime), execution, wall time, job class by queue, the `jobs.completed` counter read right after the job (in the background, so it does not lengthen the gap); then a table queue vs gap and the gap where the median queue first exceeds 0.6 s |
| key | the key file `/mnt/c/mme_keys/runpod_tts.txt` read into `RUNPOD_TTS_KEY` for that one process only; never printed |
| command | `RUNPOD_TTS_KEY=... python tools/voice_gap_sweep.py --result-file <outside the repo>/sweep.json --cap-usd 0.20 --approved "<id>"` |
| stop rules | the cost cap (projected with the worst case of a 300 s idle tail at the higher price), the first HTTP 401 or 403, 3 consecutive errors |
| endpoints that must be warm | none (the priming job pays any cold start and is excluded); the voice endpoint only |
| how to read it | flat 1.2 s from gap 0 = fixed delay (H2). A queue near 0.15 s at gap 0-2 s that rises to 1.2 s by 5-20 s = workers shut down and restored (H1) and the gap where it passes 0.6 s is the effective idle timeout. Two repetitions in opposite orders show whether time drift matters |
| duration | about 205 s of jobs and gaps (about 253 s if the priming job is a cold start) |

Cost (ESTIMATE; GPU price per second; only Billing is authoritative):

| case | worker time billed | at $0.00019 per second | at $0.0003 per second |
|---|---|---|---|
| effective idle timeout 300 s (the worker stays up the whole sweep), warm priming job | 205 s + 300 s idle tail = 505 s | $0.096 | $0.152 |
| the same, priming job is a cold start | 253 s + 300 s = 553 s | $0.105 | $0.166 |
| effective idle timeout 5 s (each job bills about 1.8 s run + 1.2 s restore + 5 s idle = 8 s; 13 jobs) | 104 s | $0.020 | $0.031 |
| the same, priming job is a cold start (+49 s) | 153 s | $0.029 | $0.046 |

Range $0.02 to $0.17; proposed cap $0.20.

## 6. Active workers = 1 for about 10 minutes, same 6-job test (DESIGN ONLY; the operator sets the console)

Question: does a worker that can never shut down remove the 1.2 s? (H1 says yes; H2 says no.)

Steps:
1. The operator sets the voice endpoint's Active workers to 1 in the console at time T0 and tells the assistant (nothing else is changed; idle timeout stays 300 s).
2. Wait 4 minutes (the Active worker needs a cold start of 41-52 s; no /health reading is used to decide that it is up).
3. Run the SAME test: `RUNPOD_TTS_KEY=... python tools/voice_queue_test.py --label active1 --jobs 6 --gap-s 20 --idle-tail-s 0 --cap-usd 0.05 --result-file <outside the repo>/active1.json --approved "<id>"` (idle tail 0: the worker's uptime is the operator's 10 minutes, counted below; the tool's cap covers only the extra cost of the jobs). About 2.5 minutes.
4. At T0 + 10 minutes the operator sets Active workers back to 0 (the operator does it himself).

Cost of the worker time (ESTIMATE; the active-worker price may be lower: no discount is documented, UNVERIFIED): 600 s = **$0.114 at $0.00019 per second, $0.180 at $0.0003**. After Active goes back to 0 the worker can stay up until the idle timeout (300 s) after the last job: add up to 300 s = $0.057 / $0.090, so at most **$0.171 / $0.270**. The 6 jobs add compute seconds that are already inside those 600 s. Only Billing is authoritative.

How to read the result (baseline: queue median 1.226 s, range 1.164-1.249 s at idle 300 s, n = 5):
- Job 1 should be `already_warm` (queue under 5 s). If it is a `cold_start` (20 s or more), the Active worker was not up yet or the setting did not apply: the test says nothing, repeat after waiting longer.
- Median queue of jobs 2-6 at or below 0.4 s (close to the 0.15 s of back-to-back jobs): H1 supported. A worker that never shuts down removes the queue. One always-on voice worker costs about $499 per month at $0.00019 per second and $788 at $0.0003 (UNVERIFIED rates); compare with business-hours-only scheduling.
- Median queue within 0.2 s of the baseline (1.0-1.4 s): H2 supported. Active workers does not help; the delay is a property of this endpoint's dispatch (image, GPU class, scaler type, region). Do not pay for Active workers for this purpose; look at the endpoint's settings and at a load-balancing endpoint instead.
- Between 0.4 s and 1.0 s: ambiguous; run the gap sweep (test 5) before deciding.
- Together with test 5: if the sweep shows the queue rising with the gap, Active = 1 should remove it; if the sweep is flat at 1.2 s from gap 0, Active = 1 should not.

## 4. Supervised call with all new flags (WP8)

The plan, the endpoints that must be warm and the idle timeouts are in the WP8 report; nothing is switched on.
