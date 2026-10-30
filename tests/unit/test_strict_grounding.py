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


def test_callback_promises_removed():
    """Verify that 'schedule a callback' and 'offer a counsellor callback' have been removed."""
    # Check default prompts
    assert "schedule a callback" not in OLD_SYSTEM_PROMPT.lower()
    assert "offer a counsellor callback" not in OLD_SYSTEM_PROMPT.lower()
    assert "admissions counsellor will verify and confirm" in OLD_SYSTEM_PROMPT
    assert "never promise a callback" in OLD_SYSTEM_PROMPT.lower()

    bsp = build_system_prompt()
    assert "schedule a callback" not in bsp.lower()
    assert "offer a counsellor callback" not in bsp.lower()
    assert "admissions counsellor will verify and confirm" in bsp
    assert "never promise a callback" in bsp.lower()


def test_strict_grounding_flag_behavior():
    """Verify strict_grounding is default OFF and adds strict rules when enabled."""
    # Default OFF
    prompt_off = system_prompt(strict_grounding=False)
    assert "STRICT GROUNDING RULES:" not in prompt_off

    bsp_off = build_system_prompt(strict_grounding=False)
    assert "STRICT GROUNDING RULES:" not in bsp_off

    # Enabled
    prompt_on = system_prompt(strict_grounding=True)
    assert "STRICT GROUNDING RULES:" in prompt_on
    assert "NEVER state specific fees, percentage rules, cut-offs, dates" in prompt_on
    assert "NEVER promise a callback or any action the system cannot take" in prompt_on

    bsp_on = build_system_prompt(strict_grounding=True)
    assert "STRICT GROUNDING RULES:" in bsp_on
    assert "NEVER state specific fees, percentage rules, cut-offs, dates" in bsp_on


def test_conversation_strict_grounding_wiring():
    """Verify Conversation honours strict_grounding flag."""
    conv_default = Conversation()
    assert conv_default.strict_grounding is False
    assert "STRICT GROUNDING RULES:" not in conv_default.messages[0]["content"]

    conv_strict = Conversation(strict_grounding=True)
    assert conv_strict.strict_grounding is True
    assert "STRICT GROUNDING RULES:" in conv_strict.messages[0]["content"]


def test_tenant_config_defaults_and_put():
    """Verify tenant config defaults strict_grounding and glossary_correction to False."""
    s = Settings()
    svc = AiService(db=None, repos=None, s=s, sessions=None)
    cfg = svc.default_config("tenant_test")

    assert "strict_grounding" in cfg
    assert cfg["strict_grounding"] is False
    assert "glossary_correction" in cfg
    assert cfg["glossary_correction"] is False
