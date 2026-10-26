"""Unit tests for scripts/start_service_with_keys.sh (WSL Bash script)."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
import pytest

SCRIPT_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "start_service_with_keys.sh"


@pytest.fixture
def key_dir(tmp_path):
    d = tmp_path / "keys"
    d.mkdir()
    stt = d / "stt.txt"
    stt.write_text("rpa_STT_SEC_SECRET_TESTKEY_9876\n")
    brain = d / "brain.txt"
    brain.write_text("rpa_BRAIN_SEC_SECRET_TESTKEY_5432\n")
    tts = d / "tts.txt"
    tts.write_text("rpa_TTS_SEC_SECRET_TESTKEY_1098\n")
    return {"dir": d, "stt": stt, "brain": brain, "tts": tts}


def run_bash(args: list[str]) -> subprocess.CompletedProcess[str]:
    # Determine if running under WSL or Windows bash
    bash_bin = shutil.which("bash")
    if not bash_bin:
        pytest.skip("bash executable not available")
    return subprocess.run(
        ["bash", str(SCRIPT_PATH)] + args,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_start_service_sh_missing_args():
    res = run_bash([])
    assert res.returncode == 1
    assert "Missing required key file arguments" in res.stderr


def test_start_service_sh_missing_file(key_dir):
    res = run_bash([
        "--stt-key-file", str(key_dir["dir"] / "nonexistent.txt"),
        "--brain-key-file", str(key_dir["brain"]),
        "--tts-key-file", str(key_dir["tts"]),
        "--dry-run"
    ])
    assert res.returncode == 1
    assert "STT key file does not exist" in res.stderr


def test_start_service_sh_empty_file(key_dir):
    empty = key_dir["dir"] / "empty.txt"
    empty.write_text("   \n")
    res = run_bash([
        "--stt-key-file", str(empty),
        "--brain-key-file", str(key_dir["brain"]),
        "--tts-key-file", str(key_dir["tts"]),
        "--dry-run"
    ])
    assert res.returncode == 1
    assert "STT key file is empty" in res.stderr


def test_start_service_sh_dry_run_success_and_masked(key_dir):
    res = run_bash([
        "--stt-key-file", str(key_dir["stt"]),
        "--brain-key-file", str(key_dir["brain"]),
        "--tts-key-file", str(key_dir["tts"]),
        "--dry-run"
    ])
    assert res.returncode == 0
    assert "[DryRun] Validation passed" in res.stdout
    assert "...9876" in res.stdout
    assert "...5432" in res.stdout
    assert "...1098" in res.stdout

    # Never echo raw secret tokens
    assert "SECRET_TESTKEY" not in res.stdout
    assert "SECRET_TESTKEY" not in res.stderr
