"""Clock-skew tolerance for token verification (MME_TEL_JWT_LEEWAY_S).

Why it exists: on a host whose wall clock steps (this WSL2 dev machine flips by about 52 s), a token issued a moment after a step has an
`iat` in the FUTURE relative to the next reading, and PyJWT rejects it (ImmatureSignatureError). Measured: 4 of 4 tokens issued in the
'ahead' state were rejected seconds later, and ~15 % of dashboard e2e runs hit it as 'Session expired'. The default stays 0 (strict).

These tests use a FIXED clock inside PyJWT so they are deterministic on a host with an unstable clock.
"""
from datetime import datetime, timezone

import jwt
import pytest
from pydantic import ValidationError

from app.core.errors import Unauthorized
from app.security.auth import decode_token
from tests.conftest import JWT_SECRET, make_settings

NOW = 1_800_000_000


@pytest.fixture(autouse=True)
def fixed_pyjwt_clock(monkeypatch):
    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(NOW, tz=tz or timezone.utc)
    monkeypatch.setattr(jwt.api_jwt, "datetime", Fixed)


def tok(iat=0, exp=3600, secret=JWT_SECRET, alg="HS256"):
    return jwt.encode({"sub": "u", "tenant_id": "t1", "roles": ["telephony:viewer"], "iat": NOW + iat, "exp": NOW + exp}, secret, algorithm=alg)


def accepted(token, **kw):
    try:
        decode_token(token, make_settings(**kw))
        return True
    except Unauthorized:
        return False


def test_default_is_strict():
    assert make_settings().jwt_leeway_s == 0
    assert accepted(tok())
    assert not accepted(tok(iat=52))            # issued "in the future": rejected, as before
    assert not accepted(tok(iat=-7200, exp=-30))  # expired 30 s ago: rejected, as before


def test_leeway_absorbs_a_clock_step_in_both_directions_but_only_up_to_its_size():
    assert accepted(tok(iat=52), jwt_leeway_s=60)           # the measured ~52 s step
    assert accepted(tok(iat=-7200, exp=-30), jwt_leeway_s=60)  # expired 30 s ago, inside the tolerance
    assert not accepted(tok(iat=100), jwt_leeway_s=60)      # beyond the tolerance
    assert not accepted(tok(iat=-7200, exp=-100), jwt_leeway_s=60)  # long expired is still expired: leeway is bounded
    assert not accepted(tok(iat=-7200, exp=-301), jwt_leeway_s=300)


def test_leeway_never_helps_a_forged_or_unsigned_token():
    for lw in (0, 60, 300):
        assert not accepted(tok(secret="x" * 40), jwt_leeway_s=lw)  # wrong key
        none = jwt.encode({"sub": "u", "tenant_id": "t1", "roles": ["telephony:admin"], "exp": NOW + 600}, None, algorithm="none")
        assert not accepted(none, jwt_leeway_s=lw)               # alg=none
        assert not accepted("garbage", jwt_leeway_s=lw)
        assert not accepted(tok(alg="HS512"), jwt_leeway_s=lw)  # algorithm is pinned


def test_leeway_is_bounded_by_configuration():
    for bad in (-1, 301):
        with pytest.raises(ValidationError):
            make_settings(jwt_leeway_s=bad)
    assert make_settings(jwt_leeway_s=300).jwt_leeway_s == 300
