# Evidence: intermittent failures in the real-FreeSWITCH AI media tests

Date: 2026-10-22. Start commit `8ab9b5f`. Host: WSL2 dev machine, local FreeSWITCH 1.11.4-dev, real `mod_audio_stream`, loopback calls, tone caller, MOCK model servers. No carrier, no paid API, no tunnel, no restart, no dialplan change.
Raw artefacts: `docs/evidence/fs_media_diag_2026-10-22/` (the one preserved failure bundle with timeline and FreeSWITCH log slice, summaries). Tools: `tests/diag/fs_media_trace.py`, `tests/diag/fs_only_probe.py`.

## Before the fix

`tests/integration/test_ai_freeswitch.py` failed in 6 of 50 repeated runs of the file (about 12 %), with changing symptoms: session stuck `starting`, no `play` event, `caller heard 0.00 s` (also in the barge-in test).

## Findings

### Class 1: test helpers depended on the wall clock (confirmed, fixed)

* Measured (`clockwatch`, 90 s): this host's `time.time()` steps by **+/-51.69 s relative to `time.monotonic()` every ~5 s** (about 36 steps in 90 s). `timedatectl`: "System clock synchronized: no".
* The helpers used `time.time()` for the `wait_ai` deadline and `os.path.getmtime(f) >= time.time() - 1` to pick the caller's recording. A step between two readings makes the deadline fire early and excludes the real recording.
* Causal evidence: the harness ran the **legacy logic as a passive shadow** next to the real, monotonic logic over 40 iterations. Whenever the media path had worked (outcome `ok`, caller-side audio verified), the legacy logic would have failed in exactly the iterations where the clock offset changed by -51.70 s between the two readings: iterations 6 and 22 (legacy recording filter missed the recording: mtime-minus-reference = -45 s), iteration 15 (legacy `wait_ai(stopped)` gave up in the stepped state). A control with no app (below) shows the same +/-51 s steps do **not** disturb FreeSWITCH itself.
* Fix: monotonic deadline in `wait_ai`; recording chosen by "not present before the call" (no clock); failure messages carry `wall_clock_step_since_import`.
* Regression tests `tests/unit/test_clock_robustness.py` (3, deterministic, no FreeSWITCH). **Mutation-checked**: with the legacy logic restored, 2 of 3 fail; with the fix all pass.

### Class 2: a FreeSWITCH media-timing stall (observed once, cause NOT proven)

* Phase-1 iteration 35 (`no_turn`): the 1.2 s caller tone played from PLAYBACK_START to PLAYBACK_STOP in **4.79 s on FreeSWITCH's own clock** (healthy iterations: 1.19 s), inbound frames arrived in sparse bursts (about 37 frames in 4 s, gaps to 1.3 s), so no 700 ms of trailing silence ever completed the utterance and no turn ran. The app received what FreeSWITCH delivered; the stall is inside FreeSWITCH's media timing. In the same iteration the wall-minus-monotonic offset **jumped by 13,522 s**, and FreeSWITCH logged scheduler lateness warnings elsewhere ("Task was executed late by 3 seconds"; 238 such lines in the log's history) and its idle-CPU figure dipped (80 % to 52-58 %).
* Control (`fs_only_probe.py`, 100 runs, no app, no module, no Mongo): tone playback 1.18-1.20 s on FreeSWITCH's clock in **100/100**, max event-loop lag 11 ms, even though the +/-51 s flicker occurred in 14 of them. So the small flicker is harmless to FreeSWITCH; a large clock step or host stall coinciding with the call is the **hypothesis** for iteration 35, supported by one observation only.
* Not proven: the control produced no stall, so the mechanism was not reproduced on demand.

## After the fix

The five tests, repeated: **100 file-runs = 500 test executions, 0 failures** (batch of 40: 200/200; batch of 60: 300/300). Failure rate before about 12 % per file-run; after 0/100 (95 % upper bound by the rule of three: about 3 % per file-run). The one class-2 event seen in the 40-iteration harness phase (2.5 %) did not recur in 100 file-runs, which is consistent with it being rare, not proof it is gone.

## Caller-side audio: independently verified?

Yes for the passing path: in all 39 good harness iterations the recording of what the caller hears (RECORD_READ_ONLY) contained 0.26-0.30 s of audible signal; the dominant frequency (zero-crossings of the first audible segment) was 441-460 Hz in 37 of 39 (the 440 Hz reply tone, not the caller's own 300 Hz tone) and 725 Hz and 500 Hz in the other two (zero-crossing noise on a 0.3 s segment; the amplitude and duration still match the reply). Measured from the recording, not from a controller event. This proves the loopback media path moves audio, not that real speech, real models or real phone lines work.

## Remaining gaps (see PRODUCTION_GAP_REPORT.md)

* Class 2 stall: unexplained, rare, not reproduced; a production host with a disciplined clock may not show it, but this was not tested.
* The host's wall clock is unstable. In-process logic that uses `time.time()` (JWT/ticket expiry, `/health/ready` event freshness, sweeper age checks against stored timestamps) would be sensitive to clock steps on such a host; production hosts need NTP/chrony with no large steps.
* Everything above used a tone and mock models on loopback.
