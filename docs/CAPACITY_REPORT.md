# Capacity report (synthetic, single dev host)

**These numbers describe one WSL2 laptop, a local single-node MongoDB and loopback FreeSWITCH legs. They are not a production capacity guarantee** and say nothing about carrier limits, real codecs/RTP, network latency, Atlas latency or TLS.

* Host: Windows 11 + WSL2 Ubuntu 24.04, 4 vCPU, 5.9 GB RAM; Python 3.12.3; FreeSWITCH 1.11.4-dev; MongoDB 7.0.14 standalone on the same machine (all components share the 4 vCPUs).
* Harness: `tests/load/run_load.py` (in-process ASGI client, so API latencies exclude network/TLS/proxy). Raw results + 1 s resource samples: `docs/load/*.json`.
* Latencies use the harness monotonic clock (the WSL wall clock stepped ~50 s during testing; see ENVIRONMENT_AUDIT.md).
* No PSTN calls, no paid minutes, no AI models.

| Profile | Command | Result |
|---|---|---|
| A. mock provider, control plane only | `--mode mock --calls 1000 --concurrency 30 --hold 1 --cps 100 --client-concurrency 40` | 1000/1000 completed, 0 errors, peak active **30 (never above the cap)**, 48.8 s wall (20.5 calls/s), synthetic ESL events injected |
| B. real FreeSWITCH, loopback legs | `--mode fs --calls 300 --concurrency 30 --hold 5 --cps 10 --client-concurrency 40` | 300/300 completed, 0 errors, peak active **30**, 59.9 s, **0 channels left** afterwards, 4500 ESL events, 0 dropped, 0 handler errors |
| C. real FreeSWITCH, long hold | `--mode fs --calls 30 --concurrency 30 --hold 180 --cps 5 --client-concurrency 30` | 30/30 held **180 s** concurrently then completed, 0 errors, 188 s wall, 0 channels left, 450 events, 0 dropped |

| Metric (seconds) | A mock | B fs | C fs long |
|---|---|---|---|
| API latency p50 / p95 / p99 | 0.129 / 0.454 / 0.739 | 0.041 / 0.709 / 0.817 | 0.627 / 0.754 / 0.780 |
| initiating→answered p50 / p95 / p99 (poll resolution 50 ms) | 0.081 / 0.139 / 0.216 | 0.335 / 0.383 / 0.471 | 0.326 / 0.376 / 0.377 |
| queue wait p50 / p95 / p99 / max | 0.25 / 0.99 / 6.2 / 13.5 | 0.89 / 6.2 / 31.6 / 45.0 | 3.5 / 6.5 / 6.7 / 6.7 |
| peak API-process CPU / RSS | 49 % of one core / 80 MB | 53 % / 77 MB | 39 % / 76 MB |
| peak FreeSWITCH CPU / RSS | ~1 % / 83 MB | 27 % / 103 MB | 18 % / 104 MB |

Notes
* Queue wait in B/A is driven by the harness offering 40 concurrent requests against a 30-call cap (the extra requests queue by design), not by controller slowness.
* "API latency" p95 ≈ 0.45–0.8 s under 40-way concurrent submission reflects admission-control queries (several MongoDB counts per request) on a shared 4-vCPU box; it is the first thing to optimise (cache counters) if needed.
* The 3-minute average call at 1,000 calls/day with 20–30 concurrent calls is **covered at the control-plane level for 30 concurrent / 180 s** (profile C). A full day (1000 × 3 min ≈ 50 h of talk time) was **not** run; profiles A/B compress hold time to measure throughput, not endurance.

## What this does and does not show
Shown: no lost/duplicated calls, per-tenant and global ceilings respected, FreeSWITCH channels fully released, zero dropped ESL events at ~75 events/s, stable memory at this scale.
Not shown: media/RTP load, transcoding, real carrier setup latency, provider CPS/concurrency limits (**unknown**), Atlas round-trip effects, multi-hour soak, recovery time under load (recovery was tested functionally, not timed).

## Findings from load testing (fixed)
1. Reconciliation with a zero grace period finalized an in-flight originate and orphaned its channel → now never finalizes `initiating`/uncertain calls before `RING_TIMEOUT_S + 10 s`, and hangs up channels belonging to terminal calls (regression tests in `test_freeswitch.py`).
2. Wall-clock steps produced 50 s "durations" in DB timestamps → durations are clamped at 0 with `clock_anomaly`, and the harness uses a monotonic clock. Production hosts need NTP.

## Reproduce
```bash
bash scripts/dev_mongo.sh
bash scripts/run.sh python tests/load/run_load.py --mode fs --calls 300 --concurrency 30 --hold 5 --cps 10 --client-concurrency 40 --out docs/load/fs_300x30_hold5.json
```
