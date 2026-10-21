"""Unit tests for the Plivo adapter pieces that need no server: codec, ticket, PlivoIO parsing/formatting, WS signature."""
import asyncio
import base64
import json
import struct
import time

import pytest
from fastapi import WebSocketDisconnect
from plivo.utils.signature_v3 import get_signature_v3

from app.ai import codec
from app.ai.ws import PLIVO_MAX_PLAY_S, PlivoIO
from app.providers import plivo as plivo_mod
from app.security import tickets
from tests.conftest import make_settings

S = make_settings(plivo_inbound_enabled=True, plivo_auth_token="tok-123", webhook_public_base_url="https://h.example.com")


# ------------------------------------------------------------------ codec
def test_mulaw_known_vectors_and_roundtrip():
    assert codec.pcm16_to_mulaw(b"\x00\x00") == b"\xff"            # G.711: linear 0 encodes to 0xFF
    assert codec.mulaw_to_pcm16(b"\xff") == b"\x00\x00"
    pcm = b"".join(struct.pack("<h", int(8000 * ((i % 40) / 20 - 1))) for i in range(800))
    back = codec.mulaw_to_pcm16(codec.pcm16_to_mulaw(pcm))
    assert len(back) == len(pcm) and max(abs(a - b) for a, b in zip(struct.unpack("<800h", pcm), struct.unpack("<800h", back))) < 400
    with pytest.raises(codec.CodecError):
        codec.pcm16_to_mulaw(b"\x01\x02\x03")


def test_resample_is_c_speed_and_proportional():
    pcm = b"\x10\x00" * 16000
    out = codec.resample_pcm16(pcm, 16000, 8000)
    assert abs(len(out) - len(pcm) // 2) <= 8 and codec.resample_pcm16(pcm, 8000, 8000) == pcm
    with pytest.raises(codec.CodecError):
        codec.resample_pcm16(pcm, 0, 8000)


@pytest.mark.parametrize("payload", [None, "", 5, "***", "AAA", base64.b64encode(b"\xff" * (codec.MAX_INBOUND_PAYLOAD + 1)).decode()])
def test_decode_media_payload_rejects_unusable_input(payload):
    with pytest.raises(codec.CodecError):
        codec.decode_media_payload(payload)


def test_decode_media_payload_accepts_20ms_chunk():
    assert codec.decode_media_payload(base64.b64encode(b"\xff" * 160).decode()) == b"\xff" * 160


# ------------------------------------------------------------------ ticket
def test_ticket_roundtrip_and_binding():
    t = tickets.mint(S, session_id="s1", tenant_id="t1", call_id="c-1")
    assert ";" not in t and "=" not in t  # safe inside Plivo's key=value;key=value extraHeaders syntax
    got = tickets.verify(S, t)
    assert (got.session_id, got.tenant_id, got.call_id) == ("s1", "t1", "c-1") and got.expires > time.time()
    assert tickets.from_extra_headers(f"foo=bar;mme_ticket={t};x=y") == t and tickets.from_extra_headers(None) == ""


@pytest.mark.parametrize("mutate", ["flip_sig", "flip_body", "truncate", "empty", "junk", "dots"])
def test_ticket_tampering_is_rejected(mutate):
    t = tickets.mint(S, session_id="s1", tenant_id="t1", call_id="c-1")
    body, sig = t.split(".")
    bad = {"flip_sig": f"{body}.{'A' * len(sig)}", "flip_body": f"{'B' + body[1:]}.{sig}", "truncate": t[:-5], "empty": "",
           "junk": "not-a-ticket", "dots": "a.b.c"}[mutate]
    with pytest.raises(tickets.TicketError):
        tickets.verify(S, bad)


def test_ticket_is_bound_to_the_secret_and_is_not_a_jwt():
    t = tickets.mint(S, session_id="s1", tenant_id="t1", call_id="c")
    other = make_settings(jwt_secret="a-completely-different-secret-" + "z" * 20, plivo_inbound_enabled=True, plivo_auth_token="x",
                          webhook_public_base_url="https://h.example.com")
    with pytest.raises(tickets.TicketError):
        tickets.verify(other, t)  # rotating JWT_SECRET invalidates outstanding tickets
    from app.security.auth import decode_token
    from app.core.errors import ApiError
    with pytest.raises(ApiError):
        decode_token(t, S)  # the ticket is useless as an API credential
    import jwt
    api_jwt = jwt.encode({"sub": "u", "tenant_id": "t1", "roles": ["telephony:user"], "exp": int(time.time()) + 60}, S.jwt_secret.get_secret_value(), "HS256")
    with pytest.raises(tickets.TicketError):
        tickets.verify(S, api_jwt)  # and an API JWT is useless as a ticket


def test_ticket_expiry(monkeypatch):
    t = tickets.mint(S, session_id="s1", tenant_id="t1", call_id="c")
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + S.plivo_stream_ticket_ttl_s + 5)
    with pytest.raises(tickets.TicketError, match="expired"):
        tickets.verify(S, t)


# ------------------------------------------------------------------ handshake signature
def _sig(uri, nonce="n1", token="tok-123"):
    s = get_signature_v3(token.encode(), uri.encode(), nonce.encode())
    return s.decode() if isinstance(s, bytes) else s


def test_ws_signature_accepts_wss_and_https_forms_only():
    wss = plivo_mod.stream_wss_url(S)
    assert wss == "wss://h.example.com/v1/providers/plivo/stream"
    assert plivo_mod.verify_ws_signature(S, nonce="n1", signature=_sig(wss))
    assert plivo_mod.verify_ws_signature(S, nonce="n1", signature=_sig(wss.replace("wss://", "https://")))
    assert plivo_mod.verify_ws_signature(S, nonce="n1", signature="junk," + _sig(wss))  # comma-separated list, like the SDK
    for bad in (_sig(wss, token="other"), _sig("wss://evil.example/v1/providers/plivo/stream"), _sig(wss + "x"), "", "AAAA"):
        assert not plivo_mod.verify_ws_signature(S, nonce="n1", signature=bad)
    assert not plivo_mod.verify_ws_signature(S, nonce="other-nonce", signature=_sig(wss))
    assert not plivo_mod.verify_ws_signature(S, nonce="", signature=_sig(wss))
    assert not plivo_mod.verify_ws_signature(S, nonce="n1", signature="x" * 600)
    assert plivo_mod.verify_ws_signature(S, nonce="n1", signature=_sig(wss + "?a=1"), query="a=1")  # query is part of the signed URL


def test_public_urls_come_only_from_configuration():
    assert plivo_mod.callback_url(S, "/v1/providers/plivo/answer") == "https://h.example.com/v1/providers/plivo/answer"
    assert plivo_mod.callback_url(S, "/p", "a=1") == "https://h.example.com/p?a=1"
    with pytest.raises(ValueError):
        plivo_mod.stream_wss_url(make_settings(webhook_public_base_url="http://plain.example.com"))


# ------------------------------------------------------------------ PlivoIO (adapter)
class FakeWS:
    def __init__(self, incoming=()):
        self.incoming = list(incoming)
        self.sent: list = []

    async def receive(self):
        if not self.incoming:
            await asyncio.sleep(10)
        item = self.incoming.pop(0)
        if item == "DISCONNECT":
            return {"type": "websocket.disconnect"}
        return {"type": "websocket.receive", **item}

    async def send_text(self, s):
        self.sent.append(json.loads(s))


def txt(o):
    return {"text": o if isinstance(o, str) else json.dumps(o)}


def media(chunk, payload=None, track="inbound"):
    return txt({"event": "media", "media": {"track": track, "chunk": chunk, "timestamp": str(chunk * 20),
                                            "payload": payload if payload is not None else base64.b64encode(b"\xff" * 160).decode()}})


async def test_next_item_parses_every_documented_inbound_event():
    ws = FakeWS([media(1), media(1), media(0), media(2, "###"), media(3, track="outbound"), txt({"event": "dtmf", "dtmf": {"digit": "7"}}),
                 txt({"event": "playedStream", "name": "utt-1"}), txt({"event": "clearedAudio"}), txt("not json"), txt([1, 2]),
                 {"bytes": b"\x00"}, txt({"event": "start"}), txt({"event": "stop"})])
    io = PlivoIO(ws, "stream-1")
    got = [await io.next_item(0.5) for _ in range(13)]
    assert got[0][0] == "audio" and got[0][1] == b"\x00\x00" * 160            # mu-law 0xFF -> PCM 0, 160 samples = 20 ms at 8 kHz
    assert [g[0] for g in got[1:3]] == ["skip", "skip"]                         # duplicate and out-of-order chunk numbers dropped
    assert got[3][0] == "skip" and got[4][0] == "skip"                          # bad base64, wrong track
    assert got[5] == ("skip", None) and io.counters["dtmf"] == 1                # DTMF counted, never surfaced
    assert io.played == ["utt-1"] and io.counters["cleared"] == 1 and io.counters["bad"] >= 2
    assert got[11] == ("end", "duplicate_start") and got[12] == ("end", "stream_stopped")
    assert io.counters["dup"] == 2 and io.counters["media"] == 1


async def test_next_item_idle_disconnect_and_oversize():
    io = PlivoIO(FakeWS([]), "s")
    assert await io.next_item(0.05) == ("idle", 50)
    with pytest.raises(WebSocketDisconnect):
        await PlivoIO(FakeWS(["DISCONNECT"]), "s").next_item(0.5)
    big = PlivoIO(FakeWS([txt("x" * 70000)]), "s")
    assert await big.next_item(0.5) == ("end", "message_too_large") and big.counters["oversize"] == 1


async def test_play_formats_playaudio_chunks_then_checkpoint_and_interrupt_clears():
    ws = FakeWS()
    io = PlivoIO(ws, "stream-9")
    pcm16k = b"\x00\x10" * 16000 * 3            # 3 s at 16 kHz
    started = []
    await io.play(pcm16k, 16000, "hi", 4, started.append)
    plays, cp = ws.sent[:-1], ws.sent[-1]
    assert started == [pytest.approx(3.0, abs=0.01)]
    assert len(plays) == 3 and all(p["event"] == "playAudio" for p in plays)
    for p in plays:
        assert p["media"]["contentType"] == "audio/x-mulaw" and p["media"]["sampleRate"] == 8000
        assert len(base64.b64decode(p["media"]["payload"], validate=True)) <= 8000
    assert sum(len(base64.b64decode(p["media"]["payload"])) for p in plays) == pytest.approx(24000, abs=8)
    assert cp == {"event": "checkpoint", "streamId": "stream-9", "name": "utt-4"} and io.sent_checkpoints == ["utt-4"]
    ws.sent.clear()
    await io.interrupt()
    assert ws.sent == [{"event": "clearAudio", "streamId": "stream-9"}]


async def test_play_truncates_to_the_provider_queue_limit():
    ws = FakeWS()
    io = PlivoIO(ws, "s")
    await io.play(b"\x00\x10" * 8000 * (PLIVO_MAX_PLAY_S + 10), 8000, "en", 1, lambda d: None)
    total = sum(len(base64.b64decode(m["media"]["payload"])) for m in ws.sent if m["event"] == "playAudio")
    assert total == PLIVO_MAX_PLAY_S * 8000


# ------------------------------------------------------------------ configuration
def test_inbound_enabled_requires_its_prerequisites():
    from pydantic import ValidationError
    base = dict(plivo_inbound_enabled=True, plivo_auth_token="t", webhook_public_base_url="https://x.example")
    make_settings(**base)
    for drop, needle in (("plivo_auth_token", "PLIVO_AUTH_TOKEN"), ("webhook_public_base_url", "WEBHOOK_PUBLIC_BASE_URL")):
        with pytest.raises(ValidationError, match=needle):
            make_settings(**{k: v for k, v in base.items() if k != drop})
    with pytest.raises(ValidationError, match="https"):
        make_settings(**{**base, "webhook_public_base_url": "http://x.example"})
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        make_settings(**base, jwt_secret="")
    assert make_settings().plivo_inbound_enabled is False  # off by default: nothing changes until an operator opts in
    s = make_settings(**base)
    assert any("PLIVO_AUTH_ID" in w for w in s.problems()[1])  # REST hangup needs the Auth ID: a warning, not a silent gap
    assert s.plivo_stream_ticket_ttl_s == 120 and s.plivo_stream_connect_s == 20
    assert s.redacted()["plivo_auth_token"] == "***set***"  # secrets stay masked in diagnostics
