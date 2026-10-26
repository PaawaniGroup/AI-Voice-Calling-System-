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
| endpoint state before job 1 | the voice endpoint idle for at least 6 minutes (idle timeout 300 s) so that job 1 is a cold start; job 1 is excluded from the median |
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

## 4. Supervised call with all new flags (WP8)

The plan, the endpoints that must be warm and the idle timeouts are in the WP8 report; nothing is switched on.
