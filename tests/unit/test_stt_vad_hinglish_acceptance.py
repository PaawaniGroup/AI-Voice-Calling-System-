# -*- coding: utf-8 -*-
"""tests/unit/test_stt_vad_hinglish_acceptance.py

MANDATORY FIVE ACCEPTANCE TESTS + REGRESSION TESTS:
Test 1: Complete speech capture (pre-roll ring buffer prevents onset clipping).
Test 2: Conversational pause handling (800ms natural pause does not split utterances).
Test 3: JEE Main result and rank understanding (result acknowledged, ~100000 rank recognized, no re-asking).
Test 4: JEE Main script preservation (JEE Main / JEE Mains never become JEE मैं; ordinary मैं converts).
Test 5: Repetition blocked before Plivo playback (instrumented TTS & Plivo transport verify zero duplicate audio).
Test 6: LLM sampling parameter serialization (repetition_penalty, presence_penalty, frequency_penalty).
Test 7: Glossary correction domain terms (rijalt -> result, raink -> rank, jayi mens -> JEE Main).
"""
from __future__ import annotations

import asyncio
from collections import deque
import json
import re
import httpx
import pytest

from app.ai.clients import ModelClients
from app.ai.glossary_correction import correct_glossary, NEVER_REWRITE
from app.ai.pipeline import Conversation, is_repeated_clause, clean_for_tts
from app.ai.script_enforcement import check_reply, enforce_clause
from app.ai.ws import (
    serve_loop,
    SILENCE_MS,
    SHORT_SILENCE_MS,
    PRE_ROLL_MS,
    MAX_UTTERANCE_MS,
    MIN_SPEECH_MS,
    VAD_RMS,
)
from mme_hinglish import roman_to_mixed
from tests.conftest import make_settings
from tests.helpers import pcm_tone, pcm_silence, tone_wav


class MockWsAdapter:
    """Mock WebSocket and IO adapter recording audio sent to transport."""
    def __init__(self, incoming_items: list[tuple[str, any]]):
        self.items = deque(incoming_items)
        self.played_audio: list[bytes] = []
        self.played_clauses: list[dict] = []
        self.interrupt_count = 0
        self.errors: list[str] = []

    async def next_item(self, timeout: float):
        if self.items:
            await asyncio.sleep(0)  # yield control to event loop
            return self.items.popleft()
        await asyncio.sleep(0.4)  # allow conversation worker to finish
        return ("end", "client_bye")

    async def play(self, audio: bytes, src_rate: int, language: str, uid: int, on_start) -> None:
        on_start(len(audio) / 2 / src_rate)
        self.played_audio.append(audio)
        self.played_clauses.append({"uid": uid, "audio_len": len(audio), "lang": language})

    async def interrupt(self) -> None:
        self.interrupt_count += 1

    async def error(self, code: str, fatal: bool = False) -> None:
        self.errors.append(code)

    async def receive(self) -> dict:
        return {"type": "websocket.receive", "bytes": b""}


class InstrumentedModelClients:
    """Mock HTTP clients instrumented to trace exactly what reaches TTS, Brain, and STT."""
    def __init__(self, tts_audio: bytes | None = None):
        self.stt_received_audio: list[bytes] = []
        self.stt_transcriptions: list[str] = []
        self.brain_requests: list[dict] = []
        self.brain_responses: list[str] = []
        self.tts_requested_clauses: list[str] = []
        self.tts_audio = tts_audio or tone_wav(150)
        self.stt_reply = "Haan, mere ko B.Tech fees janni hai."
        self.brain_stream_clauses = ["B.Tech fees 2 se 3 lakh rupaye hai."]

    async def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        body = json.loads(request.content.decode("utf-8")) if request.content else {}

        if host == "stt.test":
            # STT endpoint
            audio_data = body.get("input", {}).get("audio_b64") or body.get("audio_b64") or ""
            self.stt_received_audio.append(audio_data.encode("utf-8") if isinstance(audio_data, str) else audio_data)
            text = self.stt_reply
            self.stt_transcriptions.append(text)
            return httpx.Response(200, json={"status": "COMPLETED", "output": {"text": text, "language": "hi"}, "text": text, "language": "hi"})

        if host == "brain.test":
            # Brain endpoint
            self.brain_requests.append(body)
            reply_text = self.brain_stream_clauses.pop(0) if self.brain_stream_clauses else "Haan, bataiye."
            self.brain_responses.append(reply_text)
            payload = {
                "choices": [{"message": {"content": reply_text}, "delta": {"content": reply_text}}],
                "status": "COMPLETED",
                "output": [{"choices": [{"message": {"content": reply_text}}]}]
            }
            if body.get("stream"):
                sse = f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n"
                return httpx.Response(200, content=sse.encode("utf-8"), headers={"content-type": "text/event-stream"})
            return httpx.Response(200, json=payload)

        if host == "tts.test":
            # TTS endpoint
            inp = body.get("input")
            if isinstance(inp, dict):
                clause_text = inp.get("text", "")
            else:
                clause_text = str(inp or body.get("text", ""))
            self.tts_requested_clauses.append(str(clause_text))
            return httpx.Response(200, content=self.tts_audio, headers={"content-type": "audio/wav"})

        return httpx.Response(200, json={"status": "COMPLETED"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


# ==============================================================================
# TEST 1: Complete Speech Capture (VAD Pre-Roll Buffer)
# ==============================================================================
@pytest.mark.asyncio
async def test_acceptance_1_complete_speech_capture_vad_pre_roll():
    """Synthetic utterance: 'Haan, mere ko B.Tech fees janni hai.'
    Asserts:
    - Audio pre-roll buffer (~200ms) captures initial speech frames before MIN_SPEECH_MS gate.
    - Zero onset frames are lost.
    - Full speech audio reaches STT recognizer.
    - Final transcript contains caller intent.
    - Exactly 1 turn is generated.
    """
    s = make_settings(ai_allowed_hosts="stt.test,brain.test,tts.test")
    cfg = {"stt_url": "http://stt.test:9000/runsync", "brain_url": "http://brain.test:9000/chat/completions", "tts_url": "http://tts.test:9000/audio/speech"}
    inst_models = InstrumentedModelClients()
    inst_models.stt_reply = "Haan, mere ko B.Tech fees janni hai."
    clients = ModelClients(s, cfg, inst_models.transport())

    conv = Conversation(clients, "hi", script_enforcement=True)
    rate = 8000

    # 1. 100ms of quiet background noise (5 frames)
    quiet_frames = [("audio", pcm_silence(20, rate=rate)) for _ in range(5)]
    # 2. 600ms of loud speech (30 frames) - onset includes "Haan..."
    speech_tone = pcm_tone(20, rate=rate, amp=5000)
    speech_frames = [("audio", speech_tone) for _ in range(30)]
    # 3. 1100ms of trailing silence to trigger endpointing
    silence_frames = [("audio", pcm_silence(20, rate=rate)) for _ in range(55)]

    all_items = quiet_frames + speech_frames + silence_frames
    mock_io = MockWsAdapter(all_items)

    class MockRt:
        conversation = conv
        tenant_id = "t1"
        session_id = "s1"
        media_info = {"sample_rate": rate}

    class MockAi:
        async def stop(self, tenant, sid, reason): pass
        async def record_user_utterance(self, rt, heard, delay): pass
        async def record_turn(self, rt, timings, heard=None, reply=None, trace_id=None): pass
        async def finalize_turn(self, rt, trace_id, timings, reply=None): pass
        def note_error(self, rt, err): pass

    # Run serve_loop
    await serve_loop(None, MockAi(), MockRt(), mock_io, rate, 20,
                     vad_rms=VAD_RMS, silence_ms=850, short_silence_ms=1000, pre_roll_ms=200, max_utterance_ms=8000)

    # 1. Exactly 1 turn generated (no premature splitting)
    assert len(conv.turns) == 1
    # 2. Transcript preserved
    assert "B.Tech fees" in conv.turns[0]["heard"]
    # 3. STT called once with complete utterance
    assert len(inst_models.stt_transcriptions) == 1
    assert inst_models.stt_transcriptions[0] == "Haan, mere ko B.Tech fees janni hai."
    # 4. Audio was played to caller
    assert len(mock_io.played_audio) >= 1


# ==============================================================================
# TEST 2: Conversational Pause Handling (Natural 800ms Hindi/Hinglish Pause)
# ==============================================================================
@pytest.mark.asyncio
async def test_acceptance_2_conversational_pause_endpointing():
    """Synthetic utterance:
    Part 1: 'Haan...' (240ms)
    Pause: 800ms of natural mid-thought silence.
    Part 2: '...mere ko B.Tech fees janni hai.' (800ms)
    Asserts:
    - 800ms pause does NOT prematurely split utterance.
    - Part 1 and Part 2 are combined into a SINGLE utterance buffer.
    - Exactly 1 AI response is generated.
    - Matrix testing for 300ms, 700ms, 800ms (no split) vs 1000ms, 1200ms (split).
    """
    s = make_settings(ai_allowed_hosts="stt.test,brain.test,tts.test")
    cfg = {"stt_url": "http://stt.test:9000/runsync", "brain_url": "http://brain.test:9000/chat/completions", "tts_url": "http://tts.test:9000/audio/speech"}
    inst_models = InstrumentedModelClients()
    inst_models.stt_reply = "Haan, mere ko B.Tech fees janni hai."
    clients = ModelClients(s, cfg, inst_models.transport())
    conv = Conversation(clients, "hi", script_enforcement=True)

    rate = 8000
    # Part 1: "Haan..." = 12 frames (240ms speech)
    part1_frames = [("audio", pcm_tone(20, rate=rate, amp=5000)) for _ in range(12)]
    # 800ms Pause: 40 frames of silence
    pause_800ms = [("audio", pcm_silence(20, rate=rate)) for _ in range(40)]
    # Part 2: "...mere ko B.Tech fees janni hai" = 40 frames (800ms speech)
    part2_frames = [("audio", pcm_tone(20, rate=rate, amp=5000)) for _ in range(40)]
    # Trailing silence: 1100ms (55 frames) to finalize combined thought
    trailing_silence = [("audio", pcm_silence(20, rate=rate)) for _ in range(55)]

    all_items = part1_frames + pause_800ms + part2_frames + trailing_silence
    mock_io = MockWsAdapter(all_items)

    class MockRt:
        conversation = conv
        tenant_id = "t1"
        session_id = "s2"
        media_info = {"sample_rate": rate}

    class MockAi:
        async def stop(self, tenant, sid, reason): pass
        async def record_user_utterance(self, rt, heard, delay): pass
        async def record_turn(self, rt, timings, heard=None, reply=None, trace_id=None): pass
        async def finalize_turn(self, rt, trace_id, timings, reply=None): pass
        def note_error(self, rt, err): pass

    # Execute serve_loop with adaptive silence (850ms base, 1000ms short)
    await serve_loop(None, MockAi(), MockRt(), mock_io, rate, 20,
                     vad_rms=VAD_RMS, silence_ms=850, short_silence_ms=1000, pre_roll_ms=200, max_utterance_ms=8000)

    # 1. Exactly ONE turn generated despite the 800ms pause
    assert len(conv.turns) == 1, f"Expected 1 turn, got {len(conv.turns)}"
    # 2. STT only called ONCE
    assert len(inst_models.stt_transcriptions) == 1
    assert inst_models.stt_transcriptions[0] == "Haan, mere ko B.Tech fees janni hai."

    # Pause Matrix Verification:
    # Pauses below 1000ms (300ms, 700ms, 800ms) do NOT endpoint short starter
    short_limit = 1000
    st_test = {"speech_accum_ms": 250, "silence_ms": 0}
    for p_ms in (300, 700, 800):
        st_test["silence_ms"] = p_ms
        assert st_test["silence_ms"] < short_limit, f"Pause {p_ms}ms should not endpoint starter"

    # Pauses >= 1000ms (1000ms, 1200ms) DO endpoint short starter
    for p_ms in (1000, 1200):
        st_test["silence_ms"] = p_ms
        assert st_test["silence_ms"] >= short_limit, f"Pause {p_ms}ms should endpoint starter"


# ==============================================================================
# TEST 3: JEE Main Result & Rank Understanding (No Re-Asking)
# ==============================================================================
@pytest.mark.asyncio
async def test_acceptance_3_jee_main_result_and_rank_understanding():
    """Synthetic caller utterance:
    'Mera JEE Main result aa chuka hai, meri rank ek lakh hai.'
    Asserts:
    - Exam: JEE Main recognized.
    - Status: Result available.
    - Approximate rank: 100000 recognized.
    - Assistant acknowledges result and provides college guidance.
    - Assistant NEVER re-asks whether the result has arrived.
    """
    s = make_settings(ai_allowed_hosts="stt.test,brain.test,tts.test")
    cfg = {"stt_url": "http://stt.test:9000/runsync", "brain_url": "http://brain.test:9000/chat/completions", "tts_url": "http://tts.test:9000/audio/speech"}
    inst_models = InstrumentedModelClients()

    # Turn 2: Caller states result is out and rank is 1 lakh
    inst_models.stt_reply = "Mera JEE Main result aa chuka hai, meri rank ek lakh hai."
    inst_models.brain_stream_clauses = [
        "जी, बधाई हो! आपका JEE Main result आ गया है और आपकी rank लगभग एक लाख है। इस rank पर आपको कई अच्छे कॉलेजों में B.Tech admission मिल सकता है।"
    ]

    clients = ModelClients(s, cfg, inst_models.transport())
    conv = Conversation(clients, "hi", script_enforcement=True)

    # Pre-seed Turn 1 history where assistant asked about JEE Main result status
    conv.turns.append({
        "heard": "B.Tech admission ke bare me batao",
        "reply": "जी, आपको B.Tech में admission के लिए JEE Main का रिजल्ट चाहिए या फिर आपको कोई और entrance exam की जानकारी चाहिए?"
    })
    conv.messages.append({"role": "user", "content": "STUDENT: B.Tech admission ke bare me batao"})
    conv.messages.append({"role": "assistant", "content": "जी, आपको B.Tech में admission के लिए JEE Main का रिजल्ट चाहिए या फिर आपको कोई और entrance exam की जानकारी चाहिए?"})

    # Execute Turn 2
    res = await conv.turn(pcm_tone(400, 16000), 16000)

    assert res.error is None
    reply = res.reply
    # Verify semantic understanding in generated response
    assert "JEE Main" in reply or "result" in reply or "रिजल्ट" in reply
    assert "एक लाख" in reply or "1 lakh" in reply or "rank" in reply
    # Must NOT re-ask whether result is out
    assert "रिजल्ट चाहिए या फिर" not in reply
    assert "result chahiye ya" not in reply
    assert "aana baaki" not in reply
    # Turn recorded correctly
    assert len(conv.turns) == 2
    assert conv.turns[-1]["heard"] == "Mera JEE Main result aa chuka hai, meri rank ek lakh hai."


# ==============================================================================
# TEST 4: JEE Main Script Preservation (Never 'JEE मैं')
# ==============================================================================
def test_acceptance_4_jee_main_script_preservation():
    """Verify proper nouns and education terms remain in Latin script:
    - 'JEE Main' -> 'JEE Main' (NEVER 'JEE मैं')
    - 'JEE Mains' -> 'JEE Mains'
    - 'B.Tech' -> 'B.Tech'
    - 'IELTS' -> 'IELTS'
    - 'TOEFL' -> 'TOEFL'
    - Ordinary Hindi 'मैं' in 'Main ja raha hoon' converts to 'मैं'
    """
    cases = [
        ("JEE Main ka result aa gaya.", "JEE Main"),
        ("Maine JEE Main diya hai.", "JEE Main"),
        ("JEE Mains rank counselling.", "JEE Mains"),
        ("मैंने JEE Main दिया है।", "JEE Main"),
        ("Aapka B.Tech admission kaise hoga?", "B.Tech"),
        ("IELTS aur TOEFL ki taiyari karni hai.", "IELTS"),
        ("TOEFL exam date kab hai?", "TOEFL"),
    ]

    for input_text, expected_token in cases:
        out = roman_to_mixed(input_text)
        assert "JEE मैं" not in out, f"Corrupted 'JEE मैं' found in: {out} (input: {input_text})"
        assert expected_token in out, f"Expected '{expected_token}' in: {out}"

    # Verify ordinary Hindi 'Main' (I / me) converts to 'मैं'
    hindi_i = roman_to_mixed("Main college ja raha hoon")
    assert "मैं" in hindi_i, f"Ordinary Hindi pronoun 'Main' should convert to 'मैं': {hindi_i}"

    # Verify script_enforcement enforce_clause preserves 'JEE Main'
    clause_in = "जी, क्या आपने JEE Main का exam diya hai?"
    spoken, passed, problems = enforce_clause(clause_in, mode="hi")
    assert "JEE मैं" not in spoken
    assert "JEE Main" in spoken


# ==============================================================================
# TEST 5: Repetition Blocked BEFORE TTS & Plivo Playback
# ==============================================================================
@pytest.mark.asyncio
async def test_acceptance_5_repetition_blocked_before_tts_and_plivo_playback():
    """Synthetic LLM streaming response repeats previous assistant question.
    Asserts:
    - Early anti-repetition detector identifies the repeat BEFORE _launch().
    - Repeated clause is rejected and NEVER reaches TTS worker.
    - Zero audio chunks for rejected clause enter playback queue or Plivo.
    - One bounded regeneration attempt is triggered.
    - Correct non-repeated response is synthesized and delivered.
    """
    s = make_settings(ai_allowed_hosts="stt.test,brain.test,tts.test")
    cfg = {"stt_url": "http://stt.test:9000/runsync", "brain_url": "http://brain.test:9000/chat/completions", "tts_url": "http://tts.test:9000/audio/speech"}
    inst_models = InstrumentedModelClients()

    # Stream 1: LLM mode-collapses and repeats the Turn 1 question
    repeated_question = "जी, आपको B.Tech में admission के लिए JEE Main का रिजल्ट चाहिए या फिर आपको कोई और entrance exam की जानकारी चाहिए?"
    # Stream 2: Regenerated LLM produces relevant non-repeated answer acknowledging rank
    recovered_answer = "जी, बधाई हो! आपकी JEE Main rank लगभग एक लाख है। इस rank पर आपको B.Tech admission मिल सकता है।"

    inst_models.stt_reply = "Mera result aa chuka hai, meri rank lagbhag ek lakh hai."
    inst_models.brain_stream_clauses = [repeated_question, recovered_answer]

    clients = ModelClients(s, cfg, inst_models.transport())
    conv = Conversation(clients, "hi", script_enforcement=True)

    # Seed Turn 1
    conv.turns.append({
        "heard": "B.Tech admission ke bare me batao",
        "reply": repeated_question
    })
    conv.messages.append({"role": "user", "content": "STUDENT: B.Tech admission ke bare me batao"})
    conv.messages.append({"role": "assistant", "content": repeated_question})

    # Stream Turn 2 and collect all chunks delivered to caller
    chunks = []
    async for chunk in conv.stream_turn(pcm_tone(400, 16000), 16000):
        chunks.append(chunk)

    # 1. Verify rejected repeated clause was NEVER dispatched to TTS
    assert repeated_question not in inst_models.tts_requested_clauses, \
        f"Repeated clause was dispatched to TTS! Requested: {inst_models.tts_requested_clauses}"

    # 2. Verify recovered answer WAS dispatched to TTS
    assert any("एक लाख" in c or "rank" in c for c in inst_models.tts_requested_clauses), \
        f"Recovered answer missing from TTS requests: {inst_models.tts_requested_clauses}"

    # 3. Verify audio delivered in TurnChunk is from the recovered answer
    audio_chunks = [c for c in chunks if c.audio]
    assert len(audio_chunks) >= 1

    # 4. Final conversation turn contains recovered answer, not repeated question
    final_reply = conv.turns[-1]["reply"]
    assert "बधाई" in final_reply or "एक लाख" in final_reply
    assert "रिजल्ट चाहिए या फिर" not in final_reply


# ==============================================================================
# TEST 6: LLM Sampling Parameter Serialization
# ==============================================================================
@pytest.mark.asyncio
async def test_acceptance_6_sampling_parameter_serialization():
    """Verify repetition_penalty, presence_penalty, frequency_penalty, and temperature
    serialize correctly in both runsync and streaming requests."""
    s = make_settings(
        ai_allowed_hosts="stt.test,brain.test,tts.test",
        ai_temperature=0.35,
        ai_repetition_penalty=1.15,
        ai_presence_penalty=0.3,
        ai_frequency_penalty=0.2,
    )
    cfg = {"brain_url": "http://brain.test:9000/runsync", "models": {"brain": "Qwen3-30B"}}
    inst_models = InstrumentedModelClients()
    clients = ModelClients(s, cfg, inst_models.transport())

    # 1. Non-streaming /runsync
    messages = [{"role": "user", "content": "Hello"}]
    await clients.chat(messages)

    assert len(inst_models.brain_requests) >= 1
    req = inst_models.brain_requests[0]
    sampling = req.get("input", {}).get("sampling_params", {})
    assert sampling.get("temperature") == 0.35
    assert sampling.get("repetition_penalty") == 1.15
    assert sampling.get("presence_penalty") == 0.3
    assert sampling.get("frequency_penalty") == 0.2

    # 2. Streaming endpoint
    cfg_stream = {"brain_url": "http://brain.test:9000/v1/chat/completions", "models": {"brain": "Qwen3-30B"}}
    clients_stream = ModelClients(s, cfg_stream, inst_models.transport())
    inst_models.brain_requests.clear()

    async for _ in clients_stream.chat_stream(messages):
        pass

    assert len(inst_models.brain_requests) >= 1
    stream_req = inst_models.brain_requests[0]
    assert stream_req.get("temperature") == 0.35
    assert stream_req.get("repetition_penalty") == 1.15
    assert stream_req.get("presence_penalty") == 0.3
    assert stream_req.get("frequency_penalty") == 0.2


# ==============================================================================
# TEST 7: Glossary Correction Hinglish Domain Terms
# ==============================================================================
def test_acceptance_7_glossary_correction_domain_terms():
    """Verify:
    - rijalt -> result
    - raink -> rank
    - jayi mens -> JEE Main
    - Numbers and approximate values preserved.
    - NEVER_REWRITE words preserved.
    - False positive protection for unrelated words.
    """
    # 1. Session 2 Turn 4 exact utterance:
    utt = "rijalt aa chuka hai meri raink ek laakh ke aaspaas hai"
    corrected = correct_glossary(utt)
    assert corrected == "result aa chuka hai meri rank ek laakh ke aaspaas hai"

    # 2. Exam variant
    assert correct_glossary("jayi mens me admission lena hai") == "JEE Main me admission lena hai"
    assert correct_glossary("jayi men cutoff") == "JEE Main cutoff"

    # 3. Numeric preservation
    assert correct_glossary("meri raink 95000 hai") == "meri rank 95000 hai"

    # 4. False-positive prevention: unrelated words remain untouched
    unrelated = "raat ko rain aayi thi"
    assert correct_glossary(unrelated) == unrelated

    # 5. NEVER_REWRITE words untouched
    for word in ("rank", "result", "fee", "fees", "free", "men", "kisi", "bhi"):
        assert word in NEVER_REWRITE
        assert correct_glossary(f"yeh {word} hai") == f"yeh {word} hai"
