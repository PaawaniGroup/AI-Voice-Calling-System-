"""Conservative, measurable mixed-script normalization. Never translates, rewrites words or invents text.

Operations (each counted in the returned report):
  * Unicode NFC, whitespace collapse, strip.
  * Hindi sentence terminator: '.' directly after a Devanagari word -> danda.
  * Protected-term casing: an exact case-insensitive whole-word match of a protected term (e.g. 'btech') is restored to
    its canonical spelling ('BTech'). A different word is never replaced.
Roman-script Hindi (Hinglish typed in Latin) is NOT transliterated here: doing so safely needs a verified lexicon.
The raw text is always returned alongside, so any such gap is measurable rather than hidden.
"""
from __future__ import annotations

import re
import unicodedata

DEVA = re.compile(r"[ऀ-ॿ]")
LATIN = re.compile(r"[A-Za-z]")
_DEVA_DOT = re.compile(r"(?<=[ऀ-ॿ])\s*\.(?=\s|$)")


def script_profile(text: str) -> dict:
    d, l = len(DEVA.findall(text)), len(LATIN.findall(text))
    return {"devanagari_chars": d, "latin_chars": l, "mixed": d > 0 and l > 0}


def normalize(raw: str, protected_terms=None):
    ops = {"nfc": 0, "whitespace": 0, "danda": 0, "protected_case": 0}
    t = unicodedata.normalize("NFC", raw)
    ops["nfc"] = int(t != raw)
    t2 = re.sub(r"\s+", " ", t).strip()
    ops["whitespace"] = int(t2 != t)
    t = t2
    t, ops["danda"] = _DEVA_DOT.subn("।", t)
    for term in protected_terms or []:
        pat = re.compile(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", re.IGNORECASE)
        changed = 0

        def _fix(m, term=term):
            nonlocal changed
            if m.group(0) != term:
                changed += 1
            return term

        t = pat.sub(_fix, t)
        ops["protected_case"] += changed
    return t, {"operations": ops, "script_profile": script_profile(t), "changed": t != raw}
