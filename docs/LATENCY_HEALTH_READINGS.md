# RunPod /health worker counts are NOT a warm/cold signal (UNVERIFIED worker slots)

Operator decision, 27 Oct 2026. Evidence from our own reads: the counts add up to Max workers (2) on every endpoint read (the Brain once showed idle 1 / running 1); jobs waited 41-52 s (queue time) while /health said idle 2 / ready 2; the operator's balance did not move like two billed workers; two voice workers stayed "idle" for more than 50 minutes with no job queued.

## Interpretation recorded on 27 Oct 2026 (operator; MEASURED facts, HYPOTHESES marked)

- MEASURED: job 1 of the 27 Oct test was a cold start (queue 48.9 s) while /health had shown idle 2 / ready 2. This confirms that /health counts are worker slots and not a warm signal.
- MEASURED: the warm voice queue is a steady 1.2 s at idle 300 s (1.16-1.25 s, n=5) and 1.10 s at idle 5 s; back-to-back voice jobs showed about 0.15 s (Phase 0b); the speech endpoint (idle 300 s) shows about 0.14 s between turns.
- HYPOTHESIS (UNVERIFIED): the voice workers are shut down and restored (FlashBoot) after each job, i.e. the effective idle timeout is still short. The alternative (also UNVERIFIED) is a fixed dispatch delay specific to this endpoint. The gap sweep and the Active-workers test in docs/LATENCY_PAID_TESTS_REQUESTS.md (tests 5 and 6) are designed to separate them; neither has run.

## Rule

- No script, wake logic, readiness check or budget reserve may use `workers.idle`, `ready`, `running` or `initializing` to decide that an endpoint is warm or cold, to wait for "zero workers", or to lower a cost reserve.
- The ONLY signals are COMPLETED JOBS (real jobs or warm-up replies) and their delay times (`delayTime`, the queue). Classification of one job by its own queue: 20 s or more = cold start; under 5 s = already warm; in between = in between.
- The `jobs.completed` counter of /health is a completed-jobs signal and may be recorded (before and after a test); the worker counts next to it are information only.
- Changed on 27 Oct 2026: `tools/voice_queue_test.py` (no gate; job-1 class by queue), `tools/phase0b_tts_remeasure.py` (the reserve is never lowered by /health; the old-style probe is sent only with `--allow-cold`), `tools/phase0_streaming_spike.py` and `tools/measure_devanagari_app_path.py` (a warm-up job is sent unless `--no-warm`; it is classified by its own delay; the counts are recorded as information only). The assistant's own wait-for-zero helper scripts (outside the repository) are no longer used.
- Readiness design: any wake or readiness check is built from a warm-up job reply (for example `{"warmup": true}`, which the workers answer without computing) and its delay time, never from worker counts. (The other worktree's design notes were not edited by this session.)

## Every /health reading in the assistant's reports (voice endpoint; the only endpoint read with a key)

The STT and Brain endpoints were only read WITHOUT a key (HTTP 401, no counts). Max workers = 2 (REAL, console).

| # | when (26 Oct 2026) | endpoint | idle | ready | running | init | throttled | unhealthy | Max | idle+ready+running+init | idle+running+init | completed / failed / inProgress / inQueue |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | pre-flight before the first wait script | voice | 2 | 2 | 0 | 0 | 0 | 0 | 2 | 4 | 2 | 99 / 0 / 0 / 0 |
| 2 | after the second wait script (about 55 minutes later) | voice | 2 | 2 | 0 | 0 | 0 | 0 | 2 | 4 | 2 | 99 / 0 / 0 / 0 |

Polls inside the wait scripts printed only a derived number, not the raw counts, so their raw values are UNKNOWN: first script 6 polls with "alive=4" (idle+ready+running+init+throttled+unhealthy, which double counts "ready"), second script 44 polls with "alive=2" (ready counted once), and "alive=2" before and after the warm test. Consistent with readings 1 and 2.

Does idle+ready+running+init always equal Max? **No, not literally**: both full readings give 4 (Max is 2), because `ready` repeats the idle (and running) workers here. **Without `ready`, idle+running+init = 2 = Max in both full readings** (and `ready` = idle+running). Two readings are too few to call it a law; this is why the counts are treated as slots.

## Jobs that tell the real story (MEASURED, voice, 26 Oct 2026, idle timeout 300 s, workers shown "idle 2 / ready 2")

| job | queue (delayTime) | execution | wall |
|---|---|---|---|
| 1 | no answer within the 60 s client timeout (cancelled) | - | 60,005 ms |
| 2 | 45,207 ms | 1,621 ms | 47,503 ms |
| 3 | 5,051 ms | 1,650 ms | 7,355 ms |
| 4 | 1,201 ms | 1,741 ms | 4,667 ms |
