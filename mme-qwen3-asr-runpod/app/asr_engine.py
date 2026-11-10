"""Qwen3-ASR engine: loads the official qwen-asr model once per process and reuses it.

Only the documented offline transformers API is used (Qwen3ASRModel.from_pretrained / transcribe).
Streaming is intentionally NOT implemented: upstream documents it for the vLLM backend only and the Python streaming
API is not specified in the README.
"""
from __future__ import annotations

import os
import threading
import time

from .response_schema import AsrError


class AsrEngine:
    def __init__(self, model_id: str, revision: str, dtype: str = "bfloat16", max_new_tokens: int = 256,
                 backend=None, require_cuda: bool = True, require_pinned: bool = False):
        self.model_id, self.revision = model_id, revision
        self.max_new_tokens = max_new_tokens
        self._lock = threading.Lock()          # one GPU inference at a time per worker
        self.load_ms = 0.0
        self.device = "unknown"
        if require_pinned and revision in ("", "UNPINNED"):
            raise RuntimeError("model revision is not pinned")
        if backend is not None:                # injected (tests)
            self._model = backend
            self.device = "injected"
            return
        t0 = time.perf_counter()
        import torch
        if require_cuda and not torch.cuda.is_available():
            raise RuntimeError("CUDA is not available; refusing to start (QWEN_ASR_REQUIRE_CUDA=1)")
        from qwen_asr import Qwen3ASRModel
        self.device = f"cuda:0 ({torch.cuda.get_device_name(0)})" if torch.cuda.is_available() else "cpu"
        kwargs = dict(dtype=getattr(torch, dtype), device_map="cuda:0" if torch.cuda.is_available() else "cpu",
                      max_inference_batch_size=1, max_new_tokens=max_new_tokens)
        if revision not in ("", "UNPINNED"):
            kwargs["revision"] = revision      # forwarded to HF from_pretrained
        self._model = Qwen3ASRModel.from_pretrained(model_id, **kwargs)
        self.load_ms = (time.perf_counter() - t0) * 1000

    def transcribe(self, samples, sample_rate: int, language: str | None):
        """Returns (model_language_name, raw_text, inference_ms)."""
        with self._lock:
            t0 = time.perf_counter()
            try:
                res = self._model.transcribe(audio=(samples, sample_rate), language=language)
            except Exception as e:  # noqa: BLE001 - surfaced as structured error
                raise AsrError("inference_failed", f"{type(e).__name__}: {e}"[:300])
            ms = (time.perf_counter() - t0) * 1000
        if not res:
            raise AsrError("inference_failed", "model returned no result")
        return getattr(res[0], "language", None), (getattr(res[0], "text", "") or ""), ms

    def gpu_memory_mb(self) -> float | None:
        try:
            import torch
            return torch.cuda.max_memory_allocated() / 1e6 if torch.cuda.is_available() else None
        except Exception:  # noqa: BLE001
            return None


def from_env(backend=None) -> AsrEngine:
    import yaml
    cfg_path = os.path.join(os.path.dirname(__file__), "..", "config", "model_config.yaml")
    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return AsrEngine(os.getenv("QWEN_ASR_MODEL", cfg["model_id"]), os.getenv("QWEN_ASR_REVISION", cfg["model_revision"]),
                     cfg["dtype"], int(cfg["max_new_tokens"]), backend=backend,
                     require_cuda=os.getenv("QWEN_ASR_REQUIRE_CUDA", "1") == "1",
                     require_pinned=os.getenv("QWEN_ASR_REQUIRE_PINNED", "0") == "1")
