"""Reply guard checking LLM Brain responses for unsupported numbers and dates.

Rule:
- Intercepts factual claims specifically about fees, rupees, percentages/cutoffs, and calendar dates/months.
- General conversational numbers that are NOT fee or date claims (e.g. "12th ke baad", "4 saal ka course",
  "5 minute mein", course batch year "2026") are exempt and pass safely.
- Claims matching facts present in the retrieved FAQ context pass through safely.
- If an unsupported fee, percentage, or date is introduced, the clause is replaced by a neutral statement
  that promises no action or callback:
    HI: "सही जानकारी हमारे काउंसलर से पक्की करनी होगी।"
    EN: "I don't have confirmed details on that."
- Latency cost is <0.2ms (regex scan over short sentence clause), fitting seamlessly inside
  the per-clause streaming gate before synthesis.
"""
from __future__ import annotations

import re

DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

MONTH_WORDS = {
    "जनवरी", "फ़रवरी", "फरवरी", "मार्च", "अप्रैल", "मई", "जून",
    "जुलाई", "अगस्त", "सितंबर", "सितम्बर", "अक्टूबर", "नवंबर", "नवम्बर", "दिसंबर", "दिसम्बर",
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december"
}

HINDI_NUMBER_WORDS = {
    "शून्य", "एक", "दो", "तीन", "चार", "पांच", "पाँच", "छह", "छः", "सात", "आठ", "नौ", "दस",
    "ग्यारह", "बारह", "तेरह", "चौदह", "पंद्रह", "सोलह", "सत्रह", "अट्ठारह", "उन्नीस", "बीस",
    "पच्चीस", "तीस", "पैंतीस", "चालीस", "पैंतालीस", "पचास", "साठ", "सत्तर", "अस्सी", "नब्बे", "सौ",
    "हज़ार", "हजार", "लाख", "करोड़"
}

FEE_INDICATORS = re.compile(
    r"(?:₹\s*[\d,]+(?:\.\d+)?|"
    r"\b[\d,]+(?:\.\d+)?\s*(?:rupees?|rs|inr|lakh|crore|हज़ार|हजार|लाख|करोड़|रुपये|रुपए)\b|"
    r"(?:fees?|फ़ीस|फीस|शुल्क|charge|cost|खर्च)[\w\s]{0,20}\b[\d,]+|"
    r"\b[\d,]+[\w\s]{0,20}(?:fees?|फ़ीस|फीस|शुल्क|charge|cost|खर्च))",
    re.IGNORECASE | re.UNICODE
)

PERCENT_INDICATORS = re.compile(
    r"(?:\b[\d.]+\s*%|\b[\d.]+\s*(?:percent|प्रतिशत|पर्सेंटाइल|percentile|मार्क्स|marks|अंक)\b)",
    re.IGNORECASE | re.UNICODE
)

DATE_INDICATORS = re.compile(
    r"(?:\b\d{1,2}\s*(?:st|nd|rd|th)?\s+(?:जनवरी|फ़रवरी|फरवरी|मार्च|अप्रैल|मई|जून|जुलाई|अगस्त|सितंबर|सितम्बर|अक्टूबर|नवंबर|नवम्बर|दिसंबर|दिसम्बर|january|february|march|april|may|june|july|august|september|october|november|december)\b|"
    r"\b(?:जनवरी|फ़रवरी|फरवरी|मार्च|अप्रैल|मई|जून|जुलाई|अगस्त|सितंबर|सितम्बर|अक्टूबर|नवंबर|नवम्बर|दिसंबर|दिसम्बर|january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}\b|"
    r"(?:date|तारीख|deadline|अंतिम)[\w\s]{0,20}\b\d{1,2}\b)",
    re.IGNORECASE | re.UNICODE
)

UNSUPPORTED_REPLACEMENT_HI = "सही जानकारी हमारे काउंसलर से पक्की करनी होगी।"
UNSUPPORTED_REPLACEMENT_EN = "I don't have confirmed details on that."


def extract_restricted_facts(text: str) -> set[str]:
    """Extracts factual claims specifically about fees, percentages, and dates/months.
    Exempts general conversational numbers (e.g. '12th', '4 saal', '5 minute', course years).
    """
    if not text:
        return set()

    norm = text.translate(DEVANAGARI_DIGITS)
    # Remove commas between digits so '50,000' and '50000' normalize identically
    cleaned = re.sub(r"(?<=\d),(?=\d)", "", norm)

    facts = set()

    # 1. Fee claims
    for m in FEE_INDICATORS.finditer(cleaned):
        for d in re.findall(r"\d+", m.group(0)):
            facts.add(d)

    # 2. Percentage / cutoff claims
    for m in PERCENT_INDICATORS.finditer(cleaned):
        for d in re.findall(r"\d+", m.group(0)):
            facts.add(d)

    # 3. Date claims
    for m in DATE_INDICATORS.finditer(cleaned):
        for d in re.findall(r"\d+", m.group(0)):
            facts.add(d)

    # 4. Standalone months
    tokens = re.findall(r"[\wऀ-ॿ]+", cleaned.lower())
    for t in tokens:
        if t in MONTH_WORDS:
            facts.add(t)

    # 5. Spelled out Hindi numbers if near fee keywords
    if any(k in cleaned.lower() for k in ["फीस", "फ़ीस", "रुपये", "लाख", "हज़ार", "शुल्क"]):
        for t in tokens:
            if t in HINDI_NUMBER_WORDS and t not in ["एक", "दो"]:
                facts.add(t)

    return facts


def extract_context_facts(text: str) -> set[str]:
    """Extracts all numbers, words, and months present in context to allow matching."""
    if not text:
        return set()
    norm = text.translate(DEVANAGARI_DIGITS)
    cleaned = re.sub(r"(?<=\d),(?=\d)", "", norm)
    facts = set(re.findall(r"\d+", cleaned))
    tokens = re.findall(r"[\wऀ-ॿ]+", cleaned.lower())
    for t in tokens:
        if t in MONTH_WORDS or t in HINDI_NUMBER_WORDS:
            facts.add(t)
    return facts


def guard_clause(clause: str, context_texts: list[str], language_mode: str = "hi") -> tuple[str, bool]:
    """Guards a single sentence clause before TTS synthesis.

    Returns:
        (guarded_clause, was_replaced: bool)
    """
    if not clause or not clause.strip():
        return clause, False

    clause_facts = extract_restricted_facts(clause)
    if not clause_facts:
        return clause, False  # General numbers or non-fact clauses pass safely

    allowed_facts = set()
    for ctx in context_texts:
        allowed_facts.update(extract_context_facts(ctx))

    unsupported = clause_facts - allowed_facts
    if unsupported:
        replacement = UNSUPPORTED_REPLACEMENT_EN if language_mode == "en" else UNSUPPORTED_REPLACEMENT_HI
        return replacement, True

    return clause, False
