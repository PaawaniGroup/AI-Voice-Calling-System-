"""Offline check of the proposed opening phrases (app/ai/streaming/ramp.py PROPOSED_OPENERS) against 20 varied counselling questions. NO Brain call, no network.

Part A (mechanical, pure code): every opener is 2-4 words, ends in punctuation, does not start with an agree/deny/promise word, has no digit (no fact), uses no
gendered verb form, and is in the script the reply policy needs (Devanagari for Hindi and Roman-Hinglish callers, English for English callers; the voice picks hi/en per clause).
Part B (fit with a continuation): RULES written by the reviewer (the assistant, not a native-speaker panel), applied per question kind. They encode three things:
  1. language: an English opener before a Hindi/Hinglish reply (or the reverse) changes the voice language mid-sentence -> mismatch;
  2. referent: an opener that points at "this" / "your question" / "what you asked" needs something asked; a greeting, a garbled utterance or a "please repeat" has no topic;
  3. grammar: an opener that is a relative clause ("आपने जो पूछा,", dropped by the operator) would force a correlative ("वह", "उसका") in the continuation -> constrained.
Result codes: OK = fits any continuation; CONSTRAINED = fits only some continuations; NO = does not fit. A native-speaker review of the OK pairs is still needed before the list is approved.

Run: python tools/check_openers.py
"""
from __future__ import annotations

import re
import sys

sys.path.insert(0, ".")
from app.ai.streaming.ramp import FORBIDDEN_OPENER_WORDS, NON_TOPICAL_OPENERS, PROPOSED_OPENERS   # noqa: E402

NEW_NON_TOPICAL = NON_TOPICAL_OPENERS     # for questions that have no topic (greeting, garbled, repeat)

# id, language (hi = Devanagari Hindi, hg = Roman Hinglish, en = English), kind, the question (synthetic: written for this check, not a caller's words)
QUESTIONS = [
    (1, "hi", "topical", "B.Tech कंप्यूटर साइंस की फीस कितनी है?"),
    (2, "hi", "topical", "क्या बारहवीं में साठ प्रतिशत पर admission मिल जाएगा?"),
    (3, "hi", "topical", "होस्टल की सुविधा है या नहीं?"),
    (4, "hi", "follow_up", "और scholarship के बारे में भी बताइए।"),
    (5, "hi", "callback", "मुझे किसी counsellor से बात करनी है।"),
    (6, "hi", "repeat", "आपने क्या कहा, फिर से बोलिए।"),
    (7, "hi", "complaint", "मुझे पिछली बार गलत जानकारी मिली थी।"),
    (8, "hg", "topical", "MBA ke liye last date kya hai?"),
    (9, "hg", "topical", "placement kaisa hota hai is college mein?"),
    (10, "hg", "topical", "mujhe documents kaun kaun se chahiye honge?"),
    (11, "hg", "follow_up", "aur agar mera percentage kam ho to?"),
    (12, "hg", "greeting", "hello, kya aap meri baat sun pa rahe hain?"),
    (13, "hg", "garbled", "ye wo kya... course ka naam... uh"),
    (14, "hg", "topical", "online classes hoti hain ya sirf offline?"),
    (15, "en", "topical", "What is the eligibility for the MBA programme?"),
    (16, "en", "topical", "Can I get a scholarship if I am from a small town?"),
    (17, "en", "follow_up", "And what about the hostel fees?"),
    (18, "en", "callback", "Can someone call me back tomorrow?"),
    (19, "en", "greeting", "Hello, is this the admissions helpline?"),
    (20, "en", "repeat", "Sorry, could you repeat that?"),
]

TOPICLESS = {"greeting", "garbled", "repeat"}
GENDERED = re.compile(r"(ता|ती|ते)\s+(हूँ|हूं|है|हैं)|ूँगा|ूंगा|ूँगी|ूंगी")


def script(s: str) -> str:
    return "deva" if re.search(r"[ऀ-ॿ]", s) else "latin"


def part_a(o: str) -> dict:
    words = o.split()
    first = words[0].strip(",.।").lower()
    return {"words": len(words), "ends_punct": o[-1] in ",.।", "forbidden_start": first in {w.lower() for w in FORBIDDEN_OPENER_WORDS},
            "digit": bool(re.search(r"\d", o)), "gendered_verb": bool(GENDERED.search(o)), "script": script(o)}


def fit(opener: str, lang: str, kind: str) -> tuple[str, str]:
    need = "latin" if lang == "en" else "deva"
    if script(opener) != need:
        return "NO", "language/script mismatch: the voice switches language mid-sentence"
    topical_opener = opener in PROPOSED_OPENERS
    if topical_opener and kind in TOPICLESS:
        return "NO", "points at a topic or a question that does not exist"
    if kind in ("callback", "complaint") and topical_opener:
        return "CONSTRAINED", "the referent is loose (nothing was asked about a topic)"
    if opener == "On that point," and kind != "follow_up":
        return "CONSTRAINED", "'that point' has no earlier point in a first question"
    return "OK", ""


def main() -> None:
    allo = list(PROPOSED_OPENERS) + list(NEW_NON_TOPICAL)
    print("PART A (mechanical)")
    for o in allo:
        a = part_a(o)
        ok = 2 <= a["words"] <= 4 and a["ends_punct"] and not a["forbidden_start"] and not a["digit"] and not a["gendered_verb"]
        print(f"  {'PASS' if ok else 'FAIL'}  {o!r:34} {a}")
    print("\nPART B (fit by rule): rows = question, columns = opener; OK / CONSTRAINED / NO")
    head = "| # | lang | kind | " + " | ".join(allo) + " |"
    print(head)
    print("|" + "---|" * (3 + len(allo)))
    tot = {o: {"OK": 0, "CONSTRAINED": 0, "NO": 0} for o in allo}
    for qid, lang, kind, _q in QUESTIONS:
        cells = []
        for o in allo:
            code, _why = fit(o, lang, kind)
            tot[o][code] += 1
            cells.append(code)
        print(f"| {qid} | {lang} | {kind} | " + " | ".join(cells) + " |")
    print("\nTOTALS over the 20 questions (OK / CONSTRAINED / NO):")
    for o in allo:
        t = tot[o]
        print(f"  {o!r:34} {t['OK']:2d} / {t['CONSTRAINED']:2d} / {t['NO']:2d}")


if __name__ == "__main__":
    main()
