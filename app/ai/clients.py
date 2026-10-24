"""HTTP adapters for the three model services (STT, Brain/LLM, TTS) and the RAG service.

The services are SEPARATE processes; nothing here loads a model. URLs come from per-tenant `ai_config`
(validated against `AI_ALLOWED_HOSTS`). The wire format defaults to the common OpenAI-compatible shapes
(`/v1/audio/transcriptions`, `/v1/chat/completions`, `/v1/audio/speech`) because the real servers were not
reachable when this was written: **their actual API shape is UNVERIFIED** and is configurable via `*_path`.
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import struct
import time
import wave
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from app.ai import tts_frontend
from app.core.errors import Unprocessable
from app.observability import metrics as m
from config.settings import Settings

log = logging.getLogger("mme_telephony.ai")

SERVICES = ("stt", "brain", "tts", "rag")
_REF_CACHE: dict = {}


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
        limits = httpx.Limits(max_keepalive_connections=20, max_connections=50, keepalive_expiry=120.0)
        self._http = httpx.AsyncClient(transport=transport, follow_redirects=False, limits=limits,
                                       timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=30.0))

    def _auth_headers(self, service: str | None = None) -> dict:
        key = None
        if service and isinstance(self.cfg.get("keys"), dict) and self.cfg["keys"].get(service):
            key = self.cfg["keys"][service]
        elif service and self.cfg.get(f"{service}_api_key"):
            key = self.cfg[f"{service}_api_key"]
        elif self.s.ai_api_key:
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
        if url.endswith("/runsync"):
            return ServiceEndpoint(url[:-8], "/runsync", float(t))
        return ServiceEndpoint(url, (self.cfg.get("paths") or {}).get(service, defaults[service]), float(t))

    async def _runpod_wait(self, service: str, ep: ServiceEndpoint, hdrs: dict, j):
        """RunPod `/runsync` stops waiting after ~90 s (cold start) and returns IN_QUEUE/IN_PROGRESS: poll `/status`.
        Adaptive fast polling (100ms -> 400ms) minimizes latency instead of waiting 1s per poll."""
        poll_delay = 0.1
        while isinstance(j, dict) and j.get("status") in ("IN_QUEUE", "IN_PROGRESS") and j.get("id"):
            await asyncio.sleep(poll_delay)
            poll_delay = min(poll_delay * 1.5, 0.4)
            if self._http.is_closed:
                raise AiUnavailable(service, "client_closed")
            sr = await self._http.get(f"{ep.url}/status/{j['id']}", headers=hdrs, timeout=30)
            if sr.status_code >= 400:
                raise AiError(service, "http_error", str(sr.status_code))
            j = sr.json()
        if isinstance(j, dict) and j.get("status") not in (None, "COMPLETED"):
            raise AiError(service, "runpod_error", f"status={j.get('status')} {str(j.get('error', ''))[:160]}")
        if isinstance(j, dict) and j.get("error"):
            raise AiError(service, "runpod_error", str(j["error"])[:200])
        return j

    def _tts_reference_b64(self) -> str | None:
        """Base64 WAV of the voice the TTS worker clones (it refuses requests without one). Read once per process."""
        path = self.s.ai_tts_reference_wav
        if not path:
            return None
        if _REF_CACHE.get("path") != path:
            try:
                _REF_CACHE.update(path=path, b64=base64.b64encode(open(path, "rb").read()).decode("ascii"))
            except OSError as e:
                raise AiError("tts", "reference_unreadable", type(e).__name__) from None
        return _REF_CACHE["b64"]

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
        except RuntimeError as e:
            m.AI_REQUESTS.labels(service, "error").inc()
            if "closed" in str(e).lower():
                raise AiUnavailable(service, "client_closed") from e
            raise
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
            hdrs = self._auth_headers(service)
            r = await asyncio.wait_for(self._http.get(ep.url + "/health", headers=hdrs), min(ep.timeout, 5))
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
            hdrs = self._auth_headers("stt")
            if ep.path.endswith("/runsync"):
                wav_bytes = pcm16_to_wav(pcm, rate)
                b64 = base64.b64encode(wav_bytes).decode("ascii")
                body = {
                    "input": {
                        "audio_b64": b64,  # field the mme-stt-runpod handler requires (verified live 2026-10-23)
                        "audio": b64,
                        "audio_base64": b64,
                        "language": {"hinglish": "hi"}.get(language, language) if language and language != "auto" else None
                    }
                }
                r = await self._http.post(ep.url + ep.path, json=body, headers=hdrs, timeout=ep.timeout)
                if r.status_code >= 400:
                    raise AiError("stt", "http_error", str(r.status_code))
                j = await self._runpod_wait("stt", ep, hdrs, r.json())
                out = j.get("output", {}) if isinstance(j, dict) else {}
                if isinstance(out, dict):
                    text = out.get("transcription") or out.get("text") or ""
                    lang = out.get("language") or language
                elif isinstance(out, str):
                    text, lang = out, language
                else:
                    text, lang = "", language
                return {"text": text.strip(), "language": lang}
            else:
                files = {"file": ("audio.wav", pcm16_to_wav(pcm, rate), "audio/wav")}
                data = {"model": self.cfg.get("models", {}).get("stt", self.s.ai_stt_model)}
                if language and language != "auto":
                    data["language"] = {"hinglish": "hi"}.get(language, language)
                r = await self._http.post(ep.url + ep.path, files=files, data=data, headers=hdrs, timeout=ep.timeout)
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
    async def chat(self, messages: list[dict], tools: list[dict] | None = None, temperature: float | None = None) -> dict:
        async def go(ep: ServiceEndpoint):
            hdrs = self._auth_headers("brain")
            model = self.cfg.get("models", {}).get("brain", self.s.ai_brain_model)
            max_tok = int(self.cfg.get("max_tokens", 400))
            temp = float(temperature if temperature is not None else self.cfg.get("temperature", getattr(self.s, "ai_temperature", 0.3)))
            if ep.path.endswith("/runsync"):
                prompt = "\n".join(f"{m.get('role', 'user')}: {m.get('content', '')}" for m in messages)
                body = {
                    "input": {
                        "prompt": prompt,
                        "messages": messages,
                        "model": model,
                        "sampling_params": {"temperature": temp, "max_tokens": max_tok}  # RunPod vLLM worker format
                    }
                }
            else:
                body = {"model": model, "messages": messages, "temperature": temp, "max_tokens": max_tok}
                if tools:
                    body["tools"] = tools
            r = await self._http.post(ep.url + ep.path, json=body, headers=hdrs, timeout=ep.timeout)
            if r.status_code >= 400:
                raise AiError("brain", "http_error", str(r.status_code))
            try:
                j = r.json()
                if ep.path.endswith("/runsync"):
                    j = await self._runpod_wait("brain", ep, hdrs, j)
                out = j["output"] if isinstance(j, dict) and "output" in j else j
                if isinstance(out, list):  # RunPod vLLM worker: [ {OpenAI chat.completion object} ]
                    out = out[0] if out else ""
                if isinstance(out, dict) and "choices" in out:
                    if not out["choices"]:
                        raise AiError("brain", "bad_response", "empty choices list")
                    msg = out["choices"][0].get("message") if isinstance(out["choices"][0], dict) else None
                    if not isinstance(msg, dict):
                        raise AiError("brain", "bad_response", "missing message in choice")
                    content, calls = msg.get("content"), msg.get("tool_calls")
                elif isinstance(out, dict) and ("text" in out or "content" in out):
                    content, calls = out.get("text") or out.get("content") or "", out.get("tool_calls", [])
                elif isinstance(out, str):
                    content, calls = out, []
                else:
                    raise AiError("brain", "bad_response", "unrecognized response format")
            except (KeyError, IndexError, TypeError, ValueError, AttributeError) as e:
                raise AiError("brain", "bad_response", f"unexpected response shape: {type(e).__name__}") from None
            return {"text": content.strip() if isinstance(content, str) else "", "tool_calls": calls if isinstance(calls, list) else []}
        return await self._call("brain", go)

    # ------------------------------------------------------------------ TTS
    async def synthesize(self, text: str, language: str) -> tuple[bytes, int]:
        async def go(ep: ServiceEndpoint):
            hdrs = self._auth_headers("tts")
            model = self.cfg.get("models", {}).get("tts", self.s.ai_tts_model)
            if ep.path.endswith("/runsync"):
                spoken, lang = text, language
                if self.s.ai_tts_normalize:
                    # Chatterbox test: per-sentence hi/en tag (Hindi + Hinglish -> hi, plain English -> en) and numbers/rupees/dates/phones/
                    # acronyms written as spoken Devanagari words. Other explicit language codes pass through untouched.
                    if language in ("", "auto", "hi", "en", "hinglish"):
                        lang = tts_frontend.detect_lang(text)
                    spoken = tts_frontend.normalize(text, lang)
                elif language == "hinglish":
                    lang = "hi"
                body = {
                    "input": {
                        "text": spoken,
                        "prompt": spoken,
                        "language": lang,
                        "model": model
                    }
                }
                ref = self._tts_reference_b64()
                if ref:
                    body["input"]["reference_audio_base64"] = ref  # field the mme-tts-runpod handler requires
                r = await self._http.post(ep.url + ep.path, json=body, headers=hdrs, timeout=ep.timeout)
                if r.status_code >= 400:
                    raise AiError("tts", "http_error", str(r.status_code))
                j = await self._runpod_wait("tts", ep, hdrs, r.json())
                out = j.get("output", {}) if isinstance(j, dict) else {}
                b64_audio = None
                if isinstance(out, dict):
                    b64_audio = out.get("audio") or out.get("audio_base64")
                elif isinstance(out, str):
                    b64_audio = out
                if not b64_audio:
                    raise AiError("tts", "bad_response", "missing audio in output")
                wav_bytes = base64.b64decode(b64_audio)
                return wav_to_pcm16(wav_bytes)
            else:
                body = {"model": model, "input": text, "response_format": "wav", "language": language}
                r = await self._http.post(ep.url + ep.path, json=body, headers=hdrs, timeout=ep.timeout)
                if r.status_code >= 400:
                    raise AiError("tts", "http_error", str(r.status_code))
                return wav_to_pcm16(r.content)
        return await self._call("tts", go)

    # ------------------------------------------------------------------ RAG
    async def retrieve(self, query: str, k: int = 4) -> list[str]:
        async def go(ep: ServiceEndpoint):
            r = await self._http.post(ep.url + ep.path, json={"query": query, "top_k": k}, timeout=ep.timeout)
            if r.status_code >= 400:
                raise AiError("rag", "http_error", str(r.status_code))
            try:
                return [str(x.get("text", ""))[:1200] for x in r.json().get("results", [])][:k]
            except (ValueError, AttributeError, TypeError):
                raise AiError("rag", "bad_response", "unexpected response shape") from None
        return await self._call("rag", go)
