"""Offline unit tests for key_refs wiring in ModelClients on turn-cancel worktree.

Tests required:
1. stored key still works without a ref
2. ref present (resolves from env var)
3. ref set, variable missing (raises AiUnavailable with explicit reason, NO fallback)
4. cross-service never (service A never resolves to service B's key)
5. no key in any message or log
"""
import os
import pytest
from pydantic import SecretStr

from app.ai.clients import ModelClients, AiUnavailable
from config.settings import Settings


def make_clients(cfg: dict, global_key: str | None = None) -> ModelClients:
    s = Settings()
    if global_key:
        s.ai_api_key = SecretStr(global_key)
    else:
        s.ai_api_key = None
    return ModelClients(s, cfg)


def test_stored_key_still_works_without_ref():
    """Stored key works normally when key_refs is absent."""
    cfg = {
        "keys": {"tts": "fake-stored-tts-key", "stt": "fake-stored-stt-key"}
    }
    clients = make_clients(cfg)
    assert clients.resolve_key("tts") == "fake-stored-tts-key"
    assert clients._auth_headers("tts") == {"Authorization": "Bearer fake-stored-tts-key"}
    assert clients.resolve_key("stt") == "fake-stored-stt-key"


def test_ref_present(monkeypatch):
    """Ref present resolves environment variable value, overriding stored key."""
    monkeypatch.setenv("RUNPOD_TTS_KEY", "env-tts-secret-value")
    cfg = {
        "key_refs": {"tts": "RUNPOD_TTS_KEY"},
        "keys": {"tts": "stored-tts-key-fallback"}
    }
    clients = make_clients(cfg)
    assert clients.resolve_key("tts") == "env-tts-secret-value"
    assert clients._auth_headers("tts") == {"Authorization": "Bearer env-tts-secret-value"}


def test_ref_set_variable_missing(monkeypatch):
    """When ref is set and variable is missing, raises AiUnavailable with explicit reason and NO fallback."""
    monkeypatch.delenv("RUNPOD_TTS_KEY", raising=False)
    cfg = {
        "key_refs": {"tts": "RUNPOD_TTS_KEY"},
        "keys": {"tts": "stored-tts-key-that-must-never-be-used"},
    }
    clients = make_clients(cfg, global_key="global-key-that-must-never-be-used")
    
    with pytest.raises(AiUnavailable) as exc_info:
        clients.resolve_key("tts")
    
    err = exc_info.value
    assert err.service == "tts"
    assert err.kind == "missing_key_ref"
    assert "RUNPOD_TTS_KEY" in err.detail
    assert "not set or empty" in err.detail

    with pytest.raises(AiUnavailable):
        clients._auth_headers("tts")


def test_cross_service_never():
    """Never use another service's key."""
    cfg = {
        "keys": {
            "brain": "brain-secret-key",
            "stt": "stt-secret-key"
        }
    }
    clients = make_clients(cfg, global_key=None)
    assert clients.resolve_key("tts") is None
    assert clients._auth_headers("tts") == {}

    cfg_refs = {
        "key_refs": {"brain": "RUNPOD_BRAIN_KEY"},
        "keys": {"brain": "brain-key"}
    }
    clients_refs = make_clients(cfg_refs, global_key=None)
    assert clients_refs.resolve_key("tts") is None


def test_no_key_in_any_message_or_log(monkeypatch, caplog):
    """Verify that secret keys never appear in exception messages, reprs, or logs."""
    SECRET = "super-secret-raw-key-value-99999"
    monkeypatch.delenv("RUNPOD_STT_KEY", raising=False)
    cfg = {
        "key_refs": {"stt": "RUNPOD_STT_KEY"},
        "keys": {"stt": SECRET}
    }
    clients = make_clients(cfg)
    
    with pytest.raises(AiUnavailable) as exc_info:
        clients.resolve_key("stt")

    exc = exc_info.value
    exc_str = str(exc)
    exc_repr = repr(exc)

    assert SECRET not in exc_str
    assert SECRET not in exc_repr
    assert SECRET not in caplog.text
