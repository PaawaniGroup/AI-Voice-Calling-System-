"""RunPod serverless request/response contract of the STT and TTS adapters.
The RunPod API is a MOCK (httpx.MockTransport); the real wire format was observed live on 2026-10-23
(STT handler wants `audio_b64`, TTS handler wants `reference_audio_base64`, `/runsync` may return IN_PROGRESS)."""
import base64
import io
import json
import wave

import httpx
import pytest

from app.ai.clients import AiError, ModelClients, pcm16_to_wav
from tests.conftest import make_settings
from tests.helpers import pcm_tone

BASE = "https://api.runpod.ai/v2/abc123"


def _wav(seconds=0.2, rate=24000) -> bytes:
    return pcm16_to_wav(pcm_tone(int(seconds * 1000)), rate)


def _transport(handler, seen):
    def h(request: httpx.Request):
        seen.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        return handler(request)
    return httpx.MockTransport(h)


def _clients(transport, **settings):
    s = make_settings(ai_allowed_hosts="api.runpod.ai", **settings)
    cfg = {"stt_url": BASE + "/runsync", "tts_url": BASE + "/runsync", "keys": {"stt": "k", "tts": "k"}}
    return ModelClients(s, cfg, transport)


async def test_stt_sends_audio_b64_and_polls_status_until_completed():
    seen = []

    def handler(req):
        if req.url.path.endswith("/runsync"):
            return httpx.Response(200, json={"id": "job1", "status": "IN_PROGRESS"})
        return httpx.Response(200, json={"id": "job1", "status": "COMPLETED", "output": {"text": " namaste ", "model": "m"}})

    c = _clients(_transport(handler, seen))
    res = await c.transcribe(pcm_tone(200), 16000, "hinglish")
    await c.aclose()
    assert res == {"text": "namaste", "language": "hinglish"}  # worker reports no language: caller's is echoed
    assert seen[0][1] == "/v2/abc123/runsync" and seen[1][1] == "/v2/abc123/status/job1"
    sent = seen[0][2]["input"]
    assert wave.open(io.BytesIO(base64.b64decode(sent["audio_b64"]))).getframerate() == 16000
    assert sent["language"] == "hi"


async def test_stt_failed_job_is_an_error_not_empty_text():
    seen = []
    c = _clients(_transport(lambda r: httpx.Response(200, json={"status": "FAILED", "error": "boom"}), seen))
    with pytest.raises(AiError) as e:
        await c.transcribe(pcm_tone(200), 16000, "auto")
    await c.aclose()
    assert e.value.kind == "runpod_error"


async def test_stt_unfinished_status_other_than_completed_is_an_error():
    c = _clients(_transport(lambda r: httpx.Response(200, json={"id": "j", "status": "CANCELLED"}), []))
    with pytest.raises(AiError) as e:
        await c.transcribe(pcm_tone(200), 16000, "auto")
    await c.aclose()
    assert e.value.kind == "runpod_error"


async def test_tts_sends_reference_voice_and_decodes_worker_output(tmp_path):
    ref = tmp_path / "ref.wav"
    ref.write_bytes(_wav(0.5, 48000))
    out_wav = _wav(0.3, 24000)
    seen = []

    def handler(req):
        if req.url.path.endswith("/runsync"):
            return httpx.Response(200, json={"id": "t1", "status": "IN_QUEUE"})
        return httpx.Response(200, json={"id": "t1", "status": "COMPLETED", "output": {
            "audio_base64": base64.b64encode(out_wav).decode(), "format": "wav", "sample_rate": 24000, "language": "hi"}})

    c = _clients(_transport(handler, seen), ai_tts_reference_wav=str(ref))
    pcm, rate = await c.synthesize("aap kaise hain", "hinglish")
    await c.aclose()
    with wave.open(io.BytesIO(out_wav)) as w:
        assert rate == w.getframerate() == 24000 and pcm == w.readframes(w.getnframes())
    sent = seen[0][2]["input"]
    assert base64.b64decode(sent["reference_audio_base64"]) == ref.read_bytes()
    assert sent["language"] == "hi" and sent["text"] == "aap kaise hain"


async def _tts_input(text, language, **settings):
    seen = []
    ok = {"status": "COMPLETED", "output": {"audio_base64": base64.b64encode(_wav()).decode()}}
    c = _clients(_transport(lambda r: httpx.Response(200, json=ok), seen), **settings)
    await c.synthesize(text, language)
    await c.aclose()
    return seen[0][2]["input"]


async def test_tts_numbers_and_acronyms_are_spoken_and_language_tag_follows_the_sentence():
    sent = await _tts_input("Aapki fees ₹1,25,000 hai, call 98765 43210.", "hinglish")
    assert sent["language"] == "hi"
    assert "रुपये" in sent["text"] and "₹" not in sent["text"] and not any(ch.isdigit() for ch in sent["text"])
    plain = await _tts_input("Your course starts on Monday.", "hinglish")  # plain English is tagged en, text untouched
    assert plain["language"] == "en" and plain["text"] == "Your course starts on Monday."
    other = await _tts_input("Hola, como estas", "es")  # explicit non-Hindi/English codes pass through
    assert other["language"] == "es"


async def test_tts_normalization_can_be_switched_off():
    sent = await _tts_input("Aapki fees ₹5000 hai", "hinglish", ai_tts_normalize=False)
    assert sent["text"] == "Aapki fees ₹5000 hai" and sent["language"] == "hi"


async def test_tts_without_reference_setting_omits_the_field():
    seen = []
    ok = {"status": "COMPLETED", "output": {"audio_base64": base64.b64encode(_wav()).decode()}}
    c = _clients(_transport(lambda r: httpx.Response(200, json=ok), seen), ai_tts_reference_wav="")
    await c.synthesize("x", "en")
    await c.aclose()
    assert "reference_audio_base64" not in seen[0][2]["input"]


async def test_brain_parses_runpod_vllm_list_of_chat_completions():
    seen = []
    done = {"status": "COMPLETED", "output": [{"choices": [{"message": {"role": "assistant", "content": " Namaste! ", "tool_calls": None}}]}]}

    def handler(req):
        if req.url.path.endswith("/runsync"):
            return httpx.Response(200, json={"id": "b1", "status": "IN_QUEUE"})
        return httpx.Response(200, json=done)

    s = make_settings(ai_allowed_hosts="api.runpod.ai")
    c = ModelClients(s, {"brain_url": BASE + "/runsync", "keys": {"brain": "k"}}, _transport(handler, seen))
    res = await c.chat([{"role": "user", "content": "hi"}])
    await c.aclose()
    assert res == {"text": "Namaste!", "tool_calls": []}
    sent = seen[0][2]["input"]
    assert sent["messages"] == [{"role": "user", "content": "hi"}] and sent["sampling_params"]["max_tokens"] == 400


async def test_brain_failed_job_is_an_error():
    s = make_settings(ai_allowed_hosts="api.runpod.ai")
    c = ModelClients(s, {"brain_url": BASE + "/runsync"}, _transport(lambda r: httpx.Response(200, json={"status": "FAILED", "error": "oom"}), []))
    with pytest.raises(AiError) as e:
        await c.chat([{"role": "user", "content": "hi"}])
    await c.aclose()
    assert e.value.kind == "runpod_error"


async def test_tts_unreadable_reference_is_a_clear_error(tmp_path):
    c = _clients(_transport(lambda r: httpx.Response(200, json={}), []), ai_tts_reference_wav=str(tmp_path / "missing.wav"))
    with pytest.raises(AiError) as e:
        await c.synthesize("x", "en")
    await c.aclose()
    assert e.value.kind == "reference_unreadable"
