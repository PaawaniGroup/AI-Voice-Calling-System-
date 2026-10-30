"""Unit tests for TTS Text Preparation and Speech Normalization Policy.

Verifies:
1. Deterministic metadata stripping (markdown, bullets, tool results, brackets, html).
2. Number normalization (currency, percentages, dates, times, phone numbers, 12वीं, acronyms).
3. Plain English preservation.
4. Language tag detection and routing (hi vs en).
5. Voice reference and speaker conditioning preservation in TTS client payloads.
"""
from __future__ import annotations

import base64
import pytest
from app.ai.tts_frontend import detect_lang, normalize, prepare_text_for_tts, strip_spoken_metadata
from app.ai.clients import ModelClients
from config.settings import Settings


def test_empty_and_whitespace_text():
    assert normalize("") == ""
    assert normalize("   ") == ""
    assert strip_spoken_metadata("") == ""
    assert strip_spoken_metadata("   ") == ""
    spoken, lang = prepare_text_for_tts("", lang="auto")
    assert spoken == ""


def test_metadata_and_formatting_stripping():
    # 1. Bracketed tool results & instructions
    raw_tool = '[Tool Result: {"course": "B.Tech", "fees": "₹1,25,000"}] जी, B.Tech की कुल फीस ₹1,25,000 है।'
    clean = strip_spoken_metadata(raw_tool)
    assert "[Tool Result" not in clean
    assert "₹1,25,000" in clean
    assert clean == "जी, B.Tech की कुल फीस ₹1,25,000 है।"

    # 2. Markdown headers, bold, italics, bullets
    md_text = (
        "## Course Details\n"
        "- **B.Tech** admission is open.\n"
        "- *Fees* are ₹1,20,000 per year."
    )
    clean_md = strip_spoken_metadata(md_text)
    assert "#" not in clean_md
    assert "*" not in clean_md
    assert "- " not in clean_md
    assert "B.Tech admission is open." in clean_md

    # 3. HTML/XML tags
    html_text = "<speak>नमस्ते <break time='500ms'/> आपका स्वागत है</speak>"
    clean_html = strip_spoken_metadata(html_text)
    assert "<" not in clean_html and ">" not in clean_html
    assert "नमस्ते" in clean_html and "आपका स्वागत है" in clean_html


def test_number_normalization_hindi_hinglish():
    # Currency
    assert normalize("फीस ₹1,25,000 है") == "फीस एक लाख पच्चीस हज़ार रुपये है"
    assert "पाँच हज़ार रुपये" in normalize("Registration fee ₹5,000 hai")

    # Percentages
    norm_pct = normalize("कम से कम 60% अंक चाहिए")
    assert "साठ प्रतिशत" in norm_pct

    # Ordinals
    assert normalize("12वीं कक्षा") == "बारहवीं कक्षा"

    # Dates
    norm_date = normalize("Admission 30 June tak hai")
    assert "तीस जून" in norm_date

    # Dotted Acronyms
    norm_course = normalize("B.Tech aur M.Tech programmes")
    assert "बी टेक" in norm_course
    assert "एम टेक" in norm_course

    # Word Acronyms
    assert "नीट" in normalize("NEET exam ka result aa gaya hai")


def test_plain_english_preserved():
    eng_text = "What is the eligibility for B.Tech admission?"
    assert normalize(eng_text) == eng_text

    eng_fee = "Hostel facility is available with annual charges of 60,000."
    assert normalize(eng_fee) == eng_fee


def test_language_tag_routing():
    assert detect_lang("Hello, what are the available courses?") == "en"
    assert detect_lang("Mujhe admission process ke baare mein janna hai") == "hi"
    assert detect_lang("जी, आपका स्वागत है") == "hi"


def test_tts_voice_reference_conditioning_preservation(tmp_path):
    s = Settings()
    # Create a temporary dummy reference voice wav
    wav_file = tmp_path / "ref_test.wav"
    wav_file.write_bytes(b"RIFF" + b"\x00" * 36)
    s.ai_tts_reference_wav = str(wav_file)

    cfg = {
        "tts_url": "https://api.runpod.ai/v2/test-tts/runsync",
        "models": {"tts": "Qwen/Qwen3-TTS-12Hz-1.7B-Base"},
    }
    clients = ModelClients(s, cfg)

    body = clients._tts_runpod_body("जी, B.Tech की फीस ₹1,25,000 है।", language="hi", model="Qwen/Qwen3-TTS-12Hz-1.7B-Base")
    assert "reference_audio_base64" in body["input"]
    assert len(body["input"]["reference_audio_base64"]) > 0
    assert body["input"]["language"] == "hi"
    assert "एक लाख पच्चीस हज़ार रुपये" in body["input"]["text"]
    assert body["input"]["prompt"] == body["input"]["text"]
