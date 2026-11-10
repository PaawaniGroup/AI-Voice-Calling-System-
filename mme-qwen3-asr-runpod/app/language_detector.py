"""Map between request language codes and the English language names documented by qwen-asr.

qwen-asr documents English names ("English", "Hindi") and language=None for automatic identification.
ISO codes as *input* are not documented, so they are converted here.
"""
from __future__ import annotations

_NAME_TO_ISO = {
    "chinese": "zh", "english": "en", "cantonese": "yue", "arabic": "ar", "german": "de", "french": "fr",
    "spanish": "es", "portuguese": "pt", "indonesian": "id", "italian": "it", "korean": "ko", "russian": "ru",
    "thai": "th", "vietnamese": "vi", "japanese": "ja", "turkish": "tr", "hindi": "hi", "malay": "ms",
    "dutch": "nl", "swedish": "sv", "danish": "da", "finnish": "fi", "polish": "pl", "czech": "cs",
    "filipino": "fil", "persian": "fa", "greek": "el", "hungarian": "hu", "macedonian": "mk", "romanian": "ro",
}
_ISO_TO_NAME = {v: k.capitalize() for k, v in _NAME_TO_ISO.items()}


def request_language_to_model(lang: str | None) -> str | None:
    """'auto'/None -> None (automatic language identification). 'hi'/'Hindi' -> 'Hindi'. Unknown -> ValueError."""
    if lang is None or str(lang).strip().lower() in ("", "auto"):
        return None
    k = str(lang).strip().lower()
    if k in _ISO_TO_NAME:
        return _ISO_TO_NAME[k]
    if k in _NAME_TO_ISO:
        return k.capitalize()
    raise ValueError(f"unsupported language {lang!r}")


def model_language_to_iso(name: str | None) -> str | None:
    if not name:
        return None
    return _NAME_TO_ISO.get(name.strip().lower(), name.strip().lower())
