"""Counselling sessions: durable, idempotent, linked 1:1 to a call where one exists.

Session states (separate from call states and from AI-session states):
  created -> answered -> active (AI attached) -> ending -> ended      failed reachable from any non-terminal state
A session is `active` only when an AI session is running; a plain answered call stays `answered`.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.core.errors import Conflict, NotFound
from app.core.ids import new_uuid, utcnow
from app.events.webhooks import WebhookEmitter
from app.observability import metrics as m
from app.persistence.db import Database
from config.settings import Settings

log = logging.getLogger("mme_telephony.sessions")

TERMINAL = {"ended", "failed"}
TRANSITIONS: dict[str, set[str]] = {
    "created": {"answered", "ending", "ended", "failed"},
    "answered": {"active", "ending", "ended", "failed"},
    "active": {"answered", "ending", "ended", "failed"},  # AI stopped but call continues -> back to answered
    "ending": {"ended", "failed"},
    "ended": set(),
    "failed": set(),
}
LANGUAGES = {"auto", "hi", "en", "hinglish"}


def can_transition(cur: str, target: str) -> bool:
    return target in TRANSITIONS.get(cur, set())


class SessionService:
    def __init__(self, db: Database, s: Settings, emitter: WebhookEmitter | None = None):
        self.db, self.s, self.emitter = db, s, emitter

    # ------------------------------------------------------------------ create
    async def create(self, tenant_id: str, *, call_ref: str | None, caller: str | None, destination: str | None = None,
                     language: str = "auto", metadata: dict | None = None, source: str = "api",
                     call_id: str | None = None) -> tuple[dict, bool]:
        """Idempotent per (tenant, call_ref). Returns (session, created)."""
        if language not in LANGUAGES:
            from app.core.errors import Unprocessable
            raise Unprocessable("unsupported language", code="invalid_language", details={"supported": sorted(LANGUAGES)})
        now = utcnow()
        doc = {
            "_id": new_uuid(), "tenant_id": tenant_id, "call_id": call_id, "call_ref": call_ref or call_id,
            "caller": (caller or None), "destination": destination, "language": language, "status": "created",
            "state_version": 0, "metadata": metadata or {}, "source": source, "created_at": now, "updated_at": now,
            "answered_at": None, "ended_at": None, "end_reason": None, "history": [{"ts": now, "state": "created", "source": source}],
            "purge_at": now + timedelta(days=self.s.retention_calls_days),
        }
        if not doc["call_ref"]:
            doc.pop("call_ref")  # sparse unique index: sessions without a call reference are never deduplicated
        try:
            await self.db["sessions"].insert_one(doc)
        except DuplicateKeyError:
            existing = await self.db["sessions"].find_one({"tenant_id": tenant_id, "call_ref": doc["call_ref"]})
            return existing, False
        m.SESSIONS.labels("created").inc()
        await self._emit(doc, "session.created")
        return doc, True

    # ------------------------------------------------------------------ read
    async def get(self, tenant_id: str, session_id: str) -> dict:
        s = await self.db["sessions"].find_one({"_id": session_id, "tenant_id": tenant_id})
        if not s:
            raise NotFound("session not found")
        return s

    async def get_for_call(self, tenant_id: str, call_id: str) -> dict | None:
        return await self.db["sessions"].find_one({"tenant_id": tenant_id, "$or": [{"call_id": call_id}, {"call_ref": call_id}]})

    async def list(self, tenant_id: str, *, status: str | None, q: str | None, limit: int, cursor: str | None) -> tuple[list[dict], str | None]:
        flt: dict[str, Any] = {"tenant_id": tenant_id}
        if status:
            flt["status"] = status
        if q:
            import re
            rx = re.escape(q[:40])
            flt["$or"] = [{"_id": q[:60]}, {"call_id": q[:60]}, {"call_ref": q[:150]}, {"caller": {"$regex": rx}}, {"destination": {"$regex": rx}}]
        if cursor:
            from datetime import datetime
            ts_s, _, sid = cursor.partition("|")
            ts = datetime.fromisoformat(ts_s)
            flt["$and"] = [{"$or": [{"created_at": {"$lt": ts}}, {"created_at": ts, "_id": {"$lt": sid}}]}]
        rows = await self.db["sessions"].find(flt).sort([("created_at", -1), ("_id", -1)]).limit(limit + 1).to_list(limit + 1)
        nxt = None
        if len(rows) > limit:
            rows = rows[:limit]
            from app.core.ids import aware
            nxt = f"{aware(rows[-1]['created_at']).isoformat()}|{rows[-1]['_id']}"
        return rows, nxt

    # ------------------------------------------------------------------ transitions
    async def transition(self, session: dict, target: str, *, source: str, reason: str | None = None,
                         ts=None) -> tuple[str, dict]:
        """CAS transition. Returns (outcome, session): applied | noop (already there/terminal) | invalid."""
        ts = ts or utcnow()
        for _ in range(5):
            cur = session["status"]
            if cur == target or cur in TERMINAL:
                return "noop", session
            if not can_transition(cur, target):
                return "invalid", session
            sets: dict[str, Any] = {"status": target, "updated_at": utcnow()}
            if target == "answered" and not session.get("answered_at"):
                sets["answered_at"] = ts
            if target in TERMINAL:
                sets["ended_at"] = ts
                sets["end_reason"] = reason or ("failed" if target == "failed" else "completed")
            upd = await self.db["sessions"].find_one_and_update(
                {"_id": session["_id"], "state_version": session["state_version"]},
                {"$set": sets, "$inc": {"state_version": 1},
                 "$push": {"history": {"$each": [{"ts": ts, "state": target, "source": source}], "$slice": -50}}},
                return_document=ReturnDocument.AFTER)
            if upd:
                m.SESSIONS.labels(target).inc()
                if target in ("answered", "ended", "failed"):
                    await self._emit(upd, f"session.{'ended' if target == 'failed' else target}")
                return "applied", upd
            session = await self.db["sessions"].find_one({"_id": session["_id"]}) or session
        raise Conflict("session contention", code="session_contention")

    async def answer(self, tenant_id: str, session_id: str) -> dict:
        s = await self.get(tenant_id, session_id)
        _, s = await self.transition(s, "answered", source="api")
        return s

    async def end(self, tenant_id: str, session_id: str, reason: str = "completed") -> dict:
        s = await self.get(tenant_id, session_id)
        _, s = await self.transition(s, "ended", source="api", reason=reason[:100])
        return s

    # ------------------------------------------------------------------ call coupling
    async def on_call_state(self, call: dict) -> None:
        """Called by the controller after every APPLIED call transition. Never raises."""
        try:
            sid = call.get("session_id")
            sess = await self.db["sessions"].find_one({"_id": sid}) if sid else None
            if sess is None:
                sess, _ = await self.create(
                    call["tenant_id"], call_ref=call["_id"], call_id=call["_id"],
                    caller=call.get("from_number"), destination=call.get("to_number"),
                    language=call.get("language", "auto"), metadata=call.get("metadata") or {}, source="call")
                await self.db["calls"].update_one({"_id": call["_id"]}, {"$set": {"session_id": sess["_id"]}})
            st = call["state"]
            if st in ("answered", "bridged", "transferring"):
                await self.transition(sess, "answered", source="call", ts=call.get("answered_at"))
            elif st in ("completed", "cancelled", "busy", "no_answer"):
                await self.transition(sess, "ended", source="call", reason=call.get("end_reason") or call.get("failure_reason") or st,
                                      ts=call.get("ended_at"))
            elif st == "failed":
                await self.transition(sess, "failed", source="call", reason=call.get("failure_reason") or "call_failed",
                                      ts=call.get("ended_at"))
        except Exception:
            log.exception("session sync failed for call")

    async def _emit(self, session: dict, event_type: str) -> None:
        if not self.emitter:
            return
        await self.emitter.emit_generic(
            session["tenant_id"], event_type, f"{session['_id']}:{event_type}:{session['state_version']}",
            {"session_id": session["_id"], "call_id": session.get("call_id"), "status": session["status"],
             "language": session.get("language"), "end_reason": session.get("end_reason")})
