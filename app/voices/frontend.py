"""Text front-end of a preview: the SAME normalisation the call path applies before it sends text to the TTS worker.

`spoken_text` mirrors the RunPod branch of `ModelClients._synthesize` (app/ai/clients.py). The call path is not touched; tests/unit/test_voice_frontend.py sends the same
text through the real client and through this function and compares what the worker would receive, so a change in one that is not made in the other fails there."""
from __future__ import annotations

from app.ai import tts_frontend

PREVIEW_LANGUAGES = ("auto", "hi", "en", "hinglish")   # the languages a tenant can configure for calls (app/ai/service.py LANGS)


def spoken_text(text: str, language: str, normalize: bool) -> tuple[str, str]:
    """-> (text for the worker, language tag for the worker). `normalize` is the setting `ai_tts_normalize`."""
    spoken, lang = text, language
    if normalize:
        if language in ("", "auto", "hi", "en", "hinglish"):
            lang = tts_frontend.detect_lang(text)    # per-sentence hi/en tag: Hindi and Hinglish -> hi, plain English -> en
        spoken = tts_frontend.normalize(text, lang)  # numbers, rupees, dates, phone numbers, acronyms as spoken words
    elif language == "hinglish":
        lang = "hi"
    return spoken, lang


def worker_input(text: str, language: str, normalize: bool, voice: str, phone_quality: bool) -> dict:
    """The `input` of a synth job. `phone_quality` asks for 8 kHz mu-law, exactly the format calls use (the worker's `ulaw8k`); otherwise a 24 kHz WAV."""
    spoken, lang = spoken_text(text, language, normalize)
    return {"text": spoken, "language": lang, "voice": voice, "output_format": "ulaw8k" if phone_quality else "wav"}
