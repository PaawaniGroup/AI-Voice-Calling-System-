"""Structured response/error helpers. All timings are measured by callers, never hardcoded."""
from __future__ import annotations

from typing import Any

MODEL_NAME = "Qwen/Qwen3-ASR-1.7B"


class AsrError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def error_response(code: str, message: str) -> dict[str, Any]:
    return {"status": "error", "model": MODEL_NAME, "error": {"code": code, "message": message}}


def success_response(**kw: Any) -> dict[str, Any]:
    return {"status": "success", "model": MODEL_NAME, **kw}
