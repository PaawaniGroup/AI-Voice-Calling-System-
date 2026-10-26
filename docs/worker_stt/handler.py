"""STT worker, test version: handler (7) plus
  * a per-request `language` ("hi" | "en" | "auto"; absent = the worker's WHISPER_LANGUAGE default),
  * an optional per-request `prompt` (at most 200 characters) given to the decoder through prompt_ids (UNVERIFIED with this model: see README.md),
  * a startup log line with the default language and the open options,
  * `audio_s` in the timing.
Everything else is handler (7): audio_b64 (WAV) or audio_pcm16_b64 + sample_rate, health, warmup. Never logs the transcript, the prompt or the audio.
Test-only: build it ONLY for a NEW test endpoint (README.md). Pure logic lives in stt_logic.py (self-tested without torch)."""
import base64
import io
import os
import time
import traceback

import numpy as np
import runpod
import soundfile as sf
import torch
from scipy.signal import resample_poly
from transformers import pipeline

import stt_logic

MODEL_ID = os.getenv("MODEL_ID", "mmeog/mme_stt_model")
MODEL_REVISION = os.getenv("MODEL_REVISION", "")
CHUNK_LENGTH_S = int(os.getenv("CHUNK_LENGTH_S", "0"))
WHISPER_LANGUAGE = stt_logic.parse_language(os.getenv("WHISPER_LANGUAGE", ""))          # the default for requests that name no language
ALLOW_REQUEST_LANGUAGE = os.getenv("ALLOW_REQUEST_LANGUAGE", "1") == "1"
ALLOW_PROMPT = os.getenv("ALLOW_PROMPT", "1") == "1"
TARGET_SR = 16000
USE_CUDA = torch.cuda.is_available()


def log(msg):
    print(msg, flush=True)


def build_pipeline():
    kwargs = {"model": MODEL_ID, "device": 0 if USE_CUDA else -1, "dtype": torch.float16 if USE_CUDA else torch.float32}
    if MODEL_REVISION:
        kwargs["revision"] = MODEL_REVISION
    if CHUNK_LENGTH_S > 0:
        kwargs["chunk_length_s"] = CHUNK_LENGTH_S
    return pipeline("automatic-speech-recognition", **kwargs)


BASE_GENERATE_KWARGS = {"task": "transcribe", "num_beams": 1, "do_sample": False}

_t = time.perf_counter()
ASR = build_pipeline()
log(f"[STT] model loaded in {time.perf_counter() - _t:.2f}s")
log(stt_logic.startup_line(MODEL_ID, WHISPER_LANGUAGE, ALLOW_REQUEST_LANGUAGE, ALLOW_PROMPT, USE_CUDA))


def prompt_ids_for(prompt: str):
    """Token ids of the hint, or None when this model/transformers version cannot take them (the request then runs without a hint)."""
    if not prompt:
        return None
    try:
        ids = ASR.tokenizer.get_prompt_ids(prompt, return_tensors="pt")
        return ids.to(ASR.device) if hasattr(ids, "to") else ids
    except Exception as exc:  # UNVERIFIED with this fine-tuned model: degrade, do not fail the call
        log(f"[STT] prompt_ids unavailable: {type(exc).__name__}")
        return None


def transcribe(audio: np.ndarray, language: str, prompt: str):
    ids = prompt_ids_for(prompt)
    gk = stt_logic.build_generate_kwargs(BASE_GENERATE_KWARGS, language, ids)
    with torch.inference_mode():
        try:
            result = ASR({"raw": audio, "sampling_rate": TARGET_SR}, generate_kwargs=gk)
        except Exception as exc:
            if ids is None:
                raise
            log(f"[STT] decode with prompt failed ({type(exc).__name__}): retrying without it")
            ids = None
            result = ASR({"raw": audio, "sampling_rate": TARGET_SR}, generate_kwargs=stt_logic.build_generate_kwargs(BASE_GENERATE_KWARGS, language, None))
    if USE_CUDA:
        torch.cuda.synchronize()
    return result["text"].strip(), ids is not None


_t = time.perf_counter()
_warm = (np.random.randn(TARGET_SR * 3) * 0.005).astype(np.float32)
for _ in range(2):
    try:
        transcribe(_warm, WHISPER_LANGUAGE, "")
    except Exception as exc:
        log(f"[STT] warmup error (ignored): {exc}")
log(f"[STT] warmup done in {time.perf_counter() - _t:.2f}s")


def load_audio(data):
    """float32 mono 16 kHz audio from WAV (audio_b64) or raw PCM16 (audio_pcm16_b64 + sample_rate)."""
    if data.get("audio_pcm16_b64"):
        raw = stt_logic.decode_pcm16(data["audio_pcm16_b64"])
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
    audio_s = stt_logic.audio_seconds(audio.shape[0], sample_rate)
    if sample_rate != TARGET_SR:
        audio = resample_poly(audio, TARGET_SR, sample_rate).astype(np.float32)
    return np.ascontiguousarray(audio, dtype=np.float32), audio_s


def handler(job):
    data = job.get("input") or {}
    if data.get("warmup"):
        return {"ok": True}
    task = data.get("task", "stt")
    if task == "health":
        return {"status": "ok", "model": MODEL_ID, "loaded": True, "cuda_available": USE_CUDA, "default_language": WHISPER_LANGUAGE or "auto",
                "request_language": ALLOW_REQUEST_LANGUAGE, "prompt": ALLOW_PROMPT}
    if task != "stt":
        raise ValueError("Supported tasks: health, stt")

    total_start = time.perf_counter()
    try:
        language = stt_logic.parse_language(data.get("language"), WHISPER_LANGUAGE) if ALLOW_REQUEST_LANGUAGE else WHISPER_LANGUAGE
        prompt = stt_logic.parse_prompt(data.get("prompt")) if ALLOW_PROMPT else ""
        t0 = time.perf_counter()
        audio, audio_s = load_audio(data)
        preprocess_s = time.perf_counter() - t0
        t0 = time.perf_counter()
        text, prompt_used = transcribe(audio, language, prompt)
        inference_s = time.perf_counter() - t0
        total_s = time.perf_counter() - total_start
        log(f"[STT_TIMING] audio_s={audio_s:.2f} preprocess_s={preprocess_s:.3f} inference_s={inference_s:.3f} total_s={total_s:.3f} "
            f"text_chars={len(text)} language={language or 'auto'} prompt_chars={len(prompt)} prompt_used={prompt_used}")
        return {"text": text, "model": MODEL_ID, "language": language or "auto", "prompt_used": prompt_used,
                "timing": stt_logic.timing(inference_s, total_s, audio_s)}
    except Exception as exc:
        log(f"STT inference error: {type(exc).__name__}")
        traceback.print_exc()
        raise


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
