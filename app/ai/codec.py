"""Telephony audio codecs used by the Plivo media adapter.

Plivo streams G.711 mu-law at 8 kHz (`audio/x-mulaw;rate=8000`). The AI pipeline works on signed 16-bit little-endian
mono PCM, so frames are converted here at the edge and nowhere else. Uses the stdlib `audioop` (removed in Python 3.13:
install `audioop-lts` there; see requirements.txt).
"""
from __future__ import annotations

import base64
import binascii

try:  # pragma: no cover - import guard
    import audioop
except ModuleNotFoundError:  # Python >= 3.13 without audioop-lts
    audioop = None  # type: ignore[assignment]

MULAW_RATE = 8000
MAX_INBOUND_PAYLOAD = 4000   # bytes of mu-law per Plivo media event (Plivo sends ~160 = 20 ms); anything larger is rejected


class CodecError(ValueError):
    pass


def _need() -> None:
    if audioop is None:
        raise CodecError("audioop unavailable (pip install audioop-lts on Python 3.13+)")


def mulaw_to_pcm16(data: bytes) -> bytes:
    _need()
    return audioop.ulaw2lin(data, 2)


def pcm16_to_mulaw(pcm: bytes) -> bytes:
    _need()
    if len(pcm) % 2:
        raise CodecError("PCM16 data must have an even number of bytes")
    return audioop.lin2ulaw(pcm, 2)


def resample_pcm16(pcm: bytes, src: int, dst: int) -> bytes:
    """Rate conversion in C (audioop.ratecv): a multi-second TTS reply must not stall the event loop in pure Python."""
    if src == dst or not pcm:
        return pcm
    _need()
    if src <= 0 or dst <= 0 or len(pcm) % 2:
        raise CodecError("invalid sample rate or odd PCM length")
    return audioop.ratecv(pcm, 2, 1, src, dst, None)[0]


def decode_media_payload(payload: object) -> bytes:
    """Strict base64 -> mu-law bytes for an inbound `media.payload`. Raises CodecError for anything unusable."""
    if not isinstance(payload, str) or not payload:
        raise CodecError("empty payload")
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise CodecError("payload is not valid base64") from None
    if not raw or len(raw) > MAX_INBOUND_PAYLOAD:
        raise CodecError("payload size out of range")
    return raw


def chunks(data: bytes, size: int):
    for i in range(0, len(data), size):
        yield data[i:i + size]
