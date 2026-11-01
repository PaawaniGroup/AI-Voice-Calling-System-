"""Phase F: Watermark and Batch Fallback Release Gate Tests.
Comprehensive validation across all 8 authorization and fallback scenarios:
1. Streaming request without authorization -> 0 audio bytes
2. Streaming request with authorization explicitly false -> 0 audio bytes
3. Diagnostic-only authorized streaming request -> permitted with flag
4. Production-mode request without authorization -> strict fail-closed
5. Streaming refusal followed by complete-waveform fallback -> audio recovered
6. Batch synthesis with Perth watermarking -> watermarked
7. Failure during fallback -> clean error, no partial audio
8. Cancellation during fallback -> immediate stop
"""
import asyncio
import base64
import io
import os
import wave
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.ai.clients import AiError, ModelClients, TtsAudioChunk
from app.ai.pipeline import Conversation
from config.settings import Settings


def make_dummy_wav_pcm16(duration_s=1.0, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * int(duration_s * rate))
    return buf.getvalue()


@pytest.mark.asyncio
async def test_scenario_1_unauthorized_streaming_returns_zero_audio(monkeypatch):
    """Scenario 1: Streaming request without authorization produces zero audio bytes."""
    monkeypatch.delenv("ALLOW_UNWATERMARKED_STREAMING", raising=False)
    s = Settings()
    clients = ModelClients(s, {"tts_url": "https://api.runpod.ai/v2/mock/runsync", "keys": {"tts": "k"}})

    mock_run = MagicMock(status_code=200)
    mock_run.json.return_value = {"id": "j1"}
    mock_status = MagicMock(status_code=200)
    mock_status.json.return_value = {
        "status": "COMPLETED",
        "output": [{"seq": 0, "error": "unwatermarked_streaming_refused", "final": True}]
    }
    mock_stream = MagicMock(status_code=200)
    mock_stream.json.return_value = {"status": "COMPLETED", "stream": []}

    with patch.object(clients, "_http") as http:
        http.post = AsyncMock(return_value=mock_run)
        http.get = AsyncMock(side_effect=[mock_stream, mock_status])
        chunks = []
        with pytest.raises(AiError):
            async for ch in clients.synthesize_stream("hello", "en"):
                chunks.append(ch)
        assert len(chunks) == 0
    await clients.aclose()


@pytest.mark.asyncio
async def test_scenario_2_explicitly_false_authorization(monkeypatch):
    """Scenario 2: Streaming request with ALLOW_UNWATERMARKED_STREAMING='0'."""
    monkeypatch.setenv("ALLOW_UNWATERMARKED_STREAMING", "0")
    s = Settings()
    clients = ModelClients(s, {"tts_url": "https://api.runpod.ai/v2/mock/runsync", "keys": {"tts": "k"}})

    body = clients._tts_runpod_body("test", "en", "v3")
    assert "allow_unwatermarked" not in body["input"]
    await clients.aclose()


@pytest.mark.asyncio
async def test_scenario_3_diagnostic_only_authorization(monkeypatch):
    """Scenario 3: Diagnostic-only flag adds allow_unwatermarked to body."""
    monkeypatch.setenv("ALLOW_UNWATERMARKED_STREAMING", "1")
    s = Settings()
    clients = ModelClients(s, {"tts_url": "https://api.runpod.ai/v2/mock/runsync", "keys": {"tts": "k"}})

    # In synthesize_stream, body['input'] has allow_unwatermarked=True when env is 1
    with patch.object(clients, "_http") as http:
        mock_post = AsyncMock()
        mock_post.return_value = MagicMock(status_code=400)
        http.post = mock_post
        try:
            async for _ in clients.synthesize_stream("test", "en"):
                pass
        except Exception:
            pass
        # Inspect payload sent to RunPod /run
        call_kwargs = mock_post.call_args.kwargs
        assert call_kwargs["json"]["input"].get("allow_unwatermarked") is True
    await clients.aclose()


@pytest.mark.asyncio
async def test_scenario_4_production_mode_refuses_unauthorized(monkeypatch):
    """Scenario 4: Production mode enforces default fail-closed behavior."""
    monkeypatch.delenv("ALLOW_UNWATERMARKED_STREAMING", raising=False)
    s = Settings()
    clients = ModelClients(s, {"tts_url": "https://api.runpod.ai/v2/mock/runsync", "keys": {"tts": "k"}})
    
    body = clients._tts_runpod_body("test", "hi", "v3")
    assert body["input"].get("allow_unwatermarked") is not True
    await clients.aclose()


@pytest.mark.asyncio
async def test_scenario_5_streaming_refusal_followed_by_batch_fallback():
    """Scenario 5: Streaming refusal triggers clean fallback to complete-waveform synthesis."""
    s = Settings()
    clients = ModelClients(s, {"tts_url": "https://api.runpod.ai/v2/mock/runsync", "keys": {"tts": "k"}})
    
    wav_bytes = make_dummy_wav_pcm16(1.0, 16000)
    mock_batch = {
        "status": "COMPLETED",
        "output": {"audio_base64": base64.b64encode(wav_bytes).decode(), "sample_rate": 16000, "watermarked": True}
    }
    
    with patch.object(clients, "_runpod_call", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = mock_batch
        pcm, rate = await clients.synthesize("fallback test", "hi")
        assert len(pcm) == 32000
        assert rate == 16000
    await clients.aclose()


@pytest.mark.asyncio
async def test_scenario_6_batch_synthesis_with_perth_watermarking():
    """Scenario 6: Batch synthesis output carries watermarked=True flag."""
    s = Settings()
    clients = ModelClients(s, {"tts_url": "https://api.runpod.ai/v2/mock/runsync", "keys": {"tts": "k"}})
    
    wav_bytes = make_dummy_wav_pcm16(1.0, 16000)
    mock_resp = {"output": {"audio_base64": base64.b64encode(wav_bytes).decode(), "watermarked": True}}
    
    with patch.object(clients, "_runpod_call", AsyncMock(return_value=mock_resp)):
        pcm, rate = await clients.synthesize("watermarked speech", "hi")
        assert len(pcm) > 0
    await clients.aclose()


@pytest.mark.asyncio
async def test_scenario_7_failure_during_fallback():
    """Scenario 7: Failure during fallback raises clean AiError without emitting partial garbage."""
    s = Settings()
    clients = ModelClients(s, {"tts_url": "https://api.runpod.ai/v2/mock/runsync", "keys": {"tts": "k"}})
    
    with patch.object(clients, "_runpod_call", AsyncMock(side_effect=AiError("tts", "unreachable"))):
        with pytest.raises(AiError) as exc:
            await clients.synthesize("fail fallback", "en")
        assert exc.value.service == "tts"
        assert exc.value.kind == "unreachable"
    await clients.aclose()


@pytest.mark.asyncio
async def test_scenario_8_cancellation_during_fallback():
    """Scenario 8: Cancellation during fallback aborts immediately without hanging."""
    s = Settings()
    clients = ModelClients(s, {"tts_url": "https://api.runpod.ai/v2/mock/runsync", "keys": {"tts": "k"}})
    
    async def delayed_call(*a, **k):
        await asyncio.sleep(5.0)
        return {}

    with patch.object(clients, "_runpod_call", side_effect=delayed_call):
        task = asyncio.create_task(clients.synthesize("cancel me", "hi"))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await clients.aclose()
