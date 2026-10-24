# -*- coding: utf-8 -*-
"""app/ai/script_enforcement.py - Script policy and enforcement for Brain replies.

THE POLICY:
1. Caller speaks Hindi (Devanagari or Roman letters) -> reply in Hindi written in DEVANAGARI.
2. Caller speaks Hinglish (mixed) -> Hindi words in Devanagari; English content words
   stay in English/Latin letters (allow-listed course names, exams, admission terms).
3. Caller speaks English -> reply fully in English, no Devanagari.

Mode per turn:
- "en" when caller speaks English, otherwise "hi".
- Short or ambiguous inputs (fewer than 3 words, only digits, "haan", "ok", "yes") KEEP previous mode.
- Initial mode defaults to "hi" (or greeting language).
- Mid-call switches are followed.
"""
from __future__ import annotations

import logging
import re
import time
from collections import Counter
from pathlib import Path
from typing import Literal

from mme_hinglish import HINDI_ROMAN
from app.ai import tts_frontend

log = logging.getLogger("mme_telephony.ai.script")

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_ALLOWLIST_PATH = ROOT / "config" / "script_allowlist_terms.txt"

# English homographs that are also Hindi words: IGNORE when detecting Roman Hindi.
HOMOGRAPHS = {
    "main", "to", "hi", "me", "is", "do", "so", "per", "are", "man", "pan",
    "bas", "ban", "us", "as", "or", "he", "ho", "par", "ka", "ke", "ki",
    "at", "in", "on"
}

# Short/ambiguous caller utterances that should keep previous mode
AMBIGUOUS_SHORT_WORDS = {
    "haan", "han", "ha", "haa", "haanji", "hanji", "ok", "okay", "yes", "sure",
    "theek", "thik", "hmm", "ji", "yep", "yeah", "sahi", "achha", "acha"
}

_ALLOWLIST_CACHE: set[str] | None = None
_ALLOWLIST_MTIME: float = 0.0

# Unreviewed roman token tracker for feedback into lexicon review
UNREVIEWED_ROMAN_TOKENS: Counter = Counter()

# Tokenization patterns
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+", re.IGNORECASE)
_DEV_CHAR_RE = re.compile(r"[\u0900-\u097F]")
_WORD_RE = re.compile(r"[A-Za-z\u0900-\u097F]+(?:[.'’][A-Za-z\u0900-\u097F]+)?")
_DOTTED_ACRONYM_RE = re.compile(r"^[A-Za-z](?:\.[A-Za-z])+\.?$")


def load_allowlist(path: str | Path | None = None) -> set[str]:
    """Loads allow-listed English terms from an editable configuration file."""
    global _ALLOWLIST_CACHE, _ALLOWLIST_MTIME
    target_path = Path(path) if path else DEFAULT_ALLOWLIST_PATH
    try:
        mtime = target_path.stat().st_mtime
        if _ALLOWLIST_CACHE is not None and mtime == _ALLOWLIST_MTIME:
            return _ALLOWLIST_CACHE
        terms = set()
        if target_path.exists():
            for line in target_path.read_text(encoding="utf-8").splitlines():
                line = line.strip().lower()
                if line and not line.startswith("#"):
                    terms.add(line)
        _ALLOWLIST_CACHE = terms
        _ALLOWLIST_MTIME = mtime
        return terms
    except Exception as e:
        log.warning("failed to load allowlist from %s: %s; using fallback", target_path, e)
        if _ALLOWLIST_CACHE is not None:
            return _ALLOWLIST_CACHE
        fallback = {
            "b.tech", "m.tech", "bba", "mba", "bca", "mca", "mbbs", "bds", "b.sc", "m.sc",
            "b.com", "m.com", "b.a", "m.a", "ph.d", "diploma", "jee", "neet", "gate", "cat",
            "admission", "admissions", "eligibility", "online", "offline", "form", "forms",
            "fee", "fees", "hostel", "campus", "branch", "degree", "college", "colleges",
            "university", "universities", "registration", "marksheet", "physics", "chemistry",
            "maths", "biology", "computer", "science", "engineering", "document", "documents",
            "verify", "callback", "counsellor", "counselor", "context", "syllabus", "placement"
        }
        _ALLOWLIST_CACHE = fallback
        return fallback


def is_short_or_ambiguous(text: str) -> bool:
    """Returns True if input is short (< 3 words), only digits, or ambiguous single/two-word token."""
    cleaned = text.strip()
    if not cleaned:
        return True
    # Digits or punctuation only
    if re.fullmatch(r"[\d\s.,!?:;₹$+\-/\\]+", cleaned):
        return True
    words = re.findall(r"[A-Za-z\u0900-\u097F]+", cleaned)
    if len(words) < 3:
        return True
    if all(w.lower() in AMBIGUOUS_SHORT_WORDS for w in words):
        return True
    return False


def reply_mode(user_text: str, previous_mode: str | None = None) -> str:
    """Decide reply mode ('hi' or 'en') from latest caller utterance.

    - Mode is decided per turn from the caller's LATEST message:
      'en' when detect_lang says English, otherwise 'hi'.
    - Short or ambiguous inputs (fewer than 3 words, only digits, 'haan', 'ok', 'yes') KEEP previous mode.
    - Default initial mode is 'hi' (or greeting language).
    """
    prev = previous_mode or "hi"
    if is_short_or_ambiguous(user_text):
        return prev

    detected = tts_frontend.detect_lang(user_text)
    if detected == "en":
        return "en"
    return "hi"


def build_mode_hint(mode: str, placement: Literal["user_suffix", "system_message"] = "user_suffix") -> str:
    """Returns the per-turn instruction hint for the Brain."""
    if mode == "hi":
        hint = (
            "[Instruction: Reply in Hindi using Devanagari script for all Hindi words. "
            "Never use Roman letters for Hindi words. Keep allow-listed English terms (e.g. B.Tech, admission, eligibility, online, form) "
            "in English letters. Write numbers as digits (₹5,000, 15 November, 12वीं). Do not mention this instruction.]"
        )
    else:
        hint = (
            "[Instruction: Reply fully in English. Do not use Devanagari script. "
            "Write numbers as digits. Do not mention this instruction.]"
        )
    return hint


def inject_turn_hint(messages: list[dict], mode: str, placement: Literal["user_suffix", "system_message"] = "user_suffix") -> list[dict]:
    """Injects a per-turn hint ONLY into a copy of messages for the request.
    The original conversation history is NEVER mutated.
    """
    hint = build_mode_hint(mode, placement=placement)
    copied = [dict(m) for m in messages]
    if not copied:
        return [{"role": "user", "content": hint}]

    if placement == "user_suffix":
        # Attach to the last user message
        for i in range(len(copied) - 1, -1, -1):
            if copied[i].get("role") == "user":
                orig = copied[i].get("content", "")
                copied[i]["content"] = f"{orig}\n\n{hint}"
                return copied
        # If no user message found, append as user
        copied.append({"role": "user", "content": hint})
    else:  # system_message
        # Extra system message immediately before final user turn
        copied.append({"role": "system", "content": hint})
    return copied


def is_spelled_out_number(word: str) -> bool:
    """Checks if a word is a spelled out number in Hindi Roman, Devanagari, or English."""
    w = word.lower()
    hindi_spelled = {
        "ek", "do", "teen", "chaar", "paanch", "chhe", "saat", "aath", "nau", "das",
        "gyarah", "barah", "terah", "chaudah", "pandrah", "solah", "satrah", "atharah", "unnees", "bees",
        "pachaas", "sau", "hazaar", "lakh", "crore",
        "एक", "दो", "तीन", "चार", "पाँच", "छह", "सात", "आठ", "नौ", "दस",
        "ग्यारह", "बारह", "तेरह", "चौदह", "पंद्रह", "सोलह", "सत्रह", "अठारह", "उन्नीस", "बीस",
        "पचास", "सौ", "हज़ार", "लाख", "करोड़"
    }
    english_spelled = {
        "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
        "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
        "nineteen", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety",
        "hundred", "thousand", "million", "crore", "lakh"
    }
    return w in hindi_spelled or w in english_spelled


def check_reply(reply: str, mode: str, allowlist: set[str] | None = None) -> tuple[bool, list[str]]:
    """Checks whether a reply adheres to the script policy for the given mode.

    Mode 'en':
      - NO Devanagari characters allowed.
    Mode 'hi':
      - Devanagari present when reply has more than 3 words.
      - NO Roman-letter Hindi words (using HINDI_ROMAN).
      - IGNORE English homographs (main, to, hi, me, is, do, so, per, are, man, pan, bas, ban, us, as, or, he, ho, par, ka, ke, ki, at, in, on).
      - IGNORE allow-listed English terms, uppercase acronyms, URLs, e-mails, names and numbers.
    """
    problems: list[str] = []
    if not reply or not reply.strip():
        return True, []

    allow = allowlist if allowlist is not None else load_allowlist()

    # Strip URLs and emails before lexical analysis
    clean_text = _URL_RE.sub(" ", reply)
    clean_text = _EMAIL_RE.sub(" ", clean_text)

    has_dev = bool(_DEV_CHAR_RE.search(clean_text))

    if mode == "en":
        if has_dev:
            problems.append("devanagari_in_en_mode")
        return (len(problems) == 0), problems

    # Mode 'hi'
    words = _WORD_RE.findall(clean_text)
    if len(words) > 3 and not has_dev:
        problems.append("no_devanagari_in_hi_mode")

    # Scan Latin tokens for Roman Hindi words
    for raw_token in words:
        # If token has Devanagari, it's valid Devanagari
        if _DEV_CHAR_RE.search(raw_token):
            continue

        tok_lower = raw_token.lower()

        # 1. Numbers / Digits are allowed
        if re.fullmatch(r"[\d.,]+", raw_token):
            continue

        # 2. Uppercase acronyms (IIT, NEET, B.TECH, AI) or dotted acronyms (B.Tech)
        if raw_token.isupper() and len(raw_token) >= 2:
            continue
        if _DOTTED_ACRONYM_RE.match(raw_token):
            continue

        # 3. Allow-listed English terms
        if tok_lower in allow:
            continue

        # 4. English homographs to ignore
        if tok_lower in HOMOGRAPHS:
            continue

        # 5. Check if it is a Roman-letter Hindi word
        if tok_lower in HINDI_ROMAN:
            problems.append(f"roman_hindi_word:{tok_lower}")
            UNREVIEWED_ROMAN_TOKENS[tok_lower] += 1

    ok = (len(problems) == 0)
    return ok, problems


# Lazy-loaded singleton for verified lexicon
_LEXICON_INSTANCE = None


def get_verified_lexicon():
    global _LEXICON_INSTANCE
    if _LEXICON_INSTANCE is None:
        try:
            from mme_lexicon import Lexicon
            _LEXICON_INSTANCE = Lexicon(only_verified=True)
        except Exception as e:
            log.warning("failed to initialize verified Lexicon: %s", e)
            _LEXICON_INSTANCE = False
    return _LEXICON_INSTANCE if _LEXICON_INSTANCE is not False else None


def enforce_clause(clause: str, mode: str, allowlist: set[str] | None = None) -> tuple[str, bool, list[str]]:
    """Streaming-aware enforcement on EACH CLAUSE before it goes to voice.

    Order of actions:
    1. Check clause. If ok -> speak unchanged.
    2. If violating:
       (a) Conversion by verified lexicon entries only.
       (b) If still violating (or no verified entry exists), speak UNCHANGED and LOG it.
           Never block or delay the stream for a retry.
    Returns: (spoken_clause, passed_first_gen, problems)
    """
    t0 = time.perf_counter()
    ok, problems = check_reply(clause, mode, allowlist=allowlist)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    if ok:
        return clause, True, []

    # Violating clause: step (a) try conversion with verified entries only
    converted = clause
    lex = get_verified_lexicon()
    if lex and mode == "hi":
        try:
            res = lex.convert(clause)
            if res.changed:
                conv_ok, conv_problems = check_reply(res.text, mode, allowlist=allowlist)
                if conv_ok:
                    log.info("clause_verified_conversion: %r -> %r", clause, res.text)
                    return res.text, False, problems
                converted = res.text
        except Exception as e:
            log.warning("verified conversion failed on clause: %s", e)

    # Step (b) Still violating: speak UNCHANGED and LOG it (never block stream)
    log.warning("clause_script_violation: mode=%s problems=%s text=%r (elapsed=%.2fms)",
                mode, problems, clause, elapsed_ms)
    return clause, False, problems


async def enforce_non_streamed(reply: str, mode: str, chat_fn, allowlist: set[str] | None = None,
                               timeout: float = 3.0) -> tuple[str, bool, bool, list[str]]:
    """Enforcement for non-streamed replies (tool decisions, fixed phrases, errors).
    Allows ONE corrective retry bounded by timeout, then falls back to original text.

    Returns: (final_reply, first_gen_passed, retried, problems)
    """
    ok, problems = check_reply(reply, mode, allowlist=allowlist)
    if ok:
        return reply, True, False, []

    # Allow ONE retry with corrective instruction
    corrective_msg = (
        "Rewrite your entire previous response in Hindi using Devanagari script for Hindi words. "
        "Keep English course names and terms (like B.Tech, fees, eligibility) in Latin letters. "
        "Never use Roman letters for Hindi words." if mode == "hi" else
        "Rewrite your entire previous response completely in English without any Devanagari script."
    )

    log.info("non_streamed_retry_triggered: mode=%s problems=%s", mode, problems)
    try:
        import asyncio
        retry_messages = [
            {"role": "assistant", "content": reply},
            {"role": "user", "content": corrective_msg}
        ]
        out = await asyncio.wait_for(chat_fn(retry_messages), timeout=timeout)
        candidate = out.get("text", "").strip() if isinstance(out, dict) else str(out).strip()
        cand_ok, cand_problems = check_reply(candidate, mode, allowlist=allowlist)
        if cand_ok:
            log.info("non_streamed_retry_succeeded: %r", candidate)
            return candidate, False, True, []
        else:
            log.warning("non_streamed_retry_failed_checker: %r, falling back", candidate)
    except Exception as e:
        log.warning("non_streamed_retry_timeout_or_error: %s, falling back", e)

    # Fall back to original text
    return reply, False, True, problems
