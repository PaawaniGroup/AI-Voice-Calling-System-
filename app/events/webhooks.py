"""Outbound lifecycle webhooks: signing, SSRF guard, durable queue (DB outbox), retrying delivery."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import random
import socket
import time
from datetime import timedelta
from urllib.parse import urlparse

import httpx
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.core.errors import Unprocessable
from app.core.ids import new_id, utcnow
from app.persistence.db import Database
from app.observability import metrics as m
from config.settings import Settings

log = logging.getLogger("mme_telephony.webhooks")

EVENT_TYPES = {"call.created", "call.initiated", "call.ringing", "call.answered", "call.bridged", "call.transferring",
               "call.completed", "call.ended", "call.failed", "call.cancelled", "call.busy", "call.no_answer",
               "session.created", "session.answered", "session.ended",
               "ai.session.started", "ai.session.stopped", "ai.session.failed", "transcript.ready", "summary.ready"}
_STATE_EVENT = {"initiating": "call.initiated", "ringing": "call.ringing", "answered": "call.answered",
                "bridged": "call.bridged", "transferring": "call.transferring", "completed": "call.completed",
                "failed": "call.failed", "cancelled": "call.cancelled", "busy": "call.busy",
                "no_answer": "call.no_answer"}
_BLOCKED_HOSTS = {"metadata.google.internal", "metadata", "localhost"}


def event_for_state(state: str) -> str | None:
    return _STATE_EVENT.get(state)


def derive_secret(master: str, endpoint_id: str, version: int) -> str:
    """Per-endpoint signing secret derived from the master key: nothing secret is stored in the DB."""
    return hmac.new(master.encode(), f"{endpoint_id}:{version}".encode(), hashlib.sha256).hexdigest()


def sign(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"v1={mac}"


def _ip_blocked(ip: ipaddress._BaseAddress) -> bool:
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
            or ip.is_unspecified or str(ip).startswith("169.254."))


async def validate_destination(url: str, s: Settings) -> None:
    """SSRF guard. Resolves DNS and rejects private/loopback/link-local/metadata targets."""
    u = urlparse(url)
    if u.scheme not in ("https", "http") or not u.hostname or u.username or u.password:
        raise Unprocessable("invalid webhook url", code="invalid_webhook_url")
    if u.scheme == "http" and not s.webhook_allow_http:
        raise Unprocessable("webhook url must be https", code="invalid_webhook_url")
    if s.webhook_allow_private:
        return
    host = u.hostname
    if host.lower() in _BLOCKED_HOSTS:
        raise Unprocessable("webhook destination not allowed", code="webhook_destination_blocked")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, u.port or (443 if u.scheme == "https" else 80),
                                                              type=socket.SOCK_STREAM)
    except OSError:
        raise Unprocessable("webhook host does not resolve", code="invalid_webhook_url") from None
    for info in infos:
        if _ip_blocked(ipaddress.ip_address(info[4][0])):
            raise Unprocessable("webhook destination not allowed", code="webhook_destination_blocked")


class WebhookEmitter:
    """Creates one delivery row per (endpoint, event). Unique index => emitting twice is harmless."""

    def __init__(self, db: Database, s: Settings):
        self.db, self.s = db, s

    async def emit_for_call(self, call: dict, event_type: str, event_id: str, extra: dict | None = None) -> None:
        if not self.s.webhooks_enabled:
            return
        # Payload deliberately excludes recording URLs and unneeded personal data (numbers are masked).
        data = {
            "call_id": call["_id"], "tenant_id": call["tenant_id"], "direction": call.get("direction"),
            "state": call.get("state"), "session_id": call.get("session_id"),
            "failure_reason": call.get("failure_reason"),
            "hangup_cause": call.get("hangup_cause"), "connected_duration_s": call.get("connected_duration_s"),
            "to_masked": _mask(call.get("to_number", "")), **(extra or {}),
        }
        await self.emit_generic(call["tenant_id"], event_type, event_id, data)

    async def emit_generic(self, tenant_id: str, event_type: str, event_id: str, data: dict) -> None:
        """Queue one delivery per matching endpoint. Unique (endpoint, event) index makes re-emitting harmless."""
        if not self.s.webhooks_enabled:
            return
        eps = await self.db["webhook_endpoints"].find(
            {"tenant_id": tenant_id, "enabled": True, "events": {"$in": [event_type, "*"]}}).to_list(50)
        if not eps:
            return
        if await self.db["webhook_deliveries"].count_documents({"status": {"$in": ["pending", "retrying"]}}) \
                >= self.s.webhook_queue_limit:
            m.WEBHOOK_DROPPED.inc()
            log.error("webhook queue full; dropping event")
            return
        now = utcnow()
        payload = {"id": event_id, "type": event_type, "api_version": "2025-01", "created_at": now.isoformat(),
                   "data": {"tenant_id": tenant_id, **data}}
        for ep in eps:
            try:
                await self.db["webhook_deliveries"].insert_one({
                    "_id": new_id("dlv"), "tenant_id": tenant_id, "endpoint_id": ep["_id"], "event_id": event_id,
                    "event_type": event_type, "payload": payload, "status": "pending", "attempts": [],
                    "attempt_count": 0, "next_attempt_at": now, "created_at": now,
                    "purge_at": now + timedelta(days=self.s.retention_events_days)})
            except DuplicateKeyError:
                pass


def _mask(n: str) -> str:
    return n[:3] + "*" * max(0, len(n) - 5) + n[-2:] if len(n) > 6 else n


class WebhookWorker:
    def __init__(self, db: Database, s: Settings):
        self.db, self.s = db, s
        self._sem = asyncio.Semaphore(s.webhook_concurrency)
        self.activity = None  # ActivityBus (live dashboard feed), set by the app factory
        self._task: asyncio.Task | None = None
        self._http: httpx.AsyncClient | None = None
        self._stop = False

    def start(self) -> None:
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(self.s.webhook_read_timeout_s, connect=self.s.webhook_connect_timeout_s),
            follow_redirects=False)
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop = True
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._http:
            await self._http.aclose()

    async def _loop(self) -> None:
        while not self._stop:
            try:
                n = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("webhook loop error")
                n = 0
            await asyncio.sleep(0.2 if n else 1.0)

    async def _claim(self) -> dict | None:
        now = utcnow()
        return await self.db["webhook_deliveries"].find_one_and_update(
            {"status": {"$in": ["pending", "retrying"]}, "next_attempt_at": {"$lte": now}},
            {"$set": {"status": "sending", "claimed_at": now}}, sort=[("next_attempt_at", 1)],
            return_document=ReturnDocument.AFTER)

    async def run_once(self) -> int:
        batch = []
        for _ in range(self.s.webhook_concurrency):
            d = await self._claim()
            if not d:
                break
            batch.append(d)
        await asyncio.gather(*(self._deliver(d) for d in batch))
        return len(batch)

    async def _deliver(self, d: dict) -> None:
        async with self._sem:
            ep = await self.db["webhook_endpoints"].find_one({"_id": d["endpoint_id"], "tenant_id": d["tenant_id"]})
            outcome, status_code, err = "failed", None, None
            if not ep or not ep.get("enabled"):
                err = "endpoint_disabled"
            else:
                body = json.dumps(d["payload"], separators=(",", ":"), sort_keys=True).encode()
                ts = int(time.time())
                secret = derive_secret(self.s.webhook_signing_master.get_secret_value(), ep["_id"], ep.get("secret_version", 1))
                headers = {
                    "Content-Type": "application/json", "User-Agent": "mme-telephony/2",
                    "X-MME-Event-Id": d["event_id"], "X-MME-Delivery-Id": d["_id"], "X-MME-Timestamp": str(ts),
                    "X-MME-Signature": sign(secret, ts, body), "Idempotency-Key": d["event_id"],
                }
                t0 = time.perf_counter()
                try:
                    await validate_destination(ep["url"], self.s)  # re-checked at send time (DNS may change)
                    assert self._http is not None
                    r = await self._http.post(ep["url"], content=body, headers=headers)
                    status_code = r.status_code
                    if 200 <= r.status_code < 300:
                        outcome = "delivered"
                    else:
                        err = f"http_{r.status_code}"
                except Unprocessable as e:
                    err = e.code
                except Exception as e:
                    err = type(e).__name__
                m.WEBHOOK_LATENCY.observe(time.perf_counter() - t0)
            n = d["attempt_count"] + 1
            attempt = {"n": n, "ts": utcnow(), "status_code": status_code, "error": err, "outcome": outcome}
            upd: dict = {"attempt_count": n}
            if outcome == "delivered":
                upd["status"] = "delivered"
                m.WEBHOOK_DELIVERIES.labels("delivered").inc()
            elif n >= self.s.webhook_max_attempts or err in ("endpoint_disabled", "webhook_destination_blocked"):
                upd["status"] = "dead"
                m.WEBHOOK_DELIVERIES.labels("dead").inc()
                log.error("webhook delivery dead-lettered")
            else:
                delay = min(self.s.webhook_backoff_max_s, self.s.webhook_backoff_base_s * 2 ** (n - 1))
                delay *= 1 + random.uniform(-0.2, 0.2)
                upd["status"] = "retrying"
                upd["next_attempt_at"] = utcnow() + timedelta(seconds=delay)
                m.WEBHOOK_DELIVERIES.labels("retry").inc()
            await self.db["webhook_deliveries"].update_one({"_id": d["_id"]}, {"$set": upd, "$push": {"attempts": attempt}})
            if self.activity is not None:
                self.activity.publish(d["tenant_id"], f"webhook.{upd['status']}", call_id=(d.get("payload", {}).get("data") or {}).get("call_id"),
                                      data={"event": d.get("event_type"), "attempt": n, "http": status_code, "error": err})

    async def replay(self, delivery_id: str, tenant_id: str) -> bool:
        """Safe replay: resets the same delivery (same event id => consumers dedupe via Idempotency-Key)."""
        r = await self.db["webhook_deliveries"].update_one(
            {"_id": delivery_id, "tenant_id": tenant_id, "status": {"$in": ["dead", "delivered"]}},
            {"$set": {"status": "pending", "next_attempt_at": utcnow(), "attempt_count": 0}})
        return r.modified_count == 1
