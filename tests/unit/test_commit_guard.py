# -*- coding: utf-8 -*-
"""Pre-commit guard against audio files and audio-bearing notebooks (scripts/check_commit_guard.py, .gitignore)."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import check_commit_guard as G  # noqa: E402


def files(**kw):
    return [(p, (lambda b=b: b)) for p, b in kw.items()]


def test_audio_rejected_unless_allowlisted():
    f = [("Utkarsh_voice.wav", lambda: b""), ("docs/a.MP3", lambda: b""), ("x/b.ogg", lambda: b""), ("c.flac", lambda: b""), ("d.m4a", lambda: b""), ("readme.md", lambda: b"")]
    assert len(G.check(f, [])) == 5
    assert G.check(f, ["Utkarsh_voice.wav", "docs/*.MP3", "x/*.ogg", "c.flac", "d.m4a"]) == []
    fx = [("tests/fixtures/audio/tone.wav", lambda: b"")]
    assert G.check(fx, ["tests/fixtures/audio/*.wav"]) == [] and len(G.check(fx, ["other/*.wav"])) == 1


def test_notebook_with_embedded_audio_rejected():
    html = {"cells": [{"outputs": [{"data": {"text/html": ['<audio src="data:audio/wav;base64,UklGR"></audio>']}}]}]}
    mime = {"cells": [{"outputs": [{"data": {"audio/wav": "UklGR"}}]}]}
    clean = {"cells": [{"outputs": [{"data": {"text/plain": ["hello"]}}]}]}
    assert len(G.check(files(**{"a.ipynb": json.dumps(html).encode()}), [])) == 1
    assert len(G.check(files(**{"b.ipynb": json.dumps(mime).encode()}), [])) == 1
    assert G.check(files(**{"c.ipynb": json.dumps(clean).encode()}), []) == []
    assert G.check(files(**{"notes.txt": b"data:audio"}), []) == []             # only notebooks are scanned for embedded audio


def test_allowlist_parser_ignores_comments_and_blanks():
    assert G.load_allowlist("# c\n\n  a/*.wav \n#b\n") == ["a/*.wav"]
    assert G.load_allowlist((ROOT / ".audio-allowlist").read_text(encoding="utf-8")) == []        # nothing is allowlisted today


def test_gitignore_covers_audio_and_the_colab_notebook():
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for pat in ("*.wav", "*.mp3", "*.ogg", "*.flac", "*.m4a", "MME_Chatterbox_V3_TEST_COLAB_(3).ipynb", "Utkarsh_voice.wav", "Utkarsh_voice_ref12s_24k.wav"):
        assert pat in lines, pat


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_hook_end_to_end_in_a_throwaway_repo(tmp_path):
    repo = tmp_path / "r"
    (repo / "scripts").mkdir(parents=True)
    (repo / ".githooks").mkdir()
    shutil.copy(ROOT / "scripts" / "check_commit_guard.py", repo / "scripts")
    shutil.copy(ROOT / ".githooks" / "pre-commit", repo / ".githooks")
    (repo / ".githooks" / "pre-commit").chmod(0o755)
    (repo / ".audio-allowlist").write_text("# none\n", encoding="utf-8")

    def git(*a, check=True):
        return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=repo, capture_output=True, text=True, check=check)

    git("init", "-q")
    git("config", "core.hooksPath", ".githooks")
    (repo / "ok.txt").write_text("hi", encoding="utf-8")
    git("add", "ok.txt")
    assert git("commit", "-m", "ok", check=False).returncode == 0
    (repo / "voice.wav").write_bytes(b"RIFF")
    git("add", "-f", "voice.wav")                                                  # even a forced add is stopped by the hook
    r = git("commit", "-m", "bad", check=False)
    assert r.returncode != 0 and "audio file not in .audio-allowlist" in r.stderr
    git("reset", "-q", "voice.wav")
    (repo / "n.ipynb").write_text(json.dumps({"cells": [{"outputs": [{"data": {"text/html": ["data:audio/wav;base64,AAA"]}}]}]}), encoding="utf-8")
    git("add", "n.ipynb")
    r = git("commit", "-m", "nb", check=False)
    assert r.returncode != 0 and "notebook embeds audio" in r.stderr
