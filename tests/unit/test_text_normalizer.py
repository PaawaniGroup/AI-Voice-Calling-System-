"""Unit tests for the production text normalization and script validation layer."""
import pytest
from app.voice.text_normalizer import (
    MODE_EN,
    MODE_HI,
    MODE_HINGLISH,
    canonicalize_language_mode,
    detect_scripts,
    normalize_phrase,
    normalize_text,
    validate_script_rules,
)


def test_canonicalize_language_mode():
    assert canonicalize_language_mode("en") == MODE_EN
    assert canonicalize_language_mode("en-IN") == MODE_EN
    assert canonicalize_language_mode("hi") == MODE_HI
    assert canonicalize_language_mode("hi-IN") == MODE_HI
    assert canonicalize_language_mode("hinglish") == MODE_HINGLISH
    assert canonicalize_language_mode("hinglish-IN") == MODE_HINGLISH
    assert canonicalize_language_mode(None) == MODE_HINGLISH
    assert canonicalize_language_mode("unknown") == MODE_HINGLISH


def test_script_detection():
    assert "latin" in detect_scripts("Hello world")
    assert "devanagari" in detect_scripts("नमस्ते")
    scripts = detect_scripts("आपकी fees ₹5000 है")
    assert "devanagari" in scripts
    assert "latin" in scripts
    assert "digits" in scripts


def test_hinglish_normalization_user_examples():
    # User requirement Example 1:
    # "Aap apni fees online pay kar sakte hain." -> "आप अपनी fees online pay कर सकते हैं।"
    res1 = normalize_text("Aap apni fees online pay kar sakte hain.", language_mode="hinglish-IN")
    assert "आप" in res1.text
    assert "अपनी" in res1.text
    assert "fees" in res1.text
    assert "online" in res1.text
    assert "pay" in res1.text
    assert "कर सकते हैं" in res1.text
    assert res1.valid is True
    assert res1.normalization_applied is True

    # User requirement Example 2:
    # "Mujhe computer science mein admission lena hai." -> "मुझे computer science में admission लेना है।"
    res2 = normalize_text("Mujhe computer science mein admission lena hai.", language_mode="hinglish-IN")
    assert "मुझे" in res2.text
    assert "computer" in res2.text
    assert "science" in res2.text
    assert "में" in res2.text
    assert "admission" in res2.text
    assert "लेना है" in res2.text
    assert res2.valid is True


def test_pure_hindi_mode():
    text = "आप कौन-सा कोर्स करना चाहते हैं?"
    res = normalize_text(text, language_mode="hi-IN")
    assert "आप कौन-सा कोर्स करना चाहते हैं" in res.text
    assert res.valid is True
    assert "devanagari" in res.detected_scripts


def test_indian_english_mode():
    text = "Please tell me which course you want to study. Fee is ₹5000."
    res = normalize_text(text, language_mode="en-IN")
    assert "Please tell me which course you want to study" in res.text
    assert "5000 rupees" in res.text
    assert res.valid is True
    assert "devanagari" not in res.detected_scripts


def test_entity_and_acronym_preservation():
    text = "Aapka B.Tech and MBA admission process www.mme.edu par available hai."
    res = normalize_text(text, language_mode="hinglish-IN")
    assert "B.Tech" in res.text
    assert "MBA" in res.text
    assert "www.mme.edu" in res.text
    assert "admission" in res.text
    assert "process" in res.text
    assert "available" in res.text
    assert "आपका" in res.text
    assert res.valid is True


def test_currency_and_numbers_normalization():
    # In hinglish/hindi: ₹1,25,000 -> एक लाख पच्चीस हज़ार रुपये
    text = "B.Tech ki total fees ₹1,25,000 hai aur registration 15 November tak hai."
    res = normalize_text(text, language_mode="hinglish-IN")
    assert "एक लाख पच्चीस हज़ार रुपये" in res.text
    assert "पंद्रह नवंबर" in res.text
    assert res.valid is True


def test_idempotence():
    text = "Aap apni fees online pay kar sakte hain."
    first = normalize_text(text, language_mode="hinglish-IN").text
    second = normalize_text(first, language_mode="hinglish-IN").text
    assert first == second


def test_phrase_streaming_latency():
    phrase = "जी, admission process आसान है।"
    res = normalize_phrase(phrase, language_mode="hinglish-IN", is_first=True)
    assert res.valid is True
    assert res.metrics["latency_ms"] < 10.0  # sub-10ms requirement for streaming


def test_homograph_context_disambiguation():
    # 1. "main" followed by English noun -> stays Latin "main"
    res1 = normalize_text("Yeh college ka main campus hai.", language_mode="hinglish-IN")
    assert "main campus" in res1.text
    assert "यह" in res1.text or "ये" in res1.text

    # 2. "main" preceded by "baare" -> "में"
    res2 = normalize_text("Course ke baare main jankari chahiye.", language_mode="hinglish-IN")
    assert "बारे में" in res2.text or "के बारे में" in res2.text

    # 3. "main" followed by first person verb -> "मैं"
    res3 = normalize_text("Main aapko eligibility criteria bataunga.", language_mode="hinglish-IN")
    assert "मैं" in res3.text
    assert "eligibility" in res3.text
    assert "criteria" in res3.text


def test_dates_and_percentages_edge_cases():
    text = "Application fee ₹1,500 hai, last date 30 June hai aur minimum 60% marks chahiye."
    res = normalize_text(text, language_mode="hinglish-IN")
    assert "पंद्रह सौ रुपये" in res.text or "एक हज़ार पाँच सौ रुपये" in res.text
    assert "तीस जून" in res.text
    assert "साठ प्रतिशत" in res.text
    assert "Application fee" in res.text or "application fee" in res.text.lower()
    assert res.valid is True


def test_validation_detects_pure_roman_in_hi_mode():
    text = "Hum aapko kal phone karenge aur admission process batayenge."
    # If passed to pure hi-IN, it should normalize Romanized Hindi to Devanagari
    res = normalize_text(text, language_mode="hi-IN")
    assert "हम" in res.text
    assert "आपको" in res.text
    assert "admission process" in res.text
    assert res.valid is True

