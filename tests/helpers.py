"""Test helpers: in-loop real HTTP/WebSocket server and mock model services (MOCKS: only ever used in tests)."""
import asyncio
import contextlib
import io
import json
import math
import struct
import wave

import httpx
import uvicorn


@contextlib.asynccontextmanager
async def live_server(app):
    """Run the app under a real uvicorn server (own lifespan) inside the current loop; yields http base URL."""
    cfg = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on", ws="websockets")
    server = uvicorn.Server(cfg)
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started, "server did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


def tone_wav(ms=300, rate=16000, amp=6000, freq=440) -> bytes:
    n = rate * ms // 1000
    pcm = b"".join(struct.pack("<h", int(amp * math.sin(2 * math.pi * freq * i / rate))) for i in range(n))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def pcm_tone(ms, rate=16000, amp=6000) -> bytes:
    n = rate * ms // 1000
    return b"".join(struct.pack("<h", int(amp * math.sin(2 * math.pi * 300 * i / rate))) for i in range(n))


def pcm_silence(ms, rate=16000) -> bytes:
    return b"\0\0" * (rate * ms // 1000)


class MockModels:
    """MOCK STT/Brain/TTS HTTP servers as an httpx transport. Not real inference; records what it was asked."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.stt_delay = 0.0
        self.healthy = {"stt", "brain", "tts", "rag"}
        self.reply = "Namaste! BTech ke liye eligibility 12th mein 50% hai."
        self.tts_ms = 300
        self.fail: dict[str, int] = {}  # service name -> HTTP status to answer with (failure injection)
        self.bad: dict[str, bytes] = {}  # service name -> body returned with HTTP 200 (malformed-response injection)

    async def handler(self, request: httpx.Request) -> httpx.Response:
        host_path = request.url.path
        svc = {"/v1/audio/transcriptions": "stt", "/v1/chat/completions": "brain", "/v1/audio/speech": "tts", "/search": "rag"}.get(host_path)
        if host_path == "/health":
            name = request.url.host.split(".")[0]
            return httpx.Response(200 if name in self.healthy else 503, json={"status": "ok"})
        body = {}
        if request.headers.get("content-type", "").startswith("application/json"):
            body = json.loads(request.content or b"{}")
        self.calls.append((svc or host_path, {"auth": request.headers.get("authorization"), **body}))
        if svc in self.fail:
            return httpx.Response(self.fail[svc])
        if svc in self.bad:
            return httpx.Response(200, content=self.bad[svc], headers={"content-type": "application/json" if svc != "tts" else "audio/wav"})
        if svc == "stt":
            await asyncio.sleep(self.stt_delay)
            return httpx.Response(200, json={"text": "mujhe BTech ke baare mein batao", "language": "hi"})
        if svc == "brain":
            return httpx.Response(200, json={"choices": [{"message": {"content": self.reply}}]})
        if svc == "tts":
            return httpx.Response(200, content=tone_wav(self.tts_ms), headers={"content-type": "audio/wav"})
        if svc == "rag":
            return httpx.Response(200, json={"results": [{"text": "BTech eligibility: 10+2 with PCM, 50%."}]})
        return httpx.Response(404)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
