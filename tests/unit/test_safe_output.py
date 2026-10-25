"""Operator-tool output never carries any part of a number, Auth ID, token, key or ticket."""
from app.core.safe_output import hide_number, safe_json, safe_url, scrub

AUTH_ID = "MAYTJHMWM4YTKTNWVKYY"            # shape of a Plivo Auth ID (not a real one)
TOKEN = "ODI2MTI3ZWUtYmNkYS00YzYzLTY4NDMtMjdlNmNm"
TICKET = "eyJzIjoic2Vzc2lvbiIsInQiOiJ0MSJ9.Zm9vYmFyYmF6cXV4.c2lnbmF0dXJl"
KEY = "rpa_FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE12"   # obviously fake; same shape (rpa_ + 46 characters) as a RunPod key


def test_a_number_is_never_shown_even_partially():
    assert hide_number("+919876543210") == "[number]" and hide_number() == "[number]"
    for raw in ("+919876543210", "919876543210", "+91 98765 43210", "+91-98765-43210", "call from 9198765432 now"):
        out = scrub(raw)
        for frag in ("9876", "8765", "6543", "3210", "98765", "43210"):
            assert frag not in out, (raw, out)


def test_secrets_are_scrubbed_wherever_they_appear():
    text = f"auth={AUTH_ID} token={TOKEN} ticket={TICKET} key={KEY} Authorization: Bearer abcdefgh12345678 hdr=eyJhbGciOi.eyJzdWIiOiIx.sig"
    out = scrub(text)
    for secret in (AUTH_ID, TOKEN, TICKET, KEY, "abcdefgh12345678", "eyJhbGciOi", "ODI2MTI3"):
        assert secret not in out, secret
    assert "[plivo-auth-id]" in out and "[api-key]" in out and "[jwt]" in out


def test_ordinary_diagnostic_text_survives():
    s = "turn 3 latency_ms=2450.5 state=answered 2026-10-24T15:26:56+05:30 commit 1832e47 max_duration_s=180 workers idle=1"
    assert scrub(s) == s


def test_safe_url_keeps_host_and_route_but_drops_ids_and_queries():
    u = f"https://x.ngrok-free.dev/v1/Account/{AUTH_ID}/Application/17587006602647171/?token={TOKEN}"
    out = safe_url(u)
    assert out.startswith("https://x.ngrok-free.dev/v1/Account/[id]/Application/[id]/") and AUTH_ID not in out and TOKEN not in out and "17587" not in out
    assert safe_url("https://x.ngrok-free.dev/v1/providers/plivo/answer") == "https://x.ngrok-free.dev/v1/providers/plivo/answer"
    assert safe_url(None) is None


def test_safe_json_scrubs_values_nested_anywhere():
    out = safe_json({"a": [{"caller": "+919876543210", "k": {"key": KEY}}], "auth": AUTH_ID, "ok": 1})
    assert "9876543210" not in out and KEY not in out and AUTH_ID not in out and '"ok": 1' in out
