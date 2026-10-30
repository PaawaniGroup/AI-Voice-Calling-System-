"""In-process live activity bus for the dashboard.

Every component that changes something an operator wants to watch (call events, session changes, AI turns/sessions, webhook
deliveries) publishes one small, already-redacted entry here. The dashboard stream reads entries by a monotonically increasing
integer `seq`, so it does not depend on wall-clock timestamps (this project has been bitten by a host whose clock steps) and puts
no extra load on MongoDB.

Properties
  * bounded ring (old entries are dropped; a reader that fell behind is told how many it missed);
  * tenant-scoped reads: an entry is only ever returned to the tenant that owns it;
  * data is redacted before it is stored (phone numbers masked, secret-looking keys hidden, long strings cut);
  * single process by design (the service runs one worker); history is lost on restart, per-call history lives in `call_events`;
  * publishing never raises and never blocks: a bug in the dashboard feed must not be able to disturb a call.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any

from app.core.ids import utcnow
from app.core.logging import redact

log = logging.getLogger("mme_telephony.activity")

_BAD = ("failed", "error", "dead", "rejected", "lost", "timeout_exceeded", "refused")
_WARN = ("timeout", "skipped", "retry", "disconnect", "uncertain", "overflow", "drain", "reconnect")


def _level(kind: str) -> str:
    k = kind.lower()
    if any(x in k for x in _BAD):
        return "error"
    if any(x in k for x in _WARN):
        return "warn"
    return "info"


WORD_KEYS = frozenset({"heard", "reply"})   # what the caller said / what the assistant said


def strip_words(v: Any) -> Any:
    """Remove conversation content (`heard`, `reply`) from any JSON-like structure, at any depth. Live surfaces (feed, flow, session
    view) carry timings and counts, never words: no role sees a caller's speech live."""
    if isinstance(v, dict):
        return {k: strip_words(x) for k, x in v.items() if k not in WORD_KEYS}
    if isinstance(v, (list, tuple)):
        return [strip_words(x) for x in v]
    return v


def _shrink(v: Any, depth: int = 0, key: str = "") -> Any:
    """Keep entries small: cut long strings, limit depth and collection size."""
    if depth > 3:
        return "…"
    if isinstance(v, str):
        limit = 2000 if key in ("heard", "reply", "transcript", "msg", "detail") else 120
        return v if len(v) <= limit else v[: limit - 3] + "…"
    if isinstance(v, dict):
        return {str(k)[:40]: _shrink(x, depth + 1, str(k)) for k, x in list(v.items())[:20]}
    if isinstance(v, (list, tuple)):
        return [_shrink(x, depth + 1, key) for x in list(v)[:10]]
    return v


class ActivityBus:
    def __init__(self, size: int = 2000):
        self._ring: deque[dict] = deque(maxlen=size)
        self._seq = 0
        self._waiters: set[asyncio.Future] = set()

    @property
    def last_seq(self) -> int:
        return self._seq

    def publish(self, tenant_id: str | None, kind: str, *, call_id: str | None = None, session_id: str | None = None,
                data: dict | None = None) -> int:
        """Append one entry and wake any waiting stream. Returns its seq (0 if it could not be published)."""
        try:
            if not tenant_id:
                return 0
            self._seq += 1
            entry = {"seq": self._seq, "ts": utcnow().isoformat(), "kind": kind[:60], "category": kind.split(".", 1)[0][:20],
                     "level": _level(kind), "call_id": call_id, "session_id": session_id,
                     "data": _shrink(redact(strip_words(data or {}))), "_tenant": tenant_id}
            self._ring.append(entry)
            for w in list(self._waiters):
                if not w.done():
                    w.set_result(None)
            return entry["seq"]
        except Exception:  # never let the dashboard feed affect the caller
            log.exception("activity publish failed")
            return 0

    def since(self, after_seq: int, tenant_id: str, limit: int = 200) -> tuple[list[dict], int]:
        """Entries of `tenant_id` with seq > after_seq (oldest first, at most `limit`), and how many were lost because the
        ring no longer holds them (0 normally)."""
        if not self._ring:
            return [], 0
        oldest = self._ring[0]["seq"]
        lost = max(0, oldest - after_seq - 1) if after_seq < oldest else 0
        out = [self._public(e) for e in self._ring if e["seq"] > after_seq and e["_tenant"] == tenant_id]
        return out[:limit], lost

    def recent(self, tenant_id: str, n: int = 50) -> list[dict]:
        rows = [self._public(e) for e in self._ring if e["_tenant"] == tenant_id]
        return rows[-n:]

    async def wait(self, after_seq: int, timeout: float) -> bool:
        """True as soon as an entry newer than `after_seq` exists (immediately if one already does), False on timeout."""
        if self._seq > after_seq:
            return True
        fut = asyncio.get_running_loop().create_future()
        self._waiters.add(fut)
        try:
            await asyncio.wait_for(fut, timeout)
            return True
        except asyncio.TimeoutError:
            return self._seq > after_seq
        finally:
            self._waiters.discard(fut)

    @staticmethod
    def _public(e: dict) -> dict:
        return {k: v for k, v in e.items() if not k.startswith("_")}
