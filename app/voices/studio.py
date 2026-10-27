"""Voice Studio service: uploads (consent, limits, clip preparation), voice records, and preview jobs (background task, deadline, cold-start-aware status, spend cap).

Preview audio lives in memory only (bounded, short-lived) and is never written to the database, the audit log or the logs. Audit entries and log lines carry ids, counts
and codes only: no audio, no text. Mocked in tests; the worker side is UNVERIFIED on a GPU (docs/VOICE_STUDIO_DESIGN.md)."""
from __future__ import annotations

import asyncio
import base64
import binascii
import io
import logging
import math
import re
import time
import wave
from dataclasses import dataclass, field
from datetime import timedelta

from pymongo.errors import DuplicateKeyError

from app.ai import codec
from app.ai.clients import pcm16_to_wav
from app.core.errors import Conflict, NotFound, RateLimited, Unprocessable
from app.core.ids import new_uuid, utcnow
from app.voices import clip as clipmod
from app.voices.frontend import PREVIEW_LANGUAGES, worker_input
from app.voices.store import VoiceStore, voice_out
from app.voices.worker import VoiceWorker, WorkerError, check_configured

log = logging.getLogger("mme_telephony.voices")

NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")
SCOPES = ("preview_only", "preview_and_calls")
JOB_STATES = ("queued", "warming", "speaking", "done", "error")
WORKER_ANSWERED = {"voice_unavailable", "worker_rejected_clip", "worker_prepare_failed", "worker_outdated", "worker_error"}   # the worker finished its jobs: its reported time is complete
MAX_KEPT_JOBS = 50                      # finished previews kept in memory per process (the audio of each is dropped after voice_studio_result_ttl_s)
DETAIL = {"queued": "Waiting to start.",
          "warming": "Warming up the voice server. This can take about a minute.",
          "speaking": "The voice is speaking.",
          "done": "Ready.", "error": "The preview failed."}


@dataclass
class Job:
    id: str
    tenant_id: str
    voice_id: str
    created_by: str
    phone_quality: bool
    chars: int
    started: float = field(default_factory=time.monotonic)
    status: str = "queued"
    detail: str = DETAIL["queued"]
    error_code: str | None = None
    error_message: str | None = None
    audio: bytes | None = None
    audio_s: float | None = None
    finished: float | None = None
    task: asyncio.Task | None = None

    def set(self, status: str, detail: str | None = None) -> None:
        if self.finished is None:                # a finished job never changes again
            self.status, self.detail = status, detail or DETAIL[status]

    def fail(self, code: str, message: str) -> None:
        if self.finished is None:
            self.status, self.detail, self.error_code, self.error_message, self.finished = "error", DETAIL["error"], code, message, time.monotonic()

    def succeed(self, audio: bytes, audio_s: float) -> None:
        if self.finished is None:
            self.status, self.detail, self.audio, self.audio_s, self.finished = "done", DETAIL["done"], audio, audio_s, time.monotonic()


def job_out(job: Job) -> dict:
    now = time.monotonic()
    out = {"job_id": job.id, "voice_id": job.voice_id, "status": job.status, "detail": job.detail, "phone_quality": job.phone_quality,
           "elapsed_s": round((job.finished or now) - job.started, 1), "error": None, "audio": None}
    if job.status == "error":
        out["error"] = {"code": job.error_code, "message": job.error_message}
    if job.status == "done" and job.audio is not None:
        out["audio"] = {"url": f"/v1/voices/previews/{job.id}/audio", "content_type": "audio/wav", "duration_s": job.audio_s}
    return out


def micro_usd(gpu_seconds: float, usd_per_s: float) -> int:
    """Cost in millionths of a US dollar, rounded UP (never under-counts); rounded to 3 decimals first so float noise cannot add a whole unit."""
    return int(math.ceil(round(gpu_seconds * usd_per_s * 1e6, 3)))


def wav_seconds(wav: bytes) -> float:
    with wave.open(io.BytesIO(wav), "rb") as w:
        return w.getnframes() / w.getframerate()


def decode_worker_audio(out: dict) -> tuple[bytes, float]:
    """The worker's answer -> (WAV bytes for the browser, seconds). `ulaw8k` is decoded to 8 kHz PCM like the call path does."""
    try:
        raw = base64.b64decode(out.get("audio_base64") or out.get("audio") or "", validate=True)
    except (binascii.Error, ValueError, TypeError):
        raise WorkerError("worker_bad_response", "The voice worker returned audio that could not be decoded.") from None
    if not raw:
        raise WorkerError("worker_bad_response", "The voice worker returned empty audio.")
    try:
        if out.get("format") == "ulaw8k":
            wav = pcm16_to_wav(codec.mulaw_to_pcm16(raw), codec.MULAW_RATE)
        else:
            wav = raw
        return wav, wav_seconds(wav)
    except (codec.CodecError, wave.Error, EOFError, ZeroDivisionError):
        raise WorkerError("worker_bad_response", "The voice worker returned audio in an unexpected format.") from None


def worker_failure(out: dict) -> WorkerError:
    """An `error` answer of the worker -> a stable code. An OLD worker (no Voice Studio tasks) answers with an error that has no `code`."""
    code = out.get("code")
    if code == "needs_reference":
        return WorkerError("voice_unavailable", "The voice worker still does not have this voice after it was sent.")
    if code in ("invalid_name", "name_reserved", "invalid_audio", "too_short", "too_long", "too_large"):
        return WorkerError("worker_rejected_clip", "The voice worker refused the reference clip.")
    if code == "prepare_failed":
        return WorkerError("worker_prepare_failed", "The voice worker could not prepare this voice.")
    if code is None:
        return WorkerError("worker_outdated", "The voice worker is an older version without Voice Studio support.")
    return WorkerError("worker_error", "The voice worker reported an error.")


class VoiceStudio:
    def __init__(self, db, repos, ai, s, transport=None):
        self.db, self.repos, self.ai, self.s, self.transport = db, repos, ai, s, transport
        self.store = VoiceStore(db)
        self.jobs: dict[str, Job] = {}

    # ------------------------------------------------------------------ helpers
    async def audit(self, tenant_id: str, principal: str, action: str, target: str, details: dict, request_id: str) -> None:
        await self.repos.audit(tenant_id, principal, action, target, details, request_id)    # ids, counts and codes only

    def limit_error(self, which: str, message: str) -> RateLimited:
        return RateLimited(message, code="voice_limit", details={"limit": which})

    async def overview(self, tenant_id: str) -> dict:
        s = self.s
        spent = await self.store.spend_get(tenant_id)
        return {"items": [voice_out(d) for d in await self.store.list(tenant_id)],
                "limits": {"previews_per_hour": s.voice_studio_previews_per_hour, "uploads_per_day": s.voice_studio_uploads_per_day, "text_max_chars": s.voice_studio_text_max_chars,
                           "max_voices": s.voice_studio_max_voices, "max_running": s.voice_studio_max_running, "max_upload_bytes": s.voice_studio_max_upload_bytes,
                           "job_deadline_s": s.voice_studio_job_deadline_s},
                "usage": {"previews_last_hour": await self.store.usage(tenant_id, "preview", timedelta(hours=1)),
                          "uploads_last_day": await self.store.usage(tenant_id, "upload", timedelta(days=1)),
                          "spend_usd_this_month": round(spent / 1e6, 4), "spend_cap_usd": s.voice_studio_spend_cap_usd,
                          "spend_is_an_estimate": True},
                "decoder": clipmod.decoder_report(), "languages": list(PREVIEW_LANGUAGES), "consent_scopes": list(SCOPES)}

    # ------------------------------------------------------------------ upload
    async def upload(self, tenant_id: str, principal: str, request_id: str, *, data: bytes, name: str, speaker_name: str, consent: bool, scope: str) -> dict:
        s = self.s
        name = (name or "").strip()
        speaker_name = (speaker_name or "").strip()
        if not NAME_RE.fullmatch(name):
            raise Unprocessable("The name must be 1-40 letters, digits, '_' or '-'.", code="invalid_name")
        if not consent:
            raise Unprocessable("Consent is required: the speaker must have agreed to a cloned voice being made from this recording.", code="consent_required")
        if not (1 <= len(speaker_name) <= 80):
            raise Unprocessable("Please give the speaker's name (1-80 characters).", code="consent_required")
        if scope not in SCOPES:
            raise Unprocessable("consent_scope must be preview_only or preview_and_calls.", code="invalid_scope", details={"allowed": list(SCOPES)})
        if len(data) > s.voice_studio_max_upload_bytes:
            raise Unprocessable("The file is too large.", code="clip_too_large", details={"max_bytes": s.voice_studio_max_upload_bytes})
        if await self.store.count_active(tenant_id) >= s.voice_studio_max_voices:
            raise self.limit_error("max_voices", "This tenant already holds the maximum number of voices. Delete one first.")
        if not await self.store.claim(tenant_id, "upload", s.voice_studio_uploads_per_day, timedelta(days=1)):
            raise self.limit_error("uploads_per_day", "The daily upload limit has been reached. Try again tomorrow.")
        try:
            prepared = await asyncio.to_thread(clipmod.prepare_clip, data)
        except clipmod.ClipRejected as e:
            await self.audit(tenant_id, principal, "voice.upload", "-", {"ok": False, "code": e.code}, request_id)
            raise Unprocessable(e.message, code=e.code, details={"report": e.report}) from None
        consent_ref = {"speaker_name": speaker_name, "date": utcnow().date().isoformat(), "scope": scope}
        try:
            doc = await self.store.create(tenant_id, name, prepared.wav, prepared.report, consent_ref, principal)
        except DuplicateKeyError:
            raise Conflict("A voice with this name already exists.", code="voice_name_taken") from None
        await self.audit(tenant_id, principal, "voice.upload", doc["_id"], {"ok": True, "duration_s": prepared.report.get("duration_s"), "scope": scope}, request_id)
        log.info("voice uploaded tenant=%s voice=%s duration_s=%s", tenant_id, doc["_id"], prepared.report.get("duration_s"))
        return voice_out(doc)

    async def get_voice(self, tenant_id: str, voice_id: str) -> dict:
        doc = await self.store.get(tenant_id, voice_id)
        if doc is None:
            raise NotFound("voice not found", code="voice_not_found")           # another tenant's id looks exactly like an unknown one
        return doc

    async def disable(self, tenant_id: str, principal: str, request_id: str, voice_id: str) -> dict:
        await self.get_voice(tenant_id, voice_id)
        doc = await self.store.set_status(tenant_id, voice_id, "disabled", principal)
        await self.audit(tenant_id, principal, "voice.disable", voice_id, {}, request_id)
        return voice_out(doc)

    async def delete(self, tenant_id: str, principal: str, request_id: str, voice_id: str) -> dict:
        voice = await self.get_voice(tenant_id, voice_id)
        await self.store.set_status(tenant_id, voice_id, "deleted", principal)
        await self.store.erase_clip(voice)
        await self.audit(tenant_id, principal, "voice.delete", voice_id, {}, request_id)
        return {"id": voice_id, "status": "deleted"}

    # ------------------------------------------------------------------ preview
    def estimate_micro(self, chars: int) -> int:
        s = self.s
        gpu_s = s.voice_studio_est_cold_start_s + 5.0 + 0.1 * chars      # a placeholder model of the cost: cold start + fixed + per character (not measured)
        return micro_usd(gpu_s, s.voice_studio_gpu_usd_per_s)

    def running(self, tenant_id: str) -> int:
        return sum(1 for j in self.jobs.values() if j.tenant_id == tenant_id and j.finished is None)

    def _prune(self) -> None:
        now, ttl = time.monotonic(), self.s.voice_studio_result_ttl_s
        for j in self.jobs.values():
            if j.finished is not None and j.audio is not None and now - j.finished > ttl:
                j.audio = None                                              # the audio is dropped; the status stays readable (410 for the audio)
        done = sorted((j for j in self.jobs.values() if j.finished is not None), key=lambda j: j.finished)
        for j in done[:max(0, len(done) - MAX_KEPT_JOBS)]:
            self.jobs.pop(j.id, None)

    async def start_preview(self, tenant_id: str, principal: str, request_id: str, voice_id: str, *, text: str, language: str, phone_quality: bool) -> dict:
        s = self.s
        voice = await self.get_voice(tenant_id, voice_id)
        if voice["status"] != "active":
            raise Conflict("This voice is disabled.", code="voice_not_active")
        ref = voice.get("consent_ref") or {}
        if not ref.get("speaker_name") or not ref.get("date") or not ref.get("scope"):
            raise Conflict("This voice has no consent record, so it cannot be used.", code="consent_required")
        text = (text or "").strip()
        if not text:
            raise Unprocessable("Please type some text.", code="text_required")
        if len(text) > s.voice_studio_text_max_chars:
            raise Unprocessable(f"The text is longer than {s.voice_studio_text_max_chars} characters.", code="text_too_long", details={"max_chars": s.voice_studio_text_max_chars})
        if language not in PREVIEW_LANGUAGES:
            raise Unprocessable("Unsupported language.", code="invalid_language", details={"supported": list(PREVIEW_LANGUAGES)})
        cfg = await self.ai.get_config(tenant_id)
        try:
            check_configured(cfg)
        except WorkerError as e:
            raise Conflict(e.message, code=e.code) from None
        self._prune()
        if self.running(tenant_id) >= s.voice_studio_max_running:
            raise self.limit_error("max_running", "Too many previews are running at once. Wait for one to finish.")
        if not await self.store.claim(tenant_id, "preview", s.voice_studio_previews_per_hour, timedelta(hours=1)):
            raise self.limit_error("previews_per_hour", "The hourly preview limit has been reached.")
        reserved = self.estimate_micro(len(text))
        period = await self.store.spend_reserve(tenant_id, reserved, int(s.voice_studio_spend_cap_usd * 1e6))
        if period is None:
            raise RateLimited("The monthly preview spend cap has been reached. Nothing was sent to the voice server.", code="spend_cap_reached",
                              details={"cap_usd": s.voice_studio_spend_cap_usd})
        job = Job(new_uuid().replace("-", "")[:24], tenant_id, voice_id, principal, bool(phone_quality), len(text))
        self.jobs[job.id] = job
        job.task = asyncio.create_task(self._run_job(job, voice, cfg, text, language, reserved, period))
        await self.audit(tenant_id, principal, "voice.preview", voice_id, {"job_id": job.id, "chars": len(text), "language": language, "phone_quality": bool(phone_quality)}, request_id)
        return job_out(job)

    async def _run_job(self, job: Job, voice: dict, cfg: dict, text: str, language: str, reserved_micro: int, period: str) -> None:
        s = self.s
        worker = VoiceWorker(s, cfg, self.transport)
        speaking = {"running": "speaking", "warming": "warming"}
        try:
            async with asyncio.timeout(s.voice_studio_job_deadline_s):
                clip = await self.store.read_clip(voice)
                inp = worker_input(text, language, s.ai_tts_normalize, voice["worker_name"], job.phone_quality)

                def on_state_synth(st: str) -> None:
                    job.set(speaking[st])

                res = await worker.run(inp, on_state_synth, s.voice_studio_poll_s)
                if res.output.get("code") == "needs_reference":
                    job.set("warming", "Preparing this voice on the voice server. This can take about a minute.")
                    reg = await worker.run({"task": "register_voice", "name": voice["worker_name"], "reference_audio_base64": base64.b64encode(clip).decode("ascii")},
                                           lambda st: job.set("warming", "Preparing this voice on the voice server. This can take about a minute."), s.voice_studio_poll_s)
                    if reg.output.get("error"):
                        raise worker_failure(reg.output)
                    res = await worker.run(inp, on_state_synth, s.voice_studio_poll_s)         # the clip is sent ONCE: a second needs_reference ends the job
                if res.output.get("error"):
                    raise worker_failure(res.output)
                wav, seconds = decode_worker_audio(res.output)
            job.succeed(wav, round(seconds, 2))
        except TimeoutError:
            job.fail("deadline_exceeded", "The voice server did not answer in time. Try again in a minute; the first request after a pause is the slowest.")
        except WorkerError as e:
            job.fail(e.code, e.message)
        except asyncio.CancelledError:
            job.fail("cancelled", "The preview was cancelled.")
            raise
        except Exception as e:                                    # noqa: BLE001: a job must always end in done or error
            log.error("voice preview job=%s failed: %s", job.id, type(e).__name__)
            job.fail("internal_error", "The preview failed unexpectedly.")
        finally:
            try:
                await worker.aclose()
                if worker.submitted == 0:
                    delta = -reserved_micro                                # nothing reached the GPU: refund the whole reservation
                elif worker.billable_known and (job.status == "done" or job.error_code in WORKER_ANSWERED):
                    delta = micro_usd(worker.billable_s, s.voice_studio_gpu_usd_per_s) - reserved_micro   # the measured cost replaces the estimate
                else:
                    delta = 0                                              # the worker did not report its time (or the job was cut at the deadline): the estimate stands
                await self.store.spend_adjust(job.tenant_id, period, delta)
            except Exception as e:                                # noqa: BLE001
                log.error("voice preview job=%s cleanup failed: %s", job.id, type(e).__name__)
            log.info("voice preview job=%s tenant=%s status=%s code=%s chars=%d", job.id, job.tenant_id, job.status, job.error_code, job.chars)

    def get_job(self, tenant_id: str, job_id: str) -> Job:
        self._prune()
        job = self.jobs.get(job_id)
        if job is None or job.tenant_id != tenant_id:
            raise NotFound("preview not found", code="preview_not_found")
        return job

    async def shutdown(self) -> None:
        tasks = [j.task for j in self.jobs.values() if j.task and not j.task.done()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
