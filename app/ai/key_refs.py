"""Secret Reference Lookup for Tenant AI Configuration.

Allows tenant configuration in MongoDB to specify indirect environment variable names
rather than storing raw plaintext RunPod API keys in the database.
"""
from __future__ import annotations

import os
from typing import Any

# Standard environment variable name per service (one variable name per service)
SERVICE_KEY_ENV_VARS: dict[str, str] = {
    "stt": "RUNPOD_STT_KEY",
    "brain": "RUNPOD_BRAIN_KEY",
    "tts": "RUNPOD_TTS_KEY",
    "rag": "RUNPOD_RAG_KEY",
}


def mask_key(key: str | None) -> str:
    """Mask key for safe logging/backups, showing only last 4 characters."""
    if not key:
        return "[none]"
    k = str(key).strip()
    if len(k) <= 4:
        return "[masked]"
    return f"...{k[-4:]}"
