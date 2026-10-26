"""HTTP adapters for the three model services (STT, Brain/LLM, TTS) and the RAG service.

The services are SEPARATE processes; nothing here loads a model. URLs come from per-tenant `ai_config`
(validated against `AI_ALLOWED_HOSTS`). The wire format defaults to the common OpenAI-compatible shapes
(`/v1/audio/transcriptions`, `/v1/chat/completions`, `/v1/audio/speech`) because the real servers were not
reachable when this was written: **their actual API shape is UNVERIFIED** and is configurable via `*_path`.
"""
from __future__ import annotations

import asyncio
import os
import base64
import io
import json
import logging
import struct
import time
import wave
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from app.ai import codec, tts_frontend, turn_trace
from app.core.errors import Unprocessable
from app.observability import metrics as m
from config.settings import Settings

log = logging.getLogger("mme_telephony.ai")

SERVICES = ("stt", "brain", "tts", "rag")
DEFAULT_TTS_VOICE = "utkarsh"   # the voice baked into the TTS worker image (v3); a tenant can name another with `tts_voice`
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


def parse_brain_output(out) -> tuple[str, list]:
    """Content and tool calls of a non-streamed Brain reply. Same contract as `ModelClients.chat`: anything that is not a recognised
    chat-completion shape raises AiError(brain, bad_response); only a recognised reply with no text is an (empty) reply."""
    if isinstance(out, list):  # RunPod vLLM worker: [ {OpenAI chat.completion object} ]
        out = out[0] if out else ""
    if isinstance(out, dict) and "choices" in out:
        if not out["choices"]:
            raise AiError("brain", "bad_response", "empty choices list")
        first = out["choices"][0]
        msg = first.get("message") if isinstance(first, dict) else None
        if not isinstance(msg, dict):
            raise AiError("brain", "bad_response", "missing message in choice")
        content, calls = msg.get("content"), msg.get("tool_calls")
    elif isinstance(out, dict) and ("text" in out or "content" in out):
        content, calls = out.get("text") or out.get("content") or "", out.get("tool_calls", [])
    elif isinstance(out, str):
        content, calls = out, []
    else:
        raise AiError("brain", "bad_response", "unrecognized response format")
    return (content.strip() if isinstance(content, str) else ""), (calls if isinstance(calls, list) else [])


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
        self._bg: set[asyncio.Task] = set()          # fire-and-forget RunPod cancel calls (kept referenced until they finish)
        self.cancelled_jobs: list[tuple[str, int | str]] = []   # (service, http status | error type) of each RunPod cancel attempt

    def resolve_key(self, service: str | None = None) -> str | None:
        """Resolve API key for a model service without exposing or logging it.

        Order:
        1. Indirect environment reference from cfg['key_refs'][service]
           (if configured and variable is missing/empty, raises AiUnavailable with explicit reason, NO fallback)
        2. Stored key in cfg['keys'][service] or cfg[f'{service}_api_key']
        3. Global key from self.s.ai_api_key
        """
        if not service:
            return self.s.ai_api_key.get_secret_value() if self.s.ai_api_key else None

        svc = service.lower()
        key_refs = self.cfg.get("key_refs")
        if isinstance(key_refs, dict) and svc in key_refs:
            env_var_name = key_refs[svc]
            val = os.environ.get(env_var_name) if env_var_name else None
            if not val:
                raise AiUnavailable(
                    svc,
                    "missing_key_ref",
                    f"key_ref environment variable '{env_var_name}' is not set or empty",
                )
            return val.strip()

        keys = self.cfg.get("keys")
        if isinstance(keys, dict) and keys.get(svc):
            return str(keys[svc]).strip()

        legacy_direct = self.cfg.get(f"{svc}_api_key")
        if legacy_direct:
            return str(legacy_direct).strip()

        if self.s.ai_api_key:
            return self.s.ai_api_key.get_secret_value()

        return None

    def _auth_headers(self, service: str | None = None) -> dict:
        key = self.resolve_key(service)
        return {"Authorization": f"Bearer {key}"} if key else {}

    async def aclose(self) -> None:
        if self._bg:  # let pending RunPod cancel calls finish: a cancelled job must not be left billing
            await asyncio.wait(set(self._bg), timeout=3)
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

    def _runpod_mode(self) -> str:
        """Per-tenant `runpod_mode`: "run" (default: submit with /run, poll /status, cancellable) or "runsync" (one blocking call)."""
        return "runsync" if str(self.cfg.get("runpod_mode") or "run").lower() == "runsync" else "run"

    def _spawn(self, coro) -> None:
        t = asyncio.ensure_future(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def _cancel_job(self, service: str, ep: ServiceEndpoint, hdrs: dict, job_id: str) -> None:
        try:
            r = await self._http.post(f"{ep.url}/cancel/{job_id}", headers=hdrs, timeout=5)
            self.cancelled_jobs.append((service, r.status_code))
        except Exception as e:  # reported, never raised: cancelling is best effort and must not disturb the next turn
            self.cancelled_jobs.append((service, type(e).__name__))
            log.warning("could not cancel a %s job: %s", service, type(e).__name__)

    async def _cancel_when_submitted(self, post, service: str, ep: ServiceEndpoint, hdrs: dict) -> None:
        """The turn was cancelled while the /run request was still on its way: cancel the job as soon as its id comes back."""
        try:
            r = await post
            jid = r.json().get("id") if r.status_code < 400 else None
        except Exception:
            return
        if jid:
            await self._cancel_job(service, ep, hdrs, jid)

    async def _runpod_call(self, service: str, ep: ServiceEndpoint, hdrs: dict, body: dict):
        """Submit ONE RunPod job and return its finished result.

        run mode (default): POST /run (the job id is known at once), then poll /status. If this task is cancelled (the caller spoke again,
        a hangup, a timeout) the RunPod job is CANCELLED too: it stops billing and frees the single GPU worker for the next request.
        runsync mode: one blocking POST /runsync; its id is unknown until it ends, so nothing can be cancelled."""
        if self._runpod_mode() == "runsync":
            r = await self._http.post(ep.url + "/runsync", json=body, headers=hdrs, timeout=ep.timeout)
            if r.status_code >= 400:
                raise AiError(service, "http_error", str(r.status_code))
            return await self._runpod_wait(service, ep, hdrs, r.json())
        post = asyncio.ensure_future(self._http.post(ep.url + "/run", json=body, headers=hdrs, timeout=ep.timeout))
        try:
            r = await asyncio.shield(post)
        except asyncio.CancelledError:
            self._spawn(self._cancel_when_submitted(post, service, ep, hdrs))
            raise
        if r.status_code >= 400:
            raise AiError(service, "http_error", str(r.status_code))
        j = r.json()
        jid = j.get("id") if isinstance(j, dict) else None
        try:
            return await self._runpod_wait(service, ep, hdrs, j, poll_delay=0.06)
        except asyncio.CancelledError:
            if jid:
                self._spawn(self._cancel_job(service, ep, hdrs, jid))
            raise

    async def _runpod_wait(self, service: str, ep: ServiceEndpoint, hdrs: dict, j, poll_delay: float = 0.1):
        """RunPod `/runsync` stops waiting after ~90 s (cold start) and returns IN_QUEUE/IN_PROGRESS: poll `/status`.
        Adaptive fast polling (100ms -> 400ms) minimizes latency instead of waiting 1s per poll."""
        cap = float(self.cfg.get("runpod_poll_cap_s") or 0.15)   # status polls are free: a tight cap trims the wait after the job finishes
        while isinstance(j, dict) and j.get("status") in ("IN_QUEUE", "IN_PROGRESS") and j.get("id"):
            await asyncio.sleep(poll_delay)
            poll_delay = min(poll_delay * 1.5, cap)
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
        turn_trace.note_runpod_response(service, j)  # delayTime (queue/cold start) vs executionTime (compute)
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
                j = await self._runpod_call("stt", ep, hdrs, body)
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
            if ep.path.endswith("/runsync"):
                j = await self._runpod_call("brain", ep, hdrs, body)
            else:
                r = await self._http.post(ep.url + ep.path, json=body, headers=hdrs, timeout=ep.timeout)
                if r.status_code >= 400:
                    raise AiError("brain", "http_error", str(r.status_code))
            try:
                if not ep.path.endswith("/runsync"):
                    j = r.json()
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

    async def _served_brain_model(self, ep: ServiceEndpoint, hdrs: dict, configured: str) -> str:
        """The model name the OpenAI route serves (the vLLM worker rejects any other name). Read once per client from GET /openai/v1/models
        (free); the configured name is used when it is listed or when the list cannot be read."""
        if getattr(self, "_served_model", None):
            return self._served_model
        name = configured
        try:
            r = await self._http.get(f"{ep.url}/openai/v1/models", headers=hdrs, timeout=min(ep.timeout, 30))
            ids = [d.get("id") for d in (r.json().get("data") or []) if isinstance(d, dict) and d.get("id")] if r.status_code < 400 else []
            if ids and configured not in ids:
                name = ids[0]
        except Exception as e:                                          # the configured name is tried as it is
            log.info("could not list the served brain model: %s", type(e).__name__)
        self._served_model = name
        return name

    async def chat_stream(self, messages: list[dict], tools: list[dict] | None = None,
                          temperature: float | None = None) -> AsyncIterator[dict]:
        """Stream Brain / LLM generation.
        Yields:
          {"delta": str}             - token delta
          {"tool_calls": list[dict]} - tool calls emitted by LLM
          {"done": True, "text": str}- final text when completed
        """
        ep = self.endpoint("brain")
        model = self.cfg.get("models", {}).get("brain", self.s.ai_brain_model)
        max_tok = int(self.cfg.get("max_tokens", 400))
        temp = float(temperature if temperature is not None else self.cfg.get("temperature", getattr(self.s, "ai_temperature", 0.3)))
        hdrs = self._auth_headers("brain")
        if ep.path.endswith("/runsync") and self.cfg.get("streaming_pipeline"):
            # Token streaming over RunPod: the same endpoint's OpenAI-compatible route (the /runsync route only returns whole replies).
            ep = ServiceEndpoint(ep.url, "/openai/v1/chat/completions", ep.timeout)
            model = await self._served_brain_model(ep, hdrs, model)

        # Fallback for RunPod /runsync or non-streaming paths:
        if ep.path.endswith("/runsync"):
            res = await self.chat(messages, tools=tools, temperature=temperature)
            if res.get("tool_calls"):
                yield {"tool_calls": res["tool_calls"]}
            txt = res.get("text", "")
            if txt:
                yield {"delta": txt}
            yield {"done": True, "text": txt}
            return

        body: dict = {
            "model": model,
            "messages": messages,
            "temperature": temp,
            "max_tokens": max_tok,
            "stream": True,
        }
        if tools:
            body["tools"] = tools

        t0 = time.perf_counter()
        accumulated_text: list[str] = []
        accumulated_tool_calls: dict[int, dict] = {}

        try:
            async with self._sem:
                async with self._http.stream("POST", ep.url + ep.path, json=body, headers=hdrs, timeout=ep.timeout) as resp:
                    if resp.status_code >= 400:
                        err_content = await resp.aread()
                        raise AiError("brain", "http_error", f"{resp.status_code}: {err_content.decode(errors='replace')[:160]}")

                    content_type = resp.headers.get("content-type", "").lower()
                    # If endpoint returned non-SSE (e.g. application/json fallback from a mock or older server):
                    if "text/event-stream" not in content_type:
                        raw_bytes = await resp.aread()
                        try:
                            j = json.loads(raw_bytes)
                        except Exception:
                            raise AiError("brain", "bad_response", "invalid JSON response") from None
                        out = j.get("output", j) if isinstance(j, dict) else j
                        content, calls = parse_brain_output(out)   # strict: a malformed body is `bad_response`, never a silent empty reply
                        if calls:
                            yield {"tool_calls": calls}
                        if content:
                            yield {"delta": content}
                        yield {"done": True, "text": content}
                        m.AI_REQUESTS.labels("brain", "ok").inc()
                        return

                    # Process SSE text/event-stream
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line or line.startswith(":"):
                            continue
                        if line.startswith("data:"):
                            data_str = line[5:].strip()
                            if data_str == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue
                            choices = chunk.get("choices")
                            if not choices or not isinstance(choices, list):
                                continue
                            delta = choices[0].get("delta", {})
                            if not isinstance(delta, dict):
                                continue

                            content = delta.get("content")
                            if content:
                                accumulated_text.append(content)
                                yield {"delta": content}

                            tool_deltas = delta.get("tool_calls")
                            if tool_deltas and isinstance(tool_deltas, list):
                                for td in tool_deltas:
                                    idx = td.get("index", 0)
                                    if idx not in accumulated_tool_calls:
                                        accumulated_tool_calls[idx] = {
                                            "id": td.get("id", ""),
                                            "type": td.get("type", "function"),
                                            "function": {
                                                "name": (td.get("function") or {}).get("name", ""),
                                                "arguments": (td.get("function") or {}).get("arguments", "")
                                            }
                                        }
                                    else:
                                        target_fn = accumulated_tool_calls[idx]["function"]
                                        delta_fn = td.get("function") or {}
                                        if delta_fn.get("name"):
                                            target_fn["name"] += delta_fn["name"]
                                        if delta_fn.get("arguments"):
                                            target_fn["arguments"] += delta_fn["arguments"]

            if accumulated_tool_calls:
                sorted_calls = [accumulated_tool_calls[i] for i in sorted(accumulated_tool_calls.keys())]
                yield {"tool_calls": sorted_calls}

            full_text = "".join(accumulated_text)
            yield {"done": True, "text": full_text}
            m.AI_REQUESTS.labels("brain", "ok").inc()
        except asyncio.TimeoutError:
            m.AI_REQUESTS.labels("brain", "timeout").inc()
            raise AiTimeout("brain", "timeout") from None
        except AiError:
            m.AI_REQUESTS.labels("brain", "error").inc()
            raise
        except (httpx.HTTPError, OSError) as e:
            m.AI_REQUESTS.labels("brain", "error").inc()
            raise AiUnavailable("brain", "unreachable", type(e).__name__) from e
        except RuntimeError as e:
            m.AI_REQUESTS.labels("brain", "error").inc()
            if "closed" in str(e).lower():
                raise AiUnavailable("brain", "client_closed") from e
            raise
        finally:
            m.AI_LATENCY.labels("brain").observe(time.perf_counter() - t0)

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
                ulaw = bool(self.cfg.get("tts_ulaw8k"))
                if ulaw:
                    # Per-tenant flag `tts_ulaw8k` (default OFF; needs worker v3): the voice is chosen BY NAME (prepared once on the worker) and
                    # the worker answers in 8 kHz mu-law. No reference audio is read or sent: that saves ~0.77 MB of upload per request.
                    body["input"]["voice"] = self.cfg.get("tts_voice") or DEFAULT_TTS_VOICE
                    body["input"]["output_format"] = "ulaw8k"
                else:
                    ref = self._tts_reference_b64()
                    if ref:
                        body["input"]["reference_audio_base64"] = ref  # field the mme-tts-runpod handler requires
                j = await self._runpod_call("tts", ep, hdrs, body)
                out = j.get("output", {}) if isinstance(j, dict) else {}
                if ulaw and isinstance(out, dict) and out.get("error"):
                    # the worker's own message ("Unknown voice: ...", "output_format must be ...": an old worker): its text holds no caller words
                    raise AiError("tts", "worker_error", str(out["error"])[:80])
                b64_audio = None
                if isinstance(out, dict):
                    b64_audio = out.get("audio") or out.get("audio_base64")
                elif isinstance(out, str):
                    b64_audio = out
                if not b64_audio:
                    raise AiError("tts", "bad_response", "missing audio in output")
                raw = base64.b64decode(b64_audio)
                if ulaw and isinstance(out, dict) and out.get("format") == "ulaw8k":
                    if not raw:
                        raise AiError("tts", "bad_response", "empty audio in output")
                    return codec.mulaw_to_pcm16(raw), codec.MULAW_RATE     # audioop.ulaw2lin: 8-bit mu-law -> PCM16 at 8 kHz
                return wav_to_pcm16(raw)                                   # also when a worker answers a WAV despite the request
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
