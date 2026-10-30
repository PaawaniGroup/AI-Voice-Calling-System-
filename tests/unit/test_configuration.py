import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from config.settings import Settings, load_policy_file

ROOT = Path(__file__).resolve().parents[2]
PROD = dict(env="production", provider="freeswitch_gateway", mongo_uri="mongodb://u:p@h/db", jwt_secret="j" * 40,
            webhook_signing_master="w" * 40, esl_password="e" * 12, docs_enabled=False)


def S(**kw):
    return Settings(_env_file=None, **kw)


def test_defaults_are_safe_and_valid():
    s = S()
    assert s.env == "development" and s.esl_host == "127.0.0.1" and s.bind_host == "127.0.0.1"
    assert not s.real_calls_enabled and not s.recording_enabled and not s.ai_enabled and not s.webrtc_enabled
    assert s.workers == 1


def test_production_valid_config_accepted():
    s = S(**PROD)
    errs, _ = s.problems()
    assert errs == []


@pytest.mark.parametrize("missing,needle", [("mongo_uri", "MONGO_URI"), ("jwt_secret", "JWT_SECRET"),
                                            ("webhook_signing_master", "WEBHOOK_SIGNING_MASTER"), ("esl_password", "ESL_PASSWORD")])
def test_production_requires_secrets(missing, needle):
    cfg = {k: v for k, v in PROD.items() if k != missing}
    with pytest.raises(ValidationError) as e:
        S(**cfg)
    assert needle in str(e.value)


def test_short_jwt_secret_rejected_in_production():
    with pytest.raises(ValidationError):
        S(**{**PROD, "jwt_secret": "short"})


def test_mock_provider_forbidden_in_production_but_allowed_in_dev():
    with pytest.raises(ValidationError, match="mock"):
        S(**{**PROD, "provider": "mock"})
    S(provider="mock")


@pytest.mark.parametrize("kw", [
    dict(bind_port=0), dict(bind_port=70000), dict(esl_port=-1), dict(ring_timeout_s=0), dict(max_active_global=0),
    dict(max_body_bytes=10), dict(esl_command_timeout_s=-1), dict(esl_reconnect_jitter=2),
    dict(rtp_port_start=30000, rtp_port_end=20000), dict(esl_reconnect_initial_s=10, esl_reconnect_max_s=1),
    dict(max_active_per_tenant=50, max_active_global=10), dict(ring_timeout_s=100, max_call_duration_s=50),
    dict(workers=2), dict(log_level="LOUD"), dict(provider="carrier-pigeon"),
])
def test_invalid_values_rejected(kw):
    with pytest.raises(ValidationError):
        S(**kw)


def test_esl_must_be_private_unless_explicit():
    with pytest.raises(ValidationError, match="ESL"):
        S(esl_host="8.8.8.8")
    assert S(esl_host="10.1.2.3").esl_host == "10.1.2.3"
    assert S(esl_host="127.0.0.1").esl_port == 8021
    S(esl_host="8.8.8.8", esl_allow_non_private=True)  # explicit override, warns


def test_ai_is_off_by_default_and_webrtc_cannot_be_enabled():
    s = S()
    assert not s.ai_enabled and s.ai_media_bridge == "none"
    assert any("media bridge" in w.lower() for w in S(ai_enabled=True).problems()[1])
    with pytest.raises(ValidationError, match="WEBRTC"):
        S(webrtc_enabled=True)


def test_recording_requires_consent_and_storage():
    with pytest.raises(ValidationError, match="recording"):
        S(recording_enabled=True)
    with pytest.raises(ValidationError, match="recording"):
        S(recording_enabled=True, recording_consent_mode="announce")
    S(recording_enabled=True, recording_consent_mode="announce", storage_backend="local")
    with pytest.raises(ValidationError, match="S3"):
        S(storage_backend="s3")


def test_plivo_requires_credentials_and_https_base():
    with pytest.raises(ValidationError, match="PLIVO"):
        S(provider="plivo_api")
    with pytest.raises(ValidationError, match="https"):
        S(provider="plivo_api", plivo_auth_id="a", plivo_auth_token="b")
    S(provider="plivo_api", plivo_auth_id="a", plivo_auth_token="b", webhook_public_base_url="https://x.example")


def test_webhook_relaxations_forbidden_in_production():
    with pytest.raises(ValidationError):
        S(**{**PROD, "webhook_allow_private": True})


def test_redacted_hides_secrets():
    s = S(**PROD)
    dump = str(s.redacted())
    for secret in ("mongodb://u:p@h/db", "j" * 40, "w" * 40, "e" * 12):
        assert secret not in dump
    assert "***set***" in dump


def test_secret_not_in_repr_or_error():
    s = S(**PROD)
    assert "j" * 40 not in repr(s)
    with pytest.raises(ValidationError) as e:
        S(**{**PROD, "jwt_secret": "supersecretvalue"})
    assert "supersecretvalue" not in str(e.value)


def test_policy_file_loads():
    assert isinstance(load_policy_file(), dict)
    assert load_policy_file(ROOT / "nonexistent.yaml") == {}


def run_validate(env: dict, *args, cwd=None):
    """cwd: Settings reads `.env` from the CURRENT directory, so a hermetic caller passes an empty temp directory (the developer's real .env is then ignored)."""
    import os
    e = {k: v for k, v in os.environ.items() if not k.startswith("MME_TEL_")}
    e.update(env)
    return subprocess.run([sys.executable, "-I", str(ROOT / "scripts" / "validate_config.py"), *args], capture_output=True,
                          text=True, env=e, cwd=str(cwd or ROOT), timeout=60)


def test_validate_config_script_exit_codes(tmp_path):
    """Hermetic: no MME_TEL_* variable from the caller's environment and no .env file (cwd is an empty temp dir), so a developer's local .env cannot change the result."""
    assert not (tmp_path / ".env").exists()
    ok = run_validate({"PYTHONPATH": str(ROOT)}, cwd=tmp_path)
    assert ok.returncode == 0 and "config OK" in ok.stdout
    strict = run_validate({"PYTHONPATH": str(ROOT)}, "--strict", cwd=tmp_path)
    assert strict.returncode == 1  # dev defaults produce warnings
    bad = run_validate({"PYTHONPATH": str(ROOT), "MME_TEL_ENV": "production", "MME_TEL_JWT_SECRET": "topsecret-value-123"}, cwd=tmp_path)
    assert bad.returncode == 1 and "MONGO_URI" in bad.stdout + bad.stderr
    assert "topsecret-value-123" not in bad.stdout + bad.stderr  # secrets never echoed
    garbage = run_validate({"PYTHONPATH": str(ROOT), "MME_TEL_BIND_PORT": "notanumber"}, cwd=tmp_path)
    assert garbage.returncode == 1 and "BIND_PORT" in garbage.stdout
