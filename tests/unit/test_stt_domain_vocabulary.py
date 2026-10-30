"""Unit tests for STT Domain Vocabulary prompting.

Validates:
1. DEFAULT_DOMAIN_VOCABULARY is present, maintainable, and contains education terms.
2. STT Worker v2 handler applies default domain vocabulary when request prompt is omitted or empty.
3. Custom request prompt overrides the default domain vocabulary.
4. ModelClients.transcribe() formats the RunPod / HTTP body with prompt correctly.
"""
from __future__ import annotations

import base64
import pytest
from app.ai.clients import DEFAULT_DOMAIN_VOCABULARY, ModelClients, ServiceEndpoint
from tools.stt_worker_v2.handler import (
    DEFAULT_DOMAIN_VOCABULARY as WORKER_DOMAIN_VOCABULARY,
    handler as stt_handler,
)
from config.settings import Settings


def test_stt_vocabulary_content():
    """Verify vocabulary contains critical education, entrance exam, course, and Hinglish phrases."""
    assert DEFAULT_DOMAIN_VOCABULARY == WORKER_DOMAIN_VOCABULARY
    vocab = DEFAULT_DOMAIN_VOCABULARY
    
    # Courses
    for course in ["B.Tech", "MBA", "MCA", "BBA", "BCA", "MBBS", "BDS", "LLB", "engineering"]:
        assert course in vocab, f"Missing course: {course}"

    # Entrance exams
    for exam in ["JEE Main", "JEE Advanced", "NEET", "CUET", "CAT", "XAT", "GATE", "CLAT"]:
        assert exam in vocab, f"Missing exam: {exam}"

    # Academic & admission terms
    for term in ["admission process", "tuition fees", "hostel accommodation", "eligibility", "cutoff", "scholarship"]:
        assert term in vocab, f"Missing term: {term}"

    # Hinglish search intent phrases
    for phrase in ["fees kitni hai", "admission kaise milega", "mujhe admission chahiye", "BTech ki fees"]:
        assert phrase in vocab, f"Missing Hinglish phrase: {phrase}"


def test_stt_worker_default_prompt_fallback(monkeypatch):
    """Verify STT worker uses DEFAULT_DOMAIN_VOCABULARY when request prompt is None or empty."""
    recorded_prompt = {}

    def mock_transcribe(audio, prompt=None, num_beams=None, language=None):
        recorded_prompt["prompt"] = prompt
        return "transcribed text", -0.15

    def mock_load_audio(data):
        return [0.0] * 16000

    monkeypatch.setattr("tools.stt_worker_v2.handler.transcribe", mock_transcribe)
    monkeypatch.setattr("tools.stt_worker_v2.handler.load_audio", mock_load_audio)

    # 1. No prompt supplied in input
    stt_handler({"input": {"task": "stt", "audio_b64": "dummy"}})
    assert recorded_prompt["prompt"] == WORKER_DOMAIN_VOCABULARY

    # 2. Empty string prompt supplied
    stt_handler({"input": {"task": "stt", "audio_b64": "dummy", "prompt": "   "}})
    assert recorded_prompt["prompt"] == WORKER_DOMAIN_VOCABULARY

    # 3. Custom prompt supplied
    custom = "Specialised Medical College prompt for MBBS and NEET"
    stt_handler({"input": {"task": "stt", "audio_b64": "dummy", "prompt": custom}})
    assert recorded_prompt["prompt"] == custom


@pytest.mark.asyncio
async def test_model_clients_transcribe_payload():
    """Verify ModelClients.transcribe passes prompt to RunPod and HTTP payloads."""
    s = Settings()
    cfg = {
        "stt_url": "https://api.runpod.ai/v2/test-stt/runsync",
        "keys": {"stt": "secret-key"},
        "timeouts": {"stt": 10.0},
    }
    clients = ModelClients(s, cfg)

    captured_payload = {}

    async def mock_runpod_call(service, ep, hdrs, body):
        captured_payload["service"] = service
        captured_payload["body"] = body
        captured_payload["headers"] = hdrs
        return {"output": {"text": "hello counsellor", "language": "hi"}}

    clients._runpod_call = mock_runpod_call

    # 1. Default call (no prompt arg) -> passes DEFAULT_DOMAIN_VOCABULARY
    pcm_dummy = b"\x00" * 3200  # 100ms at 16kHz
    res = await clients.transcribe(pcm_dummy, rate=16000, language="hi")
    assert res["text"] == "hello counsellor"
    assert captured_payload["body"]["input"]["prompt"] == DEFAULT_DOMAIN_VOCABULARY

    # 2. Custom prompt override
    custom_hint = "Admissions 2026 inquiry for B.Tech CSE"
    res2 = await clients.transcribe(pcm_dummy, rate=16000, language="hi", prompt=custom_hint)
    assert captured_payload["body"]["input"]["prompt"] == custom_hint

    await clients.aclose()
