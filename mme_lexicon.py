# -*- coding: utf-8 -*-
"""
mme_lexicon.py - Roman Hinglish -> Devanagari for TTS, driven by a data file (lexicon/hindi_lexicon.csv). Approach B of the pronunciation task.

    from mme_lexicon import convert
    r = convert("Main aapko bata sakta hoon, course ke baare main.")
    r.text      -> "मैं आपको बता सकता हूँ, course के बारे में."
    r.converted -> [{"roman": "main", "devanagari": "मैं", "rule": "main"}, ...]     (tokens only: never the sentence, never audio)

Design (each point is a requirement, each has a unit test in tests/unit/test_mme_lexicon.py):
  * Run it AFTER the numbers/dates/phones front-end (tts_frontend.normalize / mme_frontend.normalize). It only touches Latin words.
  * Spelling variants (chahiye/chaiya/chaiye..., hoon/hun/hu, baare/bare/baarey, han/haan, aapka/apka) are listed in the CSV and map to ONE entry.
  * Case-insensitive; punctuation, spacing and every other character are kept; URLs, e-mail addresses, numbers, ACRONYMS and mid-sentence Names are never touched.
  * English homographs (main, to, hi, me, us, is, do, per, par, are, man, bas, ban, ...) are converted ONLY inside a Hindi context (a neighbouring Hindi word):
    "main reason", "main page", "to do list", "course name" stay Latin. The ambiguous Hindi words (main/me मैं|में, ki कि|की, hai/hain) have context rules;
    when no rule is sure the fallback is chosen AND reported in Result.decisions.
  * Sentence gate: a sentence is converted only if almost all of its Latin words are KNOWN (Hindi lexicon or English list). Otherwise it is returned entirely
    UNCHANGED and the unknown words are counted (Lexicon.unknown) for the next review. This avoids half-converted sentences.
  * Deterministic, no network, idempotent (the conversion is iterated to a fixed point, so running it twice changes nothing), ~0.1 ms per sentence.
  * status column: "unreviewed" until a native speaker confirms. Nothing is verified yet. only_verified=True converts only verified rows.
"""
from __future__ import annotations

import csv
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("mme_telephony.lexicon")

HERE = Path(__file__).resolve().parent
DEFAULT_DIR = HERE / "lexicon"

RULES = {"", "main", "ki", "hai", "enclitic", "proclitic", "do", "interj", "homograph"}
HOMOGRAPH_RULES = RULES - {"", "ki", "hai"}          # rules for words that are ALSO English words: need Hindi context
DEFAULT_MIN_KNOWN = 0.9

_TOKEN = re.compile(
    r"(?P<prot>https?://\S+|www\.\S+|[\w.+-]+@[\w-]+(?:\.[\w-]+)+|[@#]\w+|(?:[A-Za-z]\.){1,3}[A-Za-z]{1,4}\b|[A-Za-z]*\d\w*|\d\w*)"
    r"|(?P<lat>[A-Za-z]+(?:['’][A-Za-z]+)?)"
    r"|(?P<dev>[ऀ-ॿ]+)")
_BREAK = re.compile(r"[,;:.!?।\n—–()\"“”]")
_SENTENCE = re.compile(r".+?(?:[.!?।]+(?=\s|$)|\n+|$)", re.S)

# --- context vocabularies (canonical lexicon spellings or plain lowercase words) ---------------------------------------------------------------------
POSTPOS_BEFORE_MAIN = {"baare", "liye", "andar", "dauran", "beech", "ke", "bahar"}      # "baare main", "liye main" = में
MAIN_AFTER_SUBJECT = {"aur", "agar", "toh", "to", "jab", "ki", "kyunki", "lekin", "magar", "phir", "ab", "abhi", "aaj", "kal", "yahan", "wahan", "bas", "sirf", "han",
                      "ok", "okay", "yes", "sure", "hello", "sorry", "thanks", "please", "so", "but", "and", "because", "well", "ji", "sir", "madam", "jo", "kya",
                      "aapko", "aapse", "aapki", "aapka", "aapke", "tumhe", "tumse", "yeh", "woh", "ise", "use", "unhe", "isliye", "tab"}
MAIN_ENGLISH_NEXT = {"course", "campus", "branch", "building", "office", "gate", "page", "menu", "street", "road", "entrance", "hall", "reason", "point", "idea",
                     "issue", "goal", "focus", "thing", "topic", "subject", "difference", "advantage", "concern", "feature", "problem", "highlight", "role", "aim"}
PRONOUN_NEXT = {"aap", "aapko", "aapse", "aapka", "aapki", "aapke", "tumhe", "tum", "tumse", "ise", "isse", "unhe", "yeh", "woh", "ye", "wo", "isko", "usko", "hum"}
VERB_STEMS = {"kar", "bata", "dekh", "bol", "samajh", "jaan", "bhej", "sun", "padh", "likh", "soch", "madad", "mil", "le", "de", "rakh", "chun", "seekh", "ja", "aa",
              "kya", "kaise", "kab", "kyun", "kahan", "kitna", "hoon", "ho", "hai", "tha", "thi"}
VERB_SUFFIX = ("ta", "ti", "te", "ega", "egi", "enge", "unga", "ungi", "oon", "iye", "iyega", "ao", "na", "ne", "ya", "yi", "ge", "gi", "nge")
FIRST_PERSON = {"hoon", "karunga", "karungi", "bataunga", "bataungi", "dunga", "dungi", "aaunga", "aaungi"}
KI_AFTER = {"pata", "maloom", "lagta", "lagti", "laga", "kaha", "bataya", "hai", "hain", "tha", "thi", "hoga", "hogi", "honge", "socha", "dekha", "yaad", "isliye", "isiliye",
            "zaroori", "jaante", "samajh", "ummeed", "khayal"}
KI_BEFORE = {"aap", "main", "mai", "hum", "tum", "yeh", "ye", "woh", "wo", "koi", "agar", "jab", "kya", "mujhe", "aapko", "humein", "unhe", "sab", "kal", "aaj"}
PLURAL_SUBJECT = {"aap", "hum", "log", "bachche", "sabhi", "sab", "students"}
NEUTRAL_LOAN = {"lakh", "crore", "hazaar", "rupaya", "rupaye", "ji", "namaste", "sir", "madam", "yaar"}     # also used inside Indian-English sentences: not evidence of Hindi
DO_NEXT_ENGLISH ={"minute", "minutes", "hours", "hour", "days", "weeks", "months", "years", "options", "log", "logon", "baar", "steps", "points"}


@dataclass
class Entry:
    canon: str
    devanagari: tuple
    rule: str
    status: str
    notes: str = ""

    @property
    def homograph(self) -> bool:
        return self.rule in HOMOGRAPH_RULES


@dataclass
class Result:
    text: str
    converted: list = field(default_factory=list)       # [{"roman","devanagari","rule"}]
    unknown: list = field(default_factory=list)         # Latin tokens that were neither Hindi nor English-known (lower-case)
    decisions: list = field(default_factory=list)       # ambiguity decisions that were not sure, and gate failures: [{"token","choice","reason"}]
    gated: int = 0                                      # sentences left unchanged by the gate

    @property
    def changed(self) -> bool:
        return bool(self.converted)


class Lexicon:
    def __init__(self, directory=None, *, min_known: float = DEFAULT_MIN_KNOWN, only_verified: bool = False, english: str = "latin"):
        d = Path(directory) if directory else DEFAULT_DIR
        self.min_known, self.only_verified, self.english = min_known, only_verified, english
        if english not in ("latin", "devanagari"):
            raise ValueError("english must be 'latin' or 'devanagari'")
        self.entries: dict[str, Entry] = {}
        self.unknown = Counter()            # token -> count, only from sentences that contain Roman Hindi
        self.gate_failures = Counter()      # token -> count of gated sentences it appeared in
        self._load_hindi(d / "hindi_lexicon.csv")
        self.respell: dict[str, str] = {}
        self._load_english(d)

    # -- loading ---------------------------------------------------------------------------------------------------------------------------------
    def _load_hindi(self, path: Path):
        with open(path, encoding="utf-8-sig", newline="") as f:
            for n, row in enumerate(csv.DictReader(f), 2):
                roman = (row.get("roman") or "").strip().lower()
                if not roman or (row.get("status") or "").strip() == "rejected":
                    continue
                rule = (row.get("context_rule") or "").strip()
                if rule not in RULES:
                    raise ValueError(f"{path.name} line {n}: unknown context_rule {rule!r}")
                devs = tuple(x.strip() for x in (row.get("devanagari") or "").split("|") if x.strip())
                if not devs:
                    raise ValueError(f"{path.name} line {n}: {roman!r} has no devanagari")
                e = Entry(roman, devs, rule, (row.get("status") or "unreviewed").strip(), row.get("notes") or "")
                for key in [roman] + [v.lower() for v in (row.get("variants") or "").split()]:
                    if key in self.entries:
                        raise ValueError(f"{path.name} line {n}: spelling {key!r} is listed twice")
                    self.entries[key] = e

    def _load_english(self, d: Path):
        words = set()
        for line in (d / "english_known.txt").read_text(encoding="utf-8").splitlines():
            line = line.strip().lower()
            if line and not line.startswith("#"):
                words.add(line)
        with open(d / "english_respell.csv", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                k = row["roman"].strip().lower()
                if (row.get("status") or "") != "rejected":
                    self.respell[k] = row["devanagari"].strip()
                    words.add(k)
        self.english_words = words

    # -- classification --------------------------------------------------------------------------------------------------------------------------
    def _is_english(self, w: str) -> bool:
        ew = self.english_words
        if w in ew or "'" in w or "’" in w:
            return True
        for suf in ("s", "es", "ed", "d", "ing", "ly", "er"):
            if w.endswith(suf) and len(w) > len(suf) + 2 and (w[:-len(suf)] in ew or w[:-len(suf)] + "e" in ew):
                return True
        return False

    def _usable(self, e: Entry) -> bool:
        return e.status == "verified" or (not self.only_verified)

    def _classify(self, text: str):
        """tokens: list of [kind, start, end, word, cls]; cls: hin (Hindi, strong) | hom (Hindi/English homograph) | eng | unk | name | oth"""
        toks, prev_end = [], 0
        for m in _TOKEN.finditer(text):
            kind = m.lastgroup
            w = m.group(0)
            lw = w.lower()
            if kind == "dev":
                cls = "hin"
            elif kind == "prot" or (len(w) > 1 and w.isupper()):
                cls = "oth"
            else:
                e = self.entries.get(lw)
                if e is not None:
                    cls = "hom" if e.homograph else "hin"
                elif self._is_english(lw):
                    cls = "eng"
                else:
                    clause_start = not toks or bool(_BREAK.search(text[prev_end:m.start()]))
                    cls = "name" if (w[0].isupper() and not clause_start) else "unk"
            toks.append([kind, m.start(), m.end(), lw, cls])
            prev_end = m.end()
        return toks

    # -- context helpers -------------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def _brk(text, toks, i):
        """True if a clause boundary lies between token i and token i+1."""
        return bool(_BREAK.search(text[toks[i][2]:toks[i + 1][1]]))

    def _nb(self, text, toks, i, step, k=1):
        j = i
        for _ in range(k):
            nxt = j + step
            if nxt < 0 or nxt >= len(toks):
                return None
            if (self._brk(text, toks, min(j, nxt))):
                return None
            j = nxt
        return j

    def _word(self, toks, j):
        """canonical roman of a token (lexicon canonical spelling if known, else the lower-case word); '' for none."""
        if j is None:
            return ""
        w = toks[j][3]
        e = self.entries.get(w)
        return e.canon if e else w

    def _cls(self, toks, j):
        return toks[j][4] if j is not None else None

    def _hindi_within(self, text, toks, i, span):
        for step in (-1, 1):
            for k in range(1, span + 1):
                j = self._nb(text, toks, i, step, k)
                if j is None:
                    break
                if toks[j][4] == "hin":
                    return True
        return False

    def _verbish(self, w: str) -> bool:
        return w in VERB_STEMS or w in FIRST_PERSON or w.endswith(VERB_SUFFIX)

    # -- rules: each returns (index into entry.devanagari or None, reason) ------------------------------------------------------------------------
    def _rule_main(self, text, toks, i, e, notes):
        w = toks[i][3]
        p, n = self._nb(text, toks, i, -1), self._nb(text, toks, i, 1)
        pw, nw = self._word(toks, p), self._word(toks, n)
        english_reading = w in ("main", "me")
        if w == "main" and nw in MAIN_ENGLISH_NEXT:
            return None, "english_compound"
        if english_reading and not self._hindi_within(text, toks, i, 2):
            return None, "no_hindi_context"
        idx_main, idx_mein = (0, 1) if e.canon in ("main", "mai") else (1, 0)      # entry order: main -> (मैं, में), me -> (में, मैं)
        pcls = self._cls(toks, p)
        in_clause = [self._word(toks, j) for j in range(len(toks)) if self._same_clause(text, toks, i, j)]
        if pw in POSTPOS_BEFORE_MAIN:
            return idx_mein, "after_postposition"
        if p is None:
            if e.canon == "me" and nw not in PRONOUN_NEXT and not self._verbish(nw):
                return idx_mein, "clause_start_me"
            return idx_main, "clause_start"
        if pw in MAIN_AFTER_SUBJECT:
            return idx_main, "after_subject_intro"
        if pcls in ("eng", "hom", "name", "oth", "unk") or (pcls == "hin" and not self._verbish(pw)):
            if nw in PRONOUN_NEXT or (nw in FIRST_PERSON):
                return idx_main, "noun_then_pronoun"
            if any(x in FIRST_PERSON for x in in_clause) and e.canon != "me":
                notes.append({"token": w, "choice": e.devanagari[idx_main], "reason": "unsure: noun before, first-person verb in clause"})
                return idx_main, "unsure_first_person"
            return idx_mein, "after_noun"
        if self._verbish(nw) or nw in PRONOUN_NEXT or any(x in FIRST_PERSON for x in in_clause):
            return idx_main, "before_verb_or_pronoun"
        choice = idx_main if e.canon != "me" else idx_mein
        notes.append({"token": w, "choice": e.devanagari[choice], "reason": "unsure: no rule matched, fallback"})
        return choice, "fallback"

    def _same_clause(self, text, toks, i, j):
        a, b = (i, j) if i < j else (j, i)
        return not any(self._brk(text, toks, k) for k in range(a, b))

    def _rule_ki(self, text, toks, i, e, notes):
        p, n = self._nb(text, toks, i, -1), self._nb(text, toks, i, 1)
        pw, nw = self._word(toks, p), self._word(toks, n)
        if pw in KI_AFTER:
            return 0, "after_knowing_verb"
        if nw in KI_BEFORE:
            return 0, "before_subject"
        if p is None and n is None:
            return None, "isolated"
        if n is None or self._verbish(nw):                              # likely a clause follows (कि ...) but no rule fired: report it
            notes.append({"token": "ki", "choice": e.devanagari[1], "reason": "unsure: default की, कि possible"})
        return 1, "default_ki"

    def _rule_hai(self, text, toks, i, e, notes):
        p = self._nb(text, toks, i, -1)
        if self._word(toks, p) in PLURAL_SUBJECT:
            notes.append({"token": toks[i][3], "choice": e.devanagari[0], "reason": "hai after a plural subject (हैं?): spelling kept as written"})
        return 0, "plain"

    def _resolve(self, text, toks, i, e, notes):
        rule = e.rule
        p, n = self._nb(text, toks, i, -1), self._nb(text, toks, i, 1)
        pc, nc = self._cls(toks, p), self._cls(toks, n)
        if rule == "":
            return 0, "plain"
        if rule == "main":
            return self._rule_main(text, toks, i, e, notes)
        if rule == "ki":
            return self._rule_ki(text, toks, i, e, notes)
        if rule == "hai":
            return self._rule_hai(text, toks, i, e, notes)
        if rule == "enclitic":
            if pc == "hin":
                return 0, "after_hindi_word"
            if p is None and nc == "hin" and e.canon == "to":
                return 0, "clause_start_to"
            return None, "no_hindi_context"
        if rule == "proclitic":
            if p is None or pc in ("hin", "oth"):
                if nc == "hin":
                    return 0, "before_hindi_word"
                n2 = self._nb(text, toks, i, 1, 2)                       # "is course ke baare mein": Hindi determiner + English noun + Hindi postposition
                if nc in ("eng", "unk", "name") and self._cls(toks, n2) == "hin":
                    return 0, "before_english_noun_then_hindi"
            return None, "no_hindi_context"
        if rule == "do":
            if nc == "hin" or self._word(toks, n) in DO_NEXT_ENGLISH or (pc == "hin" and self._verbish(self._word(toks, p))):
                return 0, "do_hindi_context"
            return None, "no_hindi_context"
        if rule == "interj":
            return (0, "clause_start_interjection") if (p is None and nc == "hin") else (None, "no_hindi_context")
        if rule == "homograph":
            return (0, "hindi_neighbour") if "hin" in (pc, nc) else (None, "no_hindi_context")
        return 0, "plain"

    # -- sentence level ---------------------------------------------------------------------------------------------------------------------------
    def _convert_sentence(self, s: str, res: Result, record: bool = True) -> str:
        if not re.search(r"[A-Za-z]", s):
            return s
        toks = self._classify(s)
        latin = [t for t in toks if t[0] == "lat"]
        has_roman_hindi = any(t[4] == "hin" and self._word(toks, toks.index(t)) not in NEUTRAL_LOAN for t in latin)
        if not has_roman_hindi:
            return s                                          # English sentence, or Hindi already in Devanagari: nothing to do (English is never changed)
        counted = [t for t in latin if t[4] in ("hin", "hom", "eng", "unk")]
        unknown = [t[3] for t in latin if t[4] == "unk"]
        known_share = (len(counted) - len(unknown)) / len(counted) if counted else 1.0
        if known_share < self.min_known:
            res.gated += 1
            if record:
                self.gate_failures.update(set(unknown))
                self.unknown.update(unknown)
            res.unknown.extend(unknown)
            res.decisions.append({"token": ",".join(sorted(set(unknown))), "choice": "unchanged", "reason": f"gate: known share {known_share:.2f} < {self.min_known}"})
            return s
        if record:
            self.unknown.update(unknown)
        res.unknown.extend(unknown)
        edits = []
        for i, t in enumerate(toks):
            if t[0] != "lat":
                continue
            e = self.entries.get(t[3])
            if e is not None and self._usable(e):
                idx, why = self._resolve(s, toks, i, e, res.decisions)
                if idx is None:
                    if e.homograph and why != "english_compound":
                        res.decisions.append({"token": t[3], "choice": "kept Latin", "reason": why})
                    continue
                edits.append((t[1], t[2], e.devanagari[idx]))
                res.converted.append({"roman": t[3], "devanagari": e.devanagari[idx], "rule": why})
            elif self.english == "devanagari" and t[3] in self.respell and e is None:
                edits.append((t[1], t[2], self.respell[t[3]]))
                res.converted.append({"roman": t[3], "devanagari": self.respell[t[3]], "rule": "english_respell"})
        out, pos = [], 0
        for a, b, rep in edits:
            out.append(s[pos:a]); out.append(rep); pos = b
        out.append(s[pos:])
        return "".join(out)

    def convert(self, text: str) -> Result:
        res = Result(text)
        cur = text
        for rounds in range(4):                                # iterate to a fixed point: makes the function idempotent
            probe = Result(cur)
            new = "".join(self._convert_sentence(m.group(0), probe, record=(rounds == 0)) for m in _SENTENCE.finditer(cur))
            if rounds == 0:                                    # the log describes the FIRST pass (later passes only see already converted text)
                res.converted, res.unknown, res.decisions, res.gated = probe.converted, probe.unknown, probe.decisions, probe.gated
            else:
                res.converted += probe.converted
            if new == cur:
                break
            cur = new
        res.text = cur
        return res

    # -- reports ----------------------------------------------------------------------------------------------------------------------------------
    def write_unknown_report(self, path):
        """CSV of Latin words that blocked conversion or were not covered: for the next native-speaker review. Tokens only, no sentences."""
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["token", "count", "gated_sentences", "decision (hindi / english / name / ignore)"])
            for tok, c in self.unknown.most_common():
                w.writerow([tok, c, self.gate_failures.get(tok, 0), ""])


def split_by_script(text: str):
    """Approach C3: [(lang, chunk)] runs, 'hi' for Devanagari words and 'en' for Latin words; spaces and punctuation stay with the run before them."""
    segs = []
    for m in re.finditer(r"([ऀ-ॿ]+|[A-Za-z][A-Za-z'’]*)([^A-Za-zऀ-ॿ]*)", text):
        lang = "hi" if re.match(r"[ऀ-ॿ]", m.group(1)) else "en"
        if segs and segs[-1][0] == lang:
            segs[-1] = (lang, segs[-1][1] + m.group(0))
        else:
            segs.append((lang, m.group(0)))
    lead = re.match(r"[^A-Za-zऀ-ॿ]+", text)
    if lead and segs:
        segs[0] = (segs[0][0], lead.group(0) + segs[0][1])
    return segs


_DEFAULT = None


def get_lexicon(**kw) -> Lexicon:
    global _DEFAULT
    if kw:
        return Lexicon(**kw)
    if _DEFAULT is None:
        _DEFAULT = Lexicon()
    return _DEFAULT


def convert(text: str, **kw) -> Result:
    return get_lexicon(**kw).convert(text)


def sentence_lang(text: str) -> str:
    """Approach D helper: 'hi' if the (converted) text has any Devanagari, else 'en'."""
    return "hi" if re.search(r"[ऀ-ॿ]", text) else "en"
