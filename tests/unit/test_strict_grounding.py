import pytest
from app.ai.pipeline import (
    OLD_SYSTEM_PROMPT,
    SYSTEM_PROMPT_DEVANAGARI,
    build_system_prompt,
    system_prompt,
    Conversation,
)
from app.ai.service import AiService
from config.settings import Settings


def test_callback_promises_removed_under_strict_grounding():
    """Verify that under strict_grounding=True, callback promises are removed and replaced."""
    strict_prompt = system_prompt(strict_grounding=True)
    assert "offer a counsellor callback" not in strict_prompt
    assert "सही जानकारी हमारे काउंसलर से पक्की करनी होगी" in strict_prompt
    assert "NEVER promise a callback" in strict_prompt

    bsp = build_system_prompt(strict_grounding=True)
    assert "offer a counsellor callback" not in bsp
    assert "सही जानकारी हमारे काउंसलर से पक्की करनी होगी" in bsp
    assert "NEVER promise a callback" in bsp


def test_strict_grounding_flag_behavior():
    """Verify strict_grounding is default OFF and adds strict rules when enabled."""
    # Default OFF
    prompt_off = system_prompt(strict_grounding=False)
    assert "STRICT GROUNDING & NO-PROMISE RULES:" not in prompt_off
    assert prompt_off == OLD_SYSTEM_PROMPT

    bsp_off = build_system_prompt(strict_grounding=False)
    assert "STRICT GROUNDING & NO-PROMISE RULES:" not in bsp_off

    # Enabled
    prompt_on = system_prompt(strict_grounding=True)
    assert "STRICT GROUNDING & NO-PROMISE RULES:" in prompt_on
    assert "NEVER state specific fees, percentage rules, cut-offs, dates" in prompt_on
    assert "NEVER promise a callback, contact, or any action the system cannot take" in prompt_on

    bsp_on = build_system_prompt(strict_grounding=True)
    assert "STRICT GROUNDING & NO-PROMISE RULES:" in bsp_on
    assert "NEVER state specific fees, percentage rules, cut-offs, dates" in bsp_on


def test_conversation_strict_grounding_wiring():
    """Verify Conversation honours strict_grounding flag."""
    conv_default = Conversation()
    assert conv_default.strict_grounding is False
    assert "STRICT GROUNDING & NO-PROMISE RULES:" not in conv_default.messages[0]["content"]

    conv_strict = Conversation(strict_grounding=True)
    assert conv_strict.strict_grounding is True
    assert "STRICT GROUNDING & NO-PROMISE RULES:" in conv_strict.messages[0]["content"]


def test_tenant_config_defaults_and_put():
    """Verify tenant config defaults strict_grounding and glossary_correction to False."""
    s = Settings()
    svc = AiService(db=None, repos=None, s=s, sessions=None)
    cfg = svc.default_config("tenant_test")

    assert "strict_grounding" in cfg
    assert cfg["strict_grounding"] is False
    assert "glossary_correction" in cfg
    assert cfg["glossary_correction"] is False
    assert "store_transcripts" in cfg
    assert cfg["store_transcripts"] is False
    assert "faq_grounding" in cfg
    assert cfg["faq_grounding"] is False
