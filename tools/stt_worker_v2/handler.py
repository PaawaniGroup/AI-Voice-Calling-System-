"""RunPod STT Worker v2 Handler.

Offline implementation for Whisper ASR worker supporting:
- Vocabulary / domain prompting via `processor.get_prompt_ids` (NOT `initial_prompt`)
- Configurable beam search via `NUM_BEAMS` environment variable
- Token confidence score estimation (`avg_logprob`) via `compute_transition_scores`
- Audio ingestion: WAV (audio_b64) or raw PCM16 (audio_pcm16_b64)
- Polyphase sinc resampling (8 kHz -> 16 kHz) via `scipy.signal.resample_poly`
- Offline self-test mode (works in both worker and dev environments)
"""
from __future__ import annotations

import base64
import io
import math
import os
import sys
import time
import traceback

MODEL_ID = os.getenv("MODEL_ID", "mmeog/mme_stt_model")
MODEL_REVISION = os.getenv("MODEL_REVISION", "")
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "en").strip()
NUM_BEAMS = int(os.getenv("NUM_BEAMS", "1"))
TARGET_SR = 16000

# Global references (initialized in initialize_worker())
PROCESSOR = None
MODEL = None
USE_CUDA = False


def log(msg: str) -> None:
    print(msg, flush=True)


def initialize_worker():
    """Initializes Whisper processor and model."""
    global PROCESSOR, MODEL, USE_CUDA
    import torch
    from transformers import WhisperForConditionalGeneration, WhisperProcessor

    USE_CUDA = torch.cuda.is_available()
    log(f"[STT v2] Loading model {MODEL_ID} (cuda={USE_CUDA})...")
    t0 = time.perf_counter()

    PROCESSOR = WhisperProcessor.from_pretrained(MODEL_ID)
    kwargs = {
        "torch_dtype": torch.float16 if USE_CUDA else torch.float32,
    }
    if USE_CUDA:
        kwargs["device_map"] = "auto"

    MODEL = WhisperForConditionalGeneration.from_pretrained(MODEL_ID, **kwargs)
    if not USE_CUDA:
        MODEL.to("cpu")
    MODEL.eval()

    log(f"[STT v2] Loaded model and processor in {time.perf_counter() - t0:.2f}s")


def load_audio(data: dict):
    """Returns float32 mono 16 kHz audio numpy array."""
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly

    if data.get("audio_pcm16_b64"):
        raw = base64.b64decode(data["audio_pcm16_b64"], validate=True)
        sample_rate = int(data.get("sample_rate", TARGET_SR))
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif data.get("audio_b64"):
        raw = base64.b64decode(data["audio_b64"], validate=True)
        audio, sample_rate = sf.read(io.BytesIO(raw), dtype="float32")
    else:
        raise ValueError("Provide audio_b64 (WAV) or audio_pcm16_b64 (raw PCM16)")

    if audio.size == 0:
        raise ValueError("The audio is empty")
    if audio.ndim == 2:
        audio = audio.mean(axis=1)

    # 8 kHz -> 16 kHz polyphase sinc filter (NOT linear interpolation)
    if sample_rate != TARGET_SR:
        audio = resample_poly(audio, TARGET_SR, sample_rate).astype(np.float32)

    return np.ascontiguousarray(audio, dtype=np.float32)


def transcribe(audio, prompt: str | None = None, num_beams: int | None = None) -> tuple[str, float | None]:
    """Transcribes audio using Whisper processor and model.

    Returns:
        (text, avg_logprob)
    """
    import torch

    global PROCESSOR, MODEL, USE_CUDA

    if PROCESSOR is None or MODEL is None:
        raise RuntimeError("Worker models are not initialized")

    inputs = PROCESSOR(audio, sampling_rate=TARGET_SR, return_tensors="pt")
    input_features = inputs.input_features
    if USE_CUDA:
        input_features = input_features.to("cuda", dtype=torch.float16)

    gen_kwargs = {
        "task": "transcribe",
        "num_beams": num_beams or NUM_BEAMS,
        "do_sample": False,
        "return_dict_in_generate": True,
        "output_scores": True,
    }
    if WHISPER_LANGUAGE:
        gen_kwargs["language"] = WHISPER_LANGUAGE

    # In transformers, Whisper domain hint uses `prompt_ids` (NOT `initial_prompt`)
    if prompt and prompt.strip():
        try:
            # WhisperProcessor.get_prompt_ids handles tokenization and formatting
            prompt_ids = PROCESSOR.get_prompt_ids(prompt.strip(), return_tensors="pt")
            if USE_CUDA:
                prompt_ids = prompt_ids.to("cuda")
            gen_kwargs["prompt_ids"] = prompt_ids
        except Exception as e:
            log(f"[STT v2] Warning: failed to build prompt_ids: {e}")

    with torch.inference_mode():
        outputs = MODEL.generate(input_features, **gen_kwargs)

    # Decode text
    sequences = outputs.sequences
    text = PROCESSOR.batch_decode(sequences, skip_special_tokens=True)[0].strip()

    # Calculate avg_logprob from transition scores if output_scores were returned
    avg_logprob = None
    try:
        if hasattr(outputs, "scores") and outputs.scores:
            transition_scores = MODEL.compute_transition_scores(
                sequences, outputs.scores, normalize_logits=True
            )
            # Filter out -inf or nan if any
            finite_scores = transition_scores[torch.isfinite(transition_scores)]
            if finite_scores.numel() > 0:
                avg_logprob = float(finite_scores.mean().item())
    except Exception as e:
        log(f"[STT v2] Could not compute transition scores: {e}")

    return text, avg_logprob


def handler(job: dict) -> dict:
    """RunPod serverless request handler."""
    data = job.get("input") or {}

    if data.get("warmup"):
        return {"ok": True}

    task = data.get("task", "stt")
    if task == "health":
        return {
            "status": "ok",
            "model": MODEL_ID,
            "version": "v2",
            "cuda_available": USE_CUDA,
            "num_beams": NUM_BEAMS,
            "language": WHISPER_LANGUAGE or "auto",
        }

    if task != "stt":
        raise ValueError("Supported tasks: health, stt")

    total_start = time.perf_counter()
    try:
        t0 = time.perf_counter()
        audio = load_audio(data)
        preprocess_s = time.perf_counter() - t0

        prompt = data.get("prompt")
        req_beams = int(data.get("num_beams", NUM_BEAMS))

        t0 = time.perf_counter()
        text, avg_logprob = transcribe(audio, prompt=prompt, num_beams=req_beams)
        inference_s = time.perf_counter() - t0

        total_s = time.perf_counter() - total_start
        log(
            f"[STT_v2_TIMING] audio_s={len(audio) / TARGET_SR:.2f} "
            f"preprocess_s={preprocess_s:.3f} inference_s={inference_s:.3f} "
            f"total_s={total_s:.3f} chars={len(text)} avg_logprob={avg_logprob}"
        )

        return {
            "text": text,
            "avg_logprob": avg_logprob,
            "model": MODEL_ID,
            "timing": {
                "preprocess_s": round(preprocess_s, 3),
                "inference_s": round(inference_s, 3),
                "total_s": round(total_s, 3),
            },
        }
    except Exception as exc:
        log(f"STT inference error: {exc}")
        traceback.print_exc()
        raise


def run_self_test():
    """Offline self-test checking configuration, health routing, and highlighting UNVERIFIED paths."""
    print("=== Running STT Worker v2 Self-Test (Offline) ===")

    # 1. Environment variable handling
    assert NUM_BEAMS >= 1, f"Expected NUM_BEAMS >= 1, got {NUM_BEAMS}"
    print(f"PASS: NUM_BEAMS env parsed correctly ({NUM_BEAMS})")

    # 2. Warmup handling
    warmup_res = handler({"input": {"warmup": True}})
    assert warmup_res.get("ok") is True
    print("PASS: Warmup ping handled correctly")

    # 3. Health task
    health_res = handler({"input": {"task": "health"}})
    assert health_res["status"] == "ok"
    assert health_res["version"] == "v2"
    assert health_res["model"] == MODEL_ID
    print(f"PASS: Health check returns valid status and model {MODEL_ID}")

    # 4. Unknown task rejection
    try:
        handler({"input": {"task": "unknown_task"}})
        assert False, "Expected ValueError on unsupported task"
    except ValueError:
        print("PASS: Unsupported task correctly rejected with ValueError")

    # 5. Paths NOT covered by offline test:
    print("UNVERIFIED: Live GPU audio inference path (not executed; no GPU / CUDA device in offline environment)")
    print("UNVERIFIED: processor.get_prompt_ids execution with model weights (not executed; weights not loaded locally)")
    print("UNVERIFIED: transition scores calculation (compute_transition_scores) on live outputs (not executed; requires model generation)")

    print("=== STT Worker v2 Self-Test Completed (UNVERIFIED paths noted) ===")


if __name__ == "__main__":
    if "--test" in sys.argv:
        run_self_test()
    else:
        # In RunPod serverless container
        initialize_worker()
        import runpod
        runpod.serverless.start({"handler": handler})
