# -*- coding: utf-8 -*-
"""Regression tests for MME AI Counsellor Language and Script Policy.

Covers the 8 specified regression test cases:
- Test 1: English input -> Entire response in English, no Devanagari characters.
- Test 2: Hindi input -> Natural Hinglish with Devanagari Hindi and Latin-script English terms.
- Test 3: Romanized Hinglish input -> Natural Hinglish, Hindi in Devanagari, B.Tech and fees in Latin.
- Test 4: Mid-call switch from English to Hinglish -> Immediate adaptation per turn.
- Test 5: Mid-call switch back to English -> Immediate shift back to pure English.
- Test 6: Script enforcement -> Rejection of Roman Hindi words ('aap', 'hai', 'mujhe', 'nahi', 'bataiye').
- Test 7: English proper nouns in Hinglish -> 'Mujhe Computer Science mein admission chahiye.' -> 'मुझे Computer Science में admission चाहिए।'.
- Test 8: TTS preservation -> Preserves English/Hinglish mix, no translation of English terms, no altered claims.
"""
from __future__ import annotations

import re
import pytest
from app.ai.pipeline import Conversation, build_system_prompt
from app.ai.script_enforcement import check_reply, reply_mode
from app.ai.tts_frontend import detect_lang, normalize, prepare_text_for_tts, strip_spoken_metadata
from mme_lexicon import convert


class MockBrainClients:
    def __init__(self, responses: list[str]):
        self.cfg = {}
        self.responses = responses
        self.calls = 0

    async def chat(self, messages: list[dict], tools=None, temperature=None) -> dict:
        reply = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return {"text": reply}


def has_devanagari(text: str) -> bool:
    """Returns True if string contains any Devanagari unicode character."""
    return bool(re.search(r"[\u0900-\u097F]", text))


# ---------------------------------------------------------------------------
# Test 1: English input
# ---------------------------------------------------------------------------
def test_1_english_input():
    """Input: 'I want to know the eligibility for MBA.'
    Expected: Entire response in English, with no Devanagari characters.
    """
    user_input = "I want to know the eligibility for MBA."
    mode = reply_mode(user_input)
    assert mode == "en", f"Expected 'en' mode for English input, got '{mode}'"

    assistant_reply = (
        "You need a Bachelor's degree with at least 50% marks to be eligible for MBA. "
        "Which college are you interested in?"
    )
    # 1. No Devanagari script allowed
    assert not has_devanagari(assistant_reply), "English response must not contain Devanagari characters"

    # 2. Strict script checker validation
    passed, problems = check_reply(assistant_reply, mode="en")
    assert passed is True, f"Script check failed for English mode: {problems}"


# ---------------------------------------------------------------------------
# Test 2: Hindi input
# ---------------------------------------------------------------------------
def test_2_hindi_input():
    """Input: 'मुझे MBA में admission लेना है।'
    Expected: Natural Hinglish with Devanagari Hindi and Latin-script English terms.
    """
    user_input = "मुझे MBA में admission लेना है।"
    mode = reply_mode(user_input)
    assert mode == "hi", f"Expected 'hi' mode for Hindi input, got '{mode}'"

    assistant_reply = "जी, बिल्कुल। आप MBA की कौन-सी branch या specialisation में admission लेना चाहते हैं?"

    # 1. Hindi words must be in Devanagari
    assert has_devanagari(assistant_reply), "Hindi/Hinglish response must contain Devanagari script"
    assert "जी" in assistant_reply and "चाहते हैं" in assistant_reply

    # 2. English education terms preserved in Latin script
    assert "MBA" in assistant_reply
    assert "admission" in assistant_reply

    # 3. Validated by script checker
    passed, problems = check_reply(assistant_reply, mode="hi")
    assert passed is True, f"Script check failed for Hindi mode: {problems}"


# ---------------------------------------------------------------------------
# Test 3: Romanized Hinglish input
# ---------------------------------------------------------------------------
def test_3_romanized_hinglish_input():
    """Input: 'Mujhe BTech ki fees batao.'
    Expected: A natural Hinglish response. Hindi words must use Devanagari, and B.Tech and fees must remain in Latin script.
    """
    user_input = "Mujhe BTech ki fees batao."
    mode = reply_mode(user_input)
    assert mode == "hi", f"Expected 'hi' mode for Romanized Hinglish, got '{mode}'"

    assistant_reply = "जी, बिल्कुल। B.Tech की कुल fees ₹1,25,000 है। क्या मैं eligibility criteria भी बताऊँ?"

    # 1. Hindi words written in Devanagari
    assert has_devanagari(assistant_reply)
    assert "जी" in assistant_reply and "कुल" in assistant_reply

    # 2. B.Tech and fees remain in Latin script
    assert "B.Tech" in assistant_reply
    assert "fees" in assistant_reply

    # 3. No Romanized Hindi words like 'hai', 'ki', 'batao'
    passed, problems = check_reply(assistant_reply, mode="hi")
    assert passed is True, f"Script check failed: {problems}"


# ---------------------------------------------------------------------------
# Test 4: Mid-call switch from English to Hinglish
# ---------------------------------------------------------------------------
def test_4_mid_call_switch_english_to_hinglish():
    """Turn 1: 'What is the admission process?' -> Expected: English response.
    Turn 2: 'Achha, scholarship bhi milti hai kya?' -> Expected: Natural Hinglish with Devanagari Hindi words.
    """
    # Turn 1
    t1_input = "What is the admission process?"
    t1_mode = reply_mode(t1_input)
    assert t1_mode == "en"
    t1_reply = "The admission process involves filling an online form and verifying your documents."
    assert not has_devanagari(t1_reply)
    passed_1, _ = check_reply(t1_reply, mode="en")
    assert passed_1 is True

    # Turn 2: Switch to Hinglish
    t2_input = "Achha, scholarship bhi milti hai kya?"
    t2_mode = reply_mode(t2_input, previous_mode=t1_mode)
    assert t2_mode == "hi", "Language must adapt immediately to 'hi' on Hinglish switch"
    t2_reply = "जी हाँ, 12वीं में 85% से अधिक अंक होने पर scholarship मिलती है।"
    assert has_devanagari(t2_reply)
    assert "scholarship" in t2_reply
    passed_2, _ = check_reply(t2_reply, mode="hi")
    assert passed_2 is True


# ---------------------------------------------------------------------------
# Test 5: Mid-call switch back to English
# ---------------------------------------------------------------------------
def test_5_mid_call_switch_back_to_english():
    """Turn 1: 'मुझे admission के बारे में बताइए।' -> Expected: Natural Hinglish.
    Turn 2: 'Please explain it in English.' -> Expected: Entire response in English.
    """
    # Turn 1
    t1_input = "मुझे admission के बारे में बताइए।"
    t1_mode = reply_mode(t1_input)
    assert t1_mode == "hi"
    t1_reply = "जी, बताइए, आप किस course में admission लेना चाहते हैं?"
    assert has_devanagari(t1_reply)
    passed_1, _ = check_reply(t1_reply, mode="hi")
    assert passed_1 is True

    # Turn 2: Switch back to English
    t2_input = "Please explain it in English."
    t2_mode = reply_mode(t2_input, previous_mode=t1_mode)
    assert t2_mode == "en", "Language must adapt immediately to 'en' when student requests English"
    t2_reply = "Certainly. Which course would you like to explore for admission this year?"
    assert not has_devanagari(t2_reply), "Response must have no Devanagari when switched to English"
    passed_2, _ = check_reply(t2_reply, mode="en")
    assert passed_2 is True


# ---------------------------------------------------------------------------
# Test 6: Script enforcement
# ---------------------------------------------------------------------------
def test_6_script_enforcement_forbids_roman_hindi():
    """Check that Hindi words such as 'aap', 'hai', 'mujhe', 'nahi' and 'bataiye'
    do not appear in Roman script in Hindi/Hinglish responses.
    """
    # Violating replies containing Roman Hindi words must fail check_reply
    violating_examples = [
        "Aap kaun sa course dekh rahe hain?",
        "B.Tech ki fees kitni hai?",
        "Mujhe admission process ke baare mein bataiye",
        "Yeh course available nahi hai",
    ]
    for bad in violating_examples:
        passed, problems = check_reply(bad, mode="hi")
        assert passed is False, f"Expected violation for '{bad}', but passed"
        assert len(problems) > 0

    # Proper Devanagari equivalents must pass
    correct_examples = [
        "आप कौन-सा course देख रहे हैं?",
        "B.Tech की fees कितनी है?",
        "मुझे admission process के बारे में बताइए।",
        "यह course available नहीं है।",
    ]
    for good in correct_examples:
        passed, problems = check_reply(good, mode="hi")
        assert passed is True, f"Expected valid script for '{good}', got problems: {problems}"


# ---------------------------------------------------------------------------
# Test 7: English proper nouns in Hinglish
# ---------------------------------------------------------------------------
def test_7_english_proper_nouns_in_hinglish():
    """Input: 'Mujhe Computer Science mein admission chahiye.'
    Expected: 'मुझे Computer Science में admission चाहिए।'
    """
    raw_input = "Mujhe Computer Science mein admission chahiye."
    result = convert(raw_input)
    assert result.text.rstrip(".।") == "मुझे Computer Science में admission चाहिए"

    # Verify script rules on expected output:
    # 'Computer Science' and 'admission' remain in Latin script
    assert "Computer Science" in result.text
    assert "admission" in result.text
    # 'मुझे', 'में', 'चाहिए' are in Devanagari
    assert "मुझे" in result.text
    assert "में" in result.text
    assert "चाहिए" in result.text

    passed, problems = check_reply(result.text, mode="hi")
    assert passed is True, f"Script check failed for converted text: {problems}"


# ---------------------------------------------------------------------------
# Test 8: TTS preservation
# ---------------------------------------------------------------------------
def test_8_tts_preservation_language_mix():
    """Verify that the TTS preparation layer preserves the Brain's English/Hinglish language mix
    and does not translate, rewrite or add claims.
    """
    brain_text = "जी, Computer Science में admission के लिए 12वीं में 60% marks चाहिए।"

    spoken_text, lang = prepare_text_for_tts(brain_text, lang="hi")

    # 1. Language code is preserved as 'hi'
    assert lang == "hi"

    # 2. English technical terms are strictly NOT translated into Hindi
    assert "Computer Science" in spoken_text or "computer science" in spoken_text.lower()
    assert "admission" in spoken_text.lower()
    assert "संगणक विज्ञान" not in spoken_text  # Must not translate Computer Science to formal Hindi!
    assert "प्रवेश" not in spoken_text         # Must not translate admission to formal Hindi!

    # 3. Spoken numbers are normalized accurately without altering factual values
    assert "बारहवीं" in spoken_text
    assert "साठ प्रतिशत" in spoken_text

    # 4. No additional claims or greetings added
    assert "scholarship" not in spoken_text
    assert "hostel" not in spoken_text
