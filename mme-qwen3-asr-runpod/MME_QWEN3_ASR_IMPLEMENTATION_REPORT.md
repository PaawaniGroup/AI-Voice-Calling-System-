# MME Qwen3-ASR-1.7B: Implementation Report (Phases 1-3)

Status: worker code written and unit-tested on CPU with a mocked model. Nothing here is GPU or live validation.

## Verified upstream facts (README + HF model card, fetched 2026-11-10)
- License: Apache-2.0. Package: `qwen-asr` (PyPI). Python 3.12 recommended; torch/transformers pins not stated upstream.
- API used: `Qwen3ASRModel.from_pretrained(id, dtype, device_map, max_inference_batch_size, max_new_tokens)` and
  `model.transcribe(audio=(np.ndarray, sr), language=None|"Hindi"|"English")` -> list of results with `.language`, `.text`.
- `language=None` = automatic language identification. Language is passed as English names; ISO codes are converted in `app/language_detector.py`.
- Hindi and English are supported. Streaming is documented for the vLLM backend only; the Python streaming API is not
  specified, so **streaming is not implemented**.
- Not stated upstream: input sample rate for the Python API (we feed 16 kHz), max audio duration, VRAM, model revision SHA.
  The revision is therefore `UNPINNED` in `config/model_config.yaml`; the Docker image sets `QWEN_ASR_REQUIRE_PINNED=1`,
  so the worker refuses to start until a real SHA is pinned.

## Current state of the Utkarsh voice (from repo docs, NOT re-verified live)
`MME_UTKARSH_LIVE_DEPLOYMENT_REPORT.md` claims `maya4567463/mme-maya2-native:v1-utkarsh` on `wvjddk8460ld96` lists `mme-test__utkarsh`.
That is a claim in a document. It must be confirmed with a live `GET /v1/voices` before any end-to-end test.

## Architecture
```
Caller audio (8k mu-law/WAV)
   -> [NEW] RunPod Serverless: Qwen3-ASR-1.7B worker  (baseline: Whisper npfv0xgfbxhsy0, untouched)
        raw_text  -> normalizer -> normalized_text
   -> Brain Qwen3-30B-A3B (o8t9mvltiq9mrb)
   -> Maya 2 TTS (wvjddk8460ld96), voice mme-test__utkarsh -> Perth watermark
   -> audio out
```

## What was built (`mme-qwen3-asr-runpod/`)
| File | Purpose |
|---|---|
| `handler.py` | RunPod handler; model loaded once at start; SIGTERM graceful shutdown; structured errors |
| `app/asr_engine.py` | Official qwen-asr loader; CUDA required; pinned-revision guard; per-process inference lock |
| `app/audio_preprocessor.py` | WAV/PCM16/mu-law decode, mono downmix, real polyphase resample to 16 kHz, duration/size limits |
| `app/transcript_normalizer.py` | NFC, whitespace, danda, protected-term casing only. No transliteration, translation or word changes |
| `app/language_detector.py`, `app/response_schema.py` | language mapping, response contract |
| `Dockerfile` | CUDA/PyTorch base, non-root, no weights or secrets baked in |
| `tests/test_worker.py` | 21 tests |

Weights are downloaded at worker init (not baked in): small image and simple updates, at the cost of a cold start that
includes the download. A model cache on a Network Volume would shorten it but needs separate approval.

## Known gaps
- Normalizer does not transliterate Roman-script Hindi to Devanagari. The example in the brief ("मेरा नाम Utkarsh है")
  depends on what the model emits; this must be measured on real audio, and a verified lexicon-based step added if needed.
- Timing: `inference_time_ms`, `real_time_factor`, `preprocess_time_ms`, `total_time_ms` are measured with `perf_counter`.
  Mocked-model timings are meaningless.
- `requirements.txt` pins are minimums; freeze exact versions from the first GPU-validated build.
- Not written yet: `evaluation/benchmark.py`, `metrics.py`, the 20/600-utterance manifest, and the MME-side feature-flagged
  Qwen STT client. These need the labelled audio set (private caller audio must not enter git).

## Test result
`pytest tests` in a clean venv: 21 passed (CPU, mocked model).
