"""Regression tests for key_refs preservation in get_config and strict no-fallback."""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock
import pytest

from config.settings import Settings
from app.ai.service import AiService
from app.ai.clients import ModelClients, AiUnavailable


@pytest.mark.asyncio
async def test_get_config_preserves_key_refs():
    """Verify that get_config preserves key_refs from MongoDB.
    
    On the old implementation, get_config only iterated over:
    ("models", "timeouts", "paths", "keys")
    which silently dropped key_refs.
    """
    s = Settings()
    tenant_doc = {
        "tenant_id": "mme-test",
        "key_refs": {
            "stt": "RUNPOD_STT_KEY",
            "brain": "RUNPOD_BRAIN_KEY",
            "tts": "RUNPOD_TTS_KEY",
        },
    }

    # 1. Old get_config logic simulation: demonstrates that the old allowlist dropped key_refs
    old_default = {"paths": {}, "keys": {}, "languages": ["en"], "default_language": "auto"}
    old_cfg = dict(old_default)
    for k in ("models", "timeouts", "paths", "keys"):
        old_cfg[k] = {**old_cfg.get(k, {}), **tenant_doc.get(k, {})}
    assert "key_refs" not in old_cfg, "Old implementation dropped key_refs"

    # 2. Real AiService.get_config with the fix
    mock_db = MagicMock()
    mock_coll = MagicMock()
    mock_coll.find_one = AsyncMock(return_value=dict(tenant_doc))
    mock_db.__getitem__.return_value = mock_coll

    svc = AiService(mock_db, None, s, None)
    cfg = await svc.get_config("mme-test")
    assert "key_refs" in cfg
    assert cfg["key_refs"] == {
        "stt": "RUNPOD_STT_KEY",
        "brain": "RUNPOD_BRAIN_KEY",
        "tts": "RUNPOD_TTS_KEY",
    }


def test_tenant_with_key_refs_never_falls_back_to_stored_key(monkeypatch):
    """Verify that when key_refs is set for a service, ModelClients NEVER falls back
    to a stored plain-text key in MongoDB or global ai_api_key.
    """
    s = Settings()
    cfg = {
        "key_refs": {"tts": "RUNPOD_TTS_KEY"},
        "keys": {"tts": "rpa_FAKE_STORED_INVALID_KEY_12345"},
        "tts_api_key": "rpa_FAKE_LEGACY_DIRECT_KEY_67890",
    }
    cl = ModelClients(s, cfg)

    # Case A: When the environment variable is set, it uses the environment key, NOT stored key
    monkeypatch.setenv("RUNPOD_TTS_KEY", "rpa_FAKE_ENV_KEY_VALID_0001")
    resolved = cl.resolve_key("tts")
    assert resolved == "rpa_FAKE_ENV_KEY_VALID_0001"
    assert resolved != cfg["keys"]["tts"]
    assert resolved != cfg["tts_api_key"]

    auth_hdrs = cl._auth_headers("tts")
    assert auth_hdrs == {"Authorization": "Bearer rpa_FAKE_ENV_KEY_VALID_0001"}

    # Case B: When the environment variable is missing / empty, it MUST raise AiUnavailable("missing_key_ref")
    # and MUST NOT fall back to cfg["keys"]["tts"] or cfg["tts_api_key"]
    monkeypatch.delenv("RUNPOD_TTS_KEY", raising=False)
    with pytest.raises(AiUnavailable) as exc_info:
        cl.resolve_key("tts")
    assert exc_info.value.kind == "missing_key_ref"

    with pytest.raises(AiUnavailable) as exc_info_auth:
        cl._auth_headers("tts")
    assert exc_info_auth.value.kind == "missing_key_ref"
