"""Integration environment verification test for MME Chatterbox V3 Streaming.
Verifies git repository state, configuration defaults, RunPod endpoint references,
safety isolation guards, and buffer configurations.
"""
import os
import shutil
import subprocess
import pytest
from config.settings import Settings

GIT_BIN = shutil.which("git") or r"C:\Program Files\Git\cmd\git.exe"

def test_mme_telephony_git_state():
    """Verify MME_Telephony git branch and commit."""
    branch = subprocess.check_output(
        [GIT_BIN, "rev-parse", "--abbrev-ref", "HEAD"], text=True
    ).strip()
    commit = subprocess.check_output(
        [GIT_BIN, "rev-parse", "HEAD"], text=True
    ).strip()
    assert branch == "streaming-implementation"
    assert commit.startswith("71a17e1")

def test_settings_safety_and_endpoint_references():
    """Verify settings defaults, production safety gates, and buffer defaults."""
    s = Settings()
    # Watermarking fail-closed check
    assert os.getenv("ALLOW_UNWATERMARKED_STREAMING", "0") == "0"
    
    # Initial playback buffer default
    assert s.ai_tts_prebuffer_ms == 400
    assert 0 <= s.ai_tts_prebuffer_ms <= 2000
    
    # Models
    assert s.ai_brain_model == "Qwen3-30B-A3B-Instruct-2507"
    assert s.ai_stt_model == "mmeog/mme_stt_model"
    
    # Provider isolation: production provider is mock/plivo_api, never real unapproved PSTN
    assert s.provider in ("mock", "plivo_api", "freeswitch_gateway")

def test_production_endpoint_isolation_guarantee():
    """Verify production endpoint ID 6bmxas8ccwzhrv is never targeted by dev tests."""
    from app.ai.service import AiService
    svc = AiService(None, None, Settings(), None)
    cfg = svc.default_config("tenant_test")
    assert cfg["tts_url"] == ""
    assert cfg["brain_url"] == ""
    assert cfg["stt_url"] == ""
    assert cfg["tts_stream"] is False
    assert cfg["tts_overlap"] is False
