"""Unit tests for scripts/start_service_with_keys.ps1.

Tests that can be validated offline without starting the uvicorn server:
1. Missing key file fails with non-zero exit code.
2. Empty key file fails with non-zero exit code.
3. -DryRun with valid files succeeds (exit 0) and masks keys to last 4 chars only.
4. Raw key values are never leaked in stdout or stderr.
"""
import os
import sys
import subprocess
import tempfile
import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell required")

SCRIPT_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "start_service_with_keys.ps1")
)


def run_ps1(args: list[str]) -> subprocess.CompletedProcess:
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", SCRIPT_PATH] + args
    return subprocess.run(cmd, capture_output=True, text=True)


def test_missing_key_file_refuses_to_start(tmp_path):
    stt_file = tmp_path / "stt.key"
    stt_file.write_text("fake-key-1234")
    brain_file = tmp_path / "brain.key"
    brain_file.write_text("fake-key-5678")
    missing_tts = tmp_path / "nonexistent.key"

    res = run_ps1([
        "-SttKeyFile", str(stt_file),
        "-BrainKeyFile", str(brain_file),
        "-TtsKeyFile", str(missing_tts),
        "-DryRun"
    ])
    assert res.returncode != 0
    assert "TTS key file does not exist" in (res.stderr + res.stdout)


def test_empty_key_file_refuses_to_start(tmp_path):
    stt_file = tmp_path / "stt.key"
    stt_file.write_text("fake-key-1234")
    brain_file = tmp_path / "brain.key"
    brain_file.write_text("   \n  ")  # empty / whitespace only
    tts_file = tmp_path / "tts.key"
    tts_file.write_text("fake-key-9012")

    res = run_ps1([
        "-SttKeyFile", str(stt_file),
        "-BrainKeyFile", str(brain_file),
        "-TtsKeyFile", str(tts_file),
        "-DryRun"
    ])
    assert res.returncode != 0
    assert "Brain key file is empty" in (res.stderr + res.stdout)


def test_dry_run_with_valid_keys_masks_to_last_4(tmp_path):
    secret_stt = "super-secret-stt-token-9999"
    secret_brain = "super-secret-brain-token-8888"
    secret_tts = "super-secret-tts-token-7777"

    stt_file = tmp_path / "stt.key"
    stt_file.write_text(secret_stt)
    brain_file = tmp_path / "brain.key"
    brain_file.write_text(secret_brain)
    tts_file = tmp_path / "tts.key"
    tts_file.write_text(secret_tts)

    res = run_ps1([
        "-SttKeyFile", str(stt_file),
        "-BrainKeyFile", str(brain_file),
        "-TtsKeyFile", str(tts_file),
        "-DryRun"
    ])
    assert res.returncode == 0
    out = res.stdout

    # Asserts masked last 4 characters are present
    assert "...9999" in out
    assert "...8888" in out
    assert "...7777" in out

    # Asserts full secret is NEVER logged or echoed
    assert secret_stt not in out
    assert secret_brain not in out
    assert secret_tts not in out
    assert "[DryRun] Validation passed" in out
