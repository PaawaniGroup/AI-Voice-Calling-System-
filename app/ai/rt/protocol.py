"""Wire protocol of the persistent real-time transport. STANDARD LIBRARY ONLY: this file is copied next to the worker servers (deploy/rt_workers/).

One WebSocket per call and service. Text frames are JSON objects with a `type`; every request carries an integer `seq` chosen by the client, and every answer to it
carries the same `seq`. Binary frames are `>I seq` + payload (audio). No frame ever carries a key, and workers log lengths and timings only.

client -> server                                         server -> client
  hello    {v, service, call, token}                       ready   {v}                       (or error {code} and close)
  ping     {n}                                             pong    {n}
  warmup   {seq}                      (no model work)        warm    {seq}
  synth    {seq, text, voice, format:"ulaw8k"}             audio_start {seq, queue_ms, format, rate}, then binary frames, then audio_end {seq, exec_ms, audio_s, bytes}
  transcribe {seq, rate, lang, prompt?, nbytes}            final   {seq, text, language, queue_ms, exec_ms}      (after binary pcm16 frames and `end`)
  end      {seq}
  cancel   {seq}                                           cancelled {seq}       (the work is stopped; a closed socket stops it too)
                                                           error {seq?, code}
The token is `call.exp.sig` with sig = HMAC-SHA256(secret, "call.exp"), short-lived, bound to the call id.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import struct
import time

VERSION = 1
SERVICES = ("stt", "tts")
MAX_MESSAGE_CHARS = 4000
MAX_TEXT_CHARS = 600
MAX_AUDIO_BYTES = 4_000_000
ULAW_RATE = 8000
ULAW_FRAME_BYTES = 1600            # 200 ms of 8 kHz mu-law
PCM_FRAME_BYTES = 16000            # 500 ms of 8 kHz PCM16
TERMINAL_TYPES = ("audio_end", "final", "error", "cancelled", "warm")
_FRAME = struct.Struct(">I")


class ProtocolError(ValueError):
    pass


def sign(secret: bytes, payload: str) -> str:
    return hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def make_token(secret: bytes, call_id: str, ttl_s: float = 120.0, now: float | None = None) -> str:
    exp = int((time.time() if now is None else now) + ttl_s)
    payload = f"{call_id}.{exp}"
    return f"{payload}.{sign(secret, payload)}"


def verify_token(secret: bytes, token: str, call_id: str, now: float | None = None) -> str | None:
    """None = valid; otherwise the reason code (never the token)."""
    try:
        payload, sig = token.rsplit(".", 1)
        call, exp = payload.rsplit(".", 1)
        exp_i = int(exp)
    except (ValueError, AttributeError):
        return "malformed_token"
    if not hmac.compare_digest(sig, sign(secret, payload)):
        return "bad_signature"
    if call != call_id:
        return "call_mismatch"
    if (time.time() if now is None else now) > exp_i:
        return "expired_token"
    return None


def dumps(msg: dict) -> str:
    return json.dumps(msg, separators=(",", ":"), ensure_ascii=False)


def parse(text: str) -> dict:
    if not isinstance(text, str) or len(text) > MAX_MESSAGE_CHARS:
        raise ProtocolError("message too large or not text")
    try:
        d = json.loads(text)
    except ValueError:
        raise ProtocolError("not JSON") from None
    if not isinstance(d, dict) or not isinstance(d.get("type"), str):
        raise ProtocolError("no type")
    return d


def pack_frame(seq: int, payload: bytes) -> bytes:
    return _FRAME.pack(seq) + payload


def unpack_frame(data: bytes) -> tuple[int, bytes]:
    if len(data) < _FRAME.size:
        raise ProtocolError("short frame")
    return _FRAME.unpack(data[:_FRAME.size])[0], data[_FRAME.size:]
