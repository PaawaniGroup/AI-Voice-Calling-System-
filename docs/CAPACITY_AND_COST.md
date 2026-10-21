# Capacity and cost: method

No prices are stated here: none were provided and none are invented. Fill the "input" cells from your Plivo/Atlas/GPU invoices and rate cards.

## What has been measured (see `CAPACITY_REPORT.md`)

Control plane only, single dev host, mock provider and real FreeSWITCH loopback: 30 concurrent calls held 180 s with 0 errors; throughput ~20 calls/s with a mock provider. This says nothing about AI capacity or carrier behaviour.

## What has NOT been measured (needs real endpoints)

* Concurrent AI calls per GPU / per model server: run `scripts/ai_bench.py --levels 1,2,4,8,16` against the real endpoints; the per-stage concurrency sweep shows where latency or errors start rising. Capacity = highest level where p95 stays inside the SLO, with headroom (use 70 %).
* Host CPU/RAM per AI call: sample `psutil` / `top` on the app host during step 12; the app does mu-law, resampling and RMS per frame (`audioop`), cheap but not free.
* Plivo concurrency/CPS limits: from the account (unknown today); set `PROVIDER_MAX_*` and `PLIVO_STREAM_MAX_CONCURRENT` (default 10) accordingly.

## Cost model (formula)

Per AI call-minute = telephony per-minute rate (Plivo, in/out differ) + stream/ASR add-ons if any + GPU cost per minute / concurrent calls per GPU at the chosen utilisation + recording storage per minute + share of fixed costs (host, Atlas, monitoring).

Monthly = calls/day x avg minutes x 30 x (per-minute cost) + fixed costs.

| Input | Value | Source |
|---|---|---|
| Plivo inbound / outbound rate per minute | (operator) | Plivo rate card |
| GPU $/hour and concurrent AI calls per GPU | (operator) / (measured by `ai_bench.py` + step 12) | provider invoice / measurement |
| Host, Atlas, storage, monitoring monthly | (operator) | invoices |
| Expected calls/day, average minutes | (operator) | business plan (earlier discussion mentioned ~1,000 calls/day, 3 min average; confirm) |

Until the "measured" cells are filled, any cost or capacity figure is an estimate and must be labelled so.
