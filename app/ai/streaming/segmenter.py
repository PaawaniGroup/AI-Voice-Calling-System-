"""Incremental clause segmenter for the token-streaming pipeline (flag `streaming_pipeline`).

Brain tokens go in, speakable clauses come out as early as is safe:
  * a boundary is one of `, ; : । . ? !` followed by whitespace (or a newline) AND followed by at least one more word (the next token has arrived),
    so "1,25,000", "10:30", "www.mme.com", "B.Tech" and "3.5" can never be cut: they contain no punctuation-then-space;
  * a boundary is NOT taken (the clause simply grows to the next boundary) when it would sit inside a protected span: an amount or number list
    ("1, 2, 3", "₹ 5,000", "1 लाख 20 हज़ार"), a date ("30 June, 2025"), a time ("10 बजे"), a dotted acronym or abbreviation ("M.B.A. course", "Rs. 5");
  * minimum words per clause (the first clause may be much shorter, because it gates the first audio), maximum words (a clause with no boundary
    is cut at a word boundary, never inside a protected span, and never later than 2 x the maximum).
It works on text only (no clock, no I/O) and never logs: the caller's and the Brain's words must not reach a log line.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

BOUNDARY_CHARS = ",;:।.?!"
HARD_CHARS = ".?!।"

# words that continue a number: units, currency, percent, time and date words (English, Roman Hindi and Devanagari)
_UNIT_WORDS = {w.lower() for w in (
    "lakh", "lakhs", "lac", "crore", "crores", "thousand", "hundred", "hazaar", "hazar", "percent", "rupees", "rupee", "rs", "inr", "usd", "lpa", "am", "pm",
    "लाख", "करोड़", "हज़ार", "हजार", "सौ", "प्रतिशत", "रुपये", "रुपए", "रुपया", "बजे", "साल", "महीने", "दिन", "घंटे", "बजकर", "मिनट")}
_MONTHS = {w.lower() for w in (
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    "जनवरी", "फ़रवरी", "फरवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त", "सितंबर", "सितम्बर", "अक्टूबर", "नवंबर", "नवम्बर", "दिसंबर", "दिसम्बर")}
_ABBREV = {"rs", "dr", "mr", "mrs", "ms", "prof", "vs", "etc", "no", "st", "sr", "jr", "approx", "govt", "dept", "univ", "i.e", "e.g", "ph.d", "b.tech", "m.tech",
           "b.e", "m.e", "b.sc", "m.sc", "b.a", "m.a", "b.com", "m.com", "m.b.a", "b.b.a", "b.ed", "m.ed", "a.m", "p.m"}
_DOTTED_ACRONYM = re.compile(r"^(?:[A-Za-z]\.){1,}[A-Za-z]?$")
_NUMERIC = re.compile(r"^[₹$€£+(]?\d")           # starts with a digit (after an optional currency sign): 12, 1,25,000, 10:30, 30/06/2025, 12वीं
_CURRENCY = {"₹", "$", "€", "£", "rs", "rs.", "inr", "usd"}


def _core(tok: str) -> str:
    """The token without trailing/leading punctuation, lower-cased."""
    return tok.strip(BOUNDARY_CHARS + "()[]{}\"'“”‘’").lower()


def is_wordlike(tok: str) -> bool:
    return any(ch.isalnum() for ch in tok)


def _numeric(tok: str) -> bool:
    return bool(_NUMERIC.match(tok.lstrip("([{\"'“‘")))


def no_break_after(tok: str, nxt: str | None) -> bool:
    """True if a clause must NOT end after `tok` because `nxt` continues a protected span (amount, number list, date, time, unit)."""
    if nxt is None:
        return False
    a, b = _core(tok), _core(nxt)
    if _numeric(tok) and (_numeric(nxt) or b in _UNIT_WORDS or b in _MONTHS or b in ("%", "percent")):
        return True                                             # 1 , 2 | 5 lakh | 30 June | 10 बजे | 12 %
    if a in _MONTHS and _numeric(nxt):
        return True                                             # June 30 | June, 2025
    if (a in _CURRENCY or tok.strip().lower() in _CURRENCY) and _numeric(nxt):
        return True                                             # Rs. 5,000 | ₹ 5,000
    if a in _UNIT_WORDS and _numeric(nxt) and a not in ("am", "pm"):
        return True                                             # लाख 20 (1 लाख 20 हज़ार)
    return False


def _dot_is_not_boundary(tok: str) -> bool:
    """A trailing '.' that belongs to an abbreviation / dotted acronym / initial, not to the end of a sentence."""
    if not tok.endswith("."):
        return False
    body = tok.rstrip(".")
    low = body.lower()
    if low in _ABBREV or _DOTTED_ACRONYM.match(tok) or (len(body) == 1 and body.isalpha() and body.isupper()):
        return True
    return False


class Clause(str):
    """A clause (compares and joins as a plain str) that remembers why it ended: "punct" (a boundary character after the minimum words),
    "max_words" (no boundary: cut at the maximum), "hard_max" (a protected span pushed it to the hard limit) or "flush" (end of the reply)."""
    reason: str | None = None

    def __new__(cls, text: str, reason: str | None = None):
        o = super().__new__(cls, text)
        o.reason = reason
        return o


@dataclass
class SegmenterConfig:
    first_min_words: int = 3      # the first clause gates first audio: allowed to be short (PROVISIONAL default, see docs/evidence/phase0)
    min_words: int = 6
    max_words: int = 14           # PROVISIONAL: the final value comes from the TTS re-measurement
    hard_max_factor: float = 2.0  # a protected span may push a clause past max_words, never past max_words * this
    # Clause-size ramp (tenant flag `stream_ramp`, default OFF: with ramp=False none of the fields below is read and the behaviour is unchanged).
    # Clause 1 ends at the first boundary after ramp_first_min_words, else at a word boundary at first_max_words (a natural break up to
    # first_soft_extra_words later is preferred); clause 2 uses second_min/second_max_words; later clauses use min_words/max_words.
    ramp: bool = False
    ramp_first_min_words: int = 2
    first_max_words: int = 4
    first_soft_extra_words: int = 1
    second_min_words: int = 6
    second_max_words: int = 8


class ClauseSegmenter:
    def __init__(self, cfg: SegmenterConfig | None = None):
        self.cfg = cfg or SegmenterConfig()
        self.buf = ""
        self.emitted = 0
        self._reason: str | None = None

    # ------------------------------------------------------------------ public
    def feed(self, text: str) -> list[str]:
        if text:
            self.buf += text
        return self._drain(final=False)

    def flush(self) -> list[str]:
        """End of the Brain stream: everything left is spoken (a short tail is merged into nothing: it is a clause of its own)."""
        out = self._drain(final=True)
        rest = " ".join(self.buf.split())
        self.buf = ""
        if rest and is_wordlike(rest):
            out.append(Clause(rest, "flush"))
            self.emitted += 1
        return out

    @property
    def pending_words(self) -> int:
        return sum(1 for t in self.buf.split() if is_wordlike(t))

    # ------------------------------------------------------------------ internals
    def _min(self) -> int:
        if self.cfg.ramp:
            return self.cfg.ramp_first_min_words if self.emitted == 0 else (self.cfg.second_min_words if self.emitted == 1 else self.cfg.min_words)
        return self.cfg.first_min_words if self.emitted == 0 else self.cfg.min_words

    def _max(self) -> int:
        if self.cfg.ramp:
            return self.cfg.first_max_words if self.emitted == 0 else (self.cfg.second_max_words if self.emitted == 1 else self.cfg.max_words)
        return self.cfg.max_words

    def _drain(self, final: bool) -> list[str]:
        out: list[str] = []
        while True:
            cut = self._find_cut(final)
            if cut is None:
                break
            clause = " ".join(self.buf[:cut].split())
            self.buf = self.buf[cut:].lstrip()
            if clause and is_wordlike(clause):
                out.append(Clause(clause, self._reason))
                self.emitted += 1
        return out

    def _find_cut(self, final: bool) -> int | None:
        toks = list(re.finditer(r"\S+", self.buf))
        words = 0
        minw, maxw = self._min(), self._max()
        hard_max = int(maxw * self.cfg.hard_max_factor)
        for i, m in enumerate(toks):
            tok = m.group()
            if is_wordlike(tok):
                words += 1
            nxt = toks[i + 1].group() if i + 1 < len(toks) else None
            if nxt is None:
                # lookahead is not available yet: the last token is never cut here (a boundary needs one more word to be confirmed)
                break
            if self.cfg.ramp and not final and i + 2 == len(toks) and not self.buf[-1].isspace():
                break                                          # ramp: the next word may still be arriving (its punctuation or the rest of a date/amount): wait for it
            if words < minw:
                continue
            if no_break_after(tok, nxt):
                continue
            punct = tok[-1] in BOUNDARY_CHARS
            if punct:
                if tok[-1] == "." and _dot_is_not_boundary(tok):
                    continue
                if tok[-1] == "." and not (nxt[:1].isupper() or nxt[:1].isdigit() or "ऀ" <= nxt[:1] <= "ॿ"):
                    continue                                   # "... vs. the ..." / "etc. and": a period before a lowercase word is not a sentence end
                self._reason = "punct"
                return m.end()
            if words >= maxw and is_wordlike(tok):
                if (self.cfg.ramp and self.emitted == 0 and words < maxw + self.cfg.first_soft_extra_words
                        and nxt[-1] in BOUNDARY_CHARS and is_wordlike(nxt)):
                    continue                                   # ramp: the NEXT word ends on a boundary: take that natural break instead of cutting here
                self._reason = "max_words"
                return m.end()                                 # too long without punctuation: cut at a word boundary (protected spans already skipped)
            if words >= hard_max:
                self._reason = "hard_max"
                return m.end()
        return None
