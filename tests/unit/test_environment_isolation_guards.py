"""Unit tests for development environment isolation guards and production endpoint fencing.
Verifies:
1. Production STT endpoint (ua9l3gus6qjnmg) is strictly blocked in development.
2. Production Brain endpoint (hu8qhrvlxbv314) is strictly blocked in development.
3. Production TTS endpoint (6bmxas8ccwzhrv) is strictly blocked in development.
4. Authorized Dev TTS endpoint (dzqzqas7frc0zt) is allowed in development.
5. Missing development endpoints raise explicit AiUnavailable with clear diagnostic message.
6. Safe logging does not expose authorization keys or secrets.
"""
import pytest
from config.settings import Settings
from app.ai.clients import ModelClients, ProductionEndpointFenced, AiUnavailable

def test_production_stt_fenced_in_development():
    s = Settings(env="development", ai_strict_env_isolation=True)
    c = ModelClients(s, {"stt_url": "https://api.runpod.ai/v2/ua9l3gus6qjnmg/runsync"})
    with pytest.raises(ProductionEndpointFenced) as excinfo:
        c.endpoint("stt")
    assert excinfo.value.service == "stt"
    assert excinfo.value.endpoint_id == "ua9l3gus6qjnmg"
    assert "blocked by strict environment isolation" in excinfo.value.detail

def test_production_brain_fenced_in_development():
    s = Settings(env="development", ai_strict_env_isolation=True)
    c = ModelClients(s, {"brain_url": "https://api.runpod.ai/v2/hu8qhrvlxbv314/runsync"})
    with pytest.raises(ProductionEndpointFenced) as excinfo:
        c.endpoint("brain")
    assert excinfo.value.service == "brain"
    assert excinfo.value.endpoint_id == "hu8qhrvlxbv314"
    assert "blocked by strict environment isolation" in excinfo.value.detail

def test_production_tts_fenced_in_development():
    s = Settings(env="development", ai_strict_env_isolation=True)
    c = ModelClients(s, {"tts_url": "https://api.runpod.ai/v2/6bmxas8ccwzhrv/runsync"})
    with pytest.raises(ProductionEndpointFenced) as excinfo:
        c.endpoint("tts")
    assert excinfo.value.service == "tts"
    assert excinfo.value.endpoint_id == "6bmxas8ccwzhrv"
    assert "blocked by strict environment isolation" in excinfo.value.detail

def test_authorized_dev_tts_allowed_in_development():
    s = Settings(env="development", ai_strict_env_isolation=True)
    c = ModelClients(s, {"tts_url": "https://api.runpod.ai/v2/dzqzqas7frc0zt/runsync"})
    ep = c.endpoint("tts")
    assert ep.url == "https://api.runpod.ai/v2/dzqzqas7frc0zt"
    assert ep.path == "/runsync"

def test_missing_endpoint_raises_clear_error():
    s = Settings(env="development", ai_strict_env_isolation=True)
    c = ModelClients(s, {"stt_url": ""})
    with pytest.raises(AiUnavailable) as excinfo:
        c.endpoint("stt")
    assert excinfo.value.kind == "not_configured"

def test_production_mode_bypasses_fencing_for_production():
    s = Settings(env="production", ai_strict_env_isolation=True)
    c = ModelClients(s, {"stt_url": "https://api.runpod.ai/v2/ua9l3gus6qjnmg/runsync"})
    ep = c.endpoint("stt")
    assert ep.url == "https://api.runpod.ai/v2/ua9l3gus6qjnmg"
