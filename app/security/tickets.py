"""Short-lived stream tickets: bind ONE Plivo media stream to ONE call and session.

Plivo echoes the Stream element's `extraHeaders` back in the `start` event, so the ticket rides there. It is deliberately NOT
a JWT: a JWT would be a reusable bearer credential for the REST API if it leaked through Plivo logs or headers. This token is
only meaningful to `verify()` for the stream purpose, names the session, tenant and provider call id, and expires.

token = b64url(payload) "." b64url(HMAC-SHA256(key, payload));  key = HMAC-SHA256(JWT_SECRET, "mme-plivo-stream-ticket-v1")
(so rotating JWT_SECRET also invalidates every outstanding ticket). The alphabet contains no ';' or '=' (extraHeaders syntax).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

from config.settings import Settings

PURPOSE = b"mme-plivo-stream-ticket-v1"


class TicketError(ValueError):
    pass


@dataclass(frozen=True)
class StreamTicket:
    session_id: str
    tenant_id: str
    call_id: str          # provider (Plivo) call UUID
    expires: int


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _key(s: Settings) -> bytes:
    secret = s.jwt_secret.get_secret_value()
    if not secret:
        raise TicketError("JWT_SECRET is required to sign stream tickets")
    return hmac.new(secret.encode(), PURPOSE, hashlib.sha256).digest()


def mint(s: Settings, *, session_id: str, tenant_id: str, call_id: str) -> str:
    body = _b64(json.dumps({"s": session_id, "t": tenant_id, "c": call_id, "e": int(time.time()) + s.plivo_stream_ticket_ttl_s},
                           separators=(",", ":")).encode())
    return f"{body}.{_b64(hmac.new(_key(s), body.encode(), hashlib.sha256).digest())}"


def verify(s: Settings, token: str) -> StreamTicket:
    try:
        body, sig = token.split(".", 1)
        if len(token) > 1024:
            raise TicketError("ticket too long")
        if not hmac.compare_digest(sig, _b64(hmac.new(_key(s), body.encode(), hashlib.sha256).digest())):
            raise TicketError("bad signature")
        d = json.loads(_unb64(body))
        t = StreamTicket(str(d["s"]), str(d["t"]), str(d["c"]), int(d["e"]))
    except TicketError:
        raise
    except Exception:
        raise TicketError("malformed ticket") from None
    if t.expires < time.time():
        raise TicketError("expired ticket")
    return t


def from_extra_headers(extra: object) -> str:
    """Plivo's `extra_headers` is the `key1=value1;key2=value2` string from the Stream element."""
    if not isinstance(extra, str):
        return ""
    for part in extra.split(";"):
        k, _, v = part.partition("=")
        if k.strip() == "mme_ticket":
            return v.strip()
    return ""
