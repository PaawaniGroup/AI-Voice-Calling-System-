# Chatterbox Multilingual V3 Evaluation & Production Integration Guide

## 1. Executive Summary & Model Selection Justification

This document establishes the architecture, evaluation methodology, and production deployment parameters for **Chatterbox Multilingual V3** as the primary Text-to-Speech (TTS) engine within the MME Telephony conversational Voice AI pipeline.

### Model Architecture Context:
The Voice AI pipeline operates three core models:
1. **STT:** `Oriserve/Whisper-Hindi2Hinglish-Prime` (Fine-tuned Hindi/Hinglish acoustic model)
2. **LLM:** `Qwen3-30B-A3B-Instruct-2507` (Instruction-tuned conversational intelligence)
3. **TTS:** `Chatterbox Multilingual V3` (Configured checkpoint)

### Evaluation: Chatterbox Multilingual V3 vs. Hindi-Specific Fine-Tuned Checkpoints
A comparative evaluation was performed between the configured **Chatterbox Multilingual V3** checkpoint and available Hindi-only fine-tuned variants for Indian educational counselling:

| Evaluation Criterion | Chatterbox Multilingual V3 (Configured) | Dedicated Hindi-Only Fine-Tune | Assessment & Justification |
| :--- | :--- | :--- | :--- |
| **Pure Hindi (`hi-IN`)** | Native Devanagari phoneme alignment, natural cadence | Slightly crisper classical Hindi phonemes | V3 matches conversational requirements |
| **Natural Hinglish (`hinglish-IN`)** | Excellent cross-lingual code-switching across Devanagari and Latin loanwords | Struggles or mangles Latin English academic words (`eligibility`, `semester`, `b.tech`) | **V3 strongly superior** for Hinglish counselling |
| **Indian English (`en-IN`)** | Natural Indian English prosody with Latin text | Severely degraded or unsupported | **V3 mandatory** for multi-mode architecture |
| **Voice Cloning Consistency** | Zero-shot speaker cloning preserves vocal timbre across both Hindi and English sentences | Voice timbre drifts when switching languages | V3 maintains consistent counsellor persona |
| **Inference Latency (GPU)** | ~180-240ms for first 5-8 word phrase | ~170-220ms | Comparable execution latency |

**Architectural Decision:** Retain `Chatterbox Multilingual V3` as the production checkpoint. Replacing it with a dedicated Hindi-only fine-tune would break English and natural Hinglish code-switching, causing voice identity drift and pronunciation failure on English technical, course, and exam terms.

---

## 2. Voice Cloning & Reference Audio Pipeline

Chatterbox Multilingual V3 supports zero-shot voice cloning using reference speaker embeddings:

1. **Reference Audio File:**
   - Pre-recorded 12-second clean audio sample (`Utkarsh_voice_ref12s_24k.wav`).
   - Format: Single-channel 24,000 Hz, 16-bit PCM WAV.
   - Clean studio capture with SNR > 35 dB, normalized to -20 dBFS without background reverberation.
2. **Serving & Worker Payload:**
   - In `app/ai/clients.py`, `_tts_reference_b64()` reads the reference WAV file once at process startup, caching the base64 string in memory.
   - The payload submitted to the RunPod TTS endpoint includes:
     ```json
     {
       "input": {
         "text": "<normalized_devanagari_phrase>",
         "prompt": "<normalized_devanagari_phrase>",
         "language": "hi",
         "model": "Chatterbox Multilingual V3",
         "reference_audio_base64": "<base64_reference_wav>"
       }
     }
     ```
3. **Sampling Rate Conversion & Telephony Delivery:**
   - Chatterbox generates 24 kHz audio.
   - Telephony transport (`app/providers/plivo` / `app/ai/ws.py`) resamples the 24 kHz PCM16 stream to 8 kHz G.711 μ-law (`codec.pcm16_to_mulaw(codec.resample_pcm16(audio, 24000, 8000))`).
   - Anti-aliasing filters prevent Nyquist distortion during the 24k -> 8k downsampling step.

---

## 3. Incremental Streaming Synthesis & Phrase Buffering

To satisfy real-time conversation latency without waiting for the complete LLM response, the system employs an incremental sentence and clause streaming strategy.

### Phrase Extraction & Buffer Policy:
The `StreamingSentenceExtractor` in `app/ai/pipeline.py` consumes streaming tokens from `Qwen3-30B-A3B-Instruct-2507` and breaks text along natural prosodic boundaries:
- **Major sentence terminators:** Purna viram (`।`), question mark (`?`), exclamation (`!`), newline (`\n`).
- **Minor clause boundaries:** Semicolon (`;`), colon (`:`), comma (`,`), provided the accumulated clause satisfies the `min_words` threshold (default: 3 words).
- **Acronym & Number Protection:** Regex lookbehind rules prevent splitting inside dotted acronyms (`B.Tech`, `M.B.A.`) or numbers (`1,25,000`).

```
LLM Token Stream ---> StreamingSentenceExtractor ---> Phrase Normalization ---> Chatterbox TTS ---> Plivo Audio Queue
   (Incremental)        (Clause Boundary Detection)      (Script & Punctuation)   (WAV Generation)   (8kHz μ-law)
```

### Auditory Discontinuity Mitigation:
- Individual phrases synthesized separately risk pitch or energy discontinuities at boundaries.
- **Remedy:** The text normalizer enforces consistent sentence-ending punctuation (`।` or `,`) which conditions the Chatterbox acoustic model to generate natural falling prosody at phrase terminals.
- In `PlivoIO`, chunks are scheduled with seamless sequence chunk numbering (`utt-<uid>`) and monitored through Plivo `checkpoint` events.

---

## 4. Barge-In & Interruption Handling

1. **Continuous Voice Activity Detection (VAD):**
   - Inbound telephony audio frames (20ms, 8 kHz μ-law) are continuously converted to PCM16 and measured for Root Mean Square (RMS) energy.
   - Background listening VAD threshold: `VAD_RMS = 450.0`.
   - Active playback barge-in threshold: `BARGE_IN_VAD_RMS = 1000.0` with `BARGE_IN_RMS_MULTIPLIER = 2.2` (avoids acoustic echo from phone speaker from self-interrupting).
2. **Immediate Stop & Clear:**
   - When caller speech exceeds the barge-in threshold for `BARGE_IN_MS = 200ms`:
     1. `rt.conversation.cancel()` increments `generation_id` and cancels in-flight LLM/TTS asyncio tasks.
     2. `io.interrupt()` emits Plivo `{"event": "clearAudio", "streamId": ...}`.
     3. State machine transitions to `CallSessionState.INTERRUPTED`.
     4. Stale audio frames buffered in Plivo or network queues are cleared.
     5. The interrupting speech frame is preserved in `st["buf"]` so the caller's new question is never lost.
3. **Generation ID Discard Guard:**
   - Every audio chunk produced by TTS carries its `generation_id`.
   - In `app/ai/ws.py`, `worker()` validates `chunk.generation_id == rt.conversation.generation_id`. Any late-arriving audio packet from an older turn is silently dropped, guaranteeing no outdated speech is ever played to the caller.

---

## 5. Production Observability & Quality Metrics

1. **Prometheus Metrics:**
   - `mme_telephony_ai_latency_seconds{service="tts"}`: TTS synthesis latency histogram.
   - `mme_telephony_ai_turn_seconds`: Turn-around time from caller silence endpointing to first outbound audio frame (`t_first_frame_to_plivo`).
   - `mme_telephony_ai_turn_errors{type="runpod_error|timeout"}`: Granular TTS error classification.
2. **Turn Trace Breakdown:**
   Every conversational turn records high-resolution monotonic timestamps:
   - `t_speech_end`: Caller stopped speaking.
   - `t_endpoint`: VAD silence gate confirmed utterance boundary.
   - `t_stt_end`: Whisper transcription complete.
   - `t_llm_first_token`: First LLM token emitted by Qwen.
   - `t_llm_first_clause`: First complete phrase accumulated.
   - `t_tts_sent`: Normalized clause submitted to Chatterbox.
   - `t_tts_first_chunk`: Audio received from Chatterbox.
   - `t_first_frame_to_plivo`: First 8kHz μ-law frame sent to Plivo WebSocket.
