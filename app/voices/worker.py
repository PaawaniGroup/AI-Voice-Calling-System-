"""Client of the RunPod TTS worker for Voice Studio: one RunPod job at a time, with its progress reported (queued = cold start / waiting for a worker, running).

It reads the tenant's `tts_url` and key reference exactly like the call path (through `ModelClients.endpoint` / `resolve_key`) but does NOT use the call path's `synthesize()`:
previews need the worker's job status (the dashboard shows "warming up") and the voice tasks `register_voice` and `voices`. Error messages never carry a response body, a key, text or audio.
UNVERIFIED against a real RunPod endpoint (mocked in tests)."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable

import httpx

from app.ai.clients import AiError, AiUnavailable, ModelClients
from config.settings import Settings

log = logging.getLogger("mme_telephony.voices")


class WorkerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(code)
        self.code, self.message = code, message


def check_configured(cfg: dict) -> None:
    """Raise WorkerError unless the tenant's TTS URL is a RunPod `/runsync` URL (the only worker Voice Studio supports). Makes no request and opens nothing."""
    url = (cfg.get("tts_url") or "").rstrip("/")
    if not url:
        raise WorkerError("tts_not_configured", "No voice endpoint is configured for this tenant (AI configuration, TTS URL).")
    if not url.endswith("/runsync"):
        raise WorkerError("tts_not_runpod", "Voice Studio needs the RunPod voice worker (a TTS URL ending in /runsync).")


@dataclass
class JobResult:
    output: dict
    billable_s: float | None      # RunPod delayTime + executionTime (seconds); None when RunPod did not report them


class VoiceWorker:
    def __init__(self, s: Settings, cfg: dict, transport: httpx.AsyncBaseTransport | None = None):
        self.s, self.mc = s, ModelClients(s, cfg, transport)    # ModelClients: URL and key resolution only (its own HTTP client is never used here)
        self._http = httpx.AsyncClient(transport=transport, follow_redirects=False, timeout=httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0))
        self._bg: set[asyncio.Task] = set()
        self.billable_s = 0.0
        self.billable_known = True
        self.submitted = 0                   # RunPod jobs this client started (0 = nothing reached the GPU: the reserved cost is refunded)

    def base_url(self) -> str:
        try:
            ep = self.mc.endpoint("tts")
        except (AiUnavailable, AiError):
            raise WorkerError("tts_not_configured", "No voice endpoint is configured for this tenant (AI configuration, TTS URL).") from None
        if ep.path != "/runsync":
            raise WorkerError("tts_not_runpod", "Voice Studio needs the RunPod voice worker (a TTS URL ending in /runsync).")
        return ep.url

    def _headers(self) -> dict:
        try:
            key = self.mc.resolve_key("tts")
        except (AiUnavailable, AiError):
            raise WorkerError("tts_not_configured", "The key of the voice endpoint is not available on this server.") from None
        return {"Authorization": f"Bearer {key}"} if key else {}

    async def aclose(self) -> None:
        if self._bg:                         # a cancelled job must not be left billing: let the cancel calls finish
            await asyncio.wait(set(self._bg), timeout=3)
        await self._http.aclose()
        await self.mc.aclose()

    async def _cancel(self, base: str, hdrs: dict, job_id: str) -> None:
        try:
            await self._http.post(f"{base}/cancel/{job_id}", headers=hdrs, timeout=5)
        except Exception as e:               # best effort: reported, never raised
            log.warning("voice worker job could not be cancelled: %s", type(e).__name__)

    async def run(self, input_: dict, on_state: Callable[[str], None] | None = None, poll_s: float = 1.0) -> JobResult:
        """Submit one job (`/run`) and poll `/status` until it ends. `on_state` gets "warming" while RunPod says IN_QUEUE and "running" while it says IN_PROGRESS.
        If this task is cancelled (deadline), the RunPod job is cancelled too."""
        base, hdrs = self.base_url(), self._headers()
        notify = on_state or (lambda _s: None)
        try:
            r = await self._http.post(f"{base}/run", json={"input": input_}, headers=hdrs)
        except httpx.HTTPError as e:
            raise WorkerError("worker_unreachable", f"The voice worker could not be reached ({type(e).__name__}).") from None
        if r.status_code >= 400:
            raise WorkerError("worker_http_error", f"The voice worker answered with HTTP {r.status_code}.")
        self.submitted += 1
        j = _json(r)
        jid = j.get("id")
        try:
            while True:
                status = j.get("status")
                if status == "COMPLETED":
                    break
                if status in ("FAILED", "CANCELLED", "TIMED_OUT"):
                    raise WorkerError("worker_failed", f"The voice worker job ended with status {status}.")
                notify("warming" if status == "IN_QUEUE" else "running")
                if not jid:
                    raise WorkerError("worker_bad_response", "The voice worker did not return a job id.")
                await asyncio.sleep(poll_s)
                try:
                    sr = await self._http.get(f"{base}/status/{jid}", headers=hdrs)
                except httpx.HTTPError as e:
                    raise WorkerError("worker_unreachable", f"The voice worker could not be reached ({type(e).__name__}).") from None
                if sr.status_code >= 400:
                    raise WorkerError("worker_http_error", f"The voice worker answered with HTTP {sr.status_code}.")
                j = _json(sr)
        except asyncio.CancelledError:
            if jid:
                t = asyncio.ensure_future(self._cancel(base, hdrs, jid))
                self._bg.add(t)
                t.add_done_callback(self._bg.discard)
            raise
        out = j.get("output")
        if not isinstance(out, dict):
            raise WorkerError("worker_bad_response", "The voice worker returned no result.")
        try:
            billable = (float(j.get("delayTime") or 0) + float(j.get("executionTime") or 0)) / 1000.0 if ("delayTime" in j or "executionTime" in j) else None
        except (TypeError, ValueError):
            billable = None
        if billable is None:
            self.billable_known = False
        else:
            self.billable_s += billable
        return JobResult(out, billable)


def _json(r: httpx.Response) -> dict:
    try:
        j = r.json()
    except ValueError:
        raise WorkerError("worker_bad_response", "The voice worker returned something that is not JSON.") from None
    if not isinstance(j, dict):
        raise WorkerError("worker_bad_response", "The voice worker returned an unexpected answer.")
    return j
