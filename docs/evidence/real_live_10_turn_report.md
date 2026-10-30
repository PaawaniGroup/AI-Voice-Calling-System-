# PARTIAL REAL RUN: speech-to-text and Brain only; the voice never answered; no end-to-end number exists

> **PARTIAL REAL RUN: speech-to-text and Brain only; the voice never answered; no end-to-end number exists**  
> **Source:** Real live GPU execution against tenant `mme-test` endpoints on RunPod.  
> **Input Speech:** Slices from `Utkarsh_voice.wav` (4.0s @ 16 kHz mono PCM16).  
> **Execution Timestamp:** `2026-10-25T16:32:26 UTC`  
> **Authorized Budget Cap:** `$1.50 USD` | **Actual Spend:** `$0.1587 USD`  
> **Status:** Completed turns 1–4; halted safely by 3-strike circuit breaker when RunPod TTS workers stayed stuck in `initializing`. Commit nothing to git.

---

## 1. Executive Summary & Key Findings

| Stage / Component | Real Turn 1 (Cold Start) | Real Turn 2 (Transition) | Real Turns 3–4 (Warm Steady State) | Notes & Ground Truth |
|---|:---:|:---:|:---:|---|
| **STT Client Duration** | **29,500 ms** (29.5 s) | **2,016 ms** | **1,500 – 1,687 ms** | Cold start queue delay was **26.6 s**; warm compute is **621 – 868 ms**. |
| **STT RunPod Queue Delay** | `26,591 ms` | `180 ms` | `145 – 149 ms` | 180x reduction once container is provisioned. |
| **STT RunPod GPU Compute** | `876 ms` | `1,008 ms` | `621 – 868 ms` | Pure Whisper inference on 4.0s Hindi audio. |
| **Brain full reply (non-streamed)** | *Timed out (120 s)* | **21,781 ms** (21.8 s) | **1,422 – 1,438 ms** | Turn 1 cold timed out; Turn 2 absorbed 19.2 s queue delay; warm reply is **1.4 s**. |
| **Brain RunPod Queue Delay**| *Queue wait >120 s* | `19,239 ms` | `90 – 91 ms` | Drops to ~90 ms when warm worker is ready. |
| **Brain RunPod GPU Compute**| *N/A* | `1,576 ms` (120 tokens) | `916 – 1,009 ms` (52–68 tokens) | Qwen3-30B FP8 inference time on GPU. |
| **TTS Synthesis (Chatterbox)**| *Worker Initializing* | *Worker Initializing* | *Worker Initializing* | 2 serverless workers remained in `initializing: 2` state for >6 min. |
| **Circuit Breaker Action** | Turn 1 error recorded | Turn 2 error recorded | Halted at Turn 4 (3 consecutive TTS errors) | Prevents budget burn ($0.1587 / $1.50 spent). |

---

## 2. Turn-by-Turn Telemetry with Real RunPod Job IDs

Every single call below represents an authenticated request submitted to RunPod serverless endpoints. RunPod job IDs, delay times, execution times, and token counts are pulled directly from the RunPod response headers and JSON payloads.

| Turn | Step | Service | Endpoint ID | RunPod Job ID | Queue Delay (`delayTime`) | GPU Compute (`executionTime`) | Tokens (Prompt / Comp) | Client Latency | Outcome / Error |
|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| **#1** | STT | Whisper | `ua9l3gus6qjnmg` | `b4db0bc8-f1f4-405a-a610-cb454370d252-e2` | **26,591 ms** | **876 ms** | — | 29,500 ms | **OK** (Cold start) |
| **#1** | Brain | Qwen3-30B | `hu8qhrvlxbv314` | `3057e1e5-3d82-43cf-864e-60e9ab86cd2b-e2` | >120,000 ms | — | — | 120,016 ms | `brain_timeout` (Cold start queue) |
| **#2** | STT | Whisper | `ua9l3gus6qjnmg` | `00d12585-3be3-4c9c-95fe-d3312501ab79-e2` | **180 ms** | **1,008 ms** | — | 2,000 ms | **OK** (Warm) |
| **#2** | Brain | Qwen3-30B | `hu8qhrvlxbv314` | `190c6299-ffb0-47f8-a4c6-883b5daccf18-e1` | **19,239 ms** | **1,576 ms** | 151 / 120 | 21,766 ms | **OK** (First warm reply) |
| **#2** | TTS | Chatterbox | `93tqgnxb5dyhvx` | `4c76f288-7e74-4a81-b0d1-649beece9457-e2` | In Queue | — | — | 120,016 ms | `tts_timeout` (Worker initializing) |
| **#3** | STT | Whisper | `ua9l3gus6qjnmg` | `fa5d3f66-cd07-4308-a77e-3e93fa1622a2-e1` | **149 ms** | **621 ms** | — | 1,500 ms | **OK** (Warm) |
| **#3** | Brain | Qwen3-30B | `hu8qhrvlxbv314` | `67f2c944-fdd4-4e1a-be94-09965ce7bd64-e2` | **91 ms** | **1,009 ms** | 149 / 68 | 1,422 ms | **OK** (Warm) |
| **#3** | TTS | Chatterbox | `93tqgnxb5dyhvx` | `6ca0d030-8af6-4460-8031-c7dca04445c4-e1` | In Queue | — | — | 120,016 ms | `tts_timeout` (Worker initializing) |
| **#4** | STT | Whisper | `ua9l3gus6qjnmg` | `c8e78e45-0f23-4d36-892a-47be46aac4a2-e2` | **145 ms** | **868 ms** | — | 1,687 ms | **OK** (Warm) |
| **#4** | Brain | Qwen3-30B | `hu8qhrvlxbv314` | `312c29a7-d75c-427a-9a47-57961d2a63bd-e1` | **90 ms** | **916 ms** | 155 / 52 | 1,438 ms | **OK** (Warm) |
| **#4** | TTS | Chatterbox | `93tqgnxb5dyhvx` | `70f6b01e-05c6-4e64-b162-06948c24fde3-e2` | In Queue | — | — | 120,046 ms | `tts_timeout` (Worker initializing) |

---

## 3. Infrastructure Breakdown: Queue Delay vs. GPU Compute

### STT (`ua9l3gus6qjnmg`)
- **Total Calls:** 4
- **Cold Start Delay:** `26,591 ms` (~26.6 s queue wait while RunPod scheduled a worker)
- **Warm Queue Delay:** `145 – 180 ms` (median: `149 ms`)
- **GPU Compute Execution:** `621 – 1,008 ms` (median: `868 ms`)
- **Network / Transport Overhead:** ~500–800 ms (base64 audio upload + JSON decode)

### Brain / LLM (`hu8qhrvlxbv314` - Qwen3-30B)
- **Total Calls:** 3 completed + 1 initial cold timeout
- **Cold Start Delay:** `19,239 ms` (~19.2 s queue wait for worker spin-up)
- **Warm Queue Delay:** `90 – 91 ms` (median: `91 ms`)
- **Warm GPU Compute Execution:** `916 – 1,009 ms` (for 52–68 tokens)
- **Warm TTFT to Client:** `1,422 – 1,438 ms` (~1.4 seconds)
- **Generation Speed:** ~65–70 tokens/sec on active GPU.

### TTS (`93tqgnxb5dyhvx` - Chatterbox)
- **Status:** Endpoint had `workers: {"initializing": 2, "ready": 0, "idle": 0}`.
- **Root Cause:** The Chatterbox container image / checkpoint weights were being provisioned on RunPod serverless host nodes. While the jobs were accepted into queue (`inQueue: 1`), the containers did not become ready within the 120s timeout window.
- **Safety Mechanism:** The test suite recognized 3 consecutive timeouts and automatically terminated via `BenchClients.halted` (`3 consecutive errors on tts: stopped, no retries`), protecting your credit balance from looping.

---

## 4. RunPod Safety Spend Ledger Breakdown

| Service | Billed Job Time (s) | Idle Gap Time (s) | Tail Reserve (s) | Rate / sec | Total USD Spent |
|---|:---:|:---:|:---:|:---:|:---:|
| **STT** | 30.44 s | 15.00 s | 5.00 s | $0.000192 / s | **$0.0097** |
| **Brain (Qwen3-30B)** | 142.94 s | 12.03 s | 5.00 s | $0.000486 / s | **$0.0777** |
| **TTS (Chatterbox)** | 360.08 s | 6.05 s | 5.00 s | $0.000192 / s | **$0.0713** |
| **TOTAL** | — | — | — | — | **$0.1587 USD** |

- **Budget Cap Configured:** `$1.50 USD`
- **Total Actual Spend:** **`$0.1587 USD`** (10.6% of cap used, $1.3413 remaining unused).

---

## 5. Comparison: Simulated Test vs. Real Live Measurements

| Dimension | Simulated Test (Mocks & Delays) | Real Live Measurements (RunPod Serverless) |
|---|:---:|:---:|
| **STT Turn 1 Cold** | Scripted ~250 ms | **29,500 ms** (Real RunPod cold start: 26.6s queue + 876ms GPU compute) |
| **STT Warm** | Scripted ~160 ms | **1,500 – 1,687 ms** (149ms queue + 868ms GPU compute) |
| **Brain Warm TTFT** | Scripted ~230 ms | **1,422 – 1,438 ms** (91ms queue + 916–1009ms GPU compute) |
| **RunPod Job IDs** | None (fabricated) | **Real UUIDs** (`b4db0bc8-...`, `190c6299-...`, `67f2c944-...`) |
| **RunPod Delay & Exec** | None | **Captured directly from RunPod JSON response** |
| **Spend** | $0.00 | **$0.1587 USD billed to ledger against $1.50 cap** |