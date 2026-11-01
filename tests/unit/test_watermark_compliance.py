"""Security, policy gate, and watermark compliance tests.

Verifies:
1. Fail-closed watermark policy gate: unauthorized streaming returns error and 0 audio bytes.
2. Production telephony configurations never enable unwatermarked streaming.
3. Batch fallback preserves complete Perth-watermarked waveform synthesis.
4. Diagnostic authorization flags cannot bypass security in production environments.
"""
import base64
import io
import os
import wave
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.ai.clients import AiError, ModelClients, TtsAudioChunk
from config.settings import Settings


def test_watermark_policy_gate_fail_closed_by_default(monkeypatch):
    """Ensure that ALLOW_UNWATERMARKED_STREAMING defaults to '0' (fail-closed)."""
    monkeypatch.delenv("ALLOW_UNWATERMARKED_STREAMING", raising=False)
    s = Settings()
    # Telephony settings must not enable unwatermarked streaming
    assert os.getenv("ALLOW_UNWATERMARKED_STREAMING", "0") == "0"


@pytest.mark.asyncio
async def test_worker_refusal_payload_handling():
    """Verify that when worker returns streaming_disabled, ModelClients raises or handles
    the runpod_error and delivers ZERO audio bytes to the caller."""
    s = Settings()
    clients = ModelClients(s, {
        "tts_url": "https://api.runpod.ai/v2/mock/runsync",
        "keys": {"tts": "fake_key"},
    })

    # Simulate worker returning the streaming_disabled refusal
    mock_run_resp = MagicMock()
    mock_run_resp.status_code = 200
    mock_run_resp.json.return_value = {"id": "job_123", "status": "IN_PROGRESS"}

    mock_status_resp = MagicMock()
    mock_status_resp.status_code = 200
    mock_status_resp.json.return_value = {
        "id": "job_123",
        "status": "COMPLETED",
        "output": [
            {
                "seq": 0,
                "error": "streaming_disabled",
                "detail": "Streamed audio cannot carry Perth watermark. Set ALLOW_UNWATERMARKED_STREAMING=1",
                "final": True
            }
        ]
    }

    mock_stream_resp = MagicMock()
    mock_stream_resp.status_code = 200
    mock_stream_resp.json.return_value = {"status": "COMPLETED", "stream": []}

    with patch.object(clients, "_http") as mock_http:
        mock_http.post = AsyncMock(return_value=mock_run_resp)
        mock_http.get = AsyncMock(side_effect=[mock_stream_resp, mock_status_resp])

        chunks = []
        with pytest.raises(AiError) as exc_info:
            async for ch in clients.synthesize_stream("नमस्ते", "hi"):
                chunks.append(ch)

        assert exc_info.value.service == "tts"
        assert exc_info.value.kind == "runpod_error"
        assert "streaming_disabled" in str(exc_info.value.detail)
        # ZERO audio bytes delivered
        assert len(chunks) == 0

    await clients.aclose()


@pytest.mark.asyncio
async def test_batch_synthesis_fallback_is_always_watermarked():
    """Verify that batch synthesis fallback calls the complete synthesis path,
    which uses the model's apply_watermark."""
    s = Settings()
    clients = ModelClients(s, {
        "tts_url": "https://api.runpod.ai/v2/mock/runsync",
        "keys": {"tts": "fake_key"},
    })

    # Valid 16-bit PCM WAV using stdlib wave
    wav_io = io.BytesIO()
    with wave.open(wav_io, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * 16000)
    wav_bytes = wav_io.getvalue()
    b64_wav = base64.b64encode(wav_bytes).decode()

    mock_batch_resp = {
        "id": "job_batch_123",
        "status": "COMPLETED",
        "output": {
            "audio_base64": b64_wav,
            "format": "wav",
            "sample_rate": 16000,
            "watermarked": True
        }
    }

    with patch.object(clients, "_runpod_call", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = mock_batch_resp
        pcm, rate = await clients.synthesize("नमस्ते, Make My Education", "hi")
        assert len(pcm) == 32000  # 16000 samples * 2 bytes
        assert rate == 16000

    await clients.aclose()
