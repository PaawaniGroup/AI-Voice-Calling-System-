"""Data access for calls, events, idempotency, policies, routes, audit.

Consistency model: no multi-document transactions (works on standalone/Atlas alike).
 * State changes use compare-and-swap on `state_version`.
 * Events are inserted first under a unique (call_id, dedup_id) index => idempotent.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.core.errors import Conflict
from app.core.ids import aware, new_id, utcnow
from app.persistence.db import Database
from app.telephony import state_machine as sm
from app.telephony.state_machine import S
from config.settings import Settings

TS_FIELD = {
    S.QUEUED: "queued_at", S.INITIATING: "initiated_at", S.RINGING: "ringing_at", S.ANSWERED: "answered_at",
    S.BRIDGED: "bridged_at", S.TRANSFERRING: "transfer_started_at",
}


def fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


class Repos:
    def __init__(self, db: Database, settings: Settings):
        self.db, self.s = db, settings

    # ---------------------------------------------------------------- idempotency
    async def claim_idempotency(self, tenant_id: str, operation: str, key: str, fp: str, call_id: str) -> dict | None:
        """Returns None if this request owns the key, else the existing record.

        Raises Conflict when the key is reused with a different request body.
        """
        now = utcnow()
        doc = {
            "tenant_id": tenant_id, "operation": operation, "key": key, "fingerprint": fp,
            "call_id": call_id, "created_at": now,
            "purge_at": now + timedelta(hours=self.s.retention_idempotency_hours),
        }
        try:
            await self.db["idempotency"].insert_one(doc)
            return None
        except DuplicateKeyError:
            existing = await self.db["idempotency"].find_one(
                {"tenant_id": tenant_id, "operation": operation, "key": key})
            if existing and existing["fingerprint"] != fp:
                raise Conflict("Idempotency-Key reused with a different request", code="idempotency_key_reuse") from None
            return existing

    async def release_idempotency(self, tenant_id: str, operation: str, key: str) -> None:
        await self.db["idempotency"].delete_one({"tenant_id": tenant_id, "operation": operation, "key": key})

    # ---------------------------------------------------------------- calls
    async def insert_call(self, doc: dict) -> None:
        now = utcnow()
        doc.setdefault("created_at", now)
        doc.setdefault("updated_at", now)
        doc.setdefault("state_version", 0)
        doc.setdefault("schema_version", 2)
        doc["purge_at"] = doc["created_at"] + timedelta(days=self.s.retention_calls_days)
        await self.db["calls"].insert_one(doc)

    async def get_call(self, call_id: str, tenant_id: str | None = None) -> dict | None:
        q: dict[str, Any] = {"_id": call_id}
        if tenant_id is not None:
            q["tenant_id"] = tenant_id  # tenant scoping is part of the primary lookup
        return await self.db["calls"].find_one(q)

    async def find_call_by_fs_uuid(self, uuid: str) -> dict | None:
        return await self.db["calls"].find_one({"$or": [{"fs_uuid": uuid}, {"fs_uuids": uuid}]})

    async def find_call_by_provider_id(self, provider: str, pid: str) -> dict | None:
        return await self.db["calls"].find_one({"provider": provider, "provider_call_id": pid})

    async def update_call(self, call_id: str, fields: dict, *, add_to_set: dict | None = None) -> None:
        upd: dict[str, Any] = {"$set": {**fields, "updated_at": utcnow()}}
        if add_to_set:
            upd["$addToSet"] = add_to_set
        await self.db["calls"].update_one({"_id": call_id}, upd)

    async def list_calls(self, tenant_id: str, *, state: str | None, direction: str | None,
                         start: datetime | None, end: datetime | None, limit: int, cursor: str | None) -> tuple[list[dict], str | None]:
        q: dict[str, Any] = {"tenant_id": tenant_id}
        if state:
            q["state"] = state
        if direction:
            q["direction"] = direction
        if start or end:
            q["created_at"] = {k: v for k, v in (("$gte", start), ("$lt", end)) if v}
        if cursor:
            q.setdefault("created_at", {})
            ts_s, _, cid = cursor.partition("|")
            ts = datetime.fromisoformat(ts_s)
            q["$or"] = [{"created_at": {"$lt": ts}}, {"created_at": ts, "_id": {"$lt": cid}}]
            if "$gte" not in q["created_at"] and "$lt" not in q["created_at"]:
                q.pop("created_at")
        rows = await self.db["calls"].find(q).sort([("created_at", -1), ("_id", -1)]).limit(limit + 1).to_list(limit + 1)
        nxt = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            nxt = f"{aware(last['created_at']).isoformat()}|{last['_id']}"
        return rows, nxt

    async def count_active(self, tenant_id: str | None = None) -> int:
        q: dict[str, Any] = {"state": {"$in": [s.value for s in sm.ACTIVE]}}
        if tenant_id:
            q["tenant_id"] = tenant_id
        return await self.db["calls"].count_documents(q)

    async def count_queued(self) -> int:
        return await self.db["calls"].count_documents({"state": S.QUEUED.value})

    async def count_in_flight(self, tenant_id: str | None = None) -> int:
        """Active + queued; used for admission so queued work is also bounded per tenant."""
        q: dict[str, Any] = {"state": {"$in": [s.value for s in sm.ACTIVE | {S.QUEUED}]}}
        if tenant_id:
            q["tenant_id"] = tenant_id
        return await self.db["calls"].count_documents(q)

    async def claim_next_queued(self, tenant_active: dict[str, int], per_tenant_max: dict[str, int],
                                default_max: int) -> dict | None:
        """CAS-claim the oldest queued call whose tenant has spare capacity (queued -> initiating)."""
        cur = self.db["calls"].find({"state": S.QUEUED.value}).sort("queued_at", 1).limit(100)
        async for c in cur:
            t = c["tenant_id"]
            if tenant_active.get(t, 0) >= per_tenant_max.get(t, default_max):
                continue
            now = utcnow()
            claimed = await self.db["calls"].find_one_and_update(
                {"_id": c["_id"], "state": S.QUEUED.value, "state_version": c["state_version"]},
                {"$set": {"state": S.INITIATING.value, "initiated_at": now, "updated_at": now},
                 "$inc": {"state_version": 1}},
                return_document=ReturnDocument.AFTER,
            )
            if claimed:
                await self.add_event(claimed, "state.initiating", "controller", f"claim:{claimed['state_version']}",
                                     {"from": "queued"})
                return claimed
        return None

    async def transition(self, call: dict, target: S, *, source: str, dedup_id: str, ts: datetime | None = None,
                         fields: dict | None = None, payload: dict | None = None) -> tuple[str, dict]:
        """Apply an event-driven transition. Returns (outcome, call) with outcome in
        applied | duplicate | out_of_order | terminal_ignored. Raises InvalidTransition for forbidden moves.

        The event is recorded first (unique dedup index), so replays are no-ops and every
        delivered event stays auditable even when it does not change state.
        """
        ts = ts or utcnow()
        if not await self.add_event(call, f"state.{target.value}", source, dedup_id, payload or {}, ts=ts):
            return "duplicate", call
        for _ in range(6):
            cur = S(call["state"])
            if cur in sm.TERMINAL:
                return "terminal_ignored", call
            if cur == target or sm.is_out_of_order(cur, target):
                return "out_of_order", call
            sm.validate(cur, target)
            sets: dict[str, Any] = {"state": target.value, "updated_at": utcnow(), **(fields or {})}
            if target in TS_FIELD and not call.get(TS_FIELD[target]):
                sets[TS_FIELD[target]] = ts
            if target in sm.TERMINAL:
                sets["ended_at"] = ts
                ans = aware(call.get("answered_at")) or sets.get("answered_at")
                if ans:
                    raw = (ts - ans).total_seconds()
                    if raw < 0:  # wall clock stepped backwards between events: never store a negative duration
                        sets["clock_anomaly"] = True
                    sets["connected_duration_s"] = max(0.0, raw)
                    sets["duration_source"] = "controller_clock"
                    fa, fe = aware(call.get("fs_answered_ts")), aware((fields or {}).get("fs_ended_ts"))
                    if fa and fe and 0 <= (fe - fa).total_seconds() <= 172800:  # same-domain FreeSWITCH delta is more exact
                        sets["connected_duration_s"] = (fe - fa).total_seconds()
                        sets["duration_source"] = "freeswitch_clock"
                rng = aware(call.get("ringing_at") or call.get("initiated_at"))
                if rng:
                    sets["ring_duration_s"] = max(0.0, ((ans or ts) - rng).total_seconds())
            upd = await self.db["calls"].find_one_and_update(
                {"_id": call["_id"], "state_version": call["state_version"]},
                {"$set": sets, "$inc": {"state_version": 1}},
                return_document=ReturnDocument.AFTER,
            )
            if upd:
                return "applied", upd
            call = await self.db["calls"].find_one({"_id": call["_id"]}) or call  # lost a CAS race: re-evaluate
        raise Conflict("could not apply transition after retries", code="transition_contention")

    # ---------------------------------------------------------------- events
    async def add_event(self, call: dict, etype: str, source: str, dedup_id: str, payload: dict | None = None,
                        ts: datetime | None = None) -> bool:
        now = utcnow()
        doc = {
            "_id": new_id("evt"), "call_id": call["_id"], "tenant_id": call["tenant_id"], "type": etype,
            "ts": ts or now, "recorded_at": now, "source": source, "dedup_id": dedup_id,
            "correlation_id": call.get("correlation_id"), "payload": payload or {},
            "purge_at": now + timedelta(days=self.s.retention_events_days),
        }
        try:
            await self.db["call_events"].insert_one(doc)
            return True
        except DuplicateKeyError:
            return False

    async def list_events(self, call_id: str, tenant_id: str, limit: int = 500) -> list[dict]:
        return await self.db["call_events"].find({"call_id": call_id, "tenant_id": tenant_id}).sort("ts", 1).limit(limit).to_list(limit)

    # ---------------------------------------------------------------- policy / routes
    async def get_policy(self, tenant_id: str) -> dict | None:
        return await self.db["tenant_policies"].find_one({"tenant_id": tenant_id})

    async def put_policy(self, tenant_id: str, policy: dict) -> dict:
        policy = {**policy, "tenant_id": tenant_id, "updated_at": utcnow()}
        return await self.db["tenant_policies"].find_one_and_update(
            {"tenant_id": tenant_id}, {"$set": policy}, upsert=True, return_document=ReturnDocument.AFTER)

    async def get_route(self, number: str) -> dict | None:
        return await self.db["inbound_routes"].find_one({"number": number, "enabled": True})

    async def audit(self, tenant_id: str | None, principal: str, action: str, target: str, details: dict | None = None,
                    request_id: str = "-") -> None:
        now = utcnow()
        await self.db["audit"].insert_one({
            "tenant_id": tenant_id, "principal": principal, "action": action, "target": target,
            "details": details or {}, "request_id": request_id, "ts": now,
            "purge_at": now + timedelta(days=self.s.retention_audit_days),
        })

    # ---------------------------------------------------------------- provider replay protection
    async def seen_provider_key(self, key: str) -> bool:
        """Atomically record a provider callback nonce. True => already seen (replay)."""
        now = utcnow()
        try:
            await self.db["provider_dedup"].insert_one(
                {"key": key, "ts": now, "purge_at": now + timedelta(hours=self.s.retention_dedup_hours)})
            return False
        except DuplicateKeyError:
            return True
