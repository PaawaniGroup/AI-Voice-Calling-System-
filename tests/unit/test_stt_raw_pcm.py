"""`stt_raw_pcm` (tenant flag, default OFF): the STT request body. SIMULATED: httpx.MockTransport, no network, no RunPod, no real audio (a synthetic tone)."""
from __future__ import annotations

import array
import base64
import io
import json
import math
import wave

import httpx
import pytest

from app.ai import turn_trace
from app.ai.clients import ModelClients
from config.settings import Settings

RATE = 8000


def tone(seconds: float = 3.0) -> bytes:
    n = int(RATE * seconds)
    return array.array("h", [int(6000 * math.sin(2 * math.pi * 300 * i / RATE)) for i in range(n)]).tobytes()


def clients(**cfg):
    seen: dict = {"bodies": []}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path.endswith("/run"):
            seen["bodies"].append(json.loads(req.content))
            seen["bytes"] = len(req.content)
            return httpx.Response(200, json={"id": "j1", "status": "IN_QUEUE"})
        return httpx.Response(200, json={"id": "j1", "status": "COMPLETED", "delayTime": 100, "executionTime": 500, "output": {"text": "ok", "language": "hi"}})

    base = {"stt_url": "https://api.example.invalid/v2/ep/runsync", "keys": {"stt": "k"}, "runpod_poll_cap_s": 0.03}
    base.update(cfg)
    return ModelClients(Settings(), base, transport=httpx.MockTransport(handler)), seen


@pytest.fixture(autouse=True)
def _isolate_trace():
    tok = turn_trace._current.set(None)
    yield
    turn_trace._current.reset(tok)


async def test_flag_off_sends_the_old_body_three_audio_fields_wav():
    cl, seen = clients()
    pcm = tone()
    try:
        out = await cl.transcribe(pcm, RATE, "hinglish")
    finally:
        await cl.aclose()
    inp = seen["bodies"][0]["input"]
    assert set(inp) == {"audio_b64", "audio", "audio_base64", "language"} and inp["language"] == "hi"
    assert inp["audio_b64"] == inp["audio"] == inp["audio_base64"]
    with wave.open(io.BytesIO(base64.b64decode(inp["audio_b64"]))) as w:                 # a WAV at the call's own rate
        assert w.getframerate() == RATE and w.readframes(w.getnframes()) == pcm
    assert out["text"] == "ok"


async def test_flag_off_with_auto_language_keeps_language_none_as_before():
    cl, seen = clients()
    try:
        await cl.transcribe(tone(0.5), RATE, "auto")
    finally:
        await cl.aclose()
    assert seen["bodies"][0]["input"]["language"] is None


async def test_flag_on_sends_one_raw_pcm_field_and_the_sample_rate():
    cl, seen = clients(stt_raw_pcm=True)
    pcm = tone()
    try:
        out = await cl.transcribe(pcm, RATE, "auto")
    finally:
        await cl.aclose()
    inp = seen["bodies"][0]["input"]
    assert set(inp) == {"audio_pcm16_b64", "sample_rate"} and inp["sample_rate"] == RATE
    assert base64.b64decode(inp["audio_pcm16_b64"], validate=True) == pcm                  # exactly the samples: no WAV wrapper
    assert out["text"] == "ok"


async def test_flag_on_keeps_a_language_hint_when_one_is_set():
    cl, seen = clients(stt_raw_pcm=True)
    try:
        await cl.transcribe(tone(0.5), RATE, "en")
    finally:
        await cl.aclose()
    assert seen["bodies"][0]["input"]["language"] == "en"


async def test_flag_on_request_is_about_a_third_of_the_size_simulated():
    off, s_off = clients()
    on, s_on = clients(stt_raw_pcm=True)
    pcm = tone(3.0)
    try:
        await off.transcribe(pcm, RATE, "auto")
        await on.transcribe(pcm, RATE, "auto")
    finally:
        await off.aclose()
        await on.aclose()
    assert s_on["bytes"] < s_off["bytes"] * 0.4 and s_on["bytes"] > len(pcm)               # SIMULATED sizes: 1 base64 copy vs 3 plus a WAV header


async def test_the_trace_job_record_says_which_body_was_sent():
    tr = turn_trace.TurnTrace(t_speech_end=0.0)
    turn_trace.start(tr)
    cl, _ = clients(stt_raw_pcm=True)
    try:
        await cl.transcribe(tone(1.0), RATE, "auto")
    finally:
        await cl.aclose()
    j = tr.jobs[0]
    assert j["service"] == "stt" and j["raw_pcm"] is True and j["audio_fields"] == 1 and j["sample_rate"] == RATE and j["audio_s"] == 1.0
    assert j["audio_b64_chars"] > 0 and j["convert_ms"] >= 0


def test_stt_raw_pcm_is_off_by_default_in_the_tenant_config():
    from app.ai.service import LATENCY_FLAGS, AiService
    assert "stt_raw_pcm" in LATENCY_FLAGS
    svc = AiService.__new__(AiService)
    svc.s = Settings()
    assert AiService.default_config(svc, "t1")["stt_raw_pcm"] is False
