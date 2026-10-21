"""HTTP adapters for the three model services (STT, Brain/LLM, TTS) and the RAG service.

The services are SEPARATE processes; nothing here loads a model. URLs come from per-tenant `ai_config`
(validated against `AI_ALLOWED_HOSTS`). The wire format defaults to the common OpenAI-compatible shapes
(`/v1/audio/transcriptions`, `/v1/chat/completions`, `/v1/audio/speech`) because the real servers were not
reachable when this was written: **their actual API shape is UNVERIFIED** and is configurable via `*_path`.
"""
from __future__ import annotations

import asyncio
import io
import logging
import struct
import time
import wave
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from app.core.errors import Unprocessable
from app.observability import metrics as m
from config.settings import Settings

log = logging.getLogger("mme_telephony.ai")

SERVICES = ("stt", "brain", "tts", "rag")


class AiError(Exception):
    def __init__(self, service: str, kind: str, detail: str = ""):
        super().__init__(f"{service}:{kind}")
        self.service, self.kind, self.detail = service, kind, detail


class AiUnavailable(AiError):
    pass


class AiTimeout(AiError):
    pass


def validate_service_url(url: str, s: Settings) -> str:
    """Only http(s) URLs to explicitly allow-listed hosts, without credentials/query. Blocks cloud metadata."""
    u = urlparse(url)
    allowed = {h.lower() for h in s.csv(s.ai_allowed_hosts)}
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise Unprocessable("invalid service URL", code="invalid_service_url")
    if u.hostname.lower() not in allowed:
        raise Unprocessable("service host is not in AI_ALLOWED_HOSTS", code="service_host_not_allowed")
    if u.hostname.startswith("169.254.") or u.hostname.lower() in ("metadata.google.internal", "metadata"):
        raise Unprocessable("service host is blocked", code="service_host_not_allowed")
    if u.scheme == "http" and s.is_prod_like and u.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise Unprocessable("plain http to a non-loopback host is not allowed in production", code="insecure_service_url")
    return url.rstrip("/")


def pcm16_to_wav(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


TTS_RATES = {8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000}
TTS_MAX_SECONDS = 120


def wav_to_pcm16(data: bytes) -> tuple[bytes, int]:
    """Validate TTS output BEFORE it is played: mono 16-bit PCM WAV, plausible rate, non-empty, bounded length.
    Anything else is a `bad_audio` error, never garbage played to a caller."""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            ch, width, rate, frames = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            if ch != 1 or width != 2:
                raise AiError("tts", "bad_audio", "expected mono 16-bit WAV")
            if rate not in TTS_RATES:
                raise AiError("tts", "bad_audio", f"unsupported sample rate {rate}")
            if frames <= 0 or frames / rate > TTS_MAX_SECONDS:
                raise AiError("tts", "bad_audio", "empty or too long")
            pcm = w.readframes(frames)
    except (wave.Error, EOFError, struct.error) as e:
        raise AiError("tts", "bad_audio", type(e).__name__) from None
    if len(pcm) != frames * 2:
        raise AiError("tts", "bad_audio", "truncated data")
    return pcm, rate


def rms(pcm: bytes) -> float:
    n = len(pcm) // 2
    if not n:
        return 0.0
    samples = struct.unpack("<%dh" % n, pcm[: n * 2])
    return (sum(x * x for x in samples) / n) ** 0.5


@dataclass
class ServiceEndpoint:
    url: str
    path: str
    timeout: float


class ModelClients:
    """Bounded-concurrency, timeout-guarded access to the model services of ONE tenant configuration."""

    def __init__(self, s: Settings, cfg: dict, transport: httpx.AsyncBaseTransport | None = None):
        self.s, self.cfg = s, cfg or {}
        self._sem = asyncio.Semaphore(s.ai_max_concurrency)
        self._http = httpx.AsyncClient(transport=transport, follow_redirects=False,
                                       headers=self._auth_headers(), timeout=httpx.Timeout(s.ai_request_timeout_s))

    def _auth_headers(self) -> dict:
        key = self.s.ai_api_key.get_secret_value()
        return {"Authorization": f"Bearer {key}"} if key else {}

    async def aclose(self) -> None:
        await self._http.aclose()

    def endpoint(self, service: str) -> ServiceEndpoint:
        url = (self.cfg.get(f"{service}_url") or "").rstrip("/")
        if not url:
            raise AiUnavailable(service, "not_configured")
        defaults = {"stt": "/v1/audio/transcriptions", "brain": "/v1/chat/completions", "tts": "/v1/audio/speech", "rag": "/search"}
        t = (self.cfg.get("timeouts") or {}).get(service, self.s.ai_request_timeout_s)
        return ServiceEndpoint(url, (self.cfg.get("paths") or {}).get(service, defaults[service]), float(t))

    async def _call(self, service: str, fn):
        ep = self.endpoint(service)
        t0 = time.perf_counter()
        try:
            async with self._sem:
                res = await asyncio.wait_for(fn(ep), ep.timeout)
            m.AI_REQUESTS.labels(service, "ok").inc()
            return res
        except asyncio.TimeoutError:
            m.AI_REQUESTS.labels(service, "timeout").inc()
            raise AiTimeout(service, "timeout") from None
        except AiError:
            m.AI_REQUESTS.labels(service, "error").inc()
            raise
        except (httpx.HTTPError, OSError) as e:
            m.AI_REQUESTS.labels(service, "error").inc()
            raise AiUnavailable(service, "unreachable", type(e).__name__) from e
        finally:
            m.AI_LATENCY.labels(service).observe(time.perf_counter() - t0)

    # ------------------------------------------------------------------ health
    async def health(self, service: str) -> dict:
        checked = time.time()
        try:
            ep = self.endpoint(service)
        except AiUnavailable:
            return {"service": service, "status": "not_configured", "checked_at": checked}
        t0 = time.perf_counter()
        try:
            r = await asyncio.wait_for(self._http.get(ep.url + "/health"), min(ep.timeout, 5))
            status = "healthy" if r.status_code < 400 else "unhealthy"
            return {"service": service, "status": status, "http_status": r.status_code,
                    "latency_ms": round((time.perf_counter() - t0) * 1000, 1), "checked_at": checked}
        except asyncio.TimeoutError:
            return {"service": service, "status": "unreachable", "error": "timeout", "checked_at": checked}
        except (httpx.HTTPError, OSError) as e:
            return {"service": service, "status": "unreachable", "error": type(e).__name__, "checked_at": checked}

    # ------------------------------------------------------------------ STT
    async def transcribe(self, pcm: bytes, rate: int, language: str) -> dict:
        async def go(ep: ServiceEndpoint):
            files = {"file": ("audio.wav", pcm16_to_wav(pcm, rate), "audio/wav")}
            data = {"model": self.cfg.get("models", {}).get("stt", self.s.ai_stt_model)}
            if language and language != "auto":
                data["language"] = {"hinglish": "hi"}.get(language, language)
            r = await self._http.post(ep.url + ep.path, files=files, data=data)
            if r.status_code >= 400:
                raise AiError("stt", "http_error", str(r.status_code))
            try:
                j = r.json()
                text = j.get("text")
                lang = j.get("language")
            except (ValueError, AttributeError):
                raise AiError("stt", "bad_response", "not a JSON object") from None
            return {"text": text.strip() if isinstance(text, str) else "", "language": lang if isinstance(lang, str) and lang else language}
        return await self._call("stt", go)

    # ------------------------------------------------------------------ Brain
    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        async def go(ep: ServiceEndpoint):
            body = {"model": self.cfg.get("models", {}).get("brain", self.s.ai_brain_model), "messages": messages,
                    "temperature": 0.3, "max_tokens": 400}
            if tools:
                body["tools"] = tools
            r = await self._http.post(ep.url + ep.path, json=body)
            if r.status_code >= 400:
                raise AiError("brain", "http_error", str(r.status_code))
            try:
                msg = r.json()["choices"][0]["message"]
                content, calls = msg.get("content"), msg.get("tool_calls")
            except (KeyError, IndexError, TypeError, ValueError, AttributeError):
                raise AiError("brain", "bad_response", "unexpected response shape") from None
            return {"text": content.strip() if isinstance(content, str) else "", "tool_calls": calls if isinstance(calls, list) else []}
        return await self._call("brain", go)

    # ------------------------------------------------------------------ TTS
    async def synthesize(self, text: str, language: str) -> tuple[bytes, int]:
        async def go(ep: ServiceEndpoint):
            body = {"model": self.cfg.get("models", {}).get("tts", self.s.ai_tts_model), "input": text,
                    "response_format": "wav", "language": language}
            r = await self._http.post(ep.url + ep.path, json=body)
            if r.status_code >= 400:
                raise AiError("tts", "http_error", str(r.status_code))
            return wav_to_pcm16(r.content)
        return await self._call("tts", go)

    # ------------------------------------------------------------------ RAG
    async def retrieve(self, query: str, k: int = 4) -> list[str]:
        async def go(ep: ServiceEndpoint):
            r = await self._http.post(ep.url + ep.path, json={"query": query, "top_k": k})
            if r.status_code >= 400:
                raise AiError("rag", "http_error", str(r.status_code))
            try:
                return [str(x.get("text", ""))[:1200] for x in r.json().get("results", [])][:k]
            except (ValueError, AttributeError, TypeError):
                raise AiError("rag", "bad_response", "unexpected response shape") from None
        return await self._call("rag", go)
