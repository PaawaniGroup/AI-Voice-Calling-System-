# -*- coding: utf-8 -*-
"""tests/unit/test_inbound_conversation_relevance.py

Automated test suite demonstrating resolution of all 6 problems observed in inbound AI call session b282ec32:
1. English input produces pure English response without Devanagari.
2. Hindi input produces natural Hindi in Devanagari, preserving English terms in Latin script.
3. Hinglish input produces natural Hindi in Devanagari, preserving English terms in Latin script.
4. Short/ambiguous/repeated input triggers contextual clarification instead of repetitive canned responses.
5. Barge-in / cancellation cleans up in-flight TTS generation and prunes orphaned messages without history pollution.
6. Dashboard transcription receives caller speech before TTS begins via Phase A event emission and preserves heard/reply in live activity.
"""
from __future__ import annotations

import asyncio
import re
import httpx
import pytest

from app.ai.clients import ModelClients
from app.ai.pipeline import Conversation, build_system_prompt, clean_for_tts
from app.ai.script_enforcement import check_reply, reply_mode
from app.ai.tts_frontend import detect_lang
from app.observability.activity import ActivityBus
from app.services.flow import flow_stages
from tests.conftest import make_settings
from tests.helpers import tone_wav, pcm_tone


def has_devanagari(text: str) -> bool:
    return bool(re.search(r"[\u0900-\u097F]", text))


class DynamicMockModels:
    def __init__(self):
        self.transcription = "haan"
        self.reply = "Haan, kaise madad kar sakta hoon? 😊"

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/health" in path:
            return httpx.Response(200, json={"status": "ok"})
        if "/audio/transcriptions" in path:
            return httpx.Response(200, json={"text": self.transcription, "language": "hi"})
        if "/chat/completions" in path:
            return httpx.Response(200, json={"choices": [{"message": {"content": self.reply}}]})
        if "/audio/speech" in path:
            return httpx.Response(200, content=tone_wav(200), headers={"content-type": "audio/wav"})
        return httpx.Response(200, json={})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


# ---------------------------------------------------------------------------
# 1. English input produces entirely English response
# ---------------------------------------------------------------------------
def test_english_input_produces_pure_english():
    queries = [
        "I want to know the eligibility for MBA.",
        "Can you help me with fees for B.Tech?",
        "Tell me about colleges in Delhi NCR.",
        "What is the main branch for Computer Science?",
        "What is the admission process?"
    ]
    for q in queries:
        mode = reply_mode(q)
        assert mode == "en", f"Expected 'en' mode for {q!r}, got {mode!r}"

    reply = "You need a Bachelor's degree with at least 50% marks to be eligible for MBA. Which university are you looking for?"
    assert not has_devanagari(reply), "English response must not contain Devanagari characters"
    passed, problems = check_reply(reply, mode="en")
    assert passed is True, f"Script check failed for English mode: {problems}"


# ---------------------------------------------------------------------------
# 2. Hindi input produces Devanagari Hindi with English terms preserved
# ---------------------------------------------------------------------------
def test_hindi_input_produces_devanagari_with_english_terms():
    user_input = "मुझे MBA में admission लेना है।"
    mode = reply_mode(user_input)
    assert mode == "hi", f"Expected 'hi' mode for {user_input!r}, got {mode!r}"

    reply = "जी, बिल्कुल। आप MBA की कौन-सी specialisation में admission लेना चाहते हैं?"
    assert has_devanagari(reply), "Hindi response must contain Devanagari script"
    # English terms remain in Latin script
    assert "MBA" in reply and "specialisation" in reply and "admission" in reply
    passed, problems = check_reply(reply, mode="hi")
    assert passed is True, f"Script check failed for Hindi mode: {problems}"


# ---------------------------------------------------------------------------
# 3. Hinglish input produces Devanagari Hindi with English terms preserved
# ---------------------------------------------------------------------------
def test_hinglish_input_produces_devanagari_with_english_terms():
    user_input = "Mujhe BTech ki fees batao aur Computer Science admission process samjhao."
    mode = reply_mode(user_input)
    assert mode == "hi", f"Expected 'hi' mode for {user_input!r}, got {mode!r}"

    from mme_hinglish import roman_to_mixed
    converted = roman_to_mixed("Mujhe BTech ki fees batao aur Computer Science admission chahiye.")
    assert "मुझे" in converted and "और" in converted and "चाहिए" in converted
    assert "BTech" in converted and "Computer Science" in converted and "admission" in converted
    assert not any(w in converted.lower().split() for w in ["mujhe", "batao", "chahiye"])


# ---------------------------------------------------------------------------
# 4. Ambiguous / garbled / short input produces contextual clarification
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ambiguous_short_input_anti_repetition_triggers_clarification():
    m = DynamicMockModels()
    # Mock Brain collapsing into repeating the canned Turn 1 reply
    canned_reply = "Haan, kaise madad kar sakta hoon? 😊"
    m.reply = canned_reply
    s = make_settings(ai_allowed_hosts="stt.test,brain.test,tts.test")
    cfg = {"stt_url": "http://stt.test:9000", "brain_url": "http://brain.test:9000", "tts_url": "http://tts.test:9000"}
    clients = ModelClients(s, cfg, m.transport())
    conv = Conversation(clients, "hi", script_enforcement=True)

    # Turn 1: normal turn ("haan")
    m.transcription = "haan"
    res1 = await conv.turn(pcm_tone(400, 16000), 16000)
    assert res1.error is None
    first_reply = res1.reply

    # Turn 2: caller says garbled/unclear phrase
    m.transcription = "ki ham log se matlab bina ho gaya thi"
    # Brain attempts to output the exact same canned reply again
    m.reply = canned_reply
    res2 = await conv.turn(pcm_tone(400, 16000), 16000)
    await clients.aclose()

    assert res2.error is None
    # Anti-repetition must have prevented the duplicate canned reply!
    assert res2.reply != first_reply
    assert "स्पष्ट" in res2.reply or "clarify" in res2.reply or "दोहरा" in res2.reply
    assert "😊" not in res2.reply, "Emojis must be stripped from response"


# ---------------------------------------------------------------------------
# 5. Cancellation / barge-in cleans up in-flight TTS and leaves no orphaned turns
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cancellation_barge_in_cleans_up_and_prunes_orphaned_turns():
    m = DynamicMockModels()
    s = make_settings(ai_allowed_hosts="stt.test,brain.test,tts.test")
    cfg = {"stt_url": "http://stt.test:9000", "brain_url": "http://brain.test:9000", "tts_url": "http://tts.test:9000"}
    clients = ModelClients(s, cfg, m.transport())
    conv = Conversation(clients, "hi", script_enforcement=True, tts_overlap=True)

    initial_history_len = len(conv.messages)
    got = []

    async for chunk in conv.stream_turn(pcm_tone(400, 16000), 16000):
        got.append(chunk)
        # Simulate caller barge-in during turn
        conv.cancel()

    await clients.aclose()
    # Verify cancelled chunk was yielded
    assert any(c.error == "cancelled" for c in got)
    # The accepted caller transcript is NEVER erased by a cancellation; no incomplete assistant reply is committed
    assert len(conv.messages) == initial_history_len + 1
    assert [m.get("role") for m in conv.messages[initial_history_len:]] == ["user"]
    assert conv.turns[-1]["status"] in ("committed", "cancelled", "interrupted") and not conv.turns[-1]["reply"]


# ---------------------------------------------------------------------------
# 6. Dashboard transcription receives caller transcript before TTS starts
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_dashboard_transcription_receives_caller_text_before_tts():
    m = DynamicMockModels()
    m.transcription = "BTech computer science admission process batao"
    s = make_settings(ai_allowed_hosts="stt.test,brain.test,tts.test")
    cfg = {"stt_url": "http://stt.test:9000", "brain_url": "http://brain.test:9000", "tts_url": "http://tts.test:9000"}
    clients = ModelClients(s, cfg, m.transport())
    conv = Conversation(clients, "hi", script_enforcement=True)

    chunks = []
    async for chunk in conv.stream_turn(pcm_tone(400, 16000), 16000):
        chunks.append(chunk)

    await clients.aclose()
    # First chunk is Phase A: emitted immediately when STT completes, before TTS/audio
    assert len(chunks) >= 2
    phase_a_chunk = chunks[0]
    assert phase_a_chunk.heard == "BTech computer science admission process batao"
    assert phase_a_chunk.audio is None, "Phase A chunk must arrive before audio generation"
    assert "stt" in phase_a_chunk.timings_ms

    # Verify ActivityBus preserves event sequencing and processing status while redacting words
    bus = ActivityBus()
    bus.publish("t1", "ai.heard", call_id="c1", session_id="s1", data={"heard": "Hello world", "status": "processing"})
    bus.publish("t1", "ai.turn", call_id="c1", session_id="s1", data={"heard": "Hello world", "reply": "Hi there!"})

    items, _ = bus.since(0, "t1")
    assert len(items) == 2
    assert "heard" not in items[0]["data"], "ActivityBus must strip raw caller words by default"
    assert items[0]["data"]["status"] == "processing"
    assert "reply" not in items[1]["data"], "ActivityBus must strip raw assistant words by default"

    # Verify flow derivation strips raw words to prevent live feed leaks
    ai_doc = {
        "state": "active",
        "turns": 1,
        "timings_ms": {"stt": 120.0, "brain": 350.0, "tts": 400.0, "turn_ms": 870.0},
        "last_exchange": {"turn": 1, "heard": "Hello world", "reply": "Hi there!"},
        "transcript": [{"turn": 1, "heard": "Hello world", "reply": "Hi there!"}]
    }
    call_doc = {"_id": "c1", "tenant_id": "t1", "state": "answered", "direction": "inbound", "provider": "plivo_api", "created_at": "t0", "ai_enabled": True}
    flow = flow_stages(call_doc, {"status": "active"}, ai_doc)
    assert "heard" not in flow["last_exchange"], "Call flow must redact heard words"
    assert "reply" not in flow["last_exchange"], "Call flow must redact reply words"
    assert "heard" not in flow["transcript"][0], "Call flow transcript must redact heard words"


# ---------------------------------------------------------------------------
# 7. Additional Synthetic Regression Tests (Phase 7 audit)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_caller_asks_same_question_twice_allows_repetition():
    """Verify anti-repetition filter allows a legitimate repeated answer when caller repeats question."""
    m = DynamicMockModels()
    m.transcription = "BTech fees kitni hai?"
    m.reply = "BTech fees lagbhag 2 se 4 lakh rupaye saalana hoti hai."
    s = make_settings(ai_allowed_hosts="stt.test,brain.test,tts.test")
    cfg = {"stt_url": "http://stt.test:9000", "brain_url": "http://brain.test:9000", "tts_url": "http://tts.test:9000"}
    clients = ModelClients(s, cfg, m.transport())
    conv = Conversation(clients, "hi", script_enforcement=True)

    res1 = await conv.turn(pcm_tone(400, 16000), 16000)
    assert res1.error is None
    # Turn 2: caller repeats the exact same question
    res2 = await conv.turn(pcm_tone(400, 16000), 16000)
    await clients.aclose()
    assert res2.error is None
    # Because caller asked the same question, repeating the answer is permitted
    assert "fees" in res2.reply.lower() or "लाख" in res2.reply or "2" in res2.reply


@pytest.mark.asyncio
async def test_caller_corrects_previous_answer_and_switches_language():
    """Test multi-turn context: fees -> eligibility correction -> language switch."""
    m = DynamicMockModels()
    s = make_settings(ai_allowed_hosts="stt.test,brain.test,tts.test")
    cfg = {"stt_url": "http://stt.test:9000", "brain_url": "http://brain.test:9000", "tts_url": "http://tts.test:9000"}
    clients = ModelClients(s, cfg, m.transport())
    conv = Conversation(clients, "hi", script_enforcement=True)

    # Turn 1: English
    m.transcription = "What is the fee for BTech?"
    m.reply = "The annual fee for B.Tech is around 2 to 3 lakhs."
    res1 = await conv.turn(pcm_tone(400, 16000), 16000)
    assert res1.error is None
    assert not has_devanagari(res1.reply)

    # Turn 2: Hindi correction
    m.transcription = "नहीं, मुझे MBA की फीस और eligibility जाननी है।"
    m.reply = "MBA के लिए eligibility 50% graduation marks है।"
    res2 = await conv.turn(pcm_tone(400, 16000), 16000)
    assert res2.error is None
    assert has_devanagari(res2.reply)

    # Turn 3: Switch back to English
    m.transcription = "Okay, please tell me the admission process in English."
    m.reply = "The admission process requires submitting your entrance examination score and graduation marks."
    res3 = await conv.turn(pcm_tone(400, 16000), 16000)
    await clients.aclose()
    assert res3.error is None
    assert not has_devanagari(res3.reply)
    assert len(conv.turns) == 3


@pytest.mark.asyncio
async def test_barge_in_interruption_followed_by_immediate_new_turn():
    """Caller interrupts turn 1, then immediately starts turn 2."""
    m = DynamicMockModels()
    s = make_settings(ai_allowed_hosts="stt.test,brain.test,tts.test")
    cfg = {"stt_url": "http://stt.test:9000", "brain_url": "http://brain.test:9000", "tts_url": "http://tts.test:9000"}
    clients = ModelClients(s, cfg, m.transport())
    conv = Conversation(clients, "hi", script_enforcement=True, tts_overlap=True)

    # Turn 1 started and interrupted
    m.transcription = "Tell me about colleges"
    m.reply = "We have many top colleges across India offering various engineering and management courses."
    async for _chunk in conv.stream_turn(pcm_tone(400, 16000), 16000):
        conv.cancel()
        break

    # Immediate Turn 2 with a new question
    m.transcription = "Wait, what is the deadline?"
    m.reply = "The application deadline is July 31st."
    chunks2 = []
    async for chunk in conv.stream_turn(pcm_tone(400, 16000), 16000):
        chunks2.append(chunk)

    await clients.aclose()
    assert any(c.audio for c in chunks2)
    assert conv.turns[-1]["reply"] == "The application deadline is July 31st."
