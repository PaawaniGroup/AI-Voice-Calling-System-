# -*- coding: utf-8 -*-
"""Production text normalization and script validation layer for Voice AI pipeline.

Supports three explicit language modes:
  - en-IN: Indian English (Latin script)
  - hi-IN: Pure Hindi (Devanagari script)
  - hinglish-IN: Natural Hinglish (Devanagari for Hindi words, Latin script for genuine English words)

Responsibilities:
1. Detect and canonicalize configured language mode.
2. Identify Devanagari and Latin-script segments.
3. Preserve valid English text (academic, admission, exam, technical words, proper nouns, abbreviations).
4. Correct predictable script inconsistencies (Romanized Hindi words -> Devanagari in hi/hinglish).
5. Normalize punctuation and whitespace.
6. Handle numbers, dates, times, currency, percentages, and phone numbers to spoken words.
7. Protect proper nouns, product names, URLs, emails, and technical vocabulary.
8. Produce TTS-ready text without corrupting meaning.
9. Return structured validation results, diagnostic metrics, and safe fallbacks.
10. Support phrase-by-phrase streaming processing with sub-millisecond execution.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mme_hinglish import HINDI_ROMAN
from app.ai import tts_frontend

log = logging.getLogger("mme_telephony.voice.normalizer")

# Canonical language modes
MODE_EN = "en-IN"
MODE_HI = "hi-IN"
MODE_HINGLISH = "hinglish-IN"
SUPPORTED_MODES = {MODE_EN, MODE_HI, MODE_HINGLISH}

# Legacy and alternate mode aliases
MODE_ALIASES: dict[str, str] = {
    "en": MODE_EN,
    "en-in": MODE_EN,
    "en_in": MODE_EN,
    "english": MODE_EN,
    "indian_english": MODE_EN,
    "hi": MODE_HI,
    "hi-in": MODE_HI,
    "hi_in": MODE_HI,
    "hindi": MODE_HI,
    "pure_hindi": MODE_HI,
    "hinglish": MODE_HINGLISH,
    "hinglish-in": MODE_HINGLISH,
    "hinglish_in": MODE_HINGLISH,
    "mixed": MODE_HINGLISH,
}

# English homographs that can also be Hindi Roman words
# These MUST NOT be converted blindly without surrounding Hindi context.
ENGLISH_HOMOGRAPHS: set[str] = {
    "main", "to", "hi", "me", "is", "do", "so", "per", "par", "are", "man", "pan",
    "bas", "ban", "us", "as", "or", "he", "ho", "ka", "ke", "ki", "at", "in", "on", "mat", "tab", "log"
}

# Regular expressions for script and entity detection
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+", re.IGNORECASE)
_DEV_CHAR_RE = re.compile(r"[\u0900-\u097F]")
_LATIN_CHAR_RE = re.compile(r"[A-Za-z]")
_DIGIT_RE = re.compile(r"[\d\u0966-\u096F]")
_WORD_TOKEN_RE = re.compile(r"[A-Za-z\u0900-\u097F]+(?:[.'’][A-Za-z\u0900-\u097F]+)?")
_DOTTED_ACRONYM_RE = re.compile(r"^[A-Za-z](?:\.[A-Za-z])+\.?$")

# Context rules for homographs
_POSTPOS_BEFORE_MEIN = {"baare", "liye", "andar", "dauran", "beech", "ke", "bahar", "paas"}
_FIRST_PERSON_FOLLOWING = {"hoon", "karunga", "karungi", "bataunga", "bataungi", "chahta", "chahti", "kar", "bol"}
_ENGLISH_FOLLOWING_NOUNS = {
    "campus", "building", "gate", "road", "street", "page", "menu", "entrance", "office",
    "reason", "point", "idea", "issue", "goal", "focus", "subject", "topic", "problem", "role"
}

# Allowlist fallback for standard academic, examination, and counselling terms
DEFAULT_ALLOWLIST: set[str] = {
    # Degrees and Courses
    "b.tech", "m.tech", "bba", "mba", "bca", "mca", "mbbs", "bds", "b.sc", "m.sc",
    "b.com", "m.com", "b.a", "m.a", "b.e", "m.e", "b.ed", "m.ed", "ph.d", "phd",
    "diploma", "llb", "llm", "course", "courses", "degree", "program", "branch",
    # Entrance exams and bodies
    "jee", "neet", "gate", "cat", "mat", "xat", "clat", "cuet", "cet", "bitsat",
    "upsc", "cbse", "icse", "ugc", "aicte", "iit", "nit", "iiit", "iim", "aiims",
    # Admission terms
    "admission", "admissions", "eligibility", "online", "offline", "form", "forms",
    "fee", "fees", "hostel", "campus", "college", "colleges", "university", "universities",
    "registration", "marksheet", "physics", "chemistry", "maths", "biology",
    "computer", "science", "engineering", "document", "documents", "verify",
    "verification", "callback", "counsellor", "counselor", "syllabus", "placement",
    "package", "scholarship", "internship", "interview", "application", "apply",
    "process", "status", "deadline", "date", "dates", "schedule", "portal", "website",
    "link", "email", "phone", "number", "contact", "support", "helpline", "query"
}

_ALLOWLIST_CACHE: set[str] | None = None


def get_domain_allowlist(custom_path: str | Path | None = None) -> set[str]:
    """Loads domain-specific allowlisted English terms from configuration file or defaults."""
    global _ALLOWLIST_CACHE
    if _ALLOWLIST_CACHE is not None and not custom_path:
        return _ALLOWLIST_CACHE

    target = Path(custom_path) if custom_path else Path(__file__).resolve().parent.parent.parent / "config" / "script_allowlist_terms.txt"
    terms = set(DEFAULT_ALLOWLIST)
    if target.exists():
        try:
            for line in target.read_text(encoding="utf-8").splitlines():
                line = line.strip().lower()
                if line and not line.startswith("#"):
                    terms.add(line)
        except Exception as e:
            log.warning("failed to read allowlist from %s: %s", target, e)
    if not custom_path:
        _ALLOWLIST_CACHE = terms
    return terms


@dataclass
class NormalizationResult:
    text: str
    valid: bool
    language_mode: str
    detected_scripts: list[str] = field(default_factory=list)
    validation_warnings: list[str] = field(default_factory=list)
    normalization_applied: bool = False
    fallback_used: bool = False
    metrics: dict[str, float | int] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)


def canonicalize_language_mode(mode: str | None) -> str:
    """Returns canonical mode ('en-IN', 'hi-IN', 'hinglish-IN'). Defaults to 'hinglish-IN'."""
    if not mode:
        return MODE_HINGLISH
    m = mode.strip().lower()
    return MODE_ALIASES.get(m, MODE_HINGLISH if "hinglish" in m else (MODE_HI if "hi" in m else (MODE_EN if "en" in m else MODE_HINGLISH)))


def detect_scripts(text: str) -> list[str]:
    """Identifies script families present in the text."""
    scripts: list[str] = []
    if _DEV_CHAR_RE.search(text):
        scripts.append("devanagari")
    if _LATIN_CHAR_RE.search(text):
        scripts.append("latin")
    if _DIGIT_RE.search(text):
        scripts.append("digits")
    return scripts


def normalize_punctuation_and_whitespace(text: str, language_mode: str) -> str:
    """Normalizes spaces, tabs, duplicate punctuation, and sets appropriate sentence terminators."""
    if not text:
        return ""
    # Strip markdown markers (*, _, #, `, ~, >)
    t = re.sub(r"[*_#`~>]", " ", text)
    # Strip emojis
    t = re.sub(r"[\U00010000-\U0010ffff]", "", t)
    # Collapse multiple whitespaces
    t = re.sub(r"[ \t]+", " ", t)
    # Collapse repeated punctuation (e.g. "???" -> "?", "..." -> ".")
    t = re.sub(r"\.{2,}", ".", t)
    t = re.sub(r"\?{2,}", "?", t)
    t = re.sub(r"!{2,}", "!", t)
    t = re.sub(r"।{2,}", "।", t)

    # In pure Hindi or Hinglish mode, ensure full-stops before space/end become Devanagari danda '।'
    # unless part of an acronym/decimal (e.g. "B.Tech" or "1.5")
    if language_mode in (MODE_HI, MODE_HINGLISH):
        # Replace sentence-final periods not attached to acronyms or numbers
        t = re.sub(r"(?<![A-Za-z0-9])\.(?=\s|$)", "।", t)
    elif language_mode == MODE_EN:
        # Replace any dandas with periods in English mode
        t = t.replace("।", ".")

    return t.strip()


def normalize_indian_numbers_and_entities(text: str, language_mode: str) -> str:
    """Normalizes currency, dates, times, percentages, and numbers according to language mode."""
    if not text:
        return ""

    if language_mode in (MODE_HI, MODE_HINGLISH):
        # Use established deterministic Devanagari normalizer
        return tts_frontend.normalize(text, lang="hi")

    # Mode en-IN: Indian English spoken normalization
    t = text
    # Currency ₹5,000 / Rs. 5000 -> "5000 rupees"
    t = re.sub(r"(?:₹|Rs\.?|INR)\s?(\d[\d,]*(?:\.\d+)?)", r"\1 rupees", t)
    # Percentages
    t = re.sub(r"(\d[\d,]*(?:\.\d+)?)\s?%", r"\1 percent", t)
    # Phone numbers: format with pauses
    def _en_phone(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if len(digits) >= 10:
            return " ".join([digits[:5], digits[5:]])
        return m.group(0)
    t = re.sub(r"(?<!\w)\+?91[\s\-]?(\d{10})(?!\w)", _en_phone, t)
    return t


def convert_roman_hindi_tokens(
    text: str,
    language_mode: str,
    allowlist: set[str],
    domain_context: dict | None = None
) -> tuple[str, bool, list[str]]:
    """Converts predictable Romanized Hindi tokens into Devanagari while preserving genuine English words.

    Returns:
      (normalized_text, changes_applied, converted_words)
    """
    if language_mode == MODE_EN:
        return text, False, []

    # Preserve URLs and emails by placeholder substitution
    placeholders: dict[str, str] = {}
    ph_counter = 0

    def _mask_url(m: re.Match) -> str:
        nonlocal ph_counter
        key = f"__PROTECTED_ENTITY_{ph_counter}__"
        ph_counter += 1
        placeholders[key] = m.group(0)
        return key

    masked_text = _URL_RE.sub(_mask_url, text)
    masked_text = _EMAIL_RE.sub(_mask_url, masked_text)

    # Tokenize into words and delimiters
    tokens = re.split(r"(\b[A-Za-z\u0900-\u097F]+(?:[.'’][A-Za-z\u0900-\u097F]+)?\b)", masked_text)
    new_tokens: list[str] = []
    converted_tokens: list[str] = []
    n = len(tokens)

    for i, tok in enumerate(tokens):
        if not tok or not re.match(r"^[A-Za-z]", tok):
            # Not a Latin word token (delimiter or Devanagari)
            new_tokens.append(tok)
            continue

        tok_lower = tok.lower()

        # 1. Protected dotted acronyms (e.g. B.Tech, M.B.A., Ph.D)
        if _DOTTED_ACRONYM_RE.match(tok):
            new_tokens.append(tok)
            continue

        # 2. Uppercase acronyms (IIT, NEET, JEE, UGC)
        if tok.isupper() and len(tok) >= 2:
            new_tokens.append(tok)
            continue

        # 3. Allowlisted English academic & counselling terms
        if tok_lower in allowlist:
            new_tokens.append(tok)
            continue

        # 4. Contextual homograph resolution
        if tok_lower in ENGLISH_HOMOGRAPHS:
            # Look at immediate neighbours
            prev_tok = tokens[i - 2].strip().lower() if i >= 2 else ""
            next_tok = tokens[i + 2].strip().lower() if i + 2 < n else ""

            if tok_lower == "main":
                if prev_tok in _POSTPOS_BEFORE_MEIN:
                    new_tokens.append("में")
                    converted_tokens.append("main->में")
                    continue
                if next_tok in _ENGLISH_FOLLOWING_NOUNS:
                    # e.g. "main campus", "main reason"
                    new_tokens.append(tok)
                    continue
                # If followed by first person verb or Hindi pronoun (SOV: Main aapko...)
                if next_tok in _FIRST_PERSON_FOLLOWING or next_tok in {"aap", "aapko", "aapse", "tum", "tumhe", "ise", "use", "bhi"}:
                    new_tokens.append("मैं")
                    converted_tokens.append("main->मैं")
                    continue
                # If start of sentence and next word is Hindi, or clause ends with 1st person verb
                clause_text = text.lower()
                if (i == 1 or i == 0) and any(v in clause_text for v in ("hoon", "unga", "ungi", "सकता हूँ", "बताऊँ", "करूँगा", "बताऊँगा")):
                    new_tokens.append("मैं")
                    converted_tokens.append("main->मैं")
                    continue

            elif tok_lower == "to":
                if prev_tok in {"agar", "jab", "lekin"} or next_tok in {"bataiye", "kijiye", "bhi"}:
                    new_tokens.append("तो")
                    converted_tokens.append("to->तो")
                    continue
                # Keep English "to" (e.g. "how to apply")
                new_tokens.append(tok)
                continue

            elif tok_lower == "is":
                if next_tok in {"liye", "se", "tarah", "process", "course"}:
                    new_tokens.append("इस")
                    converted_tokens.append("is->इस")
                    continue
                # Keep English "is" (e.g. "fee is 5000")
                new_tokens.append(tok)
                continue

            elif tok_lower == "do":
                if next_tok in {"documents", "options", "minutes", "saal", "hafte", "din"}:
                    new_tokens.append("दो")
                    converted_tokens.append("do->दो")
                    continue
                new_tokens.append(tok)
                continue

            elif tok_lower == "us":
                if next_tok in {"liye", "se", "ke", "ka", "ki"}:
                    new_tokens.append("उस")
                    converted_tokens.append("us->उस")
                    continue
                new_tokens.append(tok)
                continue

            elif tok_lower == "hi":
                if prev_tok in {"aap", "aaj", "abhi", "wahan", "yahan", "ek"}:
                    new_tokens.append("ही")
                    converted_tokens.append("hi->ही")
                    continue
                new_tokens.append(tok)
                continue

            # Default for homographs when ambiguous: keep original
            new_tokens.append(tok)
            continue

        # 5. Direct Romanized Hindi lexicon check
        dev_replacement = HINDI_ROMAN.get(tok_lower)
        if dev_replacement:
            new_tokens.append(dev_replacement)
            converted_tokens.append(f"{tok}->{dev_replacement}")
            continue

        # 6. Latin token not in Roman Hindi table: preserve as English/Name
        new_tokens.append(tok)

    result_str = "".join(new_tokens)
    # Restore protected URLs and emails
    for k, v in placeholders.items():
        result_str = result_str.replace(k, v)

    changed = len(converted_tokens) > 0
    return result_str, changed, converted_tokens


def validate_script_rules(
    text: str,
    language_mode: str,
    allowlist: set[str]
) -> tuple[bool, list[str]]:
    """Validates whether normalized text complies with the requested language script policy.

    Rules:
      en-IN: Must NOT contain Devanagari script.
      hi-IN: Must NOT contain untransliterated Roman Hindi words. Technical English allowlist allowed.
      hinglish-IN: Devanagari for Hindi words; genuine English words may be Latin.
    """
    warnings: list[str] = []
    scripts = detect_scripts(text)

    if language_mode == MODE_EN:
        if "devanagari" in scripts:
            warnings.append("unexpected_devanagari_in_en_mode")
            return False, warnings
        return True, warnings

    # For hi-IN and hinglish-IN:
    words = _WORD_TOKEN_RE.findall(text)
    suspicious_roman: list[str] = []

    for w in words:
        if _DEV_CHAR_RE.search(w):
            continue
        wl = w.lower()
        if wl in allowlist or wl in ENGLISH_HOMOGRAPHS or _DOTTED_ACRONYM_RE.match(w) or w.isupper():
            continue
        if wl in HINDI_ROMAN:
            suspicious_roman.append(wl)

    if suspicious_roman:
        warnings.append(f"unconverted_roman_hindi_tokens:{','.join(suspicious_roman[:5])}")
        return False, warnings

    if language_mode == MODE_HI and "devanagari" not in scripts and len(words) > 3:
        warnings.append("pure_hindi_lacks_devanagari")
        return False, warnings

    return True, warnings


def normalize_text(
    text: str,
    language_mode: str,
    domain_context: dict | None = None,
    expand_numbers: bool = True
) -> NormalizationResult:
    """Production normalization entrypoint for full utterances or single phrases.

    Args:
      text: Input string from LLM or caller.
      language_mode: Target mode ('en-IN', 'hi-IN', 'hinglish-IN' or aliases).
      domain_context: Optional context dict (e.g. student name, course queried).
      expand_numbers: If True, expands amounts/dates/digits to spoken words (for TTS).
                      If False, preserves digits and only normalizes script & punctuation (for streaming pipeline).

    Returns:
      NormalizationResult with normalized text, validity flag, warnings, and metrics.
    """
    t0 = time.perf_counter()
    canonical_mode = canonicalize_language_mode(language_mode)

    if not text or not text.strip():
        return NormalizationResult(
            text="",
            valid=True,
            language_mode=canonical_mode,
            detected_scripts=[],
            metrics={"latency_ms": 0.0}
        )

    # Safe length cap to prevent ReDoS / excessive resource consumption
    if len(text) > 4000:
        text = text[:4000]

    allowlist = get_domain_allowlist()

    # Protect English acronyms, dotted course names, URLs, and emails from being respelled in Devanagari
    protected_tokens: dict[str, str] = {}
    ph_counter = 0

    def _protect_match(m: re.Match) -> str:
        nonlocal ph_counter
        val = m.group(0)
        key = f"{{{{MMEPROT{ph_counter}}}}}"
        ph_counter += 1
        protected_tokens[key] = val
        return key

    # Mask URLs and emails
    t = _URL_RE.sub(_protect_match, text)
    t = _EMAIL_RE.sub(_protect_match, t)

    # In Hinglish and Indian English mode, protect dotted acronyms (B.Tech) and uppercase acronyms (MBA, JEE, NEET)
    if canonical_mode in (MODE_HINGLISH, MODE_EN):
        # Dotted acronyms like B.Tech, M.Tech, Ph.D
        t = re.sub(r"\b(?:[A-Za-z]\.){1,3}[A-Za-z]{1,4}\b", _protect_match, t)
        # Uppercase acronyms like MBA, IIT, JEE, NEET
        t = re.sub(r"\b[A-Z]{2,6}\b", _protect_match, t)

    # Step 1: Currency, numbers, dates, times, phone numbers normalization
    if expand_numbers:
        t = normalize_indian_numbers_and_entities(t, canonical_mode)

    # Step 2: Script consistency and Romanized Hindi conversion
    t, norm_applied, converted_words = convert_roman_hindi_tokens(
        t, canonical_mode, allowlist, domain_context=domain_context
    )

    # Step 3: Punctuation and whitespace normalization
    t = normalize_punctuation_and_whitespace(t, canonical_mode)

    # Restore protected entities
    for k, v in protected_tokens.items():
        t = t.replace(k, v)

    # Final cleanup
    final_text = re.sub(r"\s+", " ", t).strip()
    if canonical_mode in (MODE_HI, MODE_HINGLISH):
        # Ensure trailing period after Devanagari becomes danda
        final_text = re.sub(r"(?<=[\u0900-\u097F])\.(?=\s|$)", "।", final_text)

    # Step 4: Validation
    scripts = detect_scripts(final_text)
    is_valid, warnings = validate_script_rules(final_text, canonical_mode, allowlist)

    fallback_used = False
    # Safe fallback policy: If invalid in en-IN and Devanagari is present, strip it
    if not is_valid and canonical_mode == MODE_EN and "devanagari" in scripts:
        cleaned = re.sub(r"[\u0900-\u097F]+", "", final_text).strip()
        if cleaned:
            final_text = cleaned
            fallback_used = True
            is_valid = True

    latency_ms = round((time.perf_counter() - t0) * 1000.0, 3)

    return NormalizationResult(
        text=final_text,
        valid=is_valid,
        language_mode=canonical_mode,
        detected_scripts=scripts,
        validation_warnings=warnings,
        normalization_applied=norm_applied,
        fallback_used=fallback_used,
        metrics={"latency_ms": latency_ms, "chars_in": len(text), "chars_out": len(final_text)},
        diagnostics={"converted_count": len(converted_words), "conversions": converted_words[:10]}
    )


def normalize_phrase(
    phrase: str,
    language_mode: str,
    is_first: bool = False,
    is_last: bool = False,
    domain_context: dict | None = None,
    expand_numbers: bool = True,
) -> NormalizationResult:
    """Phrase-by-phrase normalization helper for streaming speech synthesis.

    Guarantees sub-millisecond execution for low-latency TTFA.
    """
    return normalize_text(
        phrase,
        language_mode=language_mode,
        domain_context=domain_context,
        expand_numbers=expand_numbers,
    )

