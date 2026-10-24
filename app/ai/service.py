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
    fs_uuid: str | None = None
    sent_at: float | None = None  # monotonic time the last reply was handed to the media module
    watchdog: asyncio.Task | None = None
    tasks: list = field(default_factory=list)
    call_id: str | None = None


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
        self.activity = None  # ActivityBus (live dashboard feed), set by the app factory

    # ------------------------------------------------------------------ configuration
    def default_config(self, tenant_id: str) -> dict:
        return {"tenant_id": tenant_id, "enabled": False, "stt_url": "", "brain_url": "", "tts_url": "", "rag_url": "",
                "models": {"stt": self.s.ai_stt_model, "brain": self.s.ai_brain_model, "tts": self.s.ai_tts_model},
                "timeouts": {"stt": self.s.ai_request_timeout_s, "brain": self.s.ai_request_timeout_s * 2,
                             "tts": self.s.ai_request_timeout_s, "rag": 5.0},
                "paths": {}, "keys": {}, "languages": LANGS[1:], "default_language": "auto"}

    async def get_config(self, tenant_id: str) -> dict:
        doc = await self.db["ai_config"].find_one({"tenant_id": tenant_id})
        cfg = self.default_config(tenant_id)
        if doc:
            for k in ("enabled", "stt_url", "brain_url", "tts_url", "rag_url", "default_language", "updated_at", "updated_by"):
                if k in doc:
                    cfg[k] = doc[k]
            for k in ("models", "timeouts", "paths", "keys"):
                cfg[k] = {**cfg.get(k, {}), **doc.get(k, {})}
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
        if "keys" in body and isinstance(body["keys"], dict):
            cfg.setdefault("keys", {})
            for name, val in body["keys"].items():
                if name in SERVICES and isinstance(val, str):
                    cfg["keys"][name] = val.strip()
        cfg["updated_at"], cfg["updated_by"] = utcnow(), principal
        await self.db["ai_config"].update_one({"tenant_id": tenant_id}, {"$set": cfg}, upsert=True)
        return cfg

    def public_config(self, cfg: dict, detail: bool) -> dict:
        """Never includes secrets (none are stored). Viewers only learn whether each service is configured."""
        out = {k: v for k, v in cfg.items() if k not in ("_id", "keys")}
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
        if self.s.plivo_inbound_enabled or self.s.provider == "plivo_api":
            from app.ai.media import PlivoStreamBridge
            p_ready, p_reason = await PlivoStreamBridge(self.s).ready()
            out["plivo_stream"] = {"ready": p_ready, "reason": p_reason}
        self.last_health = out
        return out

    async def warmup(self, tenant_id: str) -> None:
        """Non-blocking background warm-up of tenant model endpoints (e.g. while call greeting plays)."""
        try:
            cfg = await self.get_config(tenant_id)
            c = self._clients(cfg)
            try:
                tasks = []
                stt_url = cfg.get("stt_url") or ""
                brain_url = cfg.get("brain_url") or ""
                tts_url = cfg.get("tts_url") or ""
                if "/runsync" in stt_url:
                    tasks.append(c.transcribe(b"\x00" * 1600, 8000, "hi"))
                else:
                    tasks.append(c.health("stt"))
                if "/runsync" in brain_url:
                    tasks.append(c.chat([{"role": "user", "content": "hello"}]))
                else:
                    tasks.append(c.health("brain"))
                if "/runsync" in tts_url:
                    tasks.append(c.synthesize("haan", "hi"))
                else:
                    tasks.append(c.health("tts"))
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=25.0)
            finally:
                await c.aclose()
        except Exception as e:
            log.debug("tenant %s ai warmup failed: %s", tenant_id, e)

    # ------------------------------------------------------------------ lifecycle
    async def _get_ai(self, tenant_id: str, session_id: str) -> dict | None:
        return await self.db["ai_sessions"].find_one({"session_id": session_id, "tenant_id": tenant_id})

    async def preflight(self, tenant_id: str, bridge: MediaBridge | None = None) -> tuple[bool, str]:
        """Can an AI conversation start for this tenant right now? (reason code, never a secret). Used to decide, BEFORE a
        call is answered into a media stream, whether to offer one. Mirrors the configuration checks in start()."""
        if not self.s.ai_enabled:
            return False, "ai_disabled"
        cfg = await self.get_config(tenant_id)
        if not cfg["enabled"]:
            return False, "ai_disabled_for_tenant"
        if not (cfg["stt_url"] and cfg["brain_url"] and cfg["tts_url"]):
            return False, "ai_not_configured"
        ready, _ = await (bridge or self.bridge).ready()
        if not ready:
            return False, "media_bridge_unavailable"
        return True, "ok"

    async def start(self, tenant_id: str, session_id: str, language: str | None = None, *,
                    bridge: MediaBridge | None = None) -> dict:
        """`bridge` overrides readiness for transports that open the media socket themselves (Plivo). The FreeSWITCH
        `begin()` hook only runs for the service's own bridge and only when the call has a FreeSWITCH UUID."""
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
        existing = await self._get_ai(tenant_id, session_id)
        if existing and existing.get("state") in ("starting", "active"):
            return existing
        ready, reason = await (bridge or self.bridge).ready()
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
        conv = Conversation(
            clients,
            lang,
            script_enforcement=getattr(self.s, "ai_script_enforcement", False),
            counsellor_gender=getattr(self.s, "ai_counsellor_gender", "masculine"),
            counsellor_name=getattr(self.s, "ai_counsellor_name", "MME AI counsellor"),
            hint_placement=getattr(self.s, "ai_hint_placement", "user_suffix"),
        )
        rt = Runtime(session_id, tenant_id, conv, clients)
        rt.fs_uuid, rt.call_id = call.get("fs_uuid"), sess.get("call_id")
        self.runtime[session_id] = rt
        m.AI_RUNTIMES.set(len(self.runtime))
        rt.watchdog = asyncio.create_task(self._media_deadline(rt, self.connect_s, "media_connect_timeout"))
        m.AI_SESSIONS.labels("starting").inc()
        if bridge is None and hasattr(self.bridge, "begin") and rt.fs_uuid:  # real bridge: make FreeSWITCH open the media socket now
            try:
                await self.bridge.begin(rt.fs_uuid, tenant_id, session_id, self.mint_media_token(tenant_id, session_id))
            except Exception as e:
                log.warning("media bridge start failed: %s", type(e).__name__)
                await self._finish(tenant_id, session_id, "failed", "bridge_start_failed", error=f"bridge_start_failed:{type(e).__name__}")
                return await self._get_ai(tenant_id, session_id)
        return doc

    def mint_media_token(self, tenant_id: str, session_id: str) -> str:
        """Short-lived operator-level token for exactly one purpose: opening the media socket of this tenant."""
        import time as _t

        import jwt as _jwt
        now = int(_t.time())
        payload = {"sub": f"media:{session_id}", self.s.jwt_tenant_claim: tenant_id, self.s.jwt_roles_claim: [self.s.role_user],
                   "iat": now, "exp": now + self.s.ai_media_token_ttl_s, "typ": "media"}
        if self.s.jwt_issuer:
            payload["iss"] = self.s.jwt_issuer
        if self.s.jwt_audience:
            payload["aud"] = self.s.jwt_audience
        return _jwt.encode(payload, self.s.jwt_secret.get_secret_value(), algorithm=self.s.jwt_algorithm)

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

    async def record_turn(self, rt: Runtime, timings: dict, count: bool = True, heard: str | None = None, reply: str | None = None) -> None:
        now = utcnow()
        turn_num = len(rt.conversation.turns)
        entry = {
            "turn": turn_num,
            "heard": heard or "",
            "reply": reply or "",
            "timings_ms": {k: v for k, v in timings.items() if isinstance(v, (int, float))},
            "ts": now.isoformat()
        }
        upd: dict = {
            "$set": {
                "last_turn_at": now,
                "last_exchange": entry,
                **{f"timings_ms.{k}": v for k, v in timings.items()}
            },
            "$push": {
                "transcript": entry
            }
        }
        if count:
            upd["$inc"] = {"turns": 1}
        await self.db["ai_sessions"].update_one({"session_id": rt.session_id, "tenant_id": rt.tenant_id}, upd)
        if self.activity is not None and count:
            self.activity.publish(rt.tenant_id, "ai.turn", call_id=rt.call_id, session_id=rt.session_id,
                                  data={
                                      "turn": turn_num,
                                      "heard": heard or "",
                                      "reply": reply or "",
                                      "stt_ms": timings.get("stt"),
                                      "brain_ms": timings.get("brain"),
                                      "tts_ms": timings.get("tts"),
                                      "turn_ms": timings.get("turn_ms"),
                                      **{k: v for k, v in timings.items() if isinstance(v, (int, float))}
                                  })

    def note_error(self, rt: Runtime, code: str) -> None:
        """A turn produced no audio (model timeout, bad reply, ...). Feed only; the metrics counter is incremented by the caller."""
        if self.activity is not None:
            self.activity.publish(rt.tenant_id, "ai.turn_error", call_id=rt.call_id, session_id=rt.session_id, data={"code": str(code)[:60]})

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
        m.AI_RUNTIMES.set(len(self.runtime))
        if rt:  # cancel inference + playback, release resources
            rt.conversation.cancel()
            if rt.fs_uuid and hasattr(self.bridge, "end"):
                await self.bridge.end(rt.fs_uuid)  # stop media bug + playback on FreeSWITCH
            if rt.watchdog:
                rt.watchdog.cancel()
            for t in rt.tasks:
                t.cancel()
            if rt.ws is not None:
                try:
                    await rt.ws.close(code=1000)
                except Exception:
                    pass
            if rt.conversation._task and not rt.conversation._task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(rt.conversation._task), timeout=0.2)
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
        sess = await self.sessions.get(tenant_id, session_id)  # 404 for other tenants / unknown ids
        ai = await self._get_ai(tenant_id, session_id)
        call = await self.repos.get_call(sess["call_id"], tenant_id) if sess.get("call_id") else None
        if call and call.get("provider") == "plivo_api":
            from app.ai.media import PlivoStreamBridge
            bridge = PlivoStreamBridge(self.s)
        else:
            bridge = self.bridge
        ready, reason = await bridge.ready()
        cfg = await self.get_config(tenant_id)
        out: dict[str, Any] = {"session_id": session_id, "state": ai["state"] if ai else "not_started",
                               "language": ai["language"] if ai else None, "turns": ai["turns"] if ai else 0,
                               "timings_ms": ai.get("timings_ms") if ai else {},
                               "last_exchange": ai.get("last_exchange") if ai else None,
                               "transcript": ai.get("transcript", []) if ai else [],
                               "error": ai.get("error") if ai else None,
                               "stop_reason": ai.get("stop_reason") if ai else None,
                               "media": {"connected": bool(ai and ai["media"]["connected"]), "bridge": bridge.name,
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
        if self.activity is not None:
            self.activity.publish(tenant_id, etype, session_id=session_id, data={"reason": reason})
        if self.emitter:
            await self.emitter.emit_generic(tenant_id, etype, f"{session_id}:{etype}:{hashlib.sha1(str(utcnow()).encode()).hexdigest()[:8]}",
                                            {"session_id": session_id, "reason": reason})

    async def recover_on_startup(self) -> dict:
        """After a restart no media socket or inference task exists, so every non-terminal AI session is stale. Fail them
        explicitly (visible reason, webhook) and put their sessions back to `answered`. Idempotent."""
        n = 0
        async for d in self.db["ai_sessions"].find({"state": {"$nin": list(AI_TERMINAL)}}):
            upd = await self.db["ai_sessions"].find_one_and_update(
                {"_id": d["_id"], "state": {"$nin": list(AI_TERMINAL)}},
                {"$set": {"state": "failed", "stopped_at": utcnow(), "stop_reason": "service_restarted", "error": "service_restarted",
                          "media.connected": False}})
            if not upd:
                continue
            sess = await self.db["sessions"].find_one({"_id": d["session_id"]})
            if sess and sess["status"] == "active":
                await self.sessions.transition(sess, "answered", source="recovery")
            await self._emit(d["tenant_id"], "ai.session.failed", d["session_id"], "service_restarted")
            m.RECOVERY.labels("ai_session_failed").inc()
            n += 1
        return {"ai_sessions_failed": n}

    async def stop_all(self, reason: str) -> list[tuple[str, str]]:
        """Close every live AI conversation (drain/shutdown). Returns [(tenant_id, session_id)] that were stopped."""
        out = []
        for sid, rt in list(self.runtime.items()):
            await self._finish(rt.tenant_id, sid, "stopped", reason)
            out.append((rt.tenant_id, sid))
        return out

    async def shutdown(self) -> None:
        for sid, rt in list(self.runtime.items()):
            rt.conversation.cancel()
            for t in rt.tasks:
                t.cancel()
            if rt.watchdog:
                rt.watchdog.cancel()
            await rt.clients.aclose()
        self.runtime.clear()
