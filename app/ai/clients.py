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
import json
import logging
import os
import re
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
_REF_CACHE: dict = {}
TTS_STREAM_MAX_B64 = 1_400_000   # one streamed audio item: ~1 MB decoded (~65 s of 8 kHz mu-law); larger is a protocol violation

DEFAULT_DOMAIN_VOCABULARY = (
    "Make My Education, MME, admission counselling, student enquiry, course selection, college selection, "
    "university admission, B.Tech, B.E., M.Tech, MBA, MCA, BBA, BCA, B.Com, M.Com, MBBS, BDS, B.Sc., M.Sc., "
    "BA, MA, B.Ed., LLB, LLM, diploma, engineering, computer science, artificial intelligence, machine learning, "
    "data science, mechanical engineering, civil engineering, electrical engineering, electronics, information technology, "
    "nursing, pharmacy, law, design, commerce, arts, science, PCM, PCB, Physics, Chemistry, Mathematics, Biology, "
    "Class 10, Class 12, graduation, postgraduate, percentage, CGPA, backlog, passing year, gap year, JEE Main, "
    "JEE Advanced, NEET, CUET, CAT, XAT, CMAT, MAT, NMAT, GATE, CLAT, eligibility, cutoff, entrance examination, "
    "merit list, counselling, admission process, registration, application form, tuition fees, hostel fees, scholarship, "
    "education loan, placement, package, specialisation, academic session, government college, private college, university, "
    "affiliated college, distance education, online learning, regular course, hostel accommodation, Delhi NCR, "
    "Uttar Pradesh, Ghaziabad, Noida, Greater Noida, Gurugram, fees kitni hai, admission kaise milega, eligibility kya hai, "
    "kaun sa course, kitne percentage, college suggest karo, scholarship milegi, placement kaisa hai, form kab niklega, "
    "last date kya hai, BTech ki fees, MBA admission, mujhe admission chahiye."
)


@dataclass
class TtsAudioChunk:
    """One ordered piece of a progressive TTS reply: raw PCM16 mono at `sample_rate`. `streamed` False = the worker sent the whole reply at once."""
    seq: int
    pcm: bytes
    sample_rate: int
    final: bool
    streamed: bool = True


class AiError(Exception):
    def __init__(self, service: str, kind: str, detail: str = ""):
        super().__init__(f"{service}:{kind}")
        self.service, self.kind, self.detail = service, kind, detail


class AiUnavailable(AiError):
    pass


class AiTimeout(AiError):
    def __init__(self, service: str, kind: str, detail: str = "", remote_confirmed_cancelled: bool = False, job_id: str | None = None):
        super().__init__(service, kind, detail)
        self.remote_confirmed_cancelled = remote_confirmed_cancelled
        self.job_id = job_id


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


def resolve_stt_model(s: Settings, cfg: dict) -> dict:
    """Which STT model NAME the application uses, where it comes from, and what it does and does not prove. No secrets.

    Precedence of the NAME (highest first): tenant `ai_config.models.stt` > process environment `MME_TEL_AI_STT_MODEL` > `.env` file > application default.
    (Environment beats the .env file in pydantic-settings; the tenant document is merged over the settings value in `AiService.get_config`.)
    IMPORTANT: for RunPod `/runsync` endpoints the name is NOT sent in the request at all (the endpoint decides which model runs); it is only sent as the
    `model` form field to OpenAI-style `/v1/audio/transcriptions` servers. So it is a label for logs/dashboards/config checks, never proof of what is
    loaded: verify the loaded model with the worker's health response (`model_id`, `model_commit_hash`)."""
    models = (cfg or {}).get("models") or {}
    effective = models.get("stt") or s.ai_stt_model
    if (cfg or {}).get("_stt_model_source") == "tenant":
        source = "tenant ai_config.models.stt"
    elif "MME_TEL_AI_STT_MODEL" in os.environ:
        source = "process environment MME_TEL_AI_STT_MODEL"
    elif "ai_stt_model" in getattr(s, "model_fields_set", set()):
        source = ".env file (MME_TEL_AI_STT_MODEL)"
    else:
        source = "application default"
    url = (cfg or {}).get("stt_url") or ""
    path = ((cfg or {}).get("paths") or {}).get("stt", "")
    runsync = url.rstrip("/").endswith("/runsync") or path.endswith("/runsync")
    return {"effective_name": effective, "source": source, "settings_expected_model_id": s.ai_stt_model,
            "name_equals_settings_expected": effective == s.ai_stt_model, "name_sent_to_provider": not runsync,
            "default_language": (cfg or {}).get("default_language", "auto"),
            "runtime_model_verified_by_this_value": False}


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


# A tenant's `key_refs[service]` names an ENVIRONMENT VARIABLE of the service process that holds the API key. Only names of this shape are honoured: a tenant setting
# must never be able to make the service read (and send to a model host) an arbitrary environment secret such as the JWT or webhook key.
KEY_REF_RE = re.compile(r"^(?:RUNPOD|MME_TEL_KEY)_[A-Z0-9_]{1,50}$")
_PLAINTEXT_WARNED: set[str] = set()


def resolve_key_ref(name) -> str | None:
    """The value of the environment variable `name`, or None if the name is not an allowed reference, or the variable is unset/blank. Never logs."""
    if not isinstance(name, str) or not KEY_REF_RE.fullmatch(name):
        return None
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else None


def credential_status(cfg: dict, s) -> dict:
    """Where each service's API key comes from, WITHOUT any value: {service: {source, ref_name, ref_resolves}}. `ref_name` is an environment-variable NAME."""
    out = {}
    refs = cfg.get("key_refs") if isinstance(cfg.get("key_refs"), dict) else {}
    keys = cfg.get("keys") if isinstance(cfg.get("keys"), dict) else {}
    for svc in SERVICES:
        ref = refs.get(svc)
        resolves = bool(ref) and resolve_key_ref(ref) is not None
        if resolves:
            source = "key_ref"
        elif keys.get(svc):
            source = "plaintext_tenant_key"
        elif cfg.get(f"{svc}_api_key"):
            source = "tenant_api_key_field"
        elif getattr(s, "ai_api_key", None) and s.ai_api_key.get_secret_value():
            source = "service_environment_key"
        else:
            source = "none"
        out[svc] = {"source": source, "ref_name": ref if isinstance(ref, str) and KEY_REF_RE.fullmatch(ref) else None, "ref_resolves": resolves,
                    "plaintext_present": bool(keys.get(svc))}
    return out


# Endpoints whose TTS worker answered with a BAKED voice (response `voice` other than "request"): such a worker ignores `reference_audio_base64`
# (handler contract: ignored unless ALLOW_REQUEST_REF=1, in which case it answers voice="request"), so the ~1 MB upload per request is skipped for them.
# Keyed by endpoint URL, per process, bounded, and re-probed after REF_SKIP_TTL_S so a worker that is redeployed with ALLOW_REQUEST_REF=1 is noticed.
_REF_SKIP: dict[str, float] = {}
REF_SKIP_TTL_S = 900.0
REF_SKIP_MAX = 64


KNOWN_PRODUCTION_ENDPOINTS = {
    "stt": "ua9l3gus6qjnmg",
    "brain": "hu8qhrvlxbv314",
    "tts": "6bmxas8ccwzhrv",
}


class ProductionEndpointFenced(AiUnavailable):
    """Raised when development code attempts to route to shared production endpoints."""

    def __init__(self, service: str, endpoint_id: str):
        super().__init__(
            service,
            "production_endpoint_fenced",
            f"Dedicated development {service.upper()} endpoint is not configured; "
            f"silent usage of shared production endpoint '{endpoint_id}' is blocked by strict environment isolation."
        )
        self.endpoint_id = endpoint_id
        self.code = "production_endpoint_fenced"


class ModelClients:
    """Bounded-concurrency, timeout-guarded access to the model services of ONE tenant configuration."""

    def __init__(self, s: Settings, cfg: dict, transport: httpx.AsyncBaseTransport | None = None):
        self.s, self.cfg = s, cfg or {}
        self._sem = asyncio.Semaphore(s.ai_max_concurrency)
        self._custom_transport = transport is not None
        limits = httpx.Limits(max_keepalive_connections=20, max_connections=50, keepalive_expiry=120.0)
        self._http = httpx.AsyncClient(transport=transport, follow_redirects=False, limits=limits,
                                       timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=30.0))
        self._bg: set[asyncio.Task] = set()          # fire-and-forget RunPod cancel calls (kept referenced until they finish)
        self.cancelled_jobs: list[tuple[str, int | str]] = []   # (service, http status | error type) of each RunPod cancel attempt
        self.job_counts: dict[str, int] = {}                    # model requests STARTED by this call's clients, per service (usage accounting, not a bill)

    def _auth_headers(self, service: str | None = None) -> dict:
        """Bearer header for one model service. Order: (1) the tenant's `key_refs[service]` = the NAME of an environment variable of the service process
        (restricted to RUNPOD_* / MME_TEL_KEY_*, see KEY_REF_RE); (2) LEGACY plaintext `keys[service]` stored in the tenant document (deprecated: warned once, never
        logged); (3) legacy `<service>_api_key`; (4) the service-wide MME_TEL_AI_API_KEY. A reference whose variable is unset falls through, so migrating never causes an
        outage."""
        key = None
        refs = self.cfg.get("key_refs")
        if service and isinstance(refs, dict) and refs.get(service):
            key = resolve_key_ref(refs[service])
        if not key and service and isinstance(self.cfg.get("keys"), dict) and self.cfg["keys"].get(service):
            key = self.cfg["keys"][service]
            if service not in _PLAINTEXT_WARNED:
                _PLAINTEXT_WARNED.add(service)
                log.warning("tenant plaintext API key in use for %s: migrate it to key_refs (an environment-variable reference)", service)
        if not key and service and self.cfg.get(f"{service}_api_key"):
            key = self.cfg[f"{service}_api_key"]
        if not key and self.s.ai_api_key:
            key = self.s.ai_api_key.get_secret_value()
        return {"Authorization": f"Bearer {key}"} if key else {}

    async def aclose(self) -> None:
        if self._bg:  # let pending RunPod cancel calls finish: a cancelled job must not be left billing
            await asyncio.wait(set(self._bg), timeout=3)
        await self._http.aclose()

    def endpoint(self, service: str) -> ServiceEndpoint:
        url = (self.cfg.get(f"{service}_url") or "").rstrip("/")
        if not url:
            raise AiUnavailable(service, "not_configured")

        # Strict Environment Isolation Guard:
        # In non-production environments, refuse to silently route live network requests to known production endpoints.
        if getattr(self.s, "ai_strict_env_isolation", True) and not self.s.is_prod_like and not getattr(self, "_custom_transport", False):
            prod_id = KNOWN_PRODUCTION_ENDPOINTS.get(service)
            if prod_id and prod_id in url:
                log.error("FENCING VIOLATION: development environment attempted to contact shared production %s endpoint %s", service, prod_id)
                raise ProductionEndpointFenced(service, prod_id)

        defaults = {"stt": "/v1/audio/transcriptions", "brain": "/v1/chat/completions", "tts": "/v1/audio/speech", "rag": "/search"}
        t = (self.cfg.get("timeouts") or {}).get(service, self.s.ai_request_timeout_s)
        custom_path = (self.cfg.get("paths") or {}).get(service)
        if custom_path:
            base = url[:-8] if url.endswith("/runsync") else url
            ep = ServiceEndpoint(base, custom_path, float(t))
        elif url.endswith("/runsync"):
            ep = ServiceEndpoint(url[:-8], "/runsync", float(t))
        else:
            ep = ServiceEndpoint(url, defaults[service], float(t))

        log.debug("Resolved %s endpoint: host=%s, path=%s, mode=%s (credentials redacted)", service, ep.url, ep.path, self._runpod_mode())
        return ep

    def _runpod_mode(self) -> str:
        """Per-tenant `runpod_mode`: "run" (default: submit with /run, poll /status, cancellable) or "runsync" (one blocking call)."""
        return "runsync" if str(self.cfg.get("runpod_mode") or "run").lower() == "runsync" else "run"

    def _spawn(self, coro) -> None:
        t = asyncio.ensure_future(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def cancel_job(self, service: str, ep: ServiceEndpoint, hdrs: dict, job_id: str, wait_confirm: bool = False) -> bool:
        """Attempt to cancel a RunPod job.
        If wait_confirm is True, awaits the cancellation call and returns True ONLY if provider confirms
        cancellation (HTTP 200 with JSON status == 'CANCELLED').
        Returns False otherwise (best-effort, unconfirmed)."""
        try:
            r = await self._http.post(f"{ep.url}/cancel/{job_id}", headers=hdrs, timeout=3.0)
            self.cancelled_jobs.append((service, r.status_code))
            if r.status_code < 400:
                try:
                    data = r.json()
                    if isinstance(data, dict) and data.get("status") == "CANCELLED":
                        return True
                except Exception:
                    pass
            return False
        except Exception as e:
            self.cancelled_jobs.append((service, type(e).__name__))
            log.warning("could not cancel a %s job: %s", service, type(e).__name__)
            return False

    async def _cancel_job(self, service: str, ep: ServiceEndpoint, hdrs: dict, job_id: str) -> None:
        await self.cancel_job(service, ep, hdrs, job_id, wait_confirm=False)

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
            return await asyncio.wait_for(self._runpod_wait(service, ep, hdrs, j, poll_delay=0.06), timeout=ep.timeout)
        except asyncio.TimeoutError:
            confirmed = False
            if jid:
                try:
                    confirmed = await self.cancel_job(service, ep, hdrs, jid, wait_confirm=True)
                except Exception:
                    pass
            raise AiTimeout(service, "timeout", detail=jid or "", remote_confirmed_cancelled=confirmed, job_id=jid) from None
        except asyncio.CancelledError:
            if jid:
                self._spawn(self._cancel_job(service, ep, hdrs, jid))
            raise

    async def _runpod_wait(self, service: str, ep: ServiceEndpoint, hdrs: dict, j, poll_delay: float = 0.1):
        """RunPod `/runsync` stops waiting after ~90 s (cold start) and returns IN_QUEUE/IN_PROGRESS: poll `/status`.
        Adaptive fast polling (100ms -> 400ms) minimizes latency instead of waiting 1s per poll."""
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

    def _count_job(self, service: str) -> None:
        self.job_counts[service] = self.job_counts.get(service, 0) + 1

    async def _call(self, service: str, fn):
        ep = self.endpoint(service)
        self._count_job(service)
        t0 = time.perf_counter()
        try:
            async with self._sem:
                res = await asyncio.wait_for(fn(ep), ep.timeout)
            m.AI_REQUESTS.labels(service, "ok").inc()
            return res
        except asyncio.TimeoutError:
            m.AI_REQUESTS.labels(service, "timeout").inc()
            turn_trace.note_count(f"{service}_timeout")      # per-turn counter (STT/Brain/TTS/RAG), shown in the trace
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
    async def transcribe(self, pcm: bytes, rate: int, language: str, prompt: str | None = None) -> dict:
        async def go(ep: ServiceEndpoint):
            hdrs = self._auth_headers("stt")
            prompt_to_send = prompt if (prompt is not None and prompt.strip()) else DEFAULT_DOMAIN_VOCABULARY
            if ep.path.endswith("/runsync"):
                wav_bytes = pcm16_to_wav(pcm, rate)
                b64 = base64.b64encode(wav_bytes).decode("ascii")
                body = {
                    "input": {
                        "audio_b64": b64,  # the ONLY audio field sent: what the mme-stt-runpod handler requires (verified live 2026-10-23; tools/stt_worker_v2 reads it too)
                        # "hi"/"en"/... go through as they are (hinglish -> hi); "auto" is sent EXPLICITLY as "auto" (auto-detect) so a worker whose default
                        # is "hi" cannot silently turn the tenant's auto into Hindi; only a missing language (None) means "the worker's default".
                        "language": {"hinglish": "hi"}.get(language, language) if language else None,
                        "prompt": prompt_to_send,
                    }
                }
                if self.s.ai_stt_legacy_audio_aliases:   # escape hatch for an unknown old worker: the same audio again under two more names (3x the upload)
                    body["input"]["audio"] = b64
                    body["input"]["audio_base64"] = b64
                j = await self._runpod_call("stt", ep, hdrs, body)
                out = j.get("output", {}) if isinstance(j, dict) else {}
                if isinstance(out, dict):
                    text = out.get("transcription") or out.get("text") or ""
                    lang = out.get("language") or language
                    effective = out.get("decode_language")
                elif isinstance(out, str):
                    text, lang, effective = out, language, None
                else:
                    text, lang, effective = "", language, None
                res = {"text": text.strip(), "language": lang}
                if effective:
                    res["decode_language"] = effective          # the language the worker actually decoded with (new workers only)
                return res
            else:
                files = {"file": ("audio.wav", pcm16_to_wav(pcm, rate), "audio/wav")}
                data = {"model": self.cfg.get("models", {}).get("stt", self.s.ai_stt_model)}
                if language and language != "auto":
                    data["language"] = {"hinglish": "hi"}.get(language, language)
                if prompt_to_send:
                    data["prompt"] = prompt_to_send
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
            rep_pen = float(self.cfg.get("repetition_penalty", getattr(self.s, "ai_repetition_penalty", 1.15)))
            pres_pen = float(self.cfg.get("presence_penalty", getattr(self.s, "ai_presence_penalty", 0.3)))
            freq_pen = float(self.cfg.get("frequency_penalty", getattr(self.s, "ai_frequency_penalty", 0.2)))
            if ep.path.endswith("/runsync"):
                chatml_parts = [f"<|im_start|>{m.get('role', 'user')}\n{m.get('content', '')}<|im_end|>" for m in messages]
                chatml_parts.append("<|im_start|>assistant\n")
                prompt = "\n".join(chatml_parts)
                sampling: dict[str, Any] = {"temperature": temp, "max_tokens": max_tok}  # RunPod vLLM worker format
                if rep_pen and rep_pen != 1.0:
                    sampling["repetition_penalty"] = rep_pen
                if pres_pen:
                    sampling["presence_penalty"] = pres_pen
                if freq_pen:
                    sampling["frequency_penalty"] = freq_pen
                body = {
                    "input": {
                        "prompt": prompt,
                        "messages": messages,
                        "model": model,
                        "sampling_params": sampling,
                    }
                }
            else:
                body = {"model": model, "messages": messages, "temperature": temp, "max_tokens": max_tok}
                if rep_pen and rep_pen != 1.0:
                    body["repetition_penalty"] = rep_pen
                if pres_pen:
                    body["presence_penalty"] = pres_pen
                if freq_pen:
                    body["frequency_penalty"] = freq_pen
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

        # Fallback for RunPod /runsync or non-streaming paths:
        if ep.path.endswith("/runsync"):
            if not self.cfg.get("brain_stream"):
                res = await self.chat(messages, tools=tools, temperature=temperature)
                if res.get("tool_calls"):
                    yield {"tool_calls": res["tool_calls"]}
                txt = res.get("text", "")
                if txt:
                    yield {"delta": txt}
                yield {"done": True, "text": txt}
                return
            # Tenant requested brain_stream on runsync endpoint: attempt streaming route
            stream_path = (self.cfg.get("paths") or {}).get("brain_stream") or "/openai/v1/chat/completions"
            ep = ServiceEndpoint(ep.url, stream_path, ep.timeout)

        body: dict = {
            "model": model,
            "messages": messages,
            "temperature": temp,
            "max_tokens": max_tok,
            "stream": True,
        }
        rep_pen = float(self.cfg.get("repetition_penalty", getattr(self.s, "ai_repetition_penalty", 1.15)))
        pres_pen = float(self.cfg.get("presence_penalty", getattr(self.s, "ai_presence_penalty", 0.3)))
        freq_pen = float(self.cfg.get("frequency_penalty", getattr(self.s, "ai_frequency_penalty", 0.2)))
        if rep_pen and rep_pen != 1.0:
            body["repetition_penalty"] = rep_pen
        if pres_pen:
            body["presence_penalty"] = pres_pen
        if freq_pen:
            body["frequency_penalty"] = freq_pen
        if tools:
            body["tools"] = tools

        t0 = time.perf_counter()
        self._count_job("brain")
        accumulated_text: list[str] = []
        accumulated_tool_calls: dict[int, dict] = {}

        try:
            async with self._sem:
                async with self._http.stream("POST", ep.url + ep.path, json=body, headers=hdrs, timeout=ep.timeout) as resp:
                    if resp.status_code in (404, 405) and self.cfg.get("brain_stream"):
                        log.warning("brain_stream route %s%s unavailable (HTTP %d); falling back to synchronous chat", ep.url, ep.path, resp.status_code)
                        res = await self.chat(messages, tools=tools, temperature=temperature)
                        if res.get("tool_calls"):
                            yield {"tool_calls": res["tool_calls"]}
                        txt = res.get("text", "")
                        if txt:
                            yield {"delta": txt}
                        yield {"done": True, "text": txt}
                        return

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
    def _reference_not_needed(self, ep_key: str | None) -> bool:
        if not ep_key or not self.s.ai_tts_reference_adaptive:
            return False
        exp = _REF_SKIP.get(ep_key)
        if exp is None:
            return False
        if time.monotonic() >= exp:          # re-probe: send the reference once more and look at the answer
            _REF_SKIP.pop(ep_key, None)
            return False
        return True

    def _note_tts_voice(self, ep_key: str | None, out) -> None:
        """Learn from the worker's own answer whether it used the reference we sent: voice == "request" means yes (keep sending), any other voice name means
        it used its baked voice and ignored the upload (stop sending, re-probe later). No `voice` field (an older worker) changes nothing."""
        if not ep_key or not self.s.ai_tts_reference_adaptive or not isinstance(out, dict) or not self._tts_reference_b64():
            return
        voice = out.get("voice")
        if not isinstance(voice, str) or not voice:
            return
        if voice == "request":
            _REF_SKIP.pop(ep_key, None)
        else:
            if len(_REF_SKIP) >= REF_SKIP_MAX and ep_key not in _REF_SKIP:
                _REF_SKIP.pop(next(iter(_REF_SKIP)))
            _REF_SKIP[ep_key] = time.monotonic() + REF_SKIP_TTL_S

    def _tts_runpod_body(self, text: str, language: str, model: str, ep_key: str | None = None) -> dict:
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
        if self.s.ai_tts_output_format == "ulaw8k":
            body["input"]["output_format"] = "ulaw8k"   # opt-in (MME_TEL_AI_TTS_OUTPUT_FORMAT); the reply's own `format` field says what actually came back
        ref = None if self._reference_not_needed(ep_key) else self._tts_reference_b64()
        if ref:
            body["input"]["reference_audio_base64"] = ref  # older workers require it; v3/v4 handlers ignore it unless ALLOW_REQUEST_REF=1 (see _note_tts_voice)
        return body

    async def synthesize_stream(self, text: str, language: str) -> AsyncIterator[TtsAudioChunk]:
        """Progressive TTS: yields `TtsAudioChunk`s in order as the worker produces them (RunPod `/run` + `/stream/{id}`; the job is cancelled on
        early close). Contract per streamed item (the worker `yield`s it): {"seq": int from 0, "audio_base64": str, "format": "ulaw8k"|"pcm16",
        "sample_rate": int, "final": bool}. A worker that does NOT stream (today's handler returns one finished output) completes with no stream
        items: the finished output is then read from /status and yielded as ONE final chunk (`streamed=False`), so old workers keep working.
        UNVERIFIED against a live RunPod endpoint: the `/stream` JSON shape ({"status", "stream": [{"output": item}]}) is not in the pages
        read; parsing is tolerant, the first live run must confirm it."""
        ep = self.endpoint("tts")
        if not ep.path.endswith("/runsync"):   # not RunPod: no progressive protocol is defined, one complete reply
            pcm, rate = await self.synthesize(text, language)
            yield TtsAudioChunk(0, pcm, rate, True, False)
            return
        hdrs = self._auth_headers("tts")
        model = self.cfg.get("models", {}).get("tts", self.s.ai_tts_model)
        body = self._tts_runpod_body(text, language, model)
        body["input"].update(stream=True, output_format="ulaw8k")
        if os.getenv("ALLOW_UNWATERMARKED_STREAMING") == "1":
            body["input"]["allow_unwatermarked"] = True
        t0 = time.perf_counter()
        self._count_job("tts")
        jid = None
        finished = failed = confirmed = False
        try:
            async with self._sem:
                post = asyncio.ensure_future(self._http.post(ep.url + "/run", json=body, headers=hdrs, timeout=ep.timeout))
                try:
                    r = await asyncio.shield(post)
                except asyncio.CancelledError:
                    self._spawn(self._cancel_when_submitted(post, "tts", ep, hdrs))
                    raise
                if r.status_code >= 400:
                    raise AiError("tts", "http_error", str(r.status_code))
                j = r.json()
                jid = j.get("id") if isinstance(j, dict) else None
                if not jid:
                    raise AiError("tts", "bad_response", "no job id")
                expected, got_any, delay = 0, False, 0.02
                deadline = time.monotonic() + ep.timeout
                while True:
                    sr = await self._http.get(f"{ep.url}/stream/{jid}", headers=hdrs, timeout=30)
                    if sr.status_code >= 400:
                        raise AiError("tts", "http_error", str(sr.status_code))
                    sj = sr.json()
                    status = sj.get("status") if isinstance(sj, dict) else None
                    items = (sj.get("stream") if isinstance(sj, dict) else None) or []
                    for item in items:
                        chunk = self._parse_tts_chunk(item)
                        if chunk.seq < expected:
                            continue   # a repeated item (a re-sent poll window) is dropped, never played twice
                        if chunk.seq > expected:
                            raise AiError("tts", "bad_response", f"stream gap: expected seq {expected}, got {chunk.seq}")
                        expected += 1
                        got_any, delay = True, 0.015
                        if chunk.final:
                            finished = True
                        yield chunk
                        if finished:
                            break
                    if finished:
                        return
                    if status in ("COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"):
                        finished = True
                        if status != "COMPLETED":
                            raise AiError("tts", "runpod_error", f"status={status}")
                        done = await self._http.get(f"{ep.url}/status/{jid}", headers=hdrs, timeout=30)
                        dj = done.json() if done.status_code < 400 else None
                        out = dj.get("output") if isinstance(dj, dict) else None
                        if isinstance(out, list) and out:
                            for item in out[expected:]:
                                chunk = self._parse_tts_chunk(item)
                                expected += 1
                                yield chunk
                                if chunk.final:
                                    return
                            if expected > 0:
                                return
                            out = out[0]
                        dj = done.json() if done.status_code < 400 else None
                        out = dj.get("output") if isinstance(dj, dict) else None
                        if isinstance(out, list) and out:
                            out = out[0]
                        if isinstance(out, dict) and out.get("error"):
                            raise AiError("tts", "runpod_error", str(out["error"])[:200])
                        b64 = (out.get("audio") or out.get("audio_base64")) if isinstance(out, dict) else None
                        if not b64:
                            raise AiError("tts", "bad_response", "missing audio in output")
                        pcm, rate = wav_to_pcm16(base64.b64decode(b64))
                        yield TtsAudioChunk(0, pcm, rate, True, False)
                        return
                    if time.monotonic() > deadline:
                        raise asyncio.TimeoutError
                    await asyncio.sleep(delay)
                    # Adaptive dynamic polling: 15-35ms once streaming has started to minimize chunk pickup latency;
                    # 20-50ms during initial worker cold startup.
                    if got_any:
                        delay = min(delay * 1.25, 0.035)
                    else:
                        delay = min(delay * 1.3, 0.05)
        except asyncio.TimeoutError:
            failed = True
            m.AI_REQUESTS.labels("tts", "timeout").inc()
            if jid:
                try:
                    confirmed = await self.cancel_job("tts", ep, hdrs, jid, wait_confirm=True)
                except Exception:
                    pass
            raise AiTimeout("tts", "timeout", detail=jid or "", remote_confirmed_cancelled=confirmed, job_id=jid) from None
        except AiError:
            failed = True
            m.AI_REQUESTS.labels("tts", "error").inc()
            raise
        except (httpx.HTTPError, OSError) as e:
            failed = True
            m.AI_REQUESTS.labels("tts", "error").inc()
            raise AiUnavailable("tts", "unreachable", type(e).__name__) from e
        finally:
            if finished and not failed:
                m.AI_REQUESTS.labels("tts", "ok").inc()
            if jid and not (finished and not failed) and not confirmed:   # consumer closed early / cancelled / failed mid-stream: stop the GPU job (it frees the single worker)
                self._spawn(self._cancel_job("tts", ep, hdrs, jid))
            m.AI_LATENCY.labels("tts").observe(time.perf_counter() - t0)

    @staticmethod
    def _parse_tts_chunk(item) -> TtsAudioChunk:
        out = item.get("output", item) if isinstance(item, dict) else None
        if not isinstance(out, dict):
            raise AiError("tts", "bad_response", "stream item is not an object")
        if out.get("error"):
            raise AiError("tts", "runpod_error", str(out["error"])[:200])
        seq, final = out.get("seq"), bool(out.get("final"))
        b64, fmt, rate = out.get("audio_base64", ""), out.get("format"), out.get("sample_rate")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0 or not isinstance(b64, str) or len(b64) > TTS_STREAM_MAX_B64:
            raise AiError("tts", "bad_response", "malformed stream chunk")
        try:
            raw = base64.b64decode(b64, validate=True)
        except ValueError:
            raise AiError("tts", "bad_response", "chunk audio is not base64") from None
        if not raw and not final:
            raise AiError("tts", "bad_response", "empty non-final chunk")
        if fmt == "ulaw8k":
            return TtsAudioChunk(seq, codec.mulaw_to_pcm16(raw), 8000, final, True)
        if fmt == "pcm16" and isinstance(rate, int) and rate > 0 and len(raw) % 2 == 0:
            return TtsAudioChunk(seq, raw, rate, final, True)
        raise AiError("tts", "bad_response", "unsupported stream chunk format")

    async def synthesize(self, text: str, language: str) -> tuple[bytes, int]:
        async def go(ep: ServiceEndpoint):
            hdrs = self._auth_headers("tts")
            model = self.cfg.get("models", {}).get("tts", self.s.ai_tts_model)
            if ep.path.endswith("/runsync"):
                body = self._tts_runpod_body(text, language, model, ep_key=ep.url)
                j = await self._runpod_call("tts", ep, hdrs, body)
                out = j.get("output", {}) if isinstance(j, dict) else {}
                if isinstance(out, list) and out:
                    out = out[0] if isinstance(out[0], dict) else {}
                self._note_tts_voice(ep.url, out)
                b64_audio = None
                if isinstance(out, dict):
                    b64_audio = out.get("audio") or out.get("audio_base64")
                elif isinstance(out, str):
                    b64_audio = out
                if not b64_audio:
                    if isinstance(out, dict) and out.get("error"):       # the handler answered with its own error (OOM, unknown voice, bad language ...)
                        raise AiError("tts", "worker_error", str(out["error"])[:160])
                    raise AiError("tts", "bad_response", "missing audio in output")
                try:
                    audio_bytes = base64.b64decode(b64_audio)
                except ValueError:
                    raise AiError("tts", "bad_audio", "audio is not base64") from None
                if isinstance(out, dict) and out.get("format") == "ulaw8k":   # G.711 mu-law 8 kHz, as requested: no WAV, no resampling
                    if not audio_bytes or len(audio_bytes) > 8000 * TTS_MAX_SECONDS:
                        raise AiError("tts", "bad_audio", "empty or too long")
                    return codec.mulaw_to_pcm16(audio_bytes), 8000
                return wav_to_pcm16(audio_bytes)   # a worker that ignores output_format (or the default) still answers with a WAV
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
