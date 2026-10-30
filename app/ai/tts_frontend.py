# -*- coding: utf-8 -*-
"""Deterministic text front-end for Hindi / Hinglish TTS (numbers, money, dates, times, phone numbers, percentages, acronyms).

Ported unchanged in behaviour from `mme_frontend.py` of MME_Chatterbox_V3_TEST_COLAB (level 1, the best-scoring level in that test:
held-out skeleton CER 0.156 vs 0.172-0.192 for the Roman->Devanagari / English-respelling levels 2-4, which are deliberately NOT ported).
It writes digits and acronyms the way they should be SPOKEN, in Devanagari, before the text goes to the TTS model: in the Chatterbox test a rupee
amount was read as "one thousand" and acronyms / phone digits were mangled; writing them out as spoken words fixed it.

UNVERIFIED: the Hindi number-word table was written from memory (as the notebook says): have a native speaker check it. Spelling variants such as
अट्ठाईस / अठाईस do not matter to a TTS, wrong words would.
"""
from __future__ import annotations

import re

ONES = ["शून्य", "एक", "दो", "तीन", "चार", "पाँच", "छह", "सात", "आठ", "नौ", "दस", "ग्यारह", "बारह", "तेरह", "चौदह", "पंद्रह", "सोलह", "सत्रह", "अठारह", "उन्नीस",
        "बीस", "इक्कीस", "बाईस", "तेईस", "चौबीस", "पच्चीस", "छब्बीस", "सत्ताईस", "अट्ठाईस", "उनतीस",
        "तीस", "इकतीस", "बत्तीस", "तैंतीस", "चौंतीस", "पैंतीस", "छत्तीस", "सैंतीस", "अड़तीस", "उनतालीस",
        "चालीस", "इकतालीस", "बयालीस", "तैंतालीस", "चौंतालीस", "पैंतालीस", "छियालीस", "सैंतालीस", "अड़तालीस", "उनचास",
        "पचास", "इक्यावन", "बावन", "तिरपन", "चौवन", "पचपन", "छप्पन", "सत्तावन", "अट्ठावन", "उनसठ",
        "साठ", "इकसठ", "बासठ", "तिरसठ", "चौंसठ", "पैंसठ", "छियासठ", "सड़सठ", "अड़सठ", "उनहत्तर",
        "सत्तर", "इकहत्तर", "बहत्तर", "तिहत्तर", "चौहत्तर", "पचहत्तर", "छिहत्तर", "सतहत्तर", "अठहत्तर", "उनासी",
        "अस्सी", "इक्यासी", "बयासी", "तिरासी", "चौरासी", "पचासी", "छियासी", "सत्तासी", "अट्ठासी", "नवासी",
        "नब्बे", "इक्यानवे", "बानवे", "तिरानवे", "चौरानवे", "पचानवे", "छियानवे", "सत्तानवे", "अट्ठानवे", "निन्यानवे"]
assert len(ONES) == 100
DIGIT = ["शून्य", "एक", "दो", "तीन", "चार", "पाँच", "छह", "सात", "आठ", "नौ"]
MONTHS = {"january": "जनवरी", "february": "फ़रवरी", "march": "मार्च", "april": "अप्रैल", "may": "मई", "june": "जून", "july": "जुलाई", "august": "अगस्त",
          "september": "सितंबर", "october": "अक्टूबर", "november": "नवंबर", "december": "दिसंबर",
          "jan": "जनवरी", "feb": "फ़रवरी", "mar": "मार्च", "apr": "अप्रैल", "jun": "जून", "jul": "जुलाई", "aug": "अगस्त", "sep": "सितंबर", "sept": "सितंबर",
          "oct": "अक्टूबर", "nov": "नवंबर", "dec": "दिसंबर"}
LETTER = dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                  ["ए", "बी", "सी", "डी", "ई", "एफ", "जी", "एच", "आई", "जे", "के", "एल", "एम", "एन", "ओ", "पी", "क्यू", "आर", "एस", "टी", "यू", "वी", "डब्ल्यू", "एक्स", "वाई", "ज़ेड"]))
ORDINAL_VEE = {10: "दसवीं", 11: "ग्यारहवीं", 12: "बारहवीं"}
WORD_ACRONYMS = {"NEET": "नीट", "OK": "ओके"}                      # read as words, not letter by letter
DOTTED = {"B.TECH": "बी टेक", "M.TECH": "एम टेक", "B.SC": "बी एस सी", "M.SC": "एम एस सी", "B.COM": "बी कॉम", "M.COM": "एम कॉम", "B.A": "बी ए", "M.A": "एम ए",
          "B.E": "बी ई", "M.E": "एम ई", "B.ED": "बी एड", "M.ED": "एम एड", "PH.D": "पी एच डी"}


def int_words(n: int) -> str:
    """Indian-system Hindi words for 0 <= n < 10**12."""
    if n < 0:
        return "ऋण " + int_words(-n)
    if n < 100:
        return ONES[n]
    parts = []
    for div, name in ((10**7, "करोड़"), (10**5, "लाख"), (10**3, "हज़ार")):
        q, n = divmod(n, div)
        if q:
            parts.append(int_words(q) + " " + name)
    if n >= 100:
        q, n = divmod(n, 100)
        parts.append(ONES[q] + " सौ")
    if n:
        parts.append(ONES[n])
    return " ".join(parts)


def year_words(y: int) -> str:
    if 1100 <= y <= 1999:
        return int_words(y // 100) + " सौ" + ((" " + ONES[y % 100]) if y % 100 else "")
    return int_words(y)


def digits_words(s: str) -> str:
    return " ".join(DIGIT[int(c)] for c in s if c.isdigit())


def number_words(txt: str) -> str:
    txt = txt.replace(",", "")
    if "." in txt:
        a, b = txt.split(".", 1)
        return int_words(int(a or 0)) + " दशमलव " + digits_words(b)
    return int_words(int(txt))


def spell_letters(tok: str) -> str:
    return " ".join(LETTER[c] for c in tok.upper() if c in LETTER)


def _phone(m):
    raw = m.group(0)
    plus = raw.strip().startswith("+")
    groups = [g for g in re.split(r"[\s\-()]+", raw.strip().lstrip("+")) if g]
    out = ", ".join(digits_words(g) for g in groups)
    return (("प्लस " if plus else "") + out)


def _money(m):
    return number_words(m.group("amt")) + " रुपये"


def _date_dmy_text(m):
    return f"{int_words(int(m.group('d')))} {MONTHS[m.group('m').lower().rstrip('.')]} {year_words(int(m.group('y')))}"


def _date_day_month(m):
    return f"{int_words(int(m.group('d')))} {MONTHS[m.group('m').lower()]}"


def _date_numeric(m):
    d, mo, y = int(m.group("d")), int(m.group("m")), int(m.group("y"))
    if not (1 <= d <= 31 and 1 <= mo <= 12):
        return m.group(0)
    names = ["जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर"]
    return f"{int_words(d)} {names[mo - 1]} {year_words(y)}"


def _time(m):
    h, mi, ap = int(m.group("h")), int(m.group("mi")), (m.group("ap") or "").upper().replace(".", "")
    if h > 24 or mi > 59:
        return m.group(0)
    part = ""
    if ap == "AM":
        part = "सुबह "
    elif ap == "PM":
        part = "दोपहर " if h in (12, 1, 2, 3) else "शाम "
    hh = h % 12 or 12 if ap else h
    tail = " बजे" if mi == 0 else f" बजकर {int_words(mi)} मिनट"
    return part + int_words(hh) + tail


def _percent(m):
    return number_words(m.group("n")) + " प्रतिशत"


def _vee(m):
    n = int(m.group(1))
    return ORDINAL_VEE.get(n, int_words(n) + "वीं")


def _dotted(m):
    return DOTTED.get(m.group(0).upper().rstrip("."), m.group(0))


def _acronym(m):
    tok = m.group(0)
    return WORD_ACRONYMS.get(tok, spell_letters(tok))


HINGLISH_MARKERS = {"hai", "hain", "hoga", "hogi", "hoon", "ke", "ki", "ka", "ko", "se", "me", "mein", "par", "aap", "aapka", "aapki", "aapko", "main", "mera", "mujhe",
                    "kaise", "kya", "kyun", "nahi", "nahin", "aur", "ya", "lekin", "pehle", "phir", "bhi", "toh", "kar", "karna", "kijiye", "rakhiye", "chahiye", "liye", "wala", "padhai", "fees"}


def detect_lang(text: str) -> str:
    """'hi' for Devanagari or Roman-script Hinglish, 'en' for plain English."""
    if re.search(r"[ऀ-ॿ]", text):
        return "hi"
    words = set(re.findall(r"[a-z]+", text.lower())) - {"fees"}
    return "hi" if words & HINGLISH_MARKERS else "en"


def normalize(text: str, lang: str = "auto") -> str:
    """lang: 'hi' (convert), 'en' (leave plain English untouched), 'auto' (detect)."""
    if (lang == "auto" and detect_lang(text) == "en") or lang == "en":
        return text
    t = text
    t = re.sub(r"(?:₹|Rs\.?|INR)\s?(?P<amt>\d[\d,]*(?:\.\d+)?)", _money, t)                                     # money first
    t = re.sub(r"(?P<d>\d{1,2})(?:st|nd|rd|th)?\s+(?P<m>[A-Za-z]{3,9})\.?,?\s+(?P<y>\d{4})",
               lambda m: _date_dmy_text(m) if m.group("m").lower().rstrip(".") in MONTHS else m.group(0), t)       # 15 November 2026
    t = re.sub(r"(?<!\d)(?P<d>\d{1,2})[/\-.](?P<m>\d{1,2})[/\-.](?P<y>\d{4})(?!\d)", _date_numeric, t)           # 15/11/2026
    t = re.sub(r"(?<![\d:])(?P<d>\d{1,2})(?:st|nd|rd|th)?\s+(?P<m>January|February|March|April|May|June|July|August|September|October|November|December)\b",
               _date_day_month, t, flags=re.I)                                                                          # 20 June (no year)
    t = re.sub(r"(?<!\d)(?P<h>\d{1,2}):(?P<mi>\d{2})\s?(?P<ap>[AaPp]\.?[Mm]\.?)?", _time, t)                    # 3:30 PM
    t = re.sub(r"(?P<n>\d[\d,]*(?:\.\d+)?)\s?%", _percent, t)
    t = re.sub(r"(?<![\w])\+?\d[\d\s\-()]{6,}\d(?![\w])", lambda m: _phone(m) if len(re.sub(r"\D", "", m.group(0))) >= 8 else m.group(0), t)   # phone numbers
    t = re.sub(r"(?<![\w.,])\d{6,}(?![\w.,])", lambda m: digits_words(m.group(0)), t)                                      # 457821 -> digit by digit (ID / OTP / PIN)
    t = re.sub(r"(\d{1,2})\s?वीं", _vee, t)                                                                       # 12वीं
    t = re.sub(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?![\w])", lambda m: number_words(m.group(0)), t)                    # remaining plain numbers
    t = re.sub(r"\b(?:[A-Za-z]\.){1,3}[A-Za-z]{1,4}\b", _dotted, t)                                               # B.Tech, M.Sc ...
    t = re.sub(r"\b([A-Z]{2,6})/([A-Z]{2,6})\b", r"\1, \2", t)
    t = re.sub(r"\b[A-Z]{2,6}\b", _acronym, t)                                                                    # IIT, JEE, UGC ...
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip()
