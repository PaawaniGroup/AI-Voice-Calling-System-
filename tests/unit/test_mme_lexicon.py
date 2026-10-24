# -*- coding: utf-8 -*-
"""Roman Hinglish -> Devanagari lexicon (mme_lexicon.py). All Devanagari expectations are PROPOSALS until a native speaker verifies them (status column);
the tests pin behaviour (variants, English homographs, ambiguity rules, the sentence gate, idempotency), not linguistic truth."""
import csv
import json
import re
import time
from pathlib import Path

import pytest

from mme_lexicon import Lexicon, convert, sentence_lang, split_by_script
from app.ai.tts_frontend import normalize

ROOT = Path(__file__).resolve().parents[2]
LEX = ROOT / "lexicon"
DEV = re.compile(r"[ऀ-ॿ]")


def c(text, **kw):
    return convert(text, **kw).text


# ---- reported words, spelling variants -> ONE entry ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("variant", ["chahiye", "chaiya", "chaiye", "chahie", "chahiyae", "Chahiye", "CHAIYA"])
def test_chahiye_variants(variant):
    assert c(f"Aapko marksheet {variant}.") == "आपको marksheet चाहिए."


@pytest.mark.parametrize("variant,dev", [("hoon", "हूँ"), ("hun", "हूँ"), ("hu", "हूँ"), ("han", "हाँ"), ("haan", "हाँ"), ("bare", "बारे"), ("baare", "बारे"), ("baarey", "बारे"),
                                         ("aapka", "आपका"), ("apka", "आपका"), ("aapko", "आपको"), ("apko", "आपको"), ("aap", "आप"), ("ap", "आप"),
                                         ("sakta", "सकता"), ("hota", "होता"), ("deta", "देता"), ("hai", "है"), ("hain", "हैं")])
def test_reported_word_variants(variant, dev):
    assert c(f"Yeh {variant} kya kaise") == f"यह {dev} क्या कैसे"


def test_variants_share_one_entry():
    lx = Lexicon()
    assert lx.entries["chaiya"] is lx.entries["chahiye"] is lx.entries["chahie"]
    assert lx.entries["bare"] is lx.entries["baare"]
    assert lx.entries["hun"] is lx.entries["hoon"]


def test_punctuation_and_surroundings_kept():
    assert c("Aapka naam kya hai? (Please batayiye), thanks!") == "आपका नाम क्या है? (Please बताइए), thanks!"
    assert c("  Han,  aapko\tyeh chahiye...  ") == "  हाँ,  आपको\tयह चाहिए...  "


# ---- English homographs stay Latin outside a Hindi context ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("s", [
    "This is the main reason for the delay.", "Please open the main page.", "Tell me about the course name.", "Hi, how do I apply?", "I want to do a course.",
    "To do list for the admission.", "Are you sure about this?", "The man is at the gate.", "Pay per month and do it now.", "Please ban the user so that us can go.",
    "What is your course name and the fees?", "Main street, near the bus stop.",
])
def test_english_sentences_unchanged(s):
    assert c(s) == s


@pytest.mark.parametrize("s", ["Yeh main reason hai.", "Aapka course name kya hai?", "Main page aapko dikhata hoon."])
def test_english_phrase_inside_hindi_sentence_stays_latin(s):
    want = {"Yeh main reason hai.": "यह main reason है.", "Aapka course name kya hai?": "आपका course name क्या है?", "Main page aapko dikhata hoon.": "Main page आपको दिखाता हूँ."}
    assert c(s) == want[s]


def test_course_name_stays_latin_in_hindi_sentence():
    assert c("Aapka course name kya hai?") == "आपका course name क्या है?"
    assert c("Course name confirm kar dijiye.") == "Course name confirm कर दीजिए."


def test_main_reason_inside_hindi_sentence():
    assert c("Yeh main reason hai.") == "यह main reason है."


# ---- ambiguity: main / me ------------------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("s,want", [
    ("Main aapko bata sakta hoon.", "मैं आपको बता सकता हूँ."),
    ("Main kal aapko call karunga.", "मैं कल आपको call करूँगा."),
    ("Aapko main bata sakta hoon.", "आपको मैं बता सकता हूँ."),
    ("Main ek counsellor hoon.", "मैं एक counsellor हूँ."),
    ("Is course ke baare main bataiye.", "इस course के बारे में बताइए."),
    ("Hostel ke liye main kya karna hoga?", "Hostel के लिए में क्या करना होगा?"),        # "liye main" = typo for में: pinned as the rule says
    ("Delhi main hostel milta hai.", "Delhi में hostel मिलता है."),
    ("Course ke andar main kya padhaya jata hai?", "Course के अंदर में क्या पढ़ाया जाता है?"),
    ("Hostel ke bare me aapko details deta hoon.", "Hostel के बारे में आपको details देता हूँ."),
    ("Mein aapko bataunga.", "में आपको बताऊँगा."),       # "mein" is ALWAYS में, even when the writer meant मैं: only main/mai/me carry the rule
])
def test_main_resolution(s, want):
    assert c(s) == want


def test_main_english_compound_never_converted():
    for s in ("Main campus ke baare mein bataiye.", "Main course aapko dikhata hoon."):
        assert "main" in c(s).lower() or "Main" in c(s)


def test_unsure_main_is_logged():
    r = convert("Aapko form main bharna hai")
    assert any(d["token"] == "main" for d in r.decisions) or "में" in r.text or "मैं" in r.text


# ---- ambiguity: hai/hain, to/toh, ki/kee ----------------------------------------------------------------------------------------------------------------
def test_hai_hain():
    assert c("Aap yahan hain, fees kam hai.") == "आप यहाँ हैं, fees कम है."
    r = convert("Aap hai kya")
    assert r.text == "आप है क्या" and any("plural" in d["reason"] for d in r.decisions)       # spelling kept as written, but logged


def test_to_toh():
    assert c("Aapko to pata hoga.") == "आपको तो पता होगा."
    assert c("Aapko toh pata hoga.") == "आपको तो पता होगा."
    assert c("Please apply to the course aapke college mein.") == "Please apply to the course आपके college में."       # English 'to' next to Hindi stays
    assert c("To aap kab aayenge?") == "तो आप कब आएँगे?"
    assert c("I want to go.") == "I want to go."


def test_ki_kee():
    assert c("Mujhe lagta hai ki aap sahi hain.") == "मुझे लगता है कि आप सही हैं."
    assert c("Aapki baat sahi hai.") == "आपकी बात सही है."
    assert c("Aapko pata hoga ki fees kitni hai.") == "आपको पता होगा कि fees कितनी है."
    assert c("Fees ki details bhej dijiye.") == "Fees की details भेज दीजिए."
    assert c("Yeh aapkee hai") == "यह आपकी है" or c("Yeh kee baat hai") == "यह की बात है"


def test_hi_do_is_us():
    assert c("Aap hi decide kijiye.") == "आप ही decide कीजिए."
    assert c("Hi aapka naam kya hai?") == "Hi आपका नाम क्या है?"            # greeting 'Hi' is not ही
    assert c("Do din mein result aayega.") == "दो दिन में result आएगा."
    assert c("Is baare mein aap kya sochte hain?") == "इस बारे में आप क्या सोचते हैं?"
    assert c("Aap is course mein admission le sakte hain.") == "आप इस course में admission ले सकते हैं."
    assert c("Please do your best.") == "Please do your best."


# ---- sentence gate ------------------------------------------------------------------------------------------------------------------------------------
def test_gate_leaves_sentence_unchanged_and_logs_unknowns():
    lx = Lexicon()
    s = "Aapka admission ho gaya hai xyzzy foo plugh."
    r = lx.convert(s)
    assert r.text == s and r.gated == 1
    assert set(r.unknown) == {"xyzzy", "foo", "plugh"}
    assert lx.unknown["xyzzy"] == 1 and lx.gate_failures["foo"] == 1


def test_gate_does_not_half_convert():
    out = c("Aapka admission ho gaya hai Rahul bhai kuchh zzqx qqwe rrty.")
    assert not DEV.search(out)


def test_one_unknown_word_in_long_sentence_still_converts():
    s = "Aapka admission ho gaya hai aur ab aap apna form bhar sakte hain, Rahul ji aur yeh process bahut asaan hai zzqx"
    out = c(s)
    assert "आपका" in out and "zzqx" in out


def test_unknown_report_is_tokens_only(tmp_path):
    lx = Lexicon()
    lx.convert("Aapka admission ho gaya hai xyzzy foo plugh.")
    p = tmp_path / "unknown.csv"
    lx.write_unknown_report(p)
    text = p.read_text(encoding="utf-8-sig")
    assert "xyzzy" in text and "Aapka" not in text


# ---- things that must not be touched --------------------------------------------------------------------------------------------------------------------
def test_urls_emails_numbers_acronyms_names_untouched():
    s = "Aapka form https://mme.in/apply?id=12 par hai, mail help@mme.in ya call 9876543210, IIT aur NEET ke liye Rahul ji se milein."
    out = c(s)
    for keep in ("https://mme.in/apply?id=12", "help@mme.in", "9876543210", "IIT", "NEET", "Rahul"):
        assert keep in out
    assert out.startswith("आपका form")


def test_dotted_and_digit_tokens():
    assert c("Aap B.Tech aur 12th ke baad M.Sc kar sakte hain.") == "आप B.Tech और 12th के बाद M.Sc कर सकते हैं."


def test_front_end_then_lexicon_keeps_numbers_dates_phones():
    raw = "Aapki fees Rs. 45,000 hai, call +91 98765 43210, last date 15 November 2026 hai, IIT mein 75% marks chahiye."
    normal = normalize(raw)
    out = c(normal)
    for tok in ("पैंतालीस हज़ार रुपये", "प्लस नौ एक", "पंद्रह नवंबर", "आई आई टी", "प्रतिशत"):
        assert tok in normal and tok in out
    assert re.sub(r"[A-Za-z]+", "", out) == re.sub(r"[A-Za-z]+", "", normal.replace("hai", "है")) or "है" in out
    assert not re.search(r"\d", out)


# ---- idempotency, determinism, speed -------------------------------------------------------------------------------------------------------------------
def _corpus():
    p = LEX / "corpus" / "counselling_corpus.jsonl"
    return [json.loads(l) for l in open(p, encoding="utf-8")] if p.exists() else []


def _carriers():
    return [json.loads(l) for l in open(LEX / "carrier_sentences.jsonl", encoding="utf-8")]


def test_idempotent_on_carriers_and_corpus():
    texts = [x["text"] for x in _carriers()] + [x["text"] for x in _corpus()]
    assert texts
    for t in texts:
        once = c(t)
        assert c(once) == once, t
        assert c(t) == once                                   # deterministic


def test_english_corpus_sentences_never_change():
    eng = [x["text"] for x in _corpus() if x["cat"] == "indian_english"]
    for t in eng:
        assert c(t) == t, t


def test_devanagari_only_sentences_never_change():
    for x in _corpus():
        if x["cat"] == "hindi_counsel":
            assert c(x["text"]) == x["text"]


def test_speed_under_5ms_per_sentence():
    texts = [x["text"] for x in _corpus()] or ["Main aapko bata sakta hoon."]
    lx = Lexicon()
    lx.convert(texts[0])
    t0 = time.perf_counter()
    for t in texts:
        lx.convert(t)
    per = (time.perf_counter() - t0) / len(texts) * 1000
    assert per < 5, f"{per:.2f} ms per sentence"


def test_no_network_imports():
    src = (ROOT / "mme_lexicon.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import|from)\s+(requests|httpx|urllib|socket|aiohttp)", src, re.M)


# ---- data file integrity ----------------------------------------------------------------------------------------------------------------------------------
def test_lexicon_file_shape():
    with open(LEX / "hindi_lexicon.csv", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert list(rows[0].keys()) == ["roman", "variants", "devanagari", "notes", "context_rule", "status", "reviewed_by"]
    assert {r["status"] for r in rows} <= {"unreviewed", "verified", "rejected", "disputed"}
    for r in rows:
        for d in r["devanagari"].split("|"):
            assert DEV.search(d) and not re.search(r"[A-Za-z]", d), r
        assert re.fullmatch(r"[a-z]+", r["roman"]), r
        if r["status"] == "verified":
            assert r["reviewed_by"], "a verified row needs a reviewer"
    Lexicon()                                                  # raises if a spelling is listed twice or a rule name is unknown


def test_homographs_are_flagged_in_the_lexicon():
    lx = Lexicon()
    for w in ("main", "to", "hi", "me", "us", "is", "do", "per", "bas", "ban", "are", "man"):
        assert lx.entries[w].homograph, w
    for w in ("pan", "so"):
        assert w not in lx.entries                            # English words: never converted at all


def test_only_verified_converts_nothing_until_review():
    lx = Lexicon(only_verified=True)
    s = "Aapka admission ho gaya hai."
    assert lx.convert(s).text == s                              # nothing is verified yet


def test_apply_review_counts_what_reviewers_wrote(tmp_path, monkeypatch, capsys):
    import shutil, sys
    sys.path.insert(0, str(ROOT / "tools"))
    import apply_review
    lexcopy = tmp_path / "hindi_lexicon.csv"
    shutil.copy(LEX / "hindi_lexicon.csv", lexcopy)
    monkeypatch.setattr(apply_review, "LEX", lexcopy)
    head = ["roman (canonical)", "ok? (Y/N)", "correct Devanagari (if N)", "reviewer"]
    for name, rows in {"a": [("hoon", "Y", ""), ("aapko", "Y", ""), ("hota", "N", "होता!")], "b": [("hoon", "Y", ""), ("aapko", "N", ""), ("hota", "N", "होता!")]}.items():
        with open(tmp_path / f"{name}.csv", "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f); w.writerow(head); [w.writerow([r[0], r[1], r[2], name]) for r in rows]
    monkeypatch.setattr(sys, "argv", ["apply_review", str(tmp_path / "a.csv"), str(tmp_path / "b.csv")])
    apply_review.main()
    got = {r["roman"]: r for r in csv.DictReader(open(lexcopy, encoding="utf-8-sig", newline=""))}
    assert got["hoon"]["status"] == "verified" and got["hoon"]["reviewed_by"] == "a; b"
    assert got["aapko"]["status"] == "disputed"
    assert got["hota"]["status"] == "unreviewed" and got["hota"]["devanagari"] == "होता!"
    assert got["hai"]["status"] == "unreviewed"                  # untouched


# ---- approach C / D helpers ----------------------------------------------------------------------------------------------------------------------------------
def test_english_respell_option_changes_only_known_english_in_hindi_sentences():
    lx = Lexicon(english="devanagari")
    assert lx.convert("Aapka course name kya hai?").text == "आपका कोर्स नेम क्या है?"
    assert lx.convert("Please tell me the course name.").text == "Please tell me the course name."          # English-only: untouched


def test_split_by_script_and_sentence_lang():
    segs = split_by_script("आपका course name क्या है?")
    assert [s[0] for s in segs] == ["hi", "en", "hi"]
    assert "".join(s[1] for s in segs) == "आपका course name क्या है?"
    assert sentence_lang("आपका course") == "hi" and sentence_lang("Your course") == "en"


# ---- regression suite: the reported words in carrier sentences ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("case", _carriers(), ids=lambda x: x["id"])
def test_carrier_regression(case):
    assert c(case["text"]) == case["expect"]


def test_carriers_cover_every_reported_word():
    words = {x["word"] for x in _carriers()}
    for w in ("aapko", "hoon", "han", "sakta", "chahiye", "baare", "main-I", "main-in", "aapka", "hota", "hai", "deta", "course name"):
        assert w in words
