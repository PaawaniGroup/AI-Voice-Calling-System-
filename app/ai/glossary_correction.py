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
    if not text or not isinstance(text, str):
        return text or ""

    result = text

    # Apply observed direct mappings
    for pattern, replacement in OBSERVED_PHRASE_MAPPINGS:
        result = pattern.sub(replacement, result)

    # Turn "bi si" into a hint instead of a blind rewrite
    result = BI_SI_PATTERN.sub(BI_SI_HINT, result)

    # Handle "eprod" as proposed unconfirmed
    if include_proposed:
        result = EPROD_PATTERN.sub(EPROD_HINT, result)

    return result
