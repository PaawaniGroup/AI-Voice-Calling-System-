"""Structured JSON logging with redaction and request/call correlation."""
from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import time
from typing import Any

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="-")

_SENSITIVE_KEYS = re.compile(
    r"(pass(word)?|secret|token|authorization|api[-_]?key|auth[-_]?id|signature|private[-_]?key|cookie|mongo_uri)", re.I
)
_URI_CRED = re.compile(r"(\w+://)([^/@\s:]+):([^/@\s]+)@")
_PHONE = re.compile(r"\+?\d{8,15}")
# inline secrets in free text: password=..., token: ..., Bearer <jwt>, api_key=...
_INLINE_SECRET = re.compile(r"(?i)\b(pass(?:word|wd)?|secret|token|api[_-]?key|authorization|auth[_-]?token)\b(\s*[=:]\s*)(?:bearer\s+)?[^\s,;&\"']+")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")


def mask_phone(value: str) -> str:
    """+919876543210 -> +91*******10 (keeps country prefix and last 2)."""
    if not value or len(value) < 7:
        return value
    return value[:3] + "*" * (len(value) - 5) + value[-2:]


def redact(obj: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "<depth>"
    if isinstance(obj, dict):
        return {
            k: ("***" if _SENSITIVE_KEYS.search(str(k)) else redact(v, depth + 1)) for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [redact(v, depth + 1) for v in obj]
    if isinstance(obj, str):
        s = _URI_CRED.sub(r"\1\2:***@", obj)
        s = _BEARER.sub("Bearer ***", _INLINE_SECRET.sub(r"\1\2***", s))
        return _PHONE.sub(lambda m: mask_phone(m.group(0)), s) if len(s) < 400 else s[:400]
    return obj


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str, env: str):
        super().__init__()
        self.service, self.env = service, env

    def format(self, record: logging.LogRecord) -> str:
        base = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "service": self.service,
            "env": self.env,
            "logger": record.name,
            "request_id": request_id_var.get(),
            "correlation_id": correlation_id_var.get(),
            "msg": redact(record.getMessage()),
        }
        extra = getattr(record, "ctx", None)
        if extra:
            base.update(redact(extra))
        if record.exc_info:
            base["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(base, default=str)


FORCED_WARNING_LOGGERS = ("httpx", "httpcore", "uvicorn.access")


def setup_logging(level: str = "INFO", fmt: str = "json", service: str = "mme-telephony", env: str = "development") -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(
        JsonFormatter(service, env) if fmt == "json"
        else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root.addHandler(h)
    root.setLevel(level)
    for name in FORCED_WARNING_LOGGERS:       # whatever LOG_LEVEL is: HTTP client debug output carries request headers (the Authorization key) and sizes
        logging.getLogger(name).setLevel(logging.WARNING)


class RingHandler(logging.Handler):
    """Keeps the most recent WARNING+ records (redacted) for the dashboard's log view."""

    def __init__(self, size: int = 500):
        super().__init__(level=logging.WARNING)
        from collections import deque
        self.buf = deque(maxlen=size)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = redact(record.getMessage())
            if record.exc_info:
                msg += " | " + type(record.exc_info[1]).__name__
            self.buf.append({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)), "level": record.levelname,
                             "logger": record.name, "msg": msg, "request_id": request_id_var.get(), **redact(getattr(record, "ctx", None) or {})})
        except Exception:
            pass

    def snapshot(self, limit: int = 100, level: str | None = None, q: str | None = None) -> list[dict]:
        rows = [r for r in self.buf if (not level or r["level"] == level.upper()) and (not q or q.lower() in r["msg"].lower())]
        return rows[-limit:][::-1]


def event(logger: logging.Logger, msg: str, level: int = logging.INFO, **ctx: Any) -> None:
    """Log with structured context: event(log, "call.hangup", call_id=..., outcome=...)."""
    logger.log(level, msg, extra={"ctx": ctx})
