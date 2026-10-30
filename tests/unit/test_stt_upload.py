"""Shape of the STT upload (multipart, no network: httpx MockTransport). The audio part must be exactly the caller's PCM plus a 44-byte WAV header,
and the domain vocabulary prompt travels as its own small text field: a bigger total upload is the prompt, never doubled audio."""
import asyncio

import httpx

import app.ai.clients as clients
from config.settings import Settings

PCM = b"\x00\x01" * (56 * 160)   # 56 frames x 320 B of 8 kHz PCM16


def _client(handler):
    s = Settings(_env_file=None, env="test", provider="mock", fs_enabled=False, mongo_uri="mongodb://127.0.0.1:1", jwt_secret="x" * 40,
                 webhook_signing_master="y" * 40, ai_allowed_hosts="stt.test", ai_api_key="k" * 12)
    return clients.ModelClients(s, {"stt_url": "http://stt.test:9000", "enabled": True}, httpx.MockTransport(handler))


def _parts(request: httpx.Request) -> dict[str, bytes]:
    boundary = request.headers["content-type"].partition("boundary=")[2].encode()
    out = {}
    for chunk in request.content.split(b"--" + boundary):
        head, sep, body = chunk.partition(b"\r\n\r\n")
        if sep and b'name="' in head:
            name = head.split(b'name="', 1)[1].split(b'"', 1)[0].decode()
            out[name] = body[:-2] if body.endswith(b"\r\n") else body
    return out


def _run(prompt=None, vocabulary=None):
    seen = {}

    def handler(request):
        seen["parts"] = _parts(request)
        return httpx.Response(200, json={"text": "ok", "language": "hi"})

    async def go():
        c = _client(handler)
        old = clients.DEFAULT_DOMAIN_VOCABULARY
        if vocabulary is not None:
            clients.DEFAULT_DOMAIN_VOCABULARY = vocabulary
        try:
            await c.transcribe(PCM, 8000, "auto", **({"prompt": prompt} if prompt is not None else {}))
        finally:
            clients.DEFAULT_DOMAIN_VOCABULARY = old
            await c.aclose()
    asyncio.run(go())
    return seen["parts"]


def test_audio_part_is_exactly_the_pcm_plus_a_wav_header_and_the_default_vocabulary_is_a_separate_field():
    parts = _run()
    assert len(parts["file"]) == len(PCM) + 44
    assert parts["prompt"].decode("utf-8") == clients.DEFAULT_DOMAIN_VOCABULARY and len(parts["prompt"]) > 1000


def test_an_explicit_prompt_replaces_the_default_and_an_empty_default_sends_none():
    assert _run(prompt="custom terms")["prompt"] == b"custom terms"
    parts = _run(vocabulary="")
    assert "prompt" not in parts and len(parts["file"]) == len(PCM) + 44
