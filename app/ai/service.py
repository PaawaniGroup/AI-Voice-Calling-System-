"""AI session lifecycle, per-tenant model configuration, health and runtime registry.

AI is OFF by default. `start()` refuses unless every precondition is true, in this order:
global AI_ENABLED -> tenant ai_config.enabled -> session open -> call answered/bridged -> media bridge ready.
The AI session only becomes `active` when a media client actually connects and says hello; until then it is
`starting`. Nothing is reported as streaming that is not.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.ai.clients import SERVICES, ModelClients, validate_service_url
from app.ai.media import MediaBridge, UnavailableBridge
from app.ai.pipeline import Conversation
from app.core.errors import Conflict, NotFound, Unprocessable
from app.core.ids import new_uuid, utcnow
from app.observability import metrics as m
from config.settings import Settings

log = logging.getLogger("mme_telephony.ai")

AI_TERMINAL = {"stopped", "failed"}
LANGS = ["auto", "hi", "en", "hinglish"]


@dataclass
class Runtime:
    session_id: str
    tenant_id: str
    conversation: Conversation
    clients: ModelClients
    ws: Any = None
    watchdog: asyncio.Task | None = None
    tasks: list = field(default_factory=list)


class AiService:
    def __init__(self, db, repos, s: Settings, sessions, emitter=None, bridge: MediaBridge | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.db, self.repos, self.s, self.sessions, self.emitter = db, repos, s, sessions, emitter
        self.bridge: MediaBridge = bridge or UnavailableBridge()
        self.transport = transport  # tests inject a mock transport (mocks are only ever used in tests)
        self.runtime: dict[str, Runtime] = {}
        self.last_health: dict | None = None
        self.connect_s = 30.0
        self.reconnect_s = 30.0

    # ------------------------------------------------------------------ configuration
    def default_config(self, tenant_id: str) -> dict:
        return {"tenant_id": tenant_id, "enabled": False, "stt_url": "", "brain_url": "", "tts_url": "", "rag_url": "",
                "models": {"stt": self.s.ai_stt_model, "brain": self.s.ai_brain_model, "tts": self.s.ai_tts_model},
                "timeouts": {"stt": self.s.ai_request_timeout_s, "brain": self.s.ai_request_timeout_s * 2,
                             "tts": self.s.ai_request_timeout_s, "rag": 5.0},
                "paths": {}, "languages": LANGS[1:], "default_language": "auto"}

    async def get_config(self, tenant_id: str) -> dict:
        doc = await self.db["ai_config"].find_one({"tenant_id": tenant_id})
        cfg = self.default_config(tenant_id)
        if doc:
            for k in ("enabled", "stt_url", "brain_url", "tts_url", "rag_url", "default_language", "updated_at", "updated_by"):
                if k in doc:
                    cfg[k] = doc[k]
            for k in ("models", "timeouts", "paths"):
                cfg[k] = {**cfg[k], **doc.get(k, {})}
            cfg["languages"] = doc.get("languages", cfg["languages"])
        return cfg

    async def put_config(self, tenant_id: str, body: dict, principal: str) -> dict:
        cfg = await self.get_config(tenant_id)
        for svc in SERVICES:
            key = f"{svc}_url"
            if key in body:
                v = (body[key] or "").strip()
                cfg[key] = validate_service_url(v, self.s) if v else ""
        if "enabled" in body:
            cfg["enabled"] = bool(body["enabled"])
        if "default_language" in body:
            if body["default_language"] not in LANGS:
                raise Unprocessable("unsupported language", code="invalid_language", details={"supported": LANGS})
            cfg["default_language"] = body["default_language"]
        if "languages" in body:
            if not set(body["languages"]) <= set(LANGS[1:]):
                raise Unprocessable("unsupported language list", code="invalid_language", details={"supported": LANGS[1:]})
            cfg["languages"] = list(body["languages"])
        for k in ("models", "timeouts"):
            if k in body:
                for name, val in body[k].items():
                    if name not in ("stt", "brain", "tts", "rag"):
                        raise Unprocessable(f"unknown {k} key", code="invalid_config")
                    if k == "timeouts" and not (0.5 <= float(val) <= 120):
                        raise Unprocessable("timeout must be between 0.5 and 120 seconds", code="invalid_timeout")
                    if k == "models" and (not isinstance(val, str) or len(val) > 200):
                        raise Unprocessable("invalid model identifier", code="invalid_config")
                    cfg[k][name] = float(val) if k == "timeouts" else val
        if "paths" in body:
            for name, val in body["paths"].items():
                if name not in SERVICES or not isinstance(val, str) or not val.startswith("/") or "//" in val or ".." in val or len(val) > 100:
                    raise Unprocessable("invalid service path", code="invalid_config")
                cfg["paths"][name] = val
        cfg["updated_at"], cfg["updated_by"] = utcnow(), principal
        await self.db["ai_config"].update_one({"tenant_id": tenant_id}, {"$set": cfg}, upsert=True)
        return cfg

    def public_config(self, cfg: dict, detail: bool) -> dict:
        """Never includes secrets (none are stored). Viewers only learn whether each service is configured."""
        out = {k: v for k, v in cfg.items() if k not in ("_id",)}
        if not detail:
            for svc in SERVICES:
                out[f"{svc}_url"] = "configured" if cfg.get(f"{svc}_url") else ""
        return out

    def _clients(self, cfg: dict) -> ModelClients:
        return ModelClients(self.s, cfg, self.transport)

    # ------------------------------------------------------------------ health
    async def health(self, tenant_id: str) -> dict:
        cfg = await self.get_config(tenant_id)
        c = self._clients(cfg)
        try:
            res = await asyncio.gather(*(c.health(svc) for svc in SERVICES))
        finally:
            await c.aclose()
        ready, reason = await self.bridge.ready()
        out = {"checked_at": utcnow().isoformat(), "ai_enabled_global": self.s.ai_enabled, "ai_enabled_tenant": cfg["enabled"],
               "services": {r["service"]: {k: v for k, v in r.items() if k != "service"} for r in res},
               "media_bridge": {"name": self.bridge.name, "ready": ready, "reason": reason},
               "models": cfg["models"]}
        self.last_health = out
        return out

    # ------------------------------------------------------------------ lifecycle
    async def _get_ai(self, tenant_id: str, session_id: str) -> dict | None:
        return await self.db["ai_sessions"].find_one({"session_id": session_id, "tenant_id": tenant_id})

    async def start(self, tenant_id: str, session_id: str, language: str | None = None) -> dict:
        sess = await self.sessions.get(tenant_id, session_id)  # 404 first: other tenants learn nothing from later checks
        if not self.s.ai_enabled:
            raise Conflict("AI is disabled globally (MME_TEL_AI_ENABLED=false)", code="ai_disabled")
        cfg = await self.get_config(tenant_id)
        if not cfg["enabled"]:
            raise Conflict("AI is disabled for this tenant", code="ai_disabled_for_tenant")
        if sess["status"] not in ("answered", "active"):
            raise Conflict(f"session is {sess['status']}; AI can start only on an answered session", code="session_not_answered")
        call = await self.repos.get_call(sess["call_id"], tenant_id) if sess.get("call_id") else None
        if call is None or call["state"] not in ("answered", "bridged"):
            raise Conflict("the call is not answered", code="call_not_answered")
        if not call.get("ai_enabled"):
            raise Conflict("this call was not flagged ai_enabled", code="call_not_ai_enabled")
        ready, reason = await self.bridge.ready()
        if not ready:
            raise Conflict(f"media bridge unavailable: {reason}", code="media_bridge_unavailable", details={"reason": reason})
        if not (cfg["stt_url"] and cfg["brain_url"] and cfg["tts_url"]):
            raise Conflict("STT, Brain and TTS URLs must be configured", code="ai_not_configured")
        lang = language or sess.get("language") or cfg["default_language"]
        if lang not in LANGS:
            raise Unprocessable("unsupported language", code="invalid_language", details={"supported": LANGS})
        now = utcnow()
        doc = {"_id": new_uuid(), "session_id": session_id, "tenant_id": tenant_id, "call_id": sess["call_id"], "state": "starting",
               "language": lang, "created_at": now, "started_at": None, "stopped_at": None, "stop_reason": None, "error": None,
               "models": cfg["models"], "turns": 0, "timings_ms": {}, "media": {"connected": False, "last_frame_at": None}}
        try:
            await self.db["ai_sessions"].insert_one(doc)
        except DuplicateKeyError:
            existing = await self._get_ai(tenant_id, session_id)
            if existing["state"] in AI_TERMINAL:  # allow a fresh AI session after a previous one ended
                await self.db["ai_sessions"].delete_one({"_id": existing["_id"]})
                await self.db["ai_sessions"].insert_one(doc)
            else:
                return existing
        clients = self._clients(cfg)
        rt = Runtime(session_id, tenant_id, Conversation(clients, lang), clients)
        self.runtime[session_id] = rt
        rt.watchdog = asyncio.create_task(self._media_deadline(rt, self.connect_s, "media_connect_timeout"))
        m.AI_SESSIONS.labels("starting").inc()
        return doc

    async def _media_deadline(self, rt: Runtime, seconds: float, reason: str) -> None:
        try:
            await asyncio.sleep(seconds)
            await self._finish(rt.tenant_id, rt.session_id, "failed", reason, error=reason)
        except asyncio.CancelledError:
            pass

    async def attach_media(self, tenant_id: str, session_id: str) -> Runtime:
        ai = await self._get_ai(tenant_id, session_id)
        if not ai or ai["state"] not in ("starting", "media_disconnected", "active"):
            raise NotFound("no AI session awaiting media")
        rt = self.runtime.get(session_id)
        if rt is None:
            raise NotFound("AI session runtime not found (service restarted?)")
        if rt.watchdog:
            rt.watchdog.cancel()
        return rt

    async def media_connected(self, rt: Runtime, hello: dict) -> None:
        now = utcnow()
        await self.db["ai_sessions"].update_one(
            {"session_id": rt.session_id, "tenant_id": rt.tenant_id, "state": {"$in": ["starting", "media_disconnected", "active"]}},
            {"$set": {"state": "active", "started_at": now, "media": {"connected": True, "hello": hello, "last_frame_at": None}}})
        sess = await self.db["sessions"].find_one({"_id": rt.session_id})
        if sess:
            await self.sessions.transition(sess, "active", source="ai")
        m.AI_SESSIONS.labels("active").inc()
        await self._emit(rt.tenant_id, "ai.session.started", rt.session_id)

    async def media_disconnected(self, rt: Runtime) -> None:
        r = await self.db["ai_sessions"].update_one(
            {"session_id": rt.session_id, "tenant_id": rt.tenant_id, "state": "active"},
            {"$set": {"state": "media_disconnected", "media.connected": False}})
        if r.modified_count:
            rt.watchdog = asyncio.create_task(self._media_deadline(rt, self.reconnect_s, "media_reconnect_timeout"))

    async def record_turn(self, rt: Runtime, timings: dict) -> None:
        await self.db["ai_sessions"].update_one(
            {"session_id": rt.session_id, "tenant_id": rt.tenant_id},
            {"$inc": {"turns": 1}, "$set": {"last_turn_at": utcnow(), **{f"timings_ms.{k}": v for k, v in timings.items()}}})

    async def stop(self, tenant_id: str, session_id: str, reason: str = "requested") -> dict:
        ai = await self._get_ai(tenant_id, session_id)
        if ai is None:
            raise NotFound("no AI session for this session")
        if ai["state"] in AI_TERMINAL:
            return ai
        return await self._finish(tenant_id, session_id, "stopped", reason)

    async def _finish(self, tenant_id: str, session_id: str, state: str, reason: str, error: str | None = None) -> dict:
        upd = await self.db["ai_sessions"].find_one_and_update(
            {"session_id": session_id, "tenant_id": tenant_id, "state": {"$nin": list(AI_TERMINAL)}},
            {"$set": {"state": state, "stopped_at": utcnow(), "stop_reason": reason, "error": error, "media.connected": False}},
            return_document=ReturnDocument.AFTER)
        rt = self.runtime.pop(session_id, None)
        if rt:  # cancel inference + playback, release resources
            rt.conversation.cancel()
            if rt.watchdog:
                rt.watchdog.cancel()
            for t in rt.tasks:
                t.cancel()
            if rt.ws is not None:
                try:
                    await rt.ws.close(code=1000)
                except Exception:
                    pass
            await rt.clients.aclose()
        if upd is None:
            return await self._get_ai(tenant_id, session_id)
        m.AI_SESSIONS.labels(state).inc()
        sess = await self.db["sessions"].find_one({"_id": session_id})
        if sess and sess["status"] == "active":
            await self.sessions.transition(sess, "answered", source="ai")
        await self._emit(tenant_id, "ai.session.failed" if state == "failed" else "ai.session.stopped", session_id, reason)
        return upd

    async def get(self, tenant_id: str, session_id: str, detail: bool) -> dict:
        await self.sessions.get(tenant_id, session_id)  # 404 for other tenants / unknown ids
        ai = await self._get_ai(tenant_id, session_id)
        ready, reason = await self.bridge.ready()
        cfg = await self.get_config(tenant_id)
        out: dict[str, Any] = {"session_id": session_id, "state": ai["state"] if ai else "not_started",
                               "language": ai["language"] if ai else None, "turns": ai["turns"] if ai else 0,
                               "timings_ms": ai.get("timings_ms") if ai else {}, "error": ai.get("error") if ai else None,
                               "stop_reason": ai.get("stop_reason") if ai else None,
                               "media": {"connected": bool(ai and ai["media"]["connected"]), "bridge": self.bridge.name,
                                         "bridge_ready": ready, "bridge_reason": reason},
                               "models": cfg["models"], "ai_enabled_global": self.s.ai_enabled, "ai_enabled_tenant": cfg["enabled"]}
        if detail:
            out["endpoints"] = {f"{svc}_url": cfg[f"{svc}_url"] for svc in SERVICES}
        return out

    async def on_call_ended(self, call: dict) -> None:
        """Hangup cancels pending inference and playback immediately. Never raises."""
        try:
            sid = call.get("session_id")
            if not sid:
                return
            ai = await self._get_ai(call["tenant_id"], sid)
            if ai and ai["state"] not in AI_TERMINAL:
                await self._finish(call["tenant_id"], sid, "stopped", "call_ended")
        except Exception:
            log.exception("AI cleanup on call end failed")

    async def _emit(self, tenant_id: str, etype: str, session_id: str, reason: str | None = None) -> None:
        if self.emitter:
            await self.emitter.emit_generic(tenant_id, etype, f"{session_id}:{etype}:{hashlib.sha1(str(utcnow()).encode()).hexdigest()[:8]}",
                                            {"session_id": session_id, "reason": reason})

    async def shutdown(self) -> None:
        for sid, rt in list(self.runtime.items()):
            rt.conversation.cancel()
            for t in rt.tasks:
                t.cancel()
            if rt.watchdog:
                rt.watchdog.cancel()
            await rt.clients.aclose()
        self.runtime.clear()
