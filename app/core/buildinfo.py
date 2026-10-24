"""Which build is this process running? Resolved once at startup from the git checkout the code was loaded from (or MME_TEL_BUILD_COMMIT
when there is no checkout), so `plivo_testcall.py status` can tell the operator the RUNNING commit instead of guessing."""
from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _git(*args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


@lru_cache(maxsize=1)
def build_info() -> dict:
    """{"commit": short hash | "unknown", "dirty": bool | None, "committed_at": ISO | None, "source": "git" | "env" | "none"}"""
    commit = _git("rev-parse", "--short", "HEAD")
    if commit:
        porcelain = _git("status", "--porcelain", "--untracked-files=no")
        return {"commit": commit, "dirty": bool(porcelain) if porcelain is not None else None,
                "committed_at": _git("log", "-1", "--format=%cI"), "source": "git"}
    env = os.environ.get("MME_TEL_BUILD_COMMIT", "").strip()
    if env:
        return {"commit": env[:12], "dirty": None, "committed_at": None, "source": "env"}
    return {"commit": "unknown", "dirty": None, "committed_at": None, "source": "none"}
