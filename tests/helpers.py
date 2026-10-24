"""Test helpers: in-loop real HTTP/WebSocket server and mock model services (MOCKS: only ever used in tests)."""
import asyncio
import base64
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
        self.brain_delay = 0.0
        self.tts_delay = 0.0
        self.cancelled: list[str] = []  # services whose in-flight request was cancelled (the client gave up)
        self.jobs: dict[str, dict] = {}        # RunPod-style serverless jobs (POST /run -> GET /status/{id} -> POST /cancel/{id})
        self.cancel_calls: list[str] = []      # service of each job the client explicitly cancelled with /cancel
        self.healthy = {"stt", "brain", "tts", "rag"}
        self.reply = "Namaste! BTech ke liye eligibility 12th mein 50% hai."
        self.reply_queue: list[str] = []   # replies handed out first (one per Brain request) before `reply` is used again
        self.brain_sse = False   # True: answer stream=true Brain requests as a server-sent-event token stream
        self.stt_text = "mujhe BTech ke baare mein batao"
        self.tts_ms = 300
        self.fail: dict[str, int] = {}  # service name -> HTTP status to answer with (failure injection)
        self.bad: dict[str, bytes] = {}  # service name -> body returned with HTTP 200 (malformed-response injection)

    def _next_reply(self) -> str:
        return self.reply_queue.pop(0) if self.reply_queue else self.reply

    def _runpod(self, request: httpx.Request, host_path: str) -> httpx.Response:
        svc = request.url.host.split(".")[0]          # stt.test / brain.test / tts.test
        now = asyncio.get_event_loop().time()
        if host_path.endswith("/run") and request.method == "POST":
            body = json.loads(request.content or b"{}")
            self.calls.append((svc, {"auth": request.headers.get("authorization"), **body}))
            jid = f"job-{len(self.jobs) + 1}"
            self.jobs[jid] = {"svc": svc, "ready_at": now + getattr(self, f"{svc}_delay", 0.0), "cancelled": False}
            return httpx.Response(200, json={"id": jid, "status": "IN_QUEUE"})
        jid = host_path.rsplit("/", 1)[-1]
        job = self.jobs.get(jid)
        if job is None:
            return httpx.Response(404)
        if "/cancel/" in host_path:
            job["cancelled"] = True
            self.cancel_calls.append(job["svc"])
            return httpx.Response(200, json={"id": jid, "status": "CANCELLED"})
        if job["cancelled"]:
            return httpx.Response(200, json={"id": jid, "status": "CANCELLED"})
        if now < job["ready_at"]:
            return httpx.Response(200, json={"id": jid, "status": "IN_PROGRESS"})
        out = {"stt": {"transcription": self.stt_text, "language": "hi"},
               "brain": [{"choices": [{"message": {"content": self._next_reply()}}]}],
               "tts": {"audio_base64": base64.b64encode(tone_wav(self.tts_ms)).decode(), "format": "wav"}}[job["svc"]]
        return httpx.Response(200, json={"id": jid, "status": "COMPLETED", "delayTime": 5, "executionTime": 7, "output": out})

    async def handler(self, request: httpx.Request) -> httpx.Response:
        host_path = request.url.path
        if host_path.startswith("/v2/") and (host_path.endswith("/run") or "/status/" in host_path or "/cancel/" in host_path):
            return self._runpod(request, host_path)
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
        if svc in ("stt", "brain", "tts") and getattr(self, f"{svc}_delay"):
            try:
                await asyncio.sleep(getattr(self, f"{svc}_delay"))
            except asyncio.CancelledError:
                self.cancelled.append(svc)
                raise
        if svc == "stt":
            return httpx.Response(200, json={"text": self.stt_text, "language": "hi"})
        if svc == "brain":
            if self.brain_sse and body.get("stream"):   # OpenAI-style token stream (3 characters per event) instead of one JSON body
                text = self._next_reply()
                events = [{"choices": [{"delta": {"content": text[i:i + 3]}}]} for i in range(0, len(text), 3)]
                sse = "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events) + "data: [DONE]\n\n"
                return httpx.Response(200, content=sse.encode("utf-8"), headers={"content-type": "text/event-stream"})
            return httpx.Response(200, json={"choices": [{"message": {"content": self._next_reply()}}]})
        if svc == "tts":
            return httpx.Response(200, content=tone_wav(self.tts_ms), headers={"content-type": "audio/wav"})
        if svc == "rag":
            return httpx.Response(200, json={"results": [{"text": "BTech eligibility: 10+2 with PCM, 50%."}]})
        return httpx.Response(404)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
