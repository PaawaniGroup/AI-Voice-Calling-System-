# MME AI Telephony: STT Accuracy, VAD Pre-Roll, Hinglish Understanding & Repetition Prevention Implementation Report

**Project:** `MME_Telephony`  
**Branch:** `realtime-latency`  
**Date:** October 30, 2026  
**Status:** IMPLEMENTED AND VERIFIED  

---

## 1. Executive Summary

This report documents the architectural fixes and comprehensive regression test suite implemented in `MME_Telephony` to resolve five core inbound audio, language, and dialogue issues:
1. **Onset Audio Clipping:** Callers' initial words (e.g. *"Haan..."*, *"Bataiye..."*) losing opening phonemes before energy thresholds trigger speech detection.
2. **Premature Utterance Splitting:** Callers pausing naturally (500–800 ms) while thinking or formulating answers being cut off, causing sentence fragmentation and loss of crucial context (such as exam results or rank numbers).
3. **Script Conversion Corruption:** Proper nouns such as `JEE Main` and `JEE Mains` erroneously converting into Hindi first-person pronoun Devanagari (`JEE मैं`), corrupting LLM tokenization and prompt semantics.
4. **Hinglish Domain Vocabulary Gaps:** Common caller phonetic transcriptions (`rijalt`, `raink`, `jayi mens`) missing canonical domain entity normalization before prompt construction.
5. **Streaming Anti-Repetition Bypass:** Greedy LLM decoding producing duplicate opening sentences that immediately entered the streaming TTS queue and Plivo playback before downstream whole-response repetition detectors could trigger.

All five mandatory acceptance criteria have been satisfied with 100% test pass rates. Full test suites (510 unit tests, 7 acceptance tests, and 273 integration tests) confirm zero regressions across the codebase.

---

## 2. Root Cause Analysis & Architecture Fixes

### 2.1. VAD Audio Pre-Roll (`app/ai/ws.py`)
- **Root Cause:** Audio arriving before the energy threshold triggered `MIN_SPEECH_MS` (120 ms) was discarded, clipping speech onsets (plosives, soft consonants, and initial vowels).
- **Implementation:**
  - Integrated a fixed-duration circular buffer using `collections.deque` maintaining ~200 ms of 16-bit 8 kHz / 16 kHz audio frames (`PRE_ROLL_MS = 200`, `ai_vad_pre_roll_ms` configurable).
  - When speech is detected (`speech_ms >= min_speech_ms`), all pre-roll frames are prepended in strict FIFO order before newly incoming speech frames.
  - State flags ensure frames are transferred exactly once per utterance without duplication.
  - The pre-roll buffer is cleanly cleared on turn finalization, barge-in cancellation, or caller silence reset, preventing cross-turn audio bleed.

### 2.2. Adaptive Silence Endpointing (`app/ai/ws.py` & `config/settings.py`)
- **Root Cause:** A rigid, overly aggressive silence threshold (400–600 ms) prematurely finalized utterances when callers paused between thoughts (e.g., *"Haan... [600ms pause] ...mera JEE Main result aa gaya hai"*), cutting off critical dialogue.
- **Implementation:**
  - Implemented dynamic adaptive endpointing via `_required_silence_ms(speech_ms, settings)`:
    - Utterances under 600 ms of active speech are classified as short/starter phrases and granted an extended silence threshold (`ai_silence_short_ms = 1000 ms`, default).
    - Longer, formulated utterances use an 850 ms silence threshold (`ai_silence_ms = 850 ms`).
  - Maximum utterance length is enforced via `ai_max_utterance_ms = 8000 ms` to prevent unbounded audio buffering while accommodating natural Hindi/Hinglish speech cadence.

### 2.3. Domain Entity Script Preservation (`mme_hinglish.py` & `app/ai/script_enforcement.py`)
- **Root Cause:** The phonetic transliteration rule converting `"main"` to `"मैं"` operated globally across token sequences without respecting education terminology, erroneously turning `"JEE Main"` into `"JEE मैं"`.
- **Implementation:**
  - Added token-level masking in `mme_hinglish.py:roman_to_mixed()` using distinct placeholders (`__PROTECTED_PHRASE_{i}__`) for `JEE Main`, `JEE Mains`, `B.Tech`, `M.Tech`, `IELTS`, `TOEFL`, `NEET`, `BITS`, `BITSAT`, and `VITEEE`.
  - Added explicit regex protections in `app/ai/script_enforcement.py:load_allowlist()` and `enforce_clause()` to ensure that even during full-sentence Devanagari enforcement, academic credentials and exam titles remain intact in their canonical English/Roman form while surrounding Hindi words convert properly (e.g., `"JEE Main ke liye main tayyar hoon"` $\rightarrow$ `"JEE Main के लिए मैं तैयार हूँ"`).
  - Added terms to `config/script_allowlist_terms.txt`.

### 2.4. Hinglish Vocabulary & Entity Normalization (`app/ai/glossary_correction.py`)
- **Root Cause:** Inbound speech-to-text transcriptions frequently produce colloquial phonetic variants (`rijalt`, `raink`, `jayi mens`) which downstream prompt templates failed to recognize as admissions entities.
- **Implementation:**
  - Expanded `app/ai/glossary_correction.py:CORRECTION_RULES` with high-confidence regex patterns:
    - `r"\b(?:rijalt|rijalts|rizalt)\b"` $\rightarrow$ `"result"`
    - `r"\b(?:raink|rainnk)\b"` $\rightarrow$ `"rank"`
    - `r"\b(?:je\s+ee\s+men|jee\s+mane|je\s+ee\s+main|jayi\s+mens?|jaye\s+mens?|jayi\s+main)\b"` $\rightarrow$ `"JEE Main"`
  - Added protected tokens (`rank`, `ranks`, `result`, `results`) to `NEVER_REWRITE` to prevent unwanted circular replacements.

### 2.5. Early First-Clause Repetition Guard & Sampling Controls (`app/ai/pipeline.py` & `app/ai/clients.py`)
- **Root Cause:** The pipeline previously generated LLM stream tokens directly into clause segmentation and launched TTS tasks immediately on the first punctuation boundary. Full-response anti-repetition checks only evaluated after the entire stream finished, by which time duplicate opening audio had already been synthesized and dispatched to Plivo. Furthermore, lack of repetition and presence penalties allowed greedy decoding loops.
- **Implementation:**
  - Added `ai_repetition_penalty = 1.15`, `ai_presence_penalty = 0.3`, `ai_frequency_penalty = 0.2`, and `temperature = 0.35` in `config/settings.py` and serialized them into RunPod vLLM and OpenAI payload configurations in `app/ai/clients.py`.
  - Implemented pre-TTS clause verification in `app/ai/pipeline.py:stream_turn()`:
    - Before calling `_launch(sentence)` for the first clause, the clause is normalized and compared against recent assistant turns via `is_repeated_clause()`.
    - If a repetition is detected, the clause is blocked from entering the TTS queue or audio dispatch pipeline.
    - The pipeline attempts a single, bounded 1-shot regeneration with targeted prompt injection directing the LLM to address the caller's specific statement without repeating recent questions.
    - If regeneration fails or times out, a safe, grounded contextual fallback (acknowledging the caller's information) is synthesized.

---

## 3. Mandatory Acceptance Test Verification

All five mandatory acceptance tests were implemented and verified in `tests/unit/test_stt_vad_hinglish_acceptance.py`:

| Test Name | Criterion | Verification Method | Result |
|:---|:---|:---|:---:|
| `test_acceptance_1_vad_preroll_audio_capture` | Pre-roll audio preservation | Injected 200 ms of pre-speech audio frames followed by speech frames into `handle_fs_media`. Verified that the STT payload received the pre-roll frames in exact FIFO order ahead of speech frames without duplicate frames or loss. | **PASSED** |
| `test_acceptance_2_natural_pause_800ms_no_split` | 800 ms natural pause tolerance | Injected speech frames, followed by 800 ms of silence (well within `ai_silence_short_ms = 1000 ms`), followed by continuation speech frames. Verified that the turn was not finalized prematurely and all frames formed a single utterance. | **PASSED** |
| `test_acceptance_3_hinglish_jee_result_rank_understanding` | Recognition of result and rank | Fed caller transcript `"rijalt aa chuka hai meri raink ek laakh ke aaspaas hai"`. Verified glossary correction normalized it to `"result aa chuka hai meri rank ek laakh ke aaspaas hai"` and the conversational brain acknowledged the result/rank without asking if the result had arrived. | **PASSED** |
| `test_acceptance_4_script_conversion_jee_main_preservation` | Script conversion integrity | Transliterated `"JEE Main ke liye main taiyar hoon"`. Verified output yielded `"JEE Main के लिए मैं तैयार हूँ"`—specifically confirming `JEE Main` was preserved and did not become `JEE मैं`, while Hindi pronoun `main` became `मैं`. | **PASSED** |
| `test_acceptance_5_early_repetition_interception_before_tts` | Pre-TTS clause interception | Mocked LLM generating an exact duplicate of the prior assistant turn as its first clause. Verified `_launch()` was prevented from synthesizing or enqueuing the duplicate clause, and the repetition guard triggered regeneration/fallback. | **PASSED** |

### Additional Unit Tests in Acceptance Suite
- `test_llm_sampling_parameters_serialized`: Confirms `repetition_penalty`, `presence_penalty`, and `frequency_penalty` are properly passed in RunPod vLLM and OpenAI payload bodies.
- `test_glossary_correction_jee_main_result_rank`: Confirms domain regex corrections accurately map phonetic variants while preserving surrounding context.

---

## 4. Test Suite Execution Summary

- **Acceptance Suite:** `tests/unit/test_stt_vad_hinglish_acceptance.py`  
  **7 passed** in 1.48s (100% pass rate).
- **Unit Test Suite:** `pytest tests/unit/`  
  **510 passed, 1 skipped, 0 failed** in 43.72s.
- **Integration Test Suite:** `pytest tests/integration/`  
  **273 passed, 0 failed** in 132.84s.
- **Overall Suite Status:** **783 passing tests, 0 failures, 1 intentional skip**.

---

## 5. Safety & Operational Boundary Compliance

In accordance with strict operational and safety protocols:
1. **Zero Real PSTN Calls:** No telephone calls were placed via Plivo, FreeSWITCH, or PSTN carriers.
2. **Zero Provider API Triggers:** No outbound REST requests were sent to real Plivo or RunPod endpoints.
3. **Zero Paid GPU Inference:** All verification utilized deterministic local mock transports and synthetic unit tests without incurring GPU billing.
4. **Preserved Working Tree:** All pre-existing modified and untracked files, previous TTS failure-recovery mechanisms, and analytical audit artifacts remain intact and uncommitted.

