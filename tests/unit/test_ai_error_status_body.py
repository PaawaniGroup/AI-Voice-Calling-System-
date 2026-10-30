"""AiError carries the HTTP status and a short, scrubbed body of a failed RunPod call (so a 403 on the voice is visible in the clause-failure log)."""
from __future__ import annotations

import httpx
import pytest

from app.ai.clients import AiError, ModelClients
from config.settings import Settings


def test_ai_error_defaults_are_none():
    e = AiError("tts", "http_error", "403")
    assert e.status_code is None and e.body is None and e.detail == "403"


@pytest.mark.asyncio
async def test_run_http_error_carries_status_and_truncated_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden " + "x" * 500)

    cl = ModelClients(Settings(), {"tts_url": "https://api.example.invalid/v2/ep1", "keys": {"tts": "k"}},
                      transport=httpx.MockTransport(handler))
    try:
        ep = cl.endpoint("tts")
        with pytest.raises(AiError) as ei:
            await cl._runpod_call("tts", ep, {}, {"input": {}})
    finally:
        await cl.aclose()
    assert ei.value.status_code == 403
    assert ei.value.detail == "403"
    assert ei.value.body is not None and len(ei.value.body) <= 80 and "\n" not in ei.value.body
