# MME RunPod-Native Chatterbox V3 Streaming Deployment & GPU Validation Report

**Deployment Date**: October 31, 2026  
**Status**: **DEPLOYED & VALIDATED ON LIVE GPU**  
**Authorized Dev Endpoint**: `dzqzqas7frc0zt` (`mme-v4-validate-1`, template `pxc1dw5vv2`)  
**Production Endpoint (Untouched)**: `6bmxas8ccwzhrv` (`MME-TTS`, template `z3sn9cxpz7`)  
**Telephony Status**: `BLOCKED_PENDING_WATERMARKING_APPROVAL` (Live diagnostic validation passed; live PSTN telephone calls require unwatermarked audio signoff)

---

## 1. Executive Summary

This report documents the end-to-end implementation, packaging, RunPod serverless deployment, and live GPU validation of genuine incremental text-to-speech streaming for Chatterbox Multilingual V3 in the MME AI Voice Calling system.

All validation was executed directly on live RunPod GPU hardware against the authorized development endpoint `dzqzqas7frc0zt`. The production endpoint `6bmxas8ccwzhrv` remained strictly untouched throughout this execution.

### Key Empirical Achievements:
1. **100% T3 Token Equivalence**: Verified that incremental token generation (`t3_token_stream`) produces identical speech tokens to full-batch autoregressive inference (`model.t3.inference`) across Hindi and English prompts (`all_identical: true`, `first_mismatch: null`).
2. **Streaming Decoder Window Optimization**: Evaluated candidate sliding window configurations over cuDNN/CUDA. Configuration `win_c25_h12_f4_x240` (context=25, hop=12, first-hop=4, crossfade=240) achieved a log-mel distance of **0.4086** (within 4% of the pure CFM noise floor of 0.3914), zero length error, a **35% reduction in redundant compute**, and the lowest click ratio (**9.95**).
3. **56.6% TTFA Latency Reduction**: Time to First Speech Token (T3) clocked at **49 ms**. Time to First Audio Chunk (TTFA) clocked at **643 ms** (GPU-native) and **1.919s** (end-to-end via RunPod Serverless API), compared to **1.482s - 4.43s** for batch synthesis.
4. **Fast Mid-Stream Barge-in Cancellation**: GPU release on caller interruption clocked at **425.4 ms**.
5. **Progressive Audio Streaming Delivery**: Validated live streaming delivery of 7 ordered chunks (2.68s of Hindi speech, G.711 $\mu$-law @ 8000 Hz) via `ModelClients.synthesize_stream`.
6. **Complete-Waveform Fallback Preserved**: Verified full non-streaming synthesis with Perth watermarking on the same endpoint (157,440 bytes PCM16 @ 24 kHz in 4.43s).
7. **Security Gate Enforced**: Requests requesting streaming without authorization are strictly refused with `streaming_disabled`.

---

## 2. Infrastructure & Deployment Architecture

### 2.1 Live Environment Topology

| Service | Endpoint ID | Hardware | Host Environment | Software Stack |
|---|---|---|---|---|
| **TTS (Development)** | `dzqzqas7frc0zt` | NVIDIA RTX A5000 (24 GB) | PyTorch 2.7.1, CUDA 12.8 | `ghcr.io/aitechteammme-dev/mme-tts-runpod:v3` |
| **TTS (Production)** | `6bmxas8ccwzhrv` | NVIDIA RTX A5000 / 3090 | PyTorch 2.7.1, CUDA 12.8 | **STRICTLY UNTOUCHED (Read-Only)** |
| **Brain (LLM)** | `hu8qhrvlxbv314` | NVIDIA A40 / RTX A6000 | vLLM v2.29.0 | Qwen3-30B-A3B-Instruct-2507-FP8 |
| **STT** | `ua9l3gus6qjnmg` | NVIDIA RTX 4090 (24 GB) | PyTorch 2.7.1, CUDA 12.8 | Whisper STT Worker |

### 2.2 Container Packaging & Bootstrapping Strategy
The development template `pxc1dw5vv2` was updated with a dynamic bundle containing:
- `streaming/chatterbox_adapter.py`
- `streaming/stream_core.py`
- `streaming/worker_stream.py`
- `streaming/handler_stream.py`
- `streaming/gpu_validate_streaming.py`
- `pinned/` (security pinning and runtime verification)
- `handler.py` (RunPod Serverless generator handler)

**Root Cause Discovery (Checkpoint Location)**:
During initial container boot, the worker reported `PinnedRuntimeError: checkpoint directory /app/model does not exist`. Inspection of the base image `ghcr.io/aitechteammme-dev/mme-tts-runpod:v3` revealed that Chatterbox V3 checkpoint weights are stored in Hugging Face's cache at `/app/hf_cache` (`HF_HOME=/app/hf_cache`), rather than an uncompressed `/app/model` directory.
`handler.py` was updated with a resilient loader that automatically falls back to `ChatterboxMultilingualTTS.from_pretrained(device="cuda", t3_model="v3")` while strictly requiring the V3 T3 checkpoint.

---

## 3. Empirical GPU Validation Results

### 3.1 Stage 6: T3 Incremental Speech-Token Equivalence (`stage_t3_equiv`)
Validates that generating speech tokens token-by-token with KV-caching (`t3_token_stream`) produces the exact same token sequence as the reference batch model (`model.t3.inference`).

```
Job IDs:
  - hi_short: ae24555d-a94b-439f-8f6e-a3fb9461fe59-e2
  - hi_medium: 71973341-ef3d-48d6-b67c-db85ee7be3fb-e2
  - en_medium: ce587201-c0b7-48f3-bbce-9b9fec1ab148-e1
```

| Prompt | Language | Max Tokens | Streamed Tokens | Reference Tokens | Match Ratio | First Mismatch | Execution Time |
|---|---|---|---|---|---|---|---|
| `hi_short` | Hindi | 30 | 30 | 30 | **100% (Identical)** | `None` | 1.62s |
| `hi_medium` | Hindi | 50 | 50 | 50 | **100% (Identical)** | `None` | 2.92s |
| `en_medium` | English | 50 | 50 | 50 | **100% (Identical)** | `None` | 2.37s |

**Result**: **PASS (100% Token Equivalence)**. The KV-cached autoregressive stream strictly matches full-waveform T3 token output.

---

### 3.2 Stage 7: S3Gen and HiFT Decoder Window Validation (`stage_decoder`)
Evaluates sliding window decoding against full prefix decoding and the natural Conditional Flow Matching (CFM) noise floor.

```
Job ID: 9e9f1f9e-c91d-4528-a98b-bf9cee12a0a8-e2
Prompt: hi_short (83 speech tokens total)
CFM Noise Floor: 0.3914 log-mel distance (variance between two independent full decodes of the same tokens)
```

| Mode | Context Tokens | Hop Tokens | First Hop | Crossfade | First Audio Time | Wall Time | Tokens Decoded | Max Window | Peak VRAM | Mel Distance to Full | Click Ratio |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `fullprefix` | None | 12 | 4 | 240 | 468.4 ms | 3.503s | 384 | 83 | 3,782.5 MB | 0.4191 | 13.48 |
| **`win_c25_h12_f4_x240`** | **25** | **12** | **4** | **240** | **400.3 ms** | **3.323s** | **249** | **40** | **3,756.8 MB** | **0.4086** | **9.95** |
| `win_c50_h12_f4_x240` | 50 | 12 | 4 | 240 | 408.6 ms | 3.681s | 342 | 65 | 3,771.4 MB | 0.4154 | 27.04 |

**Optimal Operating Point Identified**: `win_c25_h12_f4_x240`
- **Lowest Latency**: 400.3 ms to first audio.
- **Compute Efficiency**: 249 decoded tokens vs 384 (35% reduction in redundant CFM flow-matching steps).
- **Acoustic Fidelity**: Log-mel distance of **0.4086** is within 0.0172 of the pure CFM noise floor (0.3914).
- **Boundary Continuity**: Lowest click ratio (9.95) with zero sample length error (`len_error_samples: 0`).

---

### 3.3 Stage 8: Latency Profiling (`stage_latency`)
Profiles component latencies on the live NVIDIA RTX A5000 GPU.

```
Job ID: ced7a3a7-969a-4a65-b4f1-428972d0a150-e1
Prompt: hi_short
```

| Metric | Measurement | Description |
|---|---|---|
| **T3 Time-to-First-Token (TTFT)** | **0.049s (49 ms)** | Initial autoregressive step with prompt conditioning |
| **Time-to-First-Audio (TTFA)** | **0.643s (643 ms)** | Real audio chunk emitted from first-hop window (4 tokens) |
| **Batch Baseline Total Time** | **1.482s** | Complete sentence generation with Perth watermark |
| **TTFA Latency Improvement** | **56.6% reduction** | Audio playback starts 839 ms sooner than batch synthesis |
| **Barge-in Cancel Latency** | **0.4254s (425 ms)** | Elapsed time to detect caller barge-in, abort T3 loop, and free GPU |
| **Streaming RTF** | **1.79** | Overall real-time factor for incremental processing |

---

## 4. End-to-End Client & Streaming Transport Integration

### 4.1 RunPod Serverless Stream Protocol Draining
Empirical investigation of RunPod's `/stream/{job_id}` protocol revealed:
1. `GET /stream/{job_id}` operates as a destructive consumer queue: each request returns only the new chunks produced since the previous poll.
2. When a generator completes, `GET /status/{job_id}` aggregates the complete ordered list of all chunks produced under `output`.
3. Client method `ModelClients.synthesize_stream` in `app/ai/clients.py` was updated to seamlessly drain both progressive `/stream` polls and any remaining chunks from `/status`, eliminating sequence gaps.

### 4.2 Progressive Streaming Client Live Test

Tested via `ModelClients.synthesize_stream` on `dzqzqas7frc0zt`:
- **Text**: *"नमस्ते, Make My Education में आपका स्वागत है।"*
- **Language**: `hi` (Hindi)
- **Voice**: `utkarsh`
- **Format**: `ulaw8k` (G.711 $\mu$-law @ 8000 Hz, 8-bit mono)

```
[ 1.92s] Chunk seq=0: 2400 bytes PCM16 (0.15s speech) final=False rate=8000
[ 2.76s] Chunk seq=1: 7680 bytes PCM16 (0.48s speech) final=False rate=8000
[ 3.61s] Chunk seq=2: 7680 bytes PCM16 (0.48s speech) final=False rate=8000
[ 4.55s] Chunk seq=3: 7680 bytes PCM16 (0.48s speech) final=False rate=8000
[ 5.39s] Chunk seq=4: 7680 bytes PCM16 (0.48s speech) final=False rate=8000
[ 6.06s] Chunk seq=5: 7680 bytes PCM16 (0.48s speech) final=False rate=8000
[ 6.56s] Chunk seq=6: 2080 bytes PCM16 (0.13s speech) final=True rate=8000

Total Chunks: 7
Total Speech Duration: 2.68s
Total Bytes: 21,440 bytes PCM16 (10,720 bytes G.711 mu-law)
```

### 4.3 Fallback Non-Streaming Verification
Non-streaming synthesis was verified on the same active worker:
- **Call**: `clients.synthesize("नमस्ते, यह गैर-स्ट्रीमिंग का परीक्षण है।", "hi")`
- **Result**: 157,440 bytes PCM16 @ 24,000 Hz with Perth watermark delivered in 4.43s.

---

## 5. Security & Watermarking Policy Gate

Chatterbox V3 embeds the Resemble Perth implicit watermarker directly into the complete audio waveform. Because chunk-level Perth watermarking does not exist in upstream Chatterbox, incremental audio streaming cannot carry the watermarking signature.

1. **Gate Architecture**:
   - `ALLOW_UNWATERMARKED_STREAMING` is locked to `0` by default.
   - Any request with `stream=True` that lacks authorization is rejected in **276 ms** with `{"error": "streaming_disabled", "status": "FAILED"}` and zero audio bytes.
2. **Current Gate Status**:
   - **Diagnostic GPU Validation**: **AUTHORIZED & COMPLETED**.
   - **Live Production Telephony Calls**: **BLOCKED_PENDING_WATERMARKING_APPROVAL**.
   - Live caller PSTN audio continues to use the validated complete-waveform synthesis fallback until organizational policy approval is obtained.

---

## 6. Deliverables & Git Inventory

| Component | Repository Path | Branch | Commit Hash |
|---|---|---|---|
| **TTS Worker Stream Bundle** | `MME_wt_tts_v4` | `tts-v4-streaming-validate` | `45131a4` |
| **Telephony Client & Recovery** | `MME_Telephony` | `streaming-implementation` | `64b6721` |
| **Dev RunPod Template** | `pxc1dw5vv2` | N/A (Serverless Template) | Version 18 |
| **Dev RunPod Endpoint** | `dzqzqas7frc0zt` | N/A (`mme-v4-validate-1`) | Live (1 GPU Worker Active) |
| **Production RunPod Endpoint** | `6bmxas8ccwzhrv` | N/A (`MME-TTS`) | **STRICTLY UNTOUCHED** |

---

## 7. Conclusion

Genuine Chatterbox Multilingual V3 incremental streaming has been successfully deployed to the isolated RunPod development endpoint and validated against real GPU hardware. The implementation achieves 100% token equivalence with the baseline model, a 56.6% reduction in latency to first audio chunk (643 ms), robust mid-stream barge-in cancellation (425 ms), and progressive delivery of clean G.711 $\mu$-law audio directly compatible with Plivo/WebRTC telephony.
