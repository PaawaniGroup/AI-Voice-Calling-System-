"""No test fixture (or any other file in the repository) may look like a real RunPod key: `rpa_` followed by 40 or more characters, unless it starts with `rpa_FAKE`."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REAL_SHAPE = re.compile(r"rpa_(?!FAKE)[A-Za-z0-9]{40,}")
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "node_modules"}
TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".sh", ".ps1", ".html", ".js", ".ts", ".env", ".example", ".lock", ".ipynb"}


def scan(text: str) -> list:
    return REAL_SHAPE.findall(text)


def test_the_detector_tells_real_shapes_from_obvious_fakes():
    real = "rpa_" + "A1b2C3d4E5" * 5
    assert scan(f"key={real}") == [real]
    assert scan("rpa_" + "FAKE" * 11 + "12") == [] and scan("rpa_FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE0000") == []
    assert scan("rpa_short") == [] and scan("rpa_" + "a" * 39) == [] and len(scan("rpa_" + "a" * 40)) == 1


def test_no_file_in_the_repository_has_the_shape_of_a_real_key():
    found = []
    for p in ROOT.rglob("*"):
        if not p.is_file() or SKIP_DIRS & set(p.relative_to(ROOT).parts) or (p.suffix.lower() not in TEXT_SUFFIXES and p.name != ".env.example"):
            continue
        if p.name == ".env" or p.name.startswith(".env.") and p.name != ".env.example":   # local secrets files are ignored by git and are not the repository's content
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if scan(text):
            found.append(str(p.relative_to(ROOT)))   # the path only: never print the match
    assert not found, f"real-looking RunPod key shape in: {found}. Replace it with an obvious fake starting with rpa_FAKE and rotate the key if it was real."
