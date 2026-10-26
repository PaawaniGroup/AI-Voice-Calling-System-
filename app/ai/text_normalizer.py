"""Re-export app.voice.text_normalizer inside app.ai for architectural compatibility."""
from app.voice.text_normalizer import (
    MODE_EN,
    MODE_HI,
    MODE_HINGLISH,
    SUPPORTED_MODES,
    NormalizationResult,
    canonicalize_language_mode,
    detect_scripts,
    normalize_phrase,
    normalize_text,
    validate_script_rules,
)

__all__ = [
    "MODE_EN",
    "MODE_HI",
    "MODE_HINGLISH",
    "SUPPORTED_MODES",
    "NormalizationResult",
    "canonicalize_language_mode",
    "detect_scripts",
    "normalize_phrase",
    "normalize_text",
    "validate_script_rules",
]
