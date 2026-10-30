"""Cached acknowledgement ("one moment") for the token-streaming pipeline (per-tenant flag `ack_enabled`, default OFF).

A few short, NEUTRAL phrases are generated once, stored OUTSIDE the repository as raw 8 kHz mu-law (setting `ai_ack_dir`) and played right after the
speech-to-text final when the first real clause is not ready within `ack_after_ms`. They are chosen before the question is understood, so a phrase must
never agree, disagree or confirm (no yes / no / ok): the manifest text of every clip is checked against a deny-list when the set is loaded and a set with
one offending phrase is refused as a whole. Missing, unreadable or malformed files never raise: the feature simply is not available (`load` returns
None and a reason code).

Directory layout (written by the operator after the voice jobs were approved; nothing in this repository generates the audio):
    manifest.json   {"clips": [{"file": "hi_1.ulaw", "lang": "hi", "text": "..."}, ...]}      `text` is read for the neutrality check only; it is never logged
    hi_1.ulaw ...   raw mu-law bytes, 8000 per second, between 0.3 s and 2.0 s long
At least two clips per language are needed (so that a phrase is never repeated twice in a row); a language with fewer is switched off.
"""
from __future__ import annotations

import json
import logging
import random
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from app.ai import codec

log = logging.getLogger("mme_telephony.ai.streaming")

MANIFEST = "manifest.json"
LANGS = ("hi", "en")
MIN_MS, MAX_MS = 300, 2000
_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SPLIT = re.compile(r"[\s.,;:!?।\"'()\[\]\-–—]+")

# Words that agree, disagree or confirm. Roman and Devanagari spellings; compared after `_norm` (lower case, nukta dropped, chandrabindu = anusvara).
DENY = frozenset(w for w in (
    "yes", "yeah", "yep", "yup", "no", "nope", "nah", "ok", "okay", "sure", "right", "correct", "exactly", "agreed", "true", "fine", "alright",
    "absolutely", "definitely", "certainly", "indeed", "course", "not", "never",
    "haan", "han", "haa", "ha", "haanji", "hanji", "ji", "nahi", "nahin", "nai", "na", "theek", "thik", "sahi", "bilkul", "achha", "acha", "accha",
    "zaroor", "zarur", "pakka",
    "हाँ", "हां", "हा", "जी", "जीं", "नहीं", "नही", "ना", "ठीक", "सही", "बिल्कुल", "बिलकुल", "अच्छा", "अच्छी", "ज़रूर", "जरूर", "पक्का", "सच",
) if w)


# The phrases proposed for the one-off generation (NOT generated yet: it needs the owner's APPROVED and a cap). Gender-neutral Hindi verbs, none in the deny-list
# (a test enforces that); each is about one second at the voice's pace.
PROPOSED = {"hi": ["एक सेकंड।", "एक पल।", "बस एक क्षण।"], "en": ["One moment.", "Just a second."]}


def _norm(tok: str) -> str:
    tok = unicodedata.normalize("NFC", tok).lower().replace("़", "").replace("ँ", "ं")
    return tok


_DENY_N = frozenset(_norm(w) for w in DENY)


def offending_words(text: str) -> list[str]:
    """The deny-listed tokens of a phrase (for the neutrality check only; never logged)."""
    return [t for t in (_norm(x) for x in _SPLIT.split(text or "")) if t and t in _DENY_N]


@dataclass(frozen=True)
class AckClip:
    name: str
    lang: str
    pcm8: bytes          # PCM16 at 8 kHz, ready for the playout
    ms: int


class AckSet:
    """The clips of one call (the object is per call: `pick` remembers the last clip played so that it is never repeated twice in a row)."""

    def __init__(self, clips: list[AckClip], rng: random.Random | None = None):
        self.by_lang: dict[str, list[AckClip]] = {}
        for c in clips:
            self.by_lang.setdefault(c.lang, []).append(c)
        self.by_lang = {k: v for k, v in self.by_lang.items() if len(v) >= 2}
        self._last: dict[str, str] = {}
        self._rng = rng or random.Random()
        self.played = 0

    @property
    def langs(self) -> list[str]:
        return sorted(self.by_lang)

    def pick(self, lang: str) -> AckClip | None:
        pool = self.by_lang.get(lang)
        if not pool:
            return None
        choices = [c for c in pool if c.name != self._last.get(lang)]
        clip = self._rng.choice(choices)
        self._last[lang] = clip.name
        self.played += 1
        return clip

    @classmethod
    def load(cls, directory: str | Path | None, rng: random.Random | None = None) -> tuple["AckSet | None", str]:
        """(set, reason). Never raises. `reason` is a short code ("ok", "no_dir", "no_manifest", "bad_manifest", "bad_clip", "not_neutral",
        "too_few_clips"), never a path or a phrase."""
        if not directory:
            return None, "no_dir"
        try:
            base = Path(directory)
            mf = base / MANIFEST
            if not mf.is_file():
                return None, "no_manifest"
            doc = json.loads(mf.read_text(encoding="utf-8"))
            items = doc.get("clips") if isinstance(doc, dict) else None
            if not isinstance(items, list) or not items:
                return None, "bad_manifest"
            clips: list[AckClip] = []
            for it in items:
                if not isinstance(it, dict):
                    return None, "bad_manifest"
                name, lang, text = it.get("file"), it.get("lang"), it.get("text")
                if not isinstance(name, str) or not _FILE_RE.match(name) or lang not in LANGS or not isinstance(text, str) or not text.strip():
                    return None, "bad_manifest"
                if offending_words(text):
                    return None, "not_neutral"                       # one phrase that could claim agreement refuses the whole set
                p = base / name
                if not p.is_file():
                    return None, "bad_clip"
                raw = p.read_bytes()
                ms = len(raw) * 1000 // codec.MULAW_RATE
                if not (MIN_MS <= ms <= MAX_MS):
                    return None, "bad_clip"
                clips.append(AckClip(name=name, lang=lang, pcm8=codec.mulaw_to_pcm16(raw), ms=ms))
        except (OSError, ValueError, UnicodeDecodeError, codec.CodecError):
            return None, "bad_manifest"
        s = cls(clips, rng)
        if not s.by_lang:
            return None, "too_few_clips"
        return s, "ok"
