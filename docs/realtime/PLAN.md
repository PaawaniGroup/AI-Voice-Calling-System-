# Real-time latency program: plan

Goal (operator, 27 Oct 2026): from the MEASURED 7.51 s median (silence 0.70 + STT 1.49 + Brain 0.74 + voice 4.38 + path to the ear about 0.2; first turn 36 s cold) to
**real answer <= 2.2 s median and <= 3.0 s p90** (for the scenario fixed in GATES.md) and **first sound <= 1.2 s** (with a cached acknowledgement; BLOCKED until that audio and code exist).
Under 1 s to the real answer is not promised. Metric: last voiced frame -> first audio frame sent to Plivo, from REAL traces only.
Branch `realtime` (worktree `MME_wt_realtime`) from `integration` at 314b168. No push, no merge, no stash, no reset. Every new behaviour: per-tenant flag, default OFF, in the config allowlist with a test that it survives `AiService.get_config`; the legacy path (/run + poll) stays as the fallback.

## Stages

| stage | content | paid steps | gate | status |
|---|---|---|---|---|
| 0 | supervised call on `integration` (S6-S12 of the fast path) and its REAL medians | wake, queue test (cap $0.45 mine) | - | waiting for the operator ("active set") |
| 1 | measurement harness: stage timers, SIMULATED and REAL modes, comparison table, CI-style gate | none until approved | regression gate | **offline part built** (tools/realtime/, 21 tests) |
| 2 | transport: queue (default) vs persistent connection (flag `rt_transport`), server files, host decision | G1 Pod round trip, cap $0.10 | G1: p50 <= 40 ms | not started |
| 3 | streaming models: LLM, voice (chunked synthesis), STT (rolling decode), end of turn | caps $0.50 / $1.00 / $0.20 | one gate each | not started |
| 4 | orchestration, topology, capacity, reliability | ack audio $0.05, concurrency $0.30 | - | not started |
| 5 | integration and proof: synthetic harness on all gates, ONE supervised call, go/no-go memo | the call | A-median, A-p90, FS | not started |

No paid step of Stages 1-4 runs before the Stage 0 report.

## Stage 1 deliverables (built offline)
- `tools/realtime/stages.py`: stage recorder (one clock for SIMULATED and REAL), `derive_stages`, baseline (7.51 s) and budget (1.30 s) constants, percentiles.
- `tools/realtime/harness.py`: SIMULATED mode (the real segmenter, StreamingTurn, playout and ramp on a virtual clock; mock models with timing models parameterised from the MEASURED medians) and REAL mode (`--real`: refuses without `--approved`, recordings outside every repository, cost cap, stop at 401/403 and after 3 errors; NOT RUN).
- `tools/realtime/table.py`: every run against the 7.51 s baseline and the budget, each column tagged.
- `tools/realtime/gate.py` + `docs/realtime/gates.json`: thresholds, scenario and sample-size matching, a SIMULATED run never passes a MEASURED gate, regression check against a previous run.
- Evidence files: numbers and a few enumerations only (tag, plan, stop reason); a test checks that no text-like key or free string can enter.

## What exists and is reused (names verified in the worktree)
`tools/phase0_streaming_spike.py`, `phase0b_tts_remeasure.py`, `measure_devanagari_app_path.py`, `spike_clauses.py`; `tools/phase3/chunked_tts_spike.py` and `launch_pod.py`; `tools/rolling_stt_spike.py`, `eot_replay.py`, `telephone_chain.py`, `recording_intake_check.py`, `barge_in_eval.py`; `docs/RECORDING_PROTOCOL.md`; `app/ai/streaming/*`; `app/ai/turn_trace.py`; `tools/voice_queue_test.py`, `voice_gap_sweep.py`, `sim_ramp_gap.py`, `health_timing.py`; `scripts/preflight_wake.py`; `docs/PHASE4_PERSISTENT_CONNECTIONS.md`.

## Evidence the plan stands on (MEASURED)
- Voice job wall time minus execution outside the worker: 2.09 s median (jobs 2-6 of 27 Oct: 3856 - 1771 ms), above the 0.8 s the Phase 4 document asks for (its rule 1). The gateway costs about 0.22 s per request, two requests per queue-based job (docs/LATENCY_NETWORK.md).
- Warm voice queue: a steady 1.2 s (idle 5 s: 1.10 s; back-to-back 0.15 s; speech endpoint 0.14 s). Cold start 48.9 s. /health worker counts are slots, never readiness.
- Fed these medians, the harness gives 7.70 s to first audio against the MEASURED 7.31 s (7.51 s minus the path to the ear): +5.3 % (SIMULATED calibration; the test allows 10 %).

## Open inputs from the operator
Recordings (the operator's 12 caller lines, a folder outside every repository; none exist yet); balance (above or below $1: not stated, treated as UNKNOWN, no paid step runs); Stage 0 (not decided); the Pod account key later as a REQUEST with minimal permissions.
