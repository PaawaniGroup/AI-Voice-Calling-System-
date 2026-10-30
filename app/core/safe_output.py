"""Output that can be pasted anywhere: operator tools print through these helpers, so no part of a phone number, Plivo Auth ID/token,
API key, bearer token, stream ticket or URL secret ever reaches a terminal, a log or a chat.

`hide_number` shows nothing of the number (not even the last digits: a handful of digits plus the country code identify a person).
`scrub` is the last line of defence applied to everything a tool prints: anything that looks like a secret or a number is replaced,
even if some code path forgot to hide it.
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

_SCRUBBERS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=\-]{8,}"), "Bearer [secret]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}(?:\.[A-Za-z0-9_\-]*)?"), "[jwt]"),             # JWT / signed stream ticket
    (re.compile(r"\brpa_[A-Za-z0-9]{8,}"), "[api-key]"),                                                      # RunPod keys
    (re.compile(r"\bMA[A-Z0-9]{18}\b"), "[plivo-auth-id]"),                                                   # Plivo Auth ID
    (re.compile(r"\+\d[\d\- ]{5,}\d"), "[number]"),                                                           # +91 98765 43210
    (re.compile(r"(?<![A-Za-z0-9._\-])\d{7,}(?![A-Za-z0-9])"), "[number]"),                                   # 9198765432 (7+ contiguous digits)
    (re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_\-]{32,}(?![A-Za-z0-9])"), "[secret]"),                          # long opaque tokens
]


def hide_number(_n: object = None) -> str:
    """A phone number is never shown, not even partially."""
    return "[number]"


def scrub(text: str) -> str:
    for pat, repl in _SCRUBBERS:
        text = pat.sub(repl, text)
    return text


def safe_url(u: str | None) -> str | None:
    """scheme://host/path with the query and fragment dropped and any id-like path segment replaced (account ids, app ids, tokens)."""
    if not u:
        return u
    p = urlparse(u)
    segs = ["[id]" if (re.fullmatch(r"[A-Za-z0-9_\-]{12,}", s) or s.isdigit()) else s for s in p.path.split("/")]
    return f"{p.scheme}://{p.netloc}{'/'.join(segs)}" + ("?[hidden]" if p.query else "")


def safe_json(obj: Any) -> str:
    return scrub(json.dumps(obj, indent=2, default=str, ensure_ascii=False))
