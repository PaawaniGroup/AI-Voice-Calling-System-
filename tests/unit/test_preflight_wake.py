"""Offline unit tests for scripts/preflight_wake.py using mock httpx transports."""
import os
import pytest
import httpx

from scripts.preflight_wake import run_preflight_wake, get_required_key


@pytest.fixture(autouse=True)
def mock_keys(monkeypatch):
    monkeypatch.setenv("RUNPOD_BRAIN_KEY", "mock-brain-key-1111")
    monkeypatch.setenv("RUNPOD_TTS_KEY", "mock-tts-key-2222")
    monkeypatch.setenv("RUNPOD_STT_KEY", "mock-stt-key-3333")


def test_missing_env_var_raises(monkeypatch):
    monkeypatch.delenv("RUNPOD_TTS_KEY", raising=False)
    with pytest.raises(RuntimeError, match="Missing required environment variable 'RUNPOD_TTS_KEY'"):
        get_required_key("RUNPOD_TTS_KEY", "Voice")


@pytest.mark.asyncio
async def test_dry_run_makes_no_network_calls():
    # If any network call were made without mock transport, it would fail
    res = await run_preflight_wake(
        brain_url="https://brain.example.com",
        voice_url="https://voice.example.com",
        stt_url="https://stt.example.com",
        dry_run=True,
        cap_seconds=60.0
    )
    assert res["status"] == "dry_run"


@pytest.mark.asyncio
async def test_sequential_wake_order_and_pickup_reporting():
    call_order = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "brain" in url_str:
            call_order.append("brain")
        elif "voice" in url_str:
            call_order.append("voice")
        elif "stt" in url_str:
            call_order.append("stt")
        return httpx.Response(200, json={"status": "COMPLETED", "output": {}}, headers={"Content-Type": "application/json"})

    transport = httpx.MockTransport(handle_request)
    res = await run_preflight_wake(
        brain_url="https://api.runpod.ai/v2/brain/runsync",
        voice_url="https://api.runpod.ai/v2/voice/runsync",
        stt_url="https://api.runpod.ai/v2/stt/runsync",
        dry_run=False,
        cap_seconds=30.0,
        transport=transport
    )

    assert res["status"] == "completed"
    assert call_order == ["brain", "voice", "stt"]
    assert "brain" in res["services"] and res["services"]["brain"]["status"] == "ok"
    assert "voice" in res["services"] and res["services"]["voice"]["status"] == "ok"
    assert "stt" in res["services"] and res["services"]["stt"]["status"] == "ok"


@pytest.mark.asyncio
async def test_stops_on_first_error():
    call_order = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "brain" in url_str:
            call_order.append("brain")
            return httpx.Response(200, json={"status": "COMPLETED"})
        elif "voice" in url_str:
            call_order.append("voice")
            return httpx.Response(500, text="Internal Server Error")
        elif "stt" in url_str:
            call_order.append("stt")
            return httpx.Response(200, json={"status": "COMPLETED"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handle_request)
    res = await run_preflight_wake(
        brain_url="https://api.runpod.ai/v2/brain/runsync",
        voice_url="https://api.runpod.ai/v2/voice/runsync",
        stt_url="https://api.runpod.ai/v2/stt/runsync",
        dry_run=False,
        cap_seconds=2.0,  # small cap to stop retries quickly
        transport=transport
    )

    # Brain called, voice called and failed, STT NEVER called!
    assert call_order == ["brain", "voice"]
    assert "stt" not in call_order
    assert res["services"]["voice"]["status"] in ("error", "cap_exceeded")
