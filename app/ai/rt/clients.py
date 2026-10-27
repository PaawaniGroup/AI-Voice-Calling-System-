"""RtModelClients: ModelClients whose voice and STT can use the persistent WebSocket path and whose Brain can use an always-on OpenAI-compatible server,
with the legacy queue path (/run + poll) as the fallback for every request.

Tenant config (all default OFF = the plain ModelClients is used):
  rt_transport  {"tts": "persistent", "stt": "persistent", "brain": "persistent"}   (a service that is absent or "queue" stays on the queue path)
  rt_endpoints  {"tts": "wss://host/ws", "stt": "wss://host/ws", "brain": "https://host"}   (the Brain URL is the OpenAI server root; its /v1/chat/completions streams as today)
  rt_secret_env the NAME of the environment variable that holds the signing secret (default RT_TRANSPORT_SECRET); the value is never printed or stored
Switches: the tenant keys above, the setting `ai_rt_kill` (MME_TEL_AI_RT_KILL=1 turns every persistent path off for every tenant at once), `rt_kill()` per call, and the circuit
breaker (3 failures in 30 s open it for 10 s; one probe then decides). A request that fails on the persistent path is answered by the queue path, once, unless audio of that reply
has already been produced (a half-spoken answer is never replayed).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from app.ai import codec, tts_frontend, turn_trace
from app.ai.clients import AiError, ModelClients, ServiceEndpoint
from app.ai.rt import protocol as P
from app.ai.rt.connection import Connector, PersistentConnection, RtConfig, RtError, websockets_connector
from app.ai.rt.resilience import ReadinessTracker, breaker_for

log = logging.getLogger("mme_telephony.ai.rt")
RT_SERVICES = ("stt", "tts", "brain")


class RtModelClients(ModelClients):
    def __init__(self, s, cfg: dict, transport=None, *, connector: Connector | None = None, rt_cfg: RtConfig | None = None, resolvers: dict | None = None):
        super().__init__(s, cfg, transport)
        self._resolvers = resolvers or {}                                 # per service: async () -> current wss:// URL (Pod port/IP rediscovery)
        rt = cfg.get("rt_transport") or {}
        self._rt = {svc: rt.get(svc) == "persistent" for svc in RT_SERVICES}
        self._rt_urls: dict = cfg.get("rt_endpoints") or {}
        self._connector = connector or websockets_connector
        self._rt_cfg = rt_cfg or RtConfig()
        self._conns: dict[str, PersistentConnection] = {}
        self._brain_rt = False
        self._killed = False
        self.call_id = "unassigned"
        self.readiness = ReadinessTracker()
        self.rt_counters = {"rt_ok": 0, "rt_fallback": 0, "rt_breaker_open": 0}

    # ------------------------------------------------------------------ switches
    def set_call(self, call_id: str) -> None:
        self.call_id = str(call_id)

    def rt_kill(self) -> None:
        """Kill switch of this call: every later request goes to the queue path."""
        self._killed = True

    def _wanted(self, svc: str) -> bool:
        return bool(self._rt.get(svc) and self._rt_urls.get(svc) and not self._killed and not getattr(self.s, "ai_rt_kill", False))

    def _breaker(self, svc: str):
        return breaker_for(f"{svc}:{self._rt_urls.get(svc)}")

    def _secret(self) -> bytes | None:
        v = os.environ.get(self.cfg.get("rt_secret_env") or "RT_TRANSPORT_SECRET")
        return v.strip().encode("utf-8") if v and v.strip() else None

    def _conn(self, svc: str) -> PersistentConnection:
        if svc not in self._conns:
            self._conns[svc] = PersistentConnection(svc, self._rt_urls[svc], self.call_id, self._secret, self._connector, self._rt_cfg, resolver=self._resolvers.get(svc))
        return self._conns[svc]

    async def aclose(self) -> None:
        for c in self._conns.values():
            await c.close()
        await super().aclose()

    async def rt_warmup(self, svc: str) -> bool:
        """A `warmup` request (no model work): its completion, and its first-byte time, is the readiness signal. Never raises."""
        if not self._wanted(svc) or svc == "brain":
            return False
        t0 = time.monotonic()
        try:
            async for kind, _ in self._conn(svc).request({"type": "warmup"}):
                pass
            self.readiness.record(svc, True, (time.monotonic() - t0) * 1000)
            return True
        except (RtError, asyncio.TimeoutError):
            self.readiness.record(svc, False, None)
            return False

    # ------------------------------------------------------------------ voice
    async def synthesize(self, text: str, language: str):
        if self._wanted("tts"):
            br = self._breaker("tts")
            if br.allow():
                job = {"service": "tts", "transport": "rt", "words": len(text.split()), "chars": len(text), "status": "open", "t_submit": time.monotonic(), "polls": 0}
                turn_trace.note_job(job)
                try:
                    out = await self._rt_synthesize(text, language, job)
                    br.record(True)
                    self.rt_counters["rt_ok"] += 1
                    return out
                except asyncio.CancelledError:
                    job["status"] = "cancelled"
                    br.abandon()
                    raise
                except RtError as e:
                    br.record(False)
                    self.readiness.record("tts", False, None)
                    self.rt_counters["rt_fallback"] += 1
                    job["status"] = f"error:rt_{e.kind}"
                    log.warning("rt tts failed (%s): falling back to the queue path", e.kind)
            else:
                self.rt_counters["rt_breaker_open"] += 1
        return await super().synthesize(text, language)

    async def _rt_synthesize(self, text: str, language: str, job: dict):
        spoken, lang = text, language                                     # the same text preparation as the queue path (ModelClients.synthesize)
        if self.s.ai_tts_normalize:
            if language in ("", "auto", "hi", "en", "hinglish"):
                lang = tts_frontend.detect_lang(text)
            spoken = tts_frontend.normalize(text, lang)
        elif language == "hinglish":
            lang = "hi"
        if len(spoken) > P.MAX_TEXT_CHARS:
            raise RtError("server_error", "text_too_long")
        msg = {"type": "synth", "text": spoken, "voice": self.cfg.get("tts_voice") or "utkarsh", "format": "ulaw8k", "lang": lang}
        job["spoken_chars"] = len(spoken)
        t0, first, chunks, start, end = time.monotonic(), None, [], {}, {}
        async for kind, d in self._conn("tts").request(msg):
            if kind == "audio_start":
                start = d
            elif kind == "frame":
                if first is None:
                    first = time.monotonic()
                chunks.append(d)
            elif kind == "audio_end":
                end = d
        raw = b"".join(chunks)
        if not raw:
            raise RtError("server_error", "empty_audio")
        total_ms = round((time.monotonic() - t0) * 1000, 1)
        fb_ms = round((first - t0) * 1000, 1) if first is not None else None
        self.readiness.record("tts", True, fb_ms)
        queue_ms, exec_ms = start.get("queue_ms"), end.get("exec_ms")
        job.update(status="ok", first_byte_ms=fb_ms, total_ms=total_ms, delay_ms=queue_ms, exec_ms=exec_ms, audio_s=round(len(raw) / P.ULAW_RATE, 3),
                   remainder_ms=round(total_ms - (queue_ms or 0) - (exec_ms or 0), 1) if isinstance(queue_ms, (int, float)) and isinstance(exec_ms, (int, float)) else None)
        return codec.mulaw_to_pcm16(raw), codec.MULAW_RATE

    # ------------------------------------------------------------------ STT
    async def transcribe(self, pcm: bytes, rate: int, language: str) -> dict:
        if self._wanted("stt"):
            br = self._breaker("stt")
            if br.allow():
                job = {"service": "stt", "transport": "rt", "audio_pcm_bytes": len(pcm), "sample_rate": rate, "status": "open", "t_submit": time.monotonic(), "polls": 0}
                turn_trace.note_job(job)
                try:
                    out = await self._rt_transcribe(pcm, rate, language, job)
                    br.record(True)
                    self.rt_counters["rt_ok"] += 1
                    return out
                except asyncio.CancelledError:
                    job["status"] = "cancelled"
                    br.abandon()
                    raise
                except RtError as e:
                    br.record(False)
                    self.readiness.record("stt", False, None)
                    self.rt_counters["rt_fallback"] += 1
                    job["status"] = f"error:rt_{e.kind}"
                    log.warning("rt stt failed (%s): falling back to the queue path", e.kind)
            else:
                self.rt_counters["rt_breaker_open"] += 1
        return await super().transcribe(pcm, rate, language)

    async def _rt_transcribe(self, pcm: bytes, rate: int, language: str, job: dict) -> dict:
        if len(pcm) > P.MAX_AUDIO_BYTES:
            raise RtError("server_error", "audio_too_long")
        lang = {"hinglish": "hi"}.get(language, language) if language and language != "auto" else None
        frames = [pcm[i:i + P.PCM_FRAME_BYTES] for i in range(0, len(pcm), P.PCM_FRAME_BYTES)]
        t0, final = time.monotonic(), None
        async for kind, d in self._conn("stt").request({"type": "transcribe", "rate": rate, "lang": lang, "nbytes": len(pcm)}, frames=frames, trailer={"type": "end"}):
            if kind == "final":
                final = d
        if final is None or not isinstance(final.get("text"), str):
            raise RtError("server_error", "no_final")
        total_ms = round((time.monotonic() - t0) * 1000, 1)
        self.readiness.record("stt", True, total_ms)
        q, e = final.get("queue_ms"), final.get("exec_ms")
        job.update(status="ok", total_ms=total_ms, delay_ms=q, exec_ms=e,
                   remainder_ms=round(total_ms - (q or 0) - (e or 0), 1) if isinstance(q, (int, float)) and isinstance(e, (int, float)) else None)
        return {"text": final["text"].strip(), "language": final.get("language") or language}

    # ------------------------------------------------------------------ Brain: an always-on OpenAI server instead of the serverless endpoint
    def endpoint(self, service: str) -> ServiceEndpoint:
        if service == "brain" and self._brain_rt:
            t = (self.cfg.get("timeouts") or {}).get("brain", self.s.ai_request_timeout_s)
            return ServiceEndpoint(str(self._rt_urls["brain"]).rstrip("/"), "/v1/chat/completions", float(t))
        return super().endpoint(service)

    async def chat_stream(self, messages: list[dict], tools: list[dict] | None = None, temperature: float | None = None):
        if not self._wanted("brain") or not self._breaker("brain").allow():
            if self._wanted("brain"):
                self.rt_counters["rt_breaker_open"] += 1
            async for item in super().chat_stream(messages, tools, temperature):
                yield item
            return
        br, yielded = self._breaker("brain"), False
        self._brain_rt = True
        try:
            async for item in super().chat_stream(messages, tools, temperature):
                yielded = True
                yield item
            br.record(True)
            self.rt_counters["rt_ok"] += 1
            return
        except asyncio.CancelledError:
            br.abandon()
            raise
        except Exception as e:
            br.record(False)
            if yielded:
                raise                                                     # a half-spoken answer is never replayed on the other path
            self.rt_counters["rt_fallback"] += 1
            log.warning("rt brain failed (%s): falling back to the queue path", type(e).__name__)
        finally:
            self._brain_rt = False
        async for item in super().chat_stream(messages, tools, temperature):
            yield item
