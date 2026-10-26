"""Pure logic of the STT worker patch: standard library only (no torch, no numpy, no transformers), so it can be self-tested anywhere.

Everything here works on request fields and numbers. It never sees, returns or logs the caller's words or the vocabulary hint's text (only their lengths)."""
from __future__ import annotations

import base64

LANGUAGES = ("hi", "en")
MAX_PROMPT_CHARS = 200
_AUTO = ("", "auto", "none", "null")
_ALIASES = {"hinglish": "hi"}          # the app already maps hinglish -> hi; accepted here too


def parse_language(value, default: str = "") -> str:
    """The request's `language` -> "hi" | "en" | "" (auto-detect). `None` (field absent) means the worker's default (`WHISPER_LANGUAGE`);
    an explicit "auto" means auto-detect even when the worker has a default. Anything else raises ValueError."""
    v = default if value is None else value
    v = str(v).strip().lower()
    v = _ALIASES.get(v, v)
    if v in _AUTO:
        return ""
    if v in LANGUAGES:
        return v
    raise ValueError("language must be hi, en or auto")


def parse_prompt(value) -> str:
    """The optional vocabulary hint (a short text that biases the decoder towards names and course words). At most MAX_PROMPT_CHARS characters
    after collapsing whitespace; longer raises ValueError (the app keeps it short). Absent or blank -> ""."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("prompt must be a string")
    p = " ".join(value.split())
    if len(p) > MAX_PROMPT_CHARS:
        raise ValueError(f"prompt must be at most {MAX_PROMPT_CHARS} characters")
    return p


def decode_pcm16(b64: str) -> bytes:
    """Strict base64 of little-endian PCM16 samples; an odd byte count is not PCM16."""
    raw = base64.b64decode(b64, validate=True)
    if len(raw) % 2:
        raise ValueError("PCM16 data must have an even number of bytes")
    return raw


def audio_seconds(n_samples: int, sample_rate: int) -> float:
    return round(n_samples / sample_rate, 3) if sample_rate and sample_rate > 0 else 0.0


def build_generate_kwargs(base: dict, language: str, prompt_ids=None) -> dict:
    """A COPY of the base generate kwargs with this request's language (none = auto-detect) and optional prompt ids."""
    gk = dict(base)
    gk.pop("language", None)
    if language:
        gk["language"] = language
    if prompt_ids is not None:
        gk["prompt_ids"] = prompt_ids
    return gk


def startup_line(model_id: str, default_language: str, allow_request_language: bool, allow_prompt: bool, cuda: bool) -> str:
    """The one startup log line: which language the worker defaults to and which per-request options are open."""
    return (f"[STT] model={model_id} cuda={cuda} default_language={default_language or 'auto'} "
            f"request_language={'on' if allow_request_language else 'off'} prompt={'on' if allow_prompt else 'off'} max_prompt_chars={MAX_PROMPT_CHARS}")


def timing(inference_s: float, total_s: float, audio_s: float) -> dict:
    return {"inference_s": round(inference_s, 3), "total_s": round(total_s, 3), "audio_s": round(audio_s, 3)}
