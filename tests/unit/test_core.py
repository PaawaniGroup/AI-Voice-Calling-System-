import asyncio
import hashlib
import hmac

import pytest

from app.core import phone
from app.core.errors import Unprocessable
from app.core.logging import redact
from app.events import webhooks as wh
from app.telephony import dialstrings as ds
from app.telephony.esl import backoff_delay, parse_plain_event, read_message
from tests.conftest import make_settings


def test_phone_normalisation():
    assert phone.normalize("098765 43210") == "+919876543210"
    assert phone.normalize("+91 98765-43210") == "+919876543210"
    for bad in ["abc", "+0123", "12", "+91 9876; hangup", "9" * 40, "", "+91(98)76 x"]:
        with pytest.raises(Unprocessable):
            phone.normalize(bad)


def test_destination_policy():
    p = phone.DestinationPolicy(allowed_countries=["IN"], deny_prefixes=["+911860"], caller_ids=["+911140000000"],
                                transfer_destinations=["+919999999999", "+9188*"])
    p.check_destination("+919876543210")
    with pytest.raises(Unprocessable):
        p.check_destination("+918601234567".replace("+9186", "+91186") + "0")  # +911860...
    with pytest.raises(Unprocessable):
        p.check_destination("+14155550123")  # US not allowed
    p.check_caller_id("+911140000000")
    with pytest.raises(Unprocessable):
        p.check_caller_id("+919876543210")
    p.check_transfer("+919999999999")
    p.check_transfer("+918812345678")
    with pytest.raises(Unprocessable):
        p.check_transfer("+919876543210")


def test_dial_strings_reject_injection():
    ok = ds.originate_command(fs_uuid="11111111-2222-3333-4444-555555555555", call_id="call_abc", tenant_id="t1",
                              caller_id="+911140000000", dial="sofia/gateway/gw/+919876543210", ring_timeout=30, max_duration=60)
    assert ok.startswith("originate {") and ok.endswith("&park()")
    for bad in [dict(call_id="x,y=1"), dict(tenant_id="t1\napi"), dict(caller_id="+911140000000}")]:
        kw = dict(fs_uuid="11111111-2222-3333-4444-555555555555", call_id="c", tenant_id="t", caller_id="+911140000000",
                  dial="sofia/gateway/gw/+91", ring_timeout=30, max_duration=60) | bad
        with pytest.raises(ValueError):
            ds.originate_command(**kw)
    with pytest.raises(ValueError):
        ds.render("sofia/gateway/{gateway}/{destination}", "gw", "+91;system rm")
    with pytest.raises(ValueError):
        ds.render("sofia/gateway/{gateway}/{destination}", "gw bad", "+919876543210")
    with pytest.raises(ValueError):
        ds.kill_command("not-a-uuid", "NORMAL_CLEARING")
    with pytest.raises(ValueError):
        ds.setvar_dial_command("11111111-2222-3333-4444-555555555555", "loopback/9; api status")
    with pytest.raises(ValueError):
        ds.record_start_command("11111111-2222-3333-4444-555555555555", "/tmp/../etc/x.wav", 10)


def test_webhook_signature_roundtrip():
    secret = wh.derive_secret("m" * 40, "whe_1", 1)
    assert secret != wh.derive_secret("m" * 40, "whe_1", 2)
    sig = wh.sign(secret, 1700000000, b"{}")
    expect = hmac.new(secret.encode(), b"1700000000.{}", hashlib.sha256).hexdigest()
    assert sig == f"v1={expect}"


@pytest.mark.parametrize("url", ["http://127.0.0.1/x", "https://localhost/x", "https://169.254.169.254/latest",
                                 "https://10.0.0.5/x", "https://[::1]/x", "ftp://example.com/x", "https://u:p@example.com/"])
async def test_ssrf_blocked(url):
    s = make_settings(webhook_allow_private=False, webhook_allow_http=False)
    with pytest.raises(Unprocessable):
        await wh.validate_destination(url, s)


def test_redaction():
    r = redact({"password": "p", "Authorization": "Bearer x", "n": "call +919876543210 ok",
                "u": "mongodb://user:secret@host/db", "ok": 1})
    assert r["password"] == "***" and r["Authorization"] == "***"
    assert "9876543210" not in r["n"] and "secret" not in r["u"]


def test_backoff_bounded():
    for a in range(30):
        d = backoff_delay(a, 0.5, 30, 0.25)
        assert 0 <= d <= 30 * 1.25
    assert backoff_delay(0, 1, 30, 0) == 1 and backoff_delay(10, 1, 30, 0) == 30


def test_parse_event_and_body():
    ev = parse_plain_event(b"Event-Name: BACKGROUND_JOB\nJob-UUID: abc\nContent-Length: 6\n\n-ERR X\n")
    assert ev["Event-Name"] == "BACKGROUND_JOB" and ev["_body"].startswith("-ERR")
    assert parse_plain_event(b"garbage without colon")  == {}


async def test_read_message_limits():
    r = asyncio.StreamReader()
    r.feed_data(b"Content-Type: text/event-plain\nContent-Length: 999999999\n\n")
    from app.telephony.esl import EslProtocolError
    with pytest.raises(EslProtocolError):
        await read_message(r)
    r2 = asyncio.StreamReader()
    r2.feed_data(b"not a header\n\n")
    with pytest.raises(EslProtocolError):
        await read_message(r2)
