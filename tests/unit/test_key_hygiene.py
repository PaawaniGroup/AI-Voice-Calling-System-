"""Key Hygiene Test: Ensure no real RunPod API keys (rpa_ + 30+ chars) are stored in code.

Scans:
- scripts/
- app/
- tests/

Rules:
1. Any hardcoded test key fixture MUST start with 'rpa_FAKE'.
2. Regex patterns containing 'rpa_' are allowed if part of re.compile or a raw regex string r"...".
3. All other strings matching rpa_[A-Za-z0-9_-]{30,} cause this test to fail.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

# Pattern for RunPod key: rpa_ followed by 30 or more alphanumeric/underscore/dash characters
RPA_KEY_PATTERN = re.compile(r"\b(rpa_[A-Za-z0-9_-]{30,})\b")


def test_no_real_runpod_keys_in_repo():
    """Scan scripts/, app/, and tests/ to ensure zero hardcoded real rpa_ keys exist."""
    scan_dirs = [ROOT / "scripts", ROOT / "app", ROOT / "tests"]
    violations = []

    for s_dir in scan_dirs:
        if not s_dir.is_dir():
            continue
        for p in s_dir.rglob("*.py"):
            text = p.read_text(encoding="utf-8", errors="replace")
            for line_no, line in enumerate(text.splitlines(), 1):
                # Ignore lines that are defining regex patterns
                if "re.compile" in line or "re.Pattern" in line or "regex" in line.lower() or 'r"\\b' in line or "r'\\b" in line:
                    continue
                # Ignore comments explaining regexes
                if line.strip().startswith("#"):
                    continue

                for match in RPA_KEY_PATTERN.finditer(line):
                    found_key = match.group(1)
                    if not found_key.startswith("rpa_FAKE"):
                        rel_path = str(p.relative_to(ROOT))
                        masked_repr = found_key[:8] + "..." + found_key[-4:]
                        violations.append((f"{rel_path}:{line_no}", masked_repr))

    assert not violations, (
        f"Found hardcoded RunPod keys:\n"
        + "\n".join(f"  {loc}: {k}" for loc, k in violations)
    )


def test_key_hygiene_scanner_detects_real_and_allows_fake():
    """Unit test for the regex pattern itself."""
    fake_fixture = "rpa_FAKE_123456789012345678901234567890"
    # Construct synthetic non-fake key shape to test regex detection without triggering repo scan
    synthetic_non_fake = "rpa_" + "SYNTHETIC_KEY_NOT_STARTING_WITH_FAKE_12345"

    m_fake = RPA_KEY_PATTERN.findall(f"key = '{fake_fixture}'")
    assert m_fake == [fake_fixture]
    assert m_fake[0].startswith("rpa_FAKE")

    m_synthetic = RPA_KEY_PATTERN.findall(f"key = '{synthetic_non_fake}'")
    assert m_synthetic == [synthetic_non_fake]
    assert not m_synthetic[0].startswith("rpa_FAKE")
