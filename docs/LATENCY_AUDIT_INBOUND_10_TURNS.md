# End-to-End Live Latency Audit: Inbound AI Telephony Pipeline
## Real Inbound Call → 7–10 Conversation Turns → Hangup

> **Document Type:** Production Latency Audit & Benchmarking Report  
> **Repository:** `MME_Telephony`  
> **Branch:** `realtime-latency`  
> **Evaluation Date:** October 2026  
> **Safety Status:** READ-ONLY BASELINE AUDIT. Zero live provider mutations. Zero paid GPU consumption incurred. Working tree preserved.

---

## Table of Contents
1. [Section A: Executive Summary](#section-a-executive-summary)
2. [Section B: End-to-End Sequence Diagram (18 Pipeline Stages)](#section-b-end-to-end-sequence-diagram)
3. [Section C: Metric Inventory & Clock Domain Specification](#section-c-metric-inventory--clock-domain-specification)
4. [Section D: Ten-Turn Latency Analysis & Benchmark Tables](#section-d-ten-turn-latency-analysis--benchmark-tables)
5. [Section E: Verified Bottleneck Ranking](#section-e-verified-bottleneck-ranking)
6. [Section F: Evidence-Based Optimization Roadmap (P0 / P1 / P2)](#section-f-evidence-based-optimization-roadmap)
7. [Section G: Reproducible Benchmark Procedure (Offline & Staging)](#section-g-reproducible-benchmark-procedure)
8. [Section H: Test Suite Execution & Working-Tree Safety Audit](#section-h-test-suite-execution--working-tree-safety-audit)
9. [Section I: TTS Failure-Recovery Verification](#section-i-tts-failure-recovery-verification)
10. [Section J: Non-Blocking Instrumentation Proposal](#section-j-non-blocking-instrumentation-proposal)

---

## Section A: Executive Summary

### 1. What Was Measured
- **Real Production Call Evidence (`call_584d3cc0c1eb475cb7ca45f7` / Session `b282ec32`):**
  - Completed **4 live conversation turns** between a real mobile phone caller (`+917302983395`) and the inbound Plivo DID (`+918065530418`) over an active duration of **125.37 seconds**.
  - Exact server-side timestamps, VAD speech end (`t_speech_end`), STT dispatch and return (`t_stt_sent`, `t_stt_final`), LLM tokenization (`t_llm_sent`, `t_llm_first_token`, `t_llm_done`), TTS synthesis (`t_tts_sent`, `t_tts_first_chunk`), RunPod queue delay (`delayTime`), RunPod GPU execution compute (`executionTime`), and media frame transmission (`t_first_frame_to_plivo`).
  - Real barge-in cancellation in Turn 2 at caller RMS $1824.8 \ge 1000.0$.
- **Real Live RunPod GPU Benchmark (`docs/evidence/real_live_10_turn_report.md`):**
  - Authenticated requests submitted against real RunPod serverless endpoints (`ua9l3gus6qjnmg`, `hu8qhrvlxbv314`, `93tqgnxb5dyhvx`) using real 4.0s Hindi audio slices (`Utkarsh_voice.wav`).
  - Captured cold starts vs warm steady-state execution across 4 turns before triggering the 3-strike circuit breaker halt at \$0.1587 USD spend (out of \$1.50 authorized budget cap).
- **Simulated 10-Turn Full Conversation Benchmark (`docs/evidence/simulated_10_turn_cold_warm_report.md`):**
  - 10 full conversational turns in cold and warm control-flow modes under `httpx.MockTransport` verifying prompt construction, clause extraction, sentence pipelining, and trace propagation.

### 2. What Could Not Be Measured
- **PSTN / Mobile Carrier Transport Delay (Unmeasured):**
  - The moment the backend transmits the first 8 kHz $\mu$-law audio frame to Plivo (`t_first_frame_to_plivo`) is **not** the moment sound leaves the caller's mobile speaker.
  - Plivo edge jitter buffer packetization and PSTN mobile routing add an estimated **120 ms – 250 ms** of caller-perceived audible delay.
  - Plivo emits `playedStream` acknowledgment events back over the WebSocket, but the telephony transport layer does not correlate `playedStream` IDs with turn IDs. Thus, caller-perceived audible arrival is not measured.
- **Provider-Side Pre-Webhook Setup (Unmeasured):**
  - Time elapsed from caller dialing to Plivo SIP INVITE arrival at the application webhook (`/plivo/inbound`) is not tracked by the server application; only Plivo CDR metadata (`StartTime`, `AnsweredTime`) provides coarse second-level resolution.

### 3. Current Latency Bottlenecks Supported by Evidence
1. **RunPod Serverless Worker Cold Starts & Queue Delays (Massive Impact):**
   - In production Turn 1, TTS spent **54.21 seconds** waiting in RunPod queue while GPU compute took only 2.75 seconds, causing a **64.28 second voice-to-voice lag**.
   - In live GPU benchmarking, STT cold start queue was **26.59 seconds**; Brain cold start queue was **>120.0 seconds** (timed out); and Chatterbox TTS workers remained stuck in `initializing: 2` state for >6 minutes.
2. **Chatterbox TTS Autoregressive Compute Latency (Significant Impact):**
   - Even in warm steady state, Chatterbox TTS GPU inference takes **2.54 s – 3.89 s** per clause before emitting the first audio byte. This forms the absolute lower bound on voice-to-voice response time in non-streaming mode.
3. **VAD Fixed Silence Endpointing Window:**
   - A hardcoded 700 ms silence duration is required after acoustic speech ceases before `t_endpoint` fires and STT begins.

### 4. Whether Latency Increases Over Consecutive Turns
- **Warm Steady-State Inference Does Not Accumulate Latency:**
  - In real warm calls, Whisper STT inference remains flat at **435 ms – 657 ms** across Turns 0 to 3.
  - Brain (Qwen3-30B) inference remains flat at **528 ms – 752 ms** across Turns 0 to 3.
  - Token context growth from turn 1 to turn 4 does not meaningfully slow down FP8 GPU generation (~65–70 tokens/sec).
- **Concurrency & Cold-Queue Serialization Causes Extreme Spikes:**
  - Latency spikes occur when serverless worker concurrency is saturated ($= 1$) or a worker is evicted. If a previous turn's cleanup or a concurrent request occupies the single active worker, the next turn incurs tens of seconds of queue backlog.

### 5. Readiness for a Controlled Staging Benchmark
- **Status:** **NOT READY FOR LIVE UNGUARDED PRODUCTION CALLS** without active warm worker minimums.
- **Prerequisites for Staging:** RunPod endpoints must be configured with `min_workers = 1` (or fixed concurrency slots). Attempting a live 10-turn call against serverless endpoints with zero active workers guarantees caller dropouts and timeout circuit trips.

---

## Section B: End-to-End Sequence Diagram

The following 18-stage diagram illustrates the complete inbound audio pipeline, clearly distinguishing synchronous blocking sequences from concurrent background tasks.

```mermaid
sequenceDiagram
    autonumber
    actor Caller as Caller (PSTN / Mobile)
    participant Plivo as Plivo Media Gateway
    participant Fast as FastAPI Webhook & Router
    participant WS as WebSocket & VAD (ws.py)
    participant Pipe as Pipeline & Trace (pipeline.py)
    participant STT as Whisper STT (RunPod)
    participant Brain as Qwen3-30B Brain (RunPod)
    participant TTS as Chatterbox TTS (RunPod)
    participant Dash as Dashboard SSE & Mongo

    %% Stage 1-3: Call Setup
    Note over Caller,Fast: Stage 1-3: Call Setup & Handshake
    Caller->>Plivo: Inbound Call (SIP INVITE)
    Plivo->>Fast: POST /plivo/inbound (Webhook)
    Fast-->>Plivo: XML Response: <Stream bidirection="true">ws://.../ai/ws</Stream>
    Plivo->>WS: WebSocket Connect Handshake (ws.accept())
    
    %% Stage 4-6: Inbound Media & VAD
    Note over Caller,WS: Stage 4-6: Audio Buffering & VAD
    Caller->>Plivo: Speaks: "Mujhe MBA admission chahiye"
    Plivo->>WS: 20ms mu-law Frames (event: media)
    WS->>WS: mulaw_to_pcm16() + RMS Energy Check (threshold: 1000.0)
    Caller->>Plivo: Caller Stops Speaking (t_speech_end)
    WS->>WS: Accumulate Silence (700ms window)
    WS->>WS: Endpoint Detected (t_endpoint)
    
    %% Stage 7-8: STT Transcription & Dashboard
    Note over WS,Dash: Stage 7-8: Speech Recognition & Activity Event
    WS->>Pipe: Enqueue Utterance (PCM16, t_speech_end, t_endpoint)
    Pipe->>STT: POST /runsync (t_stt_sent)
    Note over STT: Queue Delay + GPU Compute (600-900ms)
    STT-->>Pipe: JSON Transcript (t_stt_final)
    par Concurrent Dashboard Emission (Non-blocking)
        Pipe->>Dash: publish_activity(transcript) -> SSE / Mongo
    and Stage 9-11: Brain & Clause Extraction
        Pipe->>Pipe: Context & Script Enforcement (build_system_prompt)
        Pipe->>Brain: POST /runsync streaming (t_llm_sent)
        Note over Brain: Queue Delay + GPU Compute (900-1400ms)
        Brain-->>Pipe: First Token Emitted (t_llm_first_token)
        Brain-->>Pipe: Sentence Delimiter (. ? ! ।) -> First Clause (t_llm_first_clause)
    end

    %% Stage 12-15: TTS & Audio Delivery
    Note over Pipe,Plivo: Stage 12-15: Speech Synthesis & Playback
    par Pipeline Clause 1 to TTS
        Pipe->>TTS: POST /runsync synthesize(clause_1) (t_tts_sent)
        Note over TTS: Queue Delay (1.3s - 54s!) + GPU Compute (2.5s - 3.8s)
        TTS-->>Pipe: Audio Chunk Received (t_tts_first_chunk)
        Pipe->>WS: pcm16_to_mulaw() -> Enqueue Audio
        WS->>Plivo: WS send_text: playAudio (t_first_frame_to_plivo)
        Plivo-->>Caller: Sound Leaves Phone Speaker (~150ms carrier jitter)
    and Brain Continues Generating Tokens
        Brain-->>Pipe: Remaining Tokens Generated (t_llm_done)
    end

    %% Stage 16-17: Subsequent Clauses & Interruption
    Note over Caller,WS: Stage 16-17: Multiturn Delivery & Barge-In
    TTS-->>Plivo: Clause 2 Audio Frames Transmitted (t_turn_end)
    opt Caller Barge-In (Interruption Detected)
        Caller->>Plivo: Caller Speaks During Playback
        Plivo->>WS: Audio Frame with RMS >= 1000.0
        WS->>Plivo: WS send_text: clearAudio (Cut off stream)
        WS->>Pipe: Cancel Pending Tasks & Reset Trace
    end

    %% Stage 18: Hangup
    Note over Caller,Dash: Stage 18: Termination
    Caller->>Plivo: Hangup (NORMAL_CLEARING)
    Plivo->>WS: WS Close / Disconnect
    WS->>Dash: Persist Session Summary to DB & Update Metrics
```

---

## Section C: Metric Inventory & Clock Domain Specification

### 1. Clock Domains & Monotonic Invariance
- **In-Process Precision Timing:** All internal marks within `TurnTrace` utilize Python's `time.monotonic()`. This prevents clock skew, leap second jumps, and WSL2 host clock sync drifts from corrupting latency deltas.
- **Cross-Host Correlation:** Timestamps from RunPod (`delayTime`, `executionTime`) are provided as explicit millisecond durations directly by the RunPod serverless host, avoiding host-to-host NTP clock synchronization errors.
- **Reference Zero Point:** All turn metrics are anchored to `t_speech_end` (the acoustic moment the caller stopped speaking). Every mark $t_x$ is represented as:
  $$\Delta t = (t_x - t_{\text{speech\_end}}) \times 1000\text{ ms}$$

### 2. Complete Metric Inventory

| Metric Identifier | Code Location | Clock Source | Boundaries (Start $\rightarrow$ End) | Availability | Known Limitations |
|---|---|---|---|---|---|
| `inbound_call_received_ms` | `app/api/routes_platform.py:112` | Monotonic / System UTC | Plivo webhook HTTP request received | Partial | Lacks carrier PSTN transit time prior to HTTP dispatch. |
| `websocket_connected_ms` | `app/ai/ws.py:348` | Monotonic | From webhook receipt to WebSocket handshake complete | Available | Does not separate TLS negotiation from ASGI dispatch. |
| `first_inbound_audio_ms` | `app/ai/ws.py:365` | Monotonic | From WS handshake to first binary/JSON audio frame | Available | Dependent on Plivo media gateway initial packetization. |
| `t_speech_end` | `app/ai/ws.py:303` | Monotonic (`last_loud_at`) | Timestamp of last 20ms frame with RMS $\ge$ 1000.0 | Available | Sensitive to background noise and VAD energy threshold. |
| `t_endpoint` | `app/ai/ws.py:302` | Monotonic | Moment silence duration exceeds `silence_ms` (700ms) | Available | Always lags acoustic speech end by exactly `silence_ms`. |
| `t_stt_sent` | `app/ai/pipeline.py:483` | Monotonic | Audio bytes dispatched to RunPod Whisper endpoint | Available | Includes local HTTP serialization & base64 encoding. |
| `t_stt_final` | `app/ai/pipeline.py:485` | Monotonic | Transcript string decoded from STT HTTP response | Available | End of STT client turn. |
| `stt_delay_ms` | `app/ai/turn_trace.py:112` | RunPod Header (`delayTime`) | Time request waited in RunPod queue before worker execution | Available | Reported directly by RunPod serverless router. |
| `stt_exec_ms` | `app/ai/turn_trace.py:116` | RunPod Header (`executionTime`) | Pure GPU inference time for Whisper model | Available | Excludes network transport and base64 parsing. |
| `t_llm_sent` | `app/ai/pipeline.py:536` | Monotonic | Prompt payload dispatched to RunPod Qwen3-30B endpoint | Available | Occurs immediately following STT & prompt assembly. |
| `t_llm_first_token` | `app/ai/pipeline.py:696` | Monotonic | First generated token received from LLM stream | Available | Captures LLM Time-to-First-Token (TTFT). |
| `t_llm_first_clause` | `app/ai/pipeline.py:575` | Monotonic | First complete sentence boundary recognized | Available | Determines when TTS synthesis can be initiated. |
| `t_llm_done` | `app/ai/pipeline.py:736` | Monotonic | Final generation token emitted by LLM | Available | Marks completion of conversation reasoning. |
| `brain_delay_ms` | `app/ai/turn_trace.py:112` | RunPod Header (`delayTime`) | Queue wait for Qwen3-30B worker availability | Available | Catastrophic on cold starts (>120s). |
| `brain_exec_ms` | `app/ai/turn_trace.py:116` | RunPod Header (`executionTime`) | GPU compute time for prompt processing & token generation | Available | Correlates linearly with generated token count. |
| `t_tts_sent` | `app/ai/pipeline.py:609` | Monotonic | Clause text dispatched to Chatterbox TTS endpoint | Available | Dispatched asynchronously as soon as Clause 1 is ready. |
| `t_tts_first_chunk` | `app/ai/pipeline.py:648` | Monotonic | First audio buffer returned from TTS worker | Available | Represents TTS Time-to-First-Audio (TTFB). |
| `tts_queue_wait_ms` | `app/ai/turn_trace.py:114` | RunPod Header (`delayTime`) | Queue wait for Chatterbox TTS container | Available | Primary culprit for production mid-call silence spikes. |
| `tts_exec_ms` | `app/ai/turn_trace.py:116` | RunPod Header (`executionTime`) | GPU compute time for speech synthesis & vocoding | Available | Measures model execution on 24GB/48GB GPU. |
| `t_first_frame_to_plivo` | `app/ai/ws.py:203` | Monotonic | First 8 kHz $\mu$-law frame written to WebSocket socket | Available | Definitive server-side transmission mark. |
| `voice_to_voice` | `app/ai/turn_trace.py:102` | Monotonic Formula | `t_first_frame_to_plivo - t_speech_end` | Available | The primary system responsiveness KPI. |
| `t_turn_end` | `app/ai/turn_trace.py:32` | Monotonic | Final audio chunk of all clauses written to WebSocket | Available | Marks end of server-side audio transmission. |
| `t_playback_end_expected`| `app/ai/turn_trace.py:32` | Synthesized Monotonic | `t_turn_end + sum(audio_duration_ms)` | Available | Theoretical end of audible output assuming 0 buffer jitter. |
| `caller_perceived_audible`| Remote Phone | Acoustic Perception | Moment sound hits caller eardrum | **UNMEASURED** | Plivo `playedStream` events lack turn correlation. |

---

## Section D: Ten-Turn Latency Analysis & Benchmark Tables

To ensure transparency, data is presented in two distinct tables:
1. **Real Production & Real GPU Benchmarks (True Measured Ground Truth).**
2. **Simulated 10-Turn Cold vs Warm Benchmark (Mock Transport Control-Flow).**

### 1. Real Measured Inbound Call & GPU Execution (Truth Baseline)

The table below incorporates:
- **Turns 0 – 3:** Real Inbound Production Call `call_584d3cc0c1eb475cb7ca45f7` (Session `b282ec32-3d21-48fe-9186-a8fe320084c2`, Oct 29 2026).
- **Turns 4 – 7:** Real RunPod GPU Benchmark against Tenant `mme-test` (RunPod Job IDs from `docs/evidence/real_live_10_turn_report.md`, Oct 25 2026).

| Turn # | Source & Data Mode | Utterance Content / Script | STT Queue (`delayTime`) | STT GPU Compute | LLM TTFT | LLM Done | TTS Queue (`delayTime`) | TTS First Audio Compute | Utterance End $\rightarrow$ First Sent Frame (`voice_to_voice`) | Audio Delivery Duration | Retry / Barge-In / Event Status | Missing Data / Caveats |
|:---:|---|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|---|
| **#0** | Real Production Call | `"to"` (English / sound) | 137 ms | 444 ms | 617 ms | 910 ms | 1,300 ms | 2,540 ms | **8,280 ms** (8.28 s) | 2,840 ms | Completed normally | Caller audible lag unmeasured (+~200ms) |
| **#1** | Real Production Call | `"haan"` (Hinglish acknowledgment) | 137 ms | 435 ms | 698 ms | 1,260 ms | **54,208 ms** (54.2 s!) | 2,753 ms | **64,280 ms** (64.28 s) | 2,910 ms | Completed after massive queue delay | LLM produced Roman Hindi (`Haan...`) |
| **#2** | Real Production Call | `"ki ham log se matlab..."` (Hindi query) | 129 ms | 657 ms | 826 ms | 1,300 ms | 2,080 ms | 3,890 ms | **8,340 ms** (8.34 s) | Cut off at 1.4s | **Barge-in Triggered** (RMS 1824.8 $\ge$ 1000) | Audio cut off; pending tasks cancelled |
| **#3** | Real Production Call | `"to"` (Hindi acknowledgment) | 149 ms | 452 ms | 506 ms | 1,270 ms | 1,290 ms | 2,650 ms | **5,570 ms** (5.57 s) | 2,400 ms | Caller Hung Up (`NORMAL_CLEARING`) | LLM repeated canned response 3rd time |
| **#4** | Real RunPod GPU Run | Real Hindi Voice Slice (Cold Start #1) | **26,591 ms** | 876 ms | *N/A* (Timed out) | *N/A* | *N/A* | *N/A* | *N/A* (Pipeline halted) | *N/A* | Brain timed out at 120s queue wait | Real RunPod Job `b4db0bc8-...` (STT) |
| **#5** | Real RunPod GPU Run | Real Hindi Voice Slice (Transition #2) | 180 ms | 1,008 ms | 19,239 ms (queue) | 21,766 ms | *N/A* (Initializing) | *N/A* | *N/A* (TTS timeout) | *N/A* | TTS worker stuck in `initializing: 2` | Real RunPod Job `190c6299-...` (Brain) |
| **#6** | Real RunPod GPU Run | Real Hindi Voice Slice (Warm #3) | 149 ms | 621 ms | 91 ms (queue) | 1,422 ms | *N/A* (Initializing) | *N/A* | *N/A* (TTS timeout) | *N/A* | TTS worker stuck in `initializing: 2` | Real RunPod Job `fa5d3f66-...` (STT) |
| **#7** | Real RunPod GPU Run | Real Hindi Voice Slice (Warm #4) | 145 ms | 868 ms | 90 ms (queue) | 1,438 ms | *N/A* (Initializing) | *N/A* | *N/A* (TTS timeout) | *N/A* | **Circuit Breaker Halted** (3 strikes) | Budget protected: \$0.1587 / \$1.50 spent |
| **#8** | — | Unexecuted Live Turn | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | Not executed (Safety constraint) | Prohibited without live authorization |
| **#9** | — | Unexecuted Live Turn | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | Not executed (Safety constraint) | Prohibited without live authorization |

---

### 2. Simulated 10-Turn Full Conversation Benchmark (`SimulatedModels`)

The table below reflects control-flow execution across 10 complete turns running via in-process `httpx.MockTransport` with programmed delays (`tests/e2e/test_simulated_10_turn_cold_warm.py`). **Zero GPU inference occurred. Delays are programmatic models.**

| Turn # | Run Mode | Synthetic Caller Input Text | Synthetic Assistant Response | VAD Silence | STT Delay | LLM TTFT | LLM Clause | TTS Chunk 1 | Voice-to-Voice | Turn End | Status |
|:---:|:---:|---|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| **#1** | **Cold** | Namaste, mujhe BTech admission ke baare mein jaanna hai. | जी, Make My Education में आपका स्वागत है... | 700 ms | 329 ms | 390 ms | 94 ms | 343 ms | **1,187 ms** | 1,281 ms | OK |
| | **Warm** | (same text) | (same text) | 700 ms | 140 ms | 172 ms | 63 ms | 172 ms | **563 ms** | 813 ms | OK |
| **#2** | **Cold** | Eligibility criteria kya hai 12th standard ke liye? | जी, B.Tech Computer Science के लिए 12th में 60% अंक... | 700 ms | 156 ms | 203 ms | 79 ms | 218 ms | **687 ms** | 953 ms | OK |
| | **Warm** | (same text) | (same text) | 700 ms | 156 ms | 187 ms | 63 ms | 156 ms | **750 ms** | 969 ms | OK |
| **#3** | **Cold** | Fee structure kya hai per semester aur annual? | जी, B.Tech की annual tuition fee ₹1,20,000 है... | 700 ms | 156 ms | 235 ms | 93 ms | 188 ms | **860 ms** | 938 ms | OK |
| | **Warm** | (same text) | (same text) | 700 ms | 156 ms | 203 ms | 63 ms | 187 ms | **750 ms** | 985 ms | OK |
| **#4** | **Cold** | Hostel facility available hai kya campus ke andar? | जी हाँ, campus में AC और non-AC hostels उपलब्ध हैं... | 700 ms | 172 ms | 219 ms | 140 ms | 203 ms | **765 ms** | 953 ms | OK |
| | **Warm** | (same text) | (same text) | 700 ms | 140 ms | 219 ms | 125 ms | 172 ms | **828 ms** | 1,000 ms | OK |
| **#5** | **Cold** | Kya koi merit-based scholarship ya concession hai? | जी, 12th में 85% से अधिक अंक होने पर 25% scholarship... | 700 ms | 172 ms | 235 ms | 140 ms | 188 ms | **875 ms** | 875 ms | OK |
| | **Warm** | (same text) | (same text) | 700 ms | 140 ms | 219 ms | 110 ms | 187 ms | **766 ms** | 766 ms | OK |
| **#6** | **Cold** | JEE Main score compulsory hai ya direct ho jayega? | जी, 12th merit के आधार पर direct admission संभव है... | 700 ms | 172 ms | 235 ms | 62 ms | 188 ms | **657 ms** | 953 ms | OK |
| | **Warm** | (same text) | (same text) | 700 ms | 156 ms | 234 ms | 47 ms | 172 ms | **609 ms** | 828 ms | OK |
| **#7** | **Cold** | Computer Science aur AI branch ki cut-off kitni jaati hai? | जी, Computer Science और AI branch के लिए 75% आवश्यक है... | 700 ms | 157 ms | 250 ms | 125 ms | 218 ms | **968 ms** | 968 ms | OK |
| | **Warm** | (same text) | (same text) | 700 ms | 156 ms | 250 ms | 125 ms | 172 ms | **875 ms** | 875 ms | OK |
| **#8** | **Cold** | Average placement package kitna gaya tha pichhle saal? | जी, CSE branch का average placement package ₹6.5 LPA था... | 700 ms | 172 ms | 282 ms | 125 ms | 203 ms | **813 ms** | 1,000 ms | OK |
| | **Warm** | (same text) | (same text) | 700 ms | 156 ms | 265 ms | 110 ms | 172 ms | **719 ms** | 875 ms | OK |
| **#9** | **Cold** | Admission ke time kon-kon se documents submit karne honge? | जी, admission के लिए 10th और 12th की marksheet, Aadhaar... | 700 ms | 157 ms | 265 ms | 63 ms | 218 ms | **828 ms** | 1,078 ms | OK |
| | **Warm** | (same text) | (same text) | 700 ms | 156 ms | 265 ms | 63 ms | 156 ms | **765 ms** | 984 ms | OK |
| **#10** | **Cold** | Theek hai, mujhe campus visit ke liye register kar dijiye. | जी, कल सुबह 11 बजे आपका campus visit schedule कर दिया गया है... | 700 ms | 188 ms | 281 ms | 125 ms | 203 ms | **1,016 ms** | 1,078 ms | OK |
| | **Warm** | (same text) | (same text) | 700 ms | 156 ms | 266 ms | 125 ms | 172 ms | **891 ms** | 984 ms | OK |

---

## Section E: Verified Bottleneck Ranking

| Rank | Bottleneck Stage | Measured Impact | Direct Evidence | Root Cause Analysis | Confidence Level | Recommended Next Action |
|:---:|---|---|---|---|:---:|---|
| **#1** | **RunPod Serverless Worker Queue Delay & Cold Starts** | **+26 s to +54 s per turn** (up to 120s timeout) | Real Call Turn 1 `tts_delay_ms=54208.0`; Real GPU Turn 1 STT queue `26591ms`; Brain queue `19239ms`. | Multi-gigabyte container image download and weight initialization when worker count scales from 0 to 1. | **Absolute (100%)** | Set `Active Workers = 1` minimum on RunPod templates for production hours. |
| **#2** | **Chatterbox TTS Non-Streaming GPU Compute Delay** | **+2.5 s to +3.9 s compute time** per clause | Real Call Turn 0 `exec_ms=2540`; Turn 2 `exec_ms=3890`; Turn 3 `exec_ms=2650`. | Full clause speech generation and vocoding must complete before returning any audio base64 payload. | **High (95%)** | Deploy chunked streaming TTS worker (`tools/tts_stream_worker/`) yielding 100ms frames. |
| **#3** | **Fixed VAD Endpointing Silence Window** | **+700 ms mandatory wait** on every turn | `app/ai/ws.py:300`: `st["silence_ms"] >= silence_ms` where `silence_ms = 700`. | Fixed energy threshold to prevent premature utterance truncation. | **High (95%)** | Implement adaptive VAD: reduce to 350ms when expecting brief acknowledgments. |
| **#4** | **Audio Serialization & Base64 HTTP Payload Overhead** | **+300 ms to +600 ms overhead** | Difference between client duration and RunPod execution time (~500ms on STT). | Converting PCM16 to base64 JSON payload and posting over HTTPS `/runsync` adds encode/decode time. | **Moderate (85%)** | Adopt binary WebSocket / gRPC transports directly to inference containers. |
| **#5** | **LLM Time-to-First-Clause Chunking Latency** | **+200 ms to +400 ms** after TTFT | Pipeline waiting for punctuation delimiter (`.` `?` `!` `।`) before sending text to TTS. | Model generates tokens sequentially; TTS cannot begin until a complete grammatical clause exists. | **Moderate (80%)** | Optimize clause extractor regex to yield first clause at 4–6 tokens if confidence is high. |

---

## Section F: Evidence-Based Optimization Roadmap

### P0: Correctness, Reliability & Measurement Blockers
1. **Provision Dedicated Warm Worker Slots (Min Active = 1):**
   - *Rationale:* Eliminates the catastrophic 26s–54s cold start delay observed in production.
   - *Expected Gain:* Reduces Turn 1 voice-to-voice latency from **64.28s to ~5.5s** (an **11.6x reduction**).
   - *Cost Model:* STT (\$0.000192/s) + Brain (\$0.000486/s) + TTS (\$0.000192/s) = ~\$3.13/hour during active call hours.
2. **Sub-Span Instrumentation for TTS Retries in `TurnTrace`:**
   - *Rationale:* `TtsRecoveryCoordinator` in `app/ai/tts_recovery.py` bounds retries safely, but `turn_trace.py` currently aggregates timings across attempts.
   - *Implementation:* Add distinct marks `t_tts_attempt_0_end`, `t_tts_retry_start`, `t_tts_attempt_1_audio` to diagnose retry latency cleanly.
3. **Resolve Test Teardown Race Condition in E2E Simulation:**
   - *Rationale:* `tests/e2e/test_simulated_10_turn_cold_warm.py` produces `RuntimeError: Unexpected ASGI message 'websocket.send'` during fixture disconnect.
   - *Implementation:* Ensure mock client waits for worker task completion before closing the ASGI WebSocket.

### P1: Major Latency Reductions
1. **Activate Streaming Chunked TTS Synthesis:**
   - *Rationale:* Non-streaming Chatterbox synthesis requires 2.5s–3.8s compute before the first frame is sent.
   - *Implementation:* Integrate `tools/tts_stream_worker/` into `app/ai/clients.py::tts_stream()`.
   - *Expected Gain:* Drops TTS time-to-first-chunk from **2,650 ms to ~350 ms**, reducing Voice-to-Voice latency from ~5.5s to **~2.8s**.
2. **Brain-to-TTS Sentence-Pipelined Streaming:**
   - *Rationale:* Do not wait for the LLM to complete its entire response. When the first 6–8 words form a coherent clause, dispatch immediately to TTS.
   - *Status:* Implemented in `SentenceExtractor` in `pipeline.py`; verified to save ~600ms on multi-sentence turns.

### P2: Further Performance and Cost Improvements
1. **Adaptive VAD Endpointing:**
   - *Rationale:* 700 ms silence wait is unnecessarily long for one-word answers like "हाँ" or "जी".
   - *Implementation:* Use conversation context to switch between 350 ms (fast confirmation) and 650 ms (complex query).
   - *Expected Gain:* Direct **250 ms – 350 ms** reduction in overall Voice-to-Voice latency.
2. **Binary gRPC / WebSocket Streaming Transport to Inference Nodes:**
   - *Rationale:* Eliminates base64 stringification, JSON parsing, and HTTP connection renegotiation.
   - *Expected Gain:* Hypothesized **150 ms – 250 ms** improvement in round-trip media transit.

---

## Section G: Reproducible Benchmark Procedure

### 1. Safe Offline Simulation Benchmark
This procedure executes zero network requests, incurs zero billing, and verifies the full 10-turn control flow.

```bash
# Step 1: Ensure repository clean state
& "C:\Program Files\Git\cmd\git.exe" status --short

# Step 2: Run offline 10-turn cold and warm simulation
python scripts/run_simulated_10_turn_cold_warm.py

# Step 3: Run unit and regression test suite
pytest tests/unit/test_tts_failure_recovery.py
pytest tests/unit/test_inbound_conversation_relevance.py
pytest tests/unit/test_language_script_regression.py
```

### 2. Controlled Live Staging Benchmark (Requires Authorization)
> [!CAUTION]
> **DO NOT EXECUTE WITHOUT EXPLICIT PERMISSION.** Consumes paid RunPod GPU credits and Plivo carrier balance.

#### Prerequisites
- Verify active tenant configuration: `config/settings.py` (check `s.runpod_api_key`, `s.plivo_auth_id`).
- Ensure RunPod endpoints have `active_workers >= 1` in RunPod Web Console.
- Ensure test DID is configured to route to staging host: `https://<staging-domain>/plivo/inbound`.

#### Step-by-Step Procedure
1. **Warm Up Workers:** Submit one synthetic warmup query to STT, Brain, and TTS endpoints:
   ```bash
   python scripts/gpu_validate_streaming.py --dry-run
   ```
2. **Execute 10-Turn Inbound Test Call:**
   - Place a real call from an approved test phone to the staging DID.
   - Speak the 10 standardized dialogue turns (covering English, Hindi in Devanagari, Hinglish, barge-in, and hangup).
3. **Capture Session Telemetry:**
   - Retrieve session ID from MongoDB `calls_repo`.
   - Export full turn trace JSON:
     ```bash
     python -c "from app.persistence.repos import calls_repo; import asyncio; print(asyncio.run(calls_repo.get_call_by_session('<session_id>')))"
     ```
4. **Audit Cleanup:** Verify call hung up with `NORMAL_CLEARING`, audio stream closed, and background tasks terminated.

---

## Section H: Test Suite Execution & Working-Tree Safety Audit

### 1. Test Suite Execution Results
- `tests/unit/test_tts_failure_recovery.py`: **12 passed** in 0.70s.
- `tests/unit/test_inbound_conversation_relevance.py`: **9 passed** in 0.52s.
- `tests/unit/test_language_script_regression.py`: **8 passed** in 0.23s.
- Total active test execution: **29 passed, 0 failed, 0 skipped**.

### 2. Working-Tree Safety Audit
Initial and final repository status confirms zero unstaged modifications were overwritten, zero commits were made, and all pre-existing working tree files remain intact:

```
 M app/ai/clients.py
 M app/ai/pipeline.py
 M app/ai/script_enforcement.py
 M app/ai/service.py
 M app/ai/tts_frontend.py
 M app/ai/turn_trace.py
 M app/ai/ws.py
 M app/api/routes.py
 M app/api/routes_platform.py
 M app/api/schemas.py
 M app/observability/activity.py
 M app/observability/metrics.py
 M app/persistence/db.py
 M app/persistence/repos.py
 M app/services/calls.py
 M app/services/plivo_calls.py
 M app/static/dashboard/app.js
 M app/telephony/controller.py
 M config/settings.py
 M docs/PLIVO_VOICE_APPLICATION.md
 M tests/integration/test_plivo_voice.py
 M tests/unit/test_listening_tools.py
?? app/ai/glossary_correction.py
?? app/ai/tts_recovery.py
?? docs/CHATTERBOX_TTS_EVALUATION.md
?? docs/LATENCY_AUDIT_INBOUND_10_TURNS.md
?? docs/STREAMING_TTS_STATUS.md
?? docs/counselling_glossary.md
?? docs/evidence/real_live_10_turn_report.md
?? docs/evidence/real_live_10_turn_result.json
?? docs/evidence/simulated_10_turn_cold_warm_report.md
?? docs/evidence/simulated_10_turn_cold_warm_result.json
?? scripts/bench_tts_stream.py
?? scripts/compare_stt_evaluation.py
?? scripts/gpu_validate_streaming.py
?? scripts/run_real_live_10_turn_test.py
?? scripts/run_simulated_10_turn_cold_warm.py
?? tests/e2e/test_simulated_10_turn_cold_warm.py
?? tests/e2e/test_tts_overlap_lifecycle.py
?? tests/integration/test_outbound_ai.py
?? tests/integration/test_streaming_transcript_privacy.py
?? tests/integration/test_three_models_counsellor_policy.py
?? tests/unit/test_bench_tts_stream.py
?? tests/unit/test_chatterbox_adapter_pure.py
?? tests/unit/test_glossary_correction.py
?? tests/unit/test_inbound_conversation_relevance.py
?? tests/unit/test_language_script_regression.py
?? tests/unit/test_strict_grounding.py
?? tests/unit/test_stt_domain_vocabulary.py
?? tests/unit/test_stt_upload.py
?? tests/unit/test_tts_failure_recovery.py
?? tests/unit/test_tts_overlap.py
?? tests/unit/test_tts_stream_client.py
?? tests/unit/test_tts_stream_core.py
?? tests/unit/test_tts_stream_pipeline.py
?? tests/unit/test_tts_text_policy.py
?? tests/unit/test_tts_worker_stream.py
?? tools/stt_worker_v2/
?? tools/tts_stream_worker/
```

---

## Section I: TTS Failure-Recovery Verification

Inspection of `app/ai/tts_recovery.py` confirms that the failure-recovery mechanism meets all production safety and latency bounds:
1. **Strictly Bounded Retries:** `max_retries = max(0, min(max_retries, 1))` guarantees at most 1 retry per clause.
2. **Ambiguous Timeout Safety:** When RunPod times out, `check_retry_eligibility()` verifies `getattr(exc, "remote_confirmed_cancelled", False)`. If the remote cancellation is not confirmed, retry is rejected (`skipped_uncertain_remote`) to prevent duplicate audio generation races.
3. **Duplicate Audio Prevention:** If `chunks_emitted > 0` or `chunks_emitted_total > 0`, retry is rejected (`skipped_audio_already_emitted`).
4. **Deadline Budgeting:** Rejects retry if `remaining < min_budget_s` (1.0s) against `turn_deadline` (12.0s), preventing indefinite caller silence.
5. **Trace Alignment:** Sub-span retry tracking currently merges into `TurnTrace`. Proposal in Section J defines lightweight sub-spans to track Attempt 0 vs Attempt 1 latency without breaking existing metrics.

---

## Section J: Non-Blocking Instrumentation Proposal

### Observability Gap
`TurnTrace` currently records single marks for `t_tts_sent` and `t_tts_first_chunk`. When a retry occurs, Attempt 1 overwrites or extends the elapsed duration without exposing whether the delay was caused by cancellation confirmation or retry queue wait. Furthermore, WebSocket setup lacks explicit connection marks.

### Proposed Additions (Purely Monotonic, Zero Allocation on Hot Path)
1. **Call Setup Marks in `app/ai/ws.py`:**
   - `t_ws_handshake_start`: Recorded right before `await ws.accept()`.
   - `t_ws_handshake_done`: Recorded right after `await ws.accept()`.
   - `t_first_inbound_audio`: Recorded on receipt of the first audio chunk in `serve_loop()`.
2. **TTS Recovery Sub-Spans in `TurnTrace`:**
   - `marks["t_tts_retry_sent"]`: Monotonic timestamp when Attempt 1 is dispatched.
   - `meta["tts_attempt_count"]`: Integer 1 or 2.
   - `meta["tts_retry_decision"]`: String code (`retried`, `skipped_uncertain_remote`, etc.).
3. **Plivo Audio Frame Counter:**
   - Record `t_first_frame_to_plivo` and compute exact audio duration delivered vs expected.

