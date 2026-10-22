"""The documented setup step is 'copy .env.example to .env'. The template must therefore load as-is (regression: its
MME_TEL_AI_MEDIA_SAMPLE_RATE=16000 line used to crash startup because env strings were not coerced to Literal[8000, 16000])."""
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from config.settings import Settings

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def clean_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("MME_TEL_"):
            monkeypatch.delenv(k)


def test_env_example_loads_unchanged(clean_env):
    s = Settings(_env_file=str(ROOT / ".env.example"))
    assert s.ai_media_sample_rate == 16000 and not s.ai_enabled and not s.real_calls_enabled and not s.plivo_inbound_enabled


@pytest.mark.parametrize("raw,expected", [("8000", 8000), ("16000", 16000), (" 16000 ", 16000)])
def test_sample_rate_accepts_text_from_the_environment(clean_env, monkeypatch, raw, expected):
    monkeypatch.setenv("MME_TEL_AI_MEDIA_SAMPLE_RATE", raw)
    assert Settings(_env_file=None).ai_media_sample_rate == expected


@pytest.mark.parametrize("raw", ["44100", "abc", "16k", ""])
def test_sample_rate_still_rejects_unsupported_values(clean_env, monkeypatch, raw):
    monkeypatch.setenv("MME_TEL_AI_MEDIA_SAMPLE_RATE", raw)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
