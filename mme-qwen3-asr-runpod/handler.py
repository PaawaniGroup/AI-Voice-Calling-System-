"""RunPod Serverless handler for Qwen3-ASR-1.7B. Model is loaded once at worker start and reused."""
from __future__ import annotations

import signal
import sys
import time
import uuid

from app import audio_preprocessor, language_detector, transcript_normalizer
from app.response_schema import AsrError, error_response, success_response

MAX_SECONDS = 60.0
TARGET_SR = 16000
PROTECTED_TERMS = ["BTech", "MTech", "BBA", "MBA", "BCA", "MCA", "Computer Science"]
_engine = None
_shutting_down = False


def get_engine():
    global _engine
    if _engine is None:
        from app.asr_engine import from_env
        _engine = from_env()
    return _engine


def process(job_input: dict, engine=None) -> dict:
    t_req = time.perf_counter()
    rid = str(job_input.get("request_id") or uuid.uuid4())
    try:
        if _shutting_down:
            raise AsrError("shutting_down", "worker is shutting down")
        if not isinstance(job_input, dict):
            raise AsrError("invalid_request", "input must be an object")
        try:
            lang = language_detector.request_language_to_model(job_input.get("language", "auto"))
        except ValueError as e:
            raise AsrError("unsupported_language", str(e))
        samples, meta = audio_preprocessor.preprocess(
            job_input.get("audio_base64"), job_input.get("audio_format", "wav"), job_input.get("sample_rate"),
            TARGET_SR, MAX_SECONDS)
        eng = engine or get_engine()
        t_pre = (time.perf_counter() - t_req) * 1000
        lang_name, raw, infer_ms = eng.transcribe(samples, TARGET_SR, lang)
        out = {"request_id": rid, "language": language_detector.model_language_to_iso(lang_name),
               "language_name": lang_name, "language_forced": lang is not None, **meta,
               "inference_time_ms": round(infer_ms, 3), "preprocess_time_ms": round(t_pre, 3),
               "real_time_factor": round(infer_ms / meta["audio_duration_ms"], 4) if meta["audio_duration_ms"] else None,
               "gpu_peak_memory_mb": eng.gpu_memory_mb() if hasattr(eng, "gpu_memory_mb") else None}
        if job_input.get("return_raw_text", True):
            out["raw_text"] = raw
        if job_input.get("return_normalized_text", True):
            norm, report = transcript_normalizer.normalize(raw, PROTECTED_TERMS)
            out["normalized_text"], out["normalization"] = norm, report
        out["total_time_ms"] = round((time.perf_counter() - t_req) * 1000, 3)
        return success_response(**out)
    except AsrError as e:
        return {**error_response(e.code, e.message), "request_id": rid}
    except Exception as e:  # noqa: BLE001
        return {**error_response("internal_error", type(e).__name__), "request_id": rid}


def handler(job):
    return process((job or {}).get("input") or {})


def _graceful(signum, _frame):
    global _shutting_down
    _shutting_down = True
    sys.exit(0)


if __name__ == "__main__":
    import runpod
    signal.signal(signal.SIGTERM, _graceful)
    get_engine()  # load model before accepting jobs; fails fast if CUDA missing
    runpod.serverless.start({"handler": handler})
