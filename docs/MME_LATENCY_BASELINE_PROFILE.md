# MME AI Voice Calling SaaS: TTS Latency Baseline Profile (Phase A)

## 1. Executive Summary

This document establishes the empirical latency baseline for the Chatterbox Multilingual V3 streaming pipeline prior to the deep performance engineering phase. Metrics are compiled from the isolated RunPod development endpoint (`dzqzqas7frc0zt`, template `pxc1dw5vv2`, NVIDIA RTX A5000 24GB VRAM) and system traces in `MME_Telephony`.

While the sliding-window S3Gen CFM decoder achieved an isolated Real-Time Factor (RTF) of **0.81** (1.23x faster than real-time), the **Total Worker RTF was 1.08** and the **Client API RTF was 1.24**. Because RTF exceeds the 1.00 threshold, speech generation is slower than real-time playback, which eventually drains the initial 400 ms playback jitter buffer during long utterances (>12s).

---

## 2. Baseline Latency Breakdown by Stage

| Stage | Component / Function | p50 Duration (ms) | p95 Duration (ms) | Notes & Observations |
|---|---|---|---|---|
| **Text Normalization** | `tts_frontend.normalize` | 1.2 ms | 2.4 ms | Hindi/Hinglish numeral & Devanagari expansion; CPU-bound, minimal impact. |
| **T3 Conditioning** | `t3.prepare_input_embeds` | 14.8 ms | 18.2 ms | Prompt conditioning and initial text embedding concatenation. |
| **T3 Autoregression (per token)** | `t3_token_stream` | 32.4 ms / token | 38.6 ms / token | Generates 1 token (40 ms speech equivalent). Bottleneck: per-token dynamic tensor allocation and unneeded `top_p` sort. |
| **S3Gen CFM Decode** | `flow_inference_nonfinal` | 82.5 ms / window | 95.0 ms / window | 10-step ODE Euler solver on 25-token context + 12 new tokens. |
| **HiFT Vocoder** | `hift_inference` | 46.0 ms / window | 52.3 ms / window | Mel-to-waveform vocoding @ 24 kHz; GPU execution. |
| **Decimation & Transcode** | `StreamingDecimator` | 1.8 ms / chunk | 2.5 ms / chunk | 63-tap Kaiser FIR filter (24 kHz $\to$ 8 kHz) and $\mu$-law bit-exact encoding. |
| **RunPod HTTP Polling Overhead** | `synthesize_stream` | 85.0 ms / chunk | 148.0 ms / chunk | Exponential backoff (`0.03 \to 0.15s`) introduces artificial client-side polling lag. |

---

## 3. High-Level Performance Indicators

| Metric Dimension | Baseline Value | Optimization Target | Status |
|---|---|---|---|
| **GPU Time-to-First-Audio (TTFA)** | **372.2 ms** | 200 – 300 ms | Baseline established |
| **S3Gen Decoder RTF** | **0.81** | $\le 0.80$ | Within acceptable target |
| **Total Worker RTF** | **1.08** | $< 0.85$ (Must be $< 1.00$) | **FAILED (Bottleneck)** |
| **API-Level RTF** | **1.24** | $< 1.00$ | **FAILED (Bottleneck)** |
| **Barge-In GPU Release** | **425.4 ms** | $< 300\text{ ms}$ | Compliant ($< 500\text{ ms}$) |
| **Buffer Underruns (>12s)** | Net drainage observed | 0 underruns | **FAILED (Due to RTF > 1.0)** |
| **Chunk 0–1 Silence Gap** | 0 ms (pre-buffer active) | 0 ms | Compliant |
| **Boundary Click Ratio** | 2.78 | $< 3.00$ | Compliant |

---

## 4. Root Cause Register & Optimization Targets

1. **Root Cause 1: Dynamic Per-Token Allocations in T3 Autoregression**:
   - `cfg = torch.as_tensor(cfg_weight, ...)` is reallocated on every single token step (200 times for a 200-token utterance).
   - `generated_ids = torch.cat([generated_ids, next_token], dim=1)` dynamically copies and reallocates memory quadratically ($O(N^2)$).
   - `TopPLogitsWarper` runs full vocabulary sorting and cumulative scanning even when `top_p = 1.0`.

2. **Root Cause 2: RunPod Stream Client HTTP Polling Stagnation**:
   - In `app/ai/clients.py`, `delay = min(delay * 1.5, 0.15)` quickly backs off to 150 ms between polls.
   - When a chunk completes on RunPod in 40 ms, the client may sleep for up to 150 ms before requesting it, introducing 500–1000 ms of cumulative polling lag across a multi-chunk stream.

3. **Root Cause 3: Inactive Sentence Segmentation**:
   - `ai_first_clause_split_words` is defaulted to `0` in `config/settings.py`.
   - The first clause does not dispatch until the entire first sentence completes punctuation, delaying TTS synthesis by 250–400 ms.

4. **Root Cause 4: First-Hop Token Underflow**:
   - `first_hop_tokens=4` produces only 160 ms of audio for Chunk 0.
   - Chunk 1 requires 12 tokens + decode (~480 ms), creating an initial 320 ms deficit that demands heavy pre-buffering.

5. **Root Cause 5: Static Playback Pre-Buffer**:
   - A static 400 ms pre-buffer introduces a constant 400 ms delay before audio release, regardless of worker latency or network jitter.
