"""Counselling glossary and phonetic correction for STT output.

Sits between STT transcription and downstream consumers (RAG and LLM Brain).
Corrects observed phonetic mis-transcriptions of degrees, entrance exams,
and counselling keywords without altering conversational Hindi or English words.
"""
from __future__ import annotations

import re

# 1. Active observed mis-spellings from supervised calls (verified live)
OBSERVED_PHRASE_MAPPINGS: list[tuple[re.Pattern, str]] = [
    # beetaik -> B.Tech
    (re.compile(r"\b(?:beetaik|bee\s+taik|bee\s+tek|btek)\b", re.IGNORECASE), "B.Tech"),
    # je ee men -> JEE Main
    (re.compile(r"\b(?:je\s+ee\s+men|jee\s+mane|je\s+ee\s+main)\b", re.IGNORECASE), "JEE Main"),
    # parsentaail -> percentile
    (re.compile(r"\b(?:parsentaail|parsentaile|percentail)\b", re.IGNORECASE), "percentile"),
    # nyoo jeelaind -> New Zealand
    (re.compile(r"\b(?:nyoo\s+jeelaind|new\s+zeeland|nyu\s+ziland)\b", re.IGNORECASE), "New Zealand"),
]

# 2. Ambiguous terms: turned into a hint instead of a blind destructive rewrite
# "bi si" could mean BCA degree or BC (Backward Class) reservation category
BI_SI_PATTERN = re.compile(r"\bbi\s+si\b", re.IGNORECASE)
BI_SI_HINT = "bi si (possible: BCA or BC category)"

# 3. Proposed unconfirmed terms
# "eprod" was observed as an unknown term; abroad is proposed but unconfirmed
EPROD_PATTERN = re.compile(r"\beprod\b", re.IGNORECASE)
EPROD_HINT = "eprod (proposed unconfirmed: abroad)"

# Words and particles that must NEVER be rewritten
NEVER_REWRITE: frozenset[str] = frozenset({
    # Common Hindi particles and words
    "men", "mein", "main", "me", "mai", "se", "ko", "ki", "ka", "ke",
    "hai", "hain", "ho", "hoon", "tha", "thi", "the",
    "karna", "karne", "kare", "karo", "karenge", "karti", "karta",
    "baare", "batao", "bataiye", "puchna", "chahiye", "milega", "mil",
    "kaise", "kya", "kyun", "kab", "kahan", "kitna", "kitni",
    "kisi", "kisko", "koi", "kuch", "sab", "apna", "apni", "aap", "tum",
    "aur", "ya", "par", "lekin", "bhi", "hi", "toh", "to",
    "acha", "theek", "shukriya", "dhanyawad", "namaste", "hello",
    # English terms that must remain untouched
    "fee", "fees", "free", "cost", "date", "form", "seat", "seats",
    "host", "home", "call", "care", "line", "rank", "mark", "marks",
    "new", "now", "not", "no", "yes", "ok", "okay"
})


def correct_glossary(text: str, include_proposed: bool = True) -> str:
    """Normalises known STT mis-transcriptions to canonical counselling terminology.

    - Rewrites observed clear terms: 'beetaik' -> 'B.Tech', 'je ee men' -> 'JEE Main',
      'parsentaail' -> 'percentile', 'nyoo jeelaind' -> 'New Zealand'.
    - Contextual hint for 'bi si' -> 'bi si (possible: BCA or BC category)'.
    - Contextual hint for 'eprod' -> 'eprod (proposed unconfirmed: abroad)' if include_proposed.
    - Strictly preserves conversational words like 'men', 'kisi', 'bhi', 'fee', 'free', 'host', 'new'.
    """
    return correct_glossary_counted(text, include_proposed)[0]


def correct_glossary_counted(text: str, include_proposed: bool = True) -> tuple[str, int]:
    """Same result as `correct_glossary`, plus HOW MANY substitutions were made (a number: the trace records it, never the words)."""
    if not text or not isinstance(text, str):
        return text or "", 0

    result, total = text, 0

    # Apply observed direct mappings
    for pattern, replacement in OBSERVED_PHRASE_MAPPINGS:
        result, n = pattern.subn(replacement, result)
        total += n

    # Turn "bi si" into a hint instead of a blind rewrite
    result, n = BI_SI_PATTERN.subn(BI_SI_HINT, result)
    total += n

    # Handle "eprod" as proposed unconfirmed
    if include_proposed:
        result, n = EPROD_PATTERN.subn(EPROD_HINT, result)
        total += n

    return result, total


# ====================================================================================================================================================
# Sound-alikes WITH a context guard, a per-tenant file of pairs kept OUTSIDE the repository, and the Brain hint block (flag `glossary_hint`, default OFF).
# The existing always-on mappings above are unchanged. Everything here is applied only when the tenant flag `glossary_correction` is on.
# ====================================================================================================================================================
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("mme_telephony.ai.glossary")

# An education cue: a guarded sound-alike is rewritten only when the utterance carries one of these (Latin cues on word boundaries, Devanagari cues as substrings),
# or when it is a very short course-name question (at most SHORT_Q_MAX_WORDS words with a question marker).
_LATIN_CUES = ("admission", "fees", "fee", "course", "courses", "college", "eligibility", "branch", "cutoff", "cut off", "cut-off", "ke liye")
_DEVA_CUES = ("एडमिशन", "फीस", "कोर्स", "कॉलेज", "के लिए", "ब्रांच", "कटऑफ", "कट ऑफ", "योग्यता", "पात्रता")
_CUE_RE = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(c) for c in _LATIN_CUES) + r")(?!\w)|" + "|".join(re.escape(c) for c in _DEVA_CUES), re.IGNORECASE)
_QUESTION_RE = re.compile(r"\?|(?<!\w)(?:kya|what|kaise|kaun|kitna|kitni|how)(?!\w)|क्या|कैसे|कौन|कितना|कितनी", re.IGNORECASE)
SHORT_Q_MAX_WORDS = 4
MAX_PAIRS, MAX_TERM_CHARS = 500, 40


def has_education_cue(text: str) -> bool:
    return bool(_CUE_RE.search(text or ""))


def is_short_course_question(text: str) -> bool:
    words = (text or "").split()
    return 0 < len(words) <= SHORT_Q_MAX_WORDS and bool(_QUESTION_RE.search(text))


def context_ok(text: str) -> bool:
    return has_education_cue(text) or is_short_course_question(text)


def _heard_pattern(heard: str) -> re.Pattern:
    """A literal sound-alike, case-insensitive, any run of spaces matches any run of whitespace; Latin ends sit on word boundaries."""
    body = r"\s+".join(re.escape(w) for w in heard.split())
    pre = r"(?<!\w)" if heard[:1].isascii() else ""
    post = r"(?!\w)" if heard[-1:].isascii() else ""
    return re.compile(pre + body + post, re.IGNORECASE)


@dataclass(frozen=True)
class Entry:
    pattern: re.Pattern
    said: str
    guard: str = "cue"          # "cue": only with an education cue / a very short course question; "always": unguarded (for terms that are never ordinary words)
    origin: str = "builtin"     # "builtin" or "file" (the operator's pair): at equal length a file pair wins over a built-in entry


# Built-in sound-alikes of B.Tech (a phone line writes "edm", "e d m", "bitech" ...). All guarded.
_BTECH_SOUNDALIKES = ("edm", "e d m", "bitech", "b tech", "beetech", "bee tech", "be tech", "bi tech", "btech", "bitek", "beetek")
_BTECH_DEVA = (re.compile(r"ई\s*डी\s*एम"),)
BUILTIN_ENTRIES: tuple[Entry, ...] = tuple(Entry(_heard_pattern(h), "B.Tech") for h in _BTECH_SOUNDALIKES) + tuple(Entry(p, "B.Tech") for p in _BTECH_DEVA)


# The always-on mappings above as (pattern, replacement) pairs, for the single-pass longest-match resolution in Glossary.correct (include_proposed: True, as in the default).
ALWAYS_ON: tuple = tuple(OBSERVED_PHRASE_MAPPINGS) + ((BI_SI_PATTERN, BI_SI_HINT), (EPROD_PATTERN, EPROD_HINT))


def apply_glossary(conv, text: str) -> tuple[str, int]:
    """THE ONE place both turn paths (streaming and legacy) correct the caller's text: the call's Glossary when it has one, else the always-on mappings only."""
    gloss = getattr(conv, "glossary", None)
    if gloss is None:
        return correct_glossary_counted(text)
    out, n = gloss.correct(text)
    from app.ai import turn_trace
    turn_trace.note_turn("glossary_overrides", getattr(gloss, "last_overrides", 0))   # a number for the evidence line: file pairs that replaced a built-in rewrite of the same span
    return out, n


class Glossary:
    """The sound-alike entries of ONE call: the built-in set plus the pairs of the operator's file for this tenant. `correct` returns (text, substitutions)."""

    def __init__(self, entries: tuple[Entry, ...] = BUILTIN_ENTRIES):
        self.entries = tuple(entries)
        self.last_overrides = 0

    def correct(self, text: str) -> tuple[str, int]:
        """ONE pass over the ORIGINAL text. Candidates: the guarded entries (built-in and the operator's file: only when the utterance has an education cue or is a very short
        course question; `always` entries unguarded) and the always-on mappings. Overlaps are resolved by LONGEST MATCH, whatever the order of the file or of the lists: the longer,
        more specific form wins over a shorter one; at equal length the operator's file pairs win over the built-in entries, which win over the always-on mappings, then the earlier
        entry. The replacements are applied together (a replacement is never scanned again). `last_overrides` = how many chosen file pairs replaced a DIFFERENT built-in rewrite of the
        same span (a number for the evidence line)."""
        self.last_overrides = 0
        if not text or not isinstance(text, str):
            return text or "", 0
        guarded_ok = context_ok(text)
        cands = []                                                      # (start, end, replacement, priority, order): priority 0 file pair, 1 built-in entry, 2 always-on mapping
        for i, e in enumerate(self.entries):
            if e.guard != "always" and not guarded_ok:
                continue
            prio = 0 if e.origin == "file" else 1
            cands += [(m.start(), m.end(), e.said, prio, i) for m in e.pattern.finditer(text) if m.end() > m.start()]
        for j, (pat, rep) in enumerate(ALWAYS_ON):
            cands += [(m.start(), m.end(), rep, 2, j) for m in pat.finditer(text) if m.end() > m.start()]
        chosen: list[tuple] = []
        for c in sorted(cands, key=lambda c: (-(c[1] - c[0]), c[3], c[0], c[4])):
            if all(c[1] <= k[0] or c[0] >= k[1] for k in chosen):
                chosen.append(c)
        self.last_overrides = sum(1 for c in chosen if c[3] == 0 and any(k[3] == 1 and (k[0], k[1]) == (c[0], c[1]) and k[2] != c[2] for k in cands))
        out = text
        for s, e_, rep, _, _ in sorted(chosen, key=lambda c: -c[0]):
            out = out[:s] + rep + out[e_:]
        return out, len(chosen)

    @classmethod
    def load(cls, path: "str | Path | None", tenant_id: str = "") -> "tuple[Glossary, str]":
        """Built-in entries plus the operator's file. File format (UTF-8 text, OUTSIDE every repository, one pair per line, tab separated):
            said <TAB> heard [<TAB> guard [<TAB> tenant_id]]          lines starting with # and blank lines are ignored
        `said` is what the caller said (the canonical term); `heard` is what the recogniser wrote; guard is `cue` (default) or `always`; a tenant_id restricts the pair to that tenant.
        Never raises. The reason is a code ("ok", "no_path", "no_file", "unreadable", "bad_lines:N"): never a path or a term."""
        if not path:
            return cls(), "no_path"
        try:
            p = Path(path)
            if not p.is_file():
                return cls(), "no_file"
            lines = p.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return cls(), "unreadable"
        extra, bad = [], 0
        protected = {w.lower() for w in NEVER_REWRITE}
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            cols = [c.strip() for c in raw.split("\t")]
            if len(cols) < 2 or len(extra) >= MAX_PAIRS:
                bad += 1
                continue
            said, heard = cols[0], cols[1]
            guard = (cols[2] if len(cols) > 2 and cols[2] else "cue").lower()
            only = cols[3] if len(cols) > 3 else ""
            if (not said or not heard or len(said) > MAX_TERM_CHARS or len(heard) > MAX_TERM_CHARS or len(heard.replace(" ", "")) < 2 or guard not in ("cue", "always")
                    or heard.lower() in protected or heard.lower() == said.lower()):
                bad += 1                                                  # too short, too long, a protected everyday word, or malformed: refused, never rewritten
                continue
            if only and only != tenant_id:
                continue
            extra.append(Entry(_heard_pattern(heard), said, guard, "file"))
        return cls(BUILTIN_ENTRIES + tuple(extra)), ("ok" if not bad else f"bad_lines:{bad}")


# Brain hint block (tenant flag `glossary_hint`, default OFF): APPENDED to the system message of a call, like the ramp rule. It does not repeat the strict-grounding rules; it adds
# only what they lack (the recogniser's known mistakes, ONE short confirming question, no guessing). It works with or without the OPENING RULE of stream_ramp and with or without strict_grounding.
CONFIRM_HINT_RULE = (
    "\n\nSPEECH RECOGNISER NOTE: the caller's words come from a phone speech recogniser that often mishears course names (for example edm or bitech mean B.Tech; "
    "bi si e may mean BCA). If a word does not fit the question, ask ONE short confirming question in the caller's language and do not answer about a guessed course "
    "until it is confirmed; a reply that is a confirming question still starts with the opening phrase when there is an OPENING RULE. "
    "Never state fees, cut-offs or dates unless they come from the provided knowledge."
)


def apply_confirm_hint(conv) -> bool:
    """Append the hint to the conversation's system message (once). Returns True when it was added."""
    msgs = getattr(conv, "messages", None)
    if not msgs or msgs[0].get("role") != "system":
        return False
    if CONFIRM_HINT_RULE in msgs[0].get("content", ""):
        return False
    msgs[0]["content"] = msgs[0]["content"] + CONFIRM_HINT_RULE
    return True
