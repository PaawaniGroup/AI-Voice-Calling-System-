# -*- coding: utf-8 -*-
"""Unit tests for app/ai/script_enforcement.py."""
import asyncio
import time
import pytest

from app.ai.script_enforcement import (
    reply_mode,
    check_reply,
    build_mode_hint,
    inject_turn_hint,
    enforce_clause,
    enforce_non_streamed,
    load_allowlist,
    HOMOGRAPHS
)


def test_load_allowlist():
    allow = load_allowlist()
    assert "b.tech" in allow
    assert "admission" in allow
    assert "eligibility" in allow
    assert "form" in allow
    assert "online" in allow


def test_reply_mode_detection():
    # 1. Hindi Roman -> 'hi'
    assert reply_mode("Mujhe B.Tech ki fees janni hai", previous_mode="hi") == "hi"
    assert reply_mode("Aapka admission process kya hai", previous_mode="en") == "hi"

    # 2. Hindi Devanagari -> 'hi'
    assert reply_mode("मुझे कोर्स के बारे में बताइए", previous_mode="en") == "hi"

    # 3. English -> 'en'
    assert reply_mode("What is the eligibility for B.Tech?", previous_mode="hi") == "en"
    assert reply_mode("Can you explain the admission process?", previous_mode="hi") == "en"

    # 4. Short / ambiguous inputs keep previous mode
    assert reply_mode("haan", previous_mode="en") == "en"
    assert reply_mode("haan", previous_mode="hi") == "hi"
    assert reply_mode("ok", previous_mode="en") == "en"
    assert reply_mode("yes", previous_mode="hi") == "hi"
    assert reply_mode("12", previous_mode="en") == "en"
    assert reply_mode("₹5000", previous_mode="hi") == "hi"
    assert reply_mode("theek hai", previous_mode="en") == "en"  # 2 words -> < 3 words -> keeps prev

    # 5. Default previous mode is 'hi'
    assert reply_mode("haan", previous_mode=None) == "hi"
    assert reply_mode("ok", previous_mode=None) == "hi"

    # 6. Mid-call language switch follows immediately
    mode = "hi"
    mode = reply_mode("Mujhe B.Tech fees batayein", mode)
    assert mode == "hi"
    mode = reply_mode("Can you explain the syllabus in English?", mode)
    assert mode == "en"
    mode = reply_mode("Sure, thanks", mode)  # 2 words -> keeps 'en'
    assert mode == "en"
    mode = reply_mode("Ab mujhe hostel ki details Hindi mein dijiye", mode)
    assert mode == "hi"


def test_check_reply_required_cases():
    # Case 1: Correct Devanagari + English reply passes
    ok, problems = check_reply("जी, B.Tech की कुल फीस ₹1,25,000 है। क्या मैं eligibility भी बताऊँ?", mode="hi")
    assert ok is True
    assert len(problems) == 0

    # Case 2: "Ji, B.Tech ki fees hai" fails in mode 'hi'
    ok, problems = check_reply("Ji, B.Tech ki fees hai", mode="hi")
    assert ok is False
    assert any("roman_hindi_word:hai" in p or "no_devanagari_in_hi_mode" in p for p in problems)

    # Case 3: An English reply in hi mode fails
    ok, problems = check_reply("You need at least 60% in 12th with Physics, Chemistry and Maths.", mode="hi")
    assert ok is False
    assert "no_devanagari_in_hi_mode" in problems

    # Case 4: A Devanagari reply in en mode fails
    ok, problems = check_reply("जी, बताइए, आप किस कोर्स के बारे में जानना चाहते हैं?", mode="en")
    assert ok is False
    assert "devanagari_in_en_mode" in problems

    # Case 5: "course ke baare main" with Devanagari around passes (English homograph "main" is ignored)
    ok, problems = check_reply("मैं आपको course के बारे main बताऊँगा।", mode="hi")
    assert ok is True
    assert len(problems) == 0


def test_check_reply_allowlist_and_acronyms():
    # Uppercase acronyms like IIT, NEET, JEE pass
    ok, problems = check_reply("आप IIT या NEET के through admission ले सकते हैं।", mode="hi")
    assert ok is True

    # Technical allow-listed terms pass in Latin
    ok, problems = check_reply("पहले आप online form भरेंगे, फिर document verification होगा।", mode="hi")
    assert ok is True

    # Pure English reply in en mode passes
    ok, problems = check_reply("You can apply online through our official portal.", mode="en")
    assert ok is True
    assert len(problems) == 0


def test_turn_hint_injection():
    messages = [
        {"role": "system", "content": "You are a counsellor."},
        {"role": "user", "content": "Mujhe B.Tech fees janni hai"}
    ]

    # Test user_suffix placement
    injected_user = inject_turn_hint(messages, mode="hi", placement="user_suffix")
    assert len(injected_user) == 2
    assert "[Instruction:" in injected_user[1]["content"]
    assert "Devanagari script" in injected_user[1]["content"]
    # Check original messages were not modified
    assert "[Instruction:" not in messages[1]["content"]

    # Test system_message placement
    injected_sys = inject_turn_hint(messages, mode="en", placement="system_message")
    assert len(injected_sys) == 3
    assert injected_sys[-1]["role"] == "system"
    assert "Reply fully in English" in injected_sys[-1]["content"]
    assert len(messages) == 2


def test_enforce_clause_streaming_and_latency():
    clause_ok = "जी, B.Tech की वार्षिक फीस ₹1,25,000 है।"
    spoken, passed, problems = enforce_clause(clause_ok, mode="hi")
    assert spoken == clause_ok
    assert passed is True
    assert len(problems) == 0

    # Violating clause: speaks unchanged without blocking, logs violation
    clause_bad = "Aapko form online bharna padega"
    spoken, passed, problems = enforce_clause(clause_bad, mode="hi")
    assert spoken == clause_bad
    assert passed is False
    assert len(problems) > 0

    # Latency benchmark: check that enforce_clause runs in under 5 ms per clause
    t0 = time.perf_counter()
    for _ in range(100):
        enforce_clause("जी, admission form online भरना होगा।", mode="hi")
    elapsed_total_ms = (time.perf_counter() - t0) * 1000.0
    avg_ms = elapsed_total_ms / 100.0
    assert avg_ms < 5.0, f"Checker took {avg_ms:.2f} ms on average (must be < 5 ms)"


@pytest.mark.asyncio
async def test_enforce_non_streamed_retry():
    # 1. Valid reply passes without retry
    async def mock_chat(messages):
        return {"text": "Should not be called"}

    reply_ok = "जी, B.Tech की कुल फीस ₹1,25,000 है।"
    final_reply, first_passed, retried, problems = await enforce_non_streamed(reply_ok, mode="hi", chat_fn=mock_chat)
    assert final_reply == reply_ok
    assert first_passed is True
    assert retried is False

    # 2. Invalid reply triggers retry and adopts successful candidate
    reply_bad = "Ji, B.Tech ki fees Rs 1,25,000 hai"
    async def mock_chat_succeed(messages):
        assert any(m["role"] == "user" and "Rewrite" in m["content"] for m in messages)
        return {"text": "जी, B.Tech की कुल फीस ₹1,25,000 है।"}

    final_reply, first_passed, retried, problems = await enforce_non_streamed(reply_bad, mode="hi", chat_fn=mock_chat_succeed)
    assert final_reply == "जी, B.Tech की कुल फीस ₹1,25,000 है।"
    assert first_passed is False
    assert retried is True

    # 3. Invalid reply where retry fails falls back to original reply
    async def mock_chat_fail(messages):
        return {"text": "Still in Roman Hindi fees hai"}

    final_reply, first_passed, retried, problems = await enforce_non_streamed(reply_bad, mode="hi", chat_fn=mock_chat_fail)
    assert final_reply == reply_bad
    assert first_passed is False
    assert retried is True


def test_conversation_prompt_switching():
    from app.ai.pipeline import Conversation, OLD_SYSTEM_PROMPT, NEW_SYSTEM_PROMPT, build_system_prompt

    # Default OFF uses old prompt
    c_off = Conversation(clients=None, script_enforcement=False)
    assert c_off.messages[0]["content"] == OLD_SYSTEM_PROMPT

    # Opt-in ON uses rewritten new prompt
    c_on = Conversation(clients=None, script_enforcement=True, counsellor_gender="masculine")
    assert c_on.messages[0]["content"] == NEW_SYSTEM_PROMPT
    assert "CORE RULES FOR SCRIPT AND VOICE DELIVERY" in c_on.messages[0]["content"]
    assert "कर सकता हूँ" in c_on.messages[0]["content"]

    # Feminine gender prompt
    c_fem = Conversation(clients=None, script_enforcement=True, counsellor_gender="feminine")
    assert "कर सकती हूँ" in c_fem.messages[0]["content"]
    assert "कर सकता हूँ" not in c_fem.messages[0]["content"]


@pytest.mark.asyncio
async def test_conversation_stream_turn_script_enforcement():
    from app.ai.pipeline import Conversation

    class MockClients:
        def __init__(self):
            self.cfg = {}
            self.last_chat_messages = []

        async def transcribe(self, pcm, rate, lang):
            return {"text": "Mujhe B.Tech ki fees janni hai", "language": "hi"}

        async def chat(self, messages, tools=None):
            self.last_chat_messages = list(messages)
            return {"text": "जी, B.Tech की कुल फीस ₹1,25,000 है। क्या मैं eligibility भी बताऊँ?"}

        async def synthesize(self, sentence, lang):
            import wave, io
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(b"\x00\x00" * 1600)
            return buf.getvalue(), 16000

    clients = MockClients()
    conv = Conversation(clients=clients, script_enforcement=True)

    chunks = []
    async for chunk in conv.stream_turn(b"\x00" * 3200, 16000):
        chunks.append(chunk)

    assert len(chunks) > 0
    # Mode was detected as 'hi'
    assert conv.current_mode == "hi"
    assert chunks[0].mode == "hi"
    assert chunks[0].first_gen_passed is True
    assert chunks[0].script_check["first_gen_passed"] is True
    assert chunks[0].script_check["clauses_violated"] == 0

    # Verify hint was injected into the request sent to Brain
    assert any("[Instruction:" in m.get("content", "") for m in clients.last_chat_messages)
    # But NOT kept in conversation history
    assert not any("[Instruction:" in m.get("content", "") for m in conv.messages)

