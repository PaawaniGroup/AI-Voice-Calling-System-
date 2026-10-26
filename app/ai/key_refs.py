"""Secret Reference Lookup for Tenant AI Configuration.

Allows tenant configuration in MongoDB to specify indirect environment variable names
rather than storing raw plaintext RunPod API keys in the database.

Example in ai_config document:
{
    "tenant_id": "mme-test",
    "key_refs": {
        "stt": "MME_TEL_KEY_STT",
        "brain": "MME_TEL_KEY_BRAIN",
        "tts": "MME_TEL_KEY_TTS"
    }
}

Precedence:
1. Indirect reference in key_refs (environment variable lookup)
2. Direct key in keys dict (legacy compatibility)
3. Direct key in {service}_api_key (legacy fallback)
4. None (if unconfigured)
"""
from __future__ import annotations

import os
from typing import Any


def resolve_service_key(cfg: dict[str, Any], service: str) -> str | None:
    """Resolve API key for a model service without exposing or logging it."""
    svc = service.lower()
    
    # 1. Indirect environment reference
    key_refs = cfg.get("key_refs")
    if isinstance(key_refs, dict) and key_refs.get(svc):
        env_var_name = key_refs[svc]
        val = os.environ.get(env_var_name)
        if val:
            return val.strip()

    # 2. Legacy direct keys dictionary
    keys = cfg.get("keys")
    if isinstance(keys, dict) and keys.get(svc):
        return str(keys[svc]).strip()

    # 3. Direct service_api_key
    legacy_direct = cfg.get(f"{svc}_api_key")
    if legacy_direct:
        return str(legacy_direct).strip()

    return None


def mask_key(key: str | None) -> str:
    """Mask key for safe logging/backups, showing only last 4 characters."""
    if not key:
        return "[none]"
    k = str(key).strip()
    if len(k) <= 4:
        return "[masked]"
    return f"...{k[-4:]}"
