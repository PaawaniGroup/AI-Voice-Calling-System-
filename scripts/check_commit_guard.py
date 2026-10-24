#!/usr/bin/env python3
"""
scripts/check_commit_guard.py - pre-commit guard so voice recordings and audio-bearing notebooks are never committed by accident.

Rejects a commit that ADDS or MODIFIES
  1. an audio file (.wav .mp3 .ogg .flac .m4a) whose path is not allowlisted in .audio-allowlist (fnmatch patterns, one per line, '#' comments), or
  2. a .ipynb notebook whose staged content embeds audio: the text "data:audio", or an output mime type "audio/..." (Colab players embed generated speech this way).
It also catches files added with `git add -f` (that bypasses .gitignore, not this hook).
Already-tracked audio is not re-flagged unless it is modified; `--all` lists it (report only, exit 0 unless --strict).

  python scripts/check_commit_guard.py            # what the hook runs: checks the staged changes
  python scripts/check_commit_guard.py --all      # audit every tracked file
Install: git config core.hooksPath .githooks      (a per-clone setting: git cannot version it)
Bypass for a deliberate exception: add the exact path to .audio-allowlist and commit that change in the same commit.
"""
import fnmatch
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AUDIO_EXT = (".wav", ".mp3", ".ogg", ".flac", ".m4a")
NB_AUDIO = re.compile(rb"data:audio|\"audio/[a-z0-9.+-]+\"\s*:")


def load_allowlist(text: str) -> list:
    return [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]


def allowed(path: str, patterns) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)


def check(files, patterns):
    """files: iterable of (path, read_bytes callable). Returns a list of problem strings (empty = OK). Pure function: unit-tested without git."""
    problems = []
    for path, read in files:
        low = path.lower()
        if low.endswith(AUDIO_EXT) and not allowed(path, patterns):
            problems.append(f"{path}: audio file not in .audio-allowlist (voice recordings must not be committed)")
        elif low.endswith(".ipynb") and NB_AUDIO.search(read()):
            problems.append(f"{path}: notebook embeds audio (data:audio / audio/* output): clear its outputs before committing")
    return problems


def _git(*args) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True).stdout


def main(argv) -> int:
    al = ROOT / ".audio-allowlist"
    patterns = load_allowlist(al.read_text(encoding="utf-8")) if al.exists() else []
    if "--all" in argv:
        names = [n for n in _git("ls-files", "-z").decode("utf-8", "replace").split("\0") if n]
        files = [(n, (lambda n=n: (ROOT / n).read_bytes() if (ROOT / n).exists() else b"")) for n in names]
    else:
        names = [n for n in _git("diff", "--cached", "--name-only", "--diff-filter=AM", "-z").decode("utf-8", "replace").split("\0") if n]
        files = [(n, (lambda n=n: _git("show", ":" + n))) for n in names]       # the STAGED content, not the working copy
    problems = check(files, patterns)
    for p in problems:
        print("commit guard:", p, file=sys.stderr)
    if problems and "--all" not in argv:
        print("commit guard: refused. See scripts/check_commit_guard.py for how to allowlist a deliberate fixture.", file=sys.stderr)
        return 1
    if "--all" in argv:
        print(f"{len(names)} tracked files audited, {len(problems)} problem(s)" + ("" if not problems else " (report only)"))
        return 1 if problems and "--strict" in argv else 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
