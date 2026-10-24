# -*- coding: utf-8 -*-
"""
tools/seed_english_words.py - BUILD-TIME ONLY: merge the most frequent English words (package `wordfreq`, pip install wordfreq) into lexicon/english_known.txt.
The runtime module never imports wordfreq. The sentence gate in mme_lexicon.py needs a broad English list so that "Aapka admission ho gaya hai, congratulations!" is not
rejected because of one ordinary English word.

Risk: a frequent English-list word may really be a Hindi word written in Latin letters (beta, ne...). Those are in BLACKLIST; words that are in hindi_lexicon.csv win anyway
(the lexicon is checked first). Anything else that slips through stays Latin: the listening test and the unknown-word report are the check.
"""
import argparse
from pathlib import Path

from wordfreq import top_n_list

ROOT = Path(__file__).resolve().parent.parent
PATH = ROOT / "lexicon" / "english_known.txt"
BLACKLIST = {"ne", "beta", "ho", "na", "hi", "se", "jo", "ab", "log", "han", "ka", "ki", "ke", "ko", "ek", "kar", "bhi", "hai", "hain", "yar", "bhai", "ji", "tum", "hum", "aap",
             "kya", "nahi", "mera", "meri", "tera", "kal", "aaj", "bas", "ban", "pan", "dil", "sab", "kab", "main", "mein", "toh", "aur", "dost", "paisa"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=8000)
    a = ap.parse_args()
    keep = [l for l in PATH.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
    words = set(keep) | {w for w in top_n_list("en", a.n) if w.isalpha() and w.isascii() and len(w) > 1 and w not in BLACKLIST}
    words |= {"a", "i"}
    header = ("# English words that may stay Latin inside a Hindi sentence (counted as KNOWN by the sentence gate). One per line.\n"
              f"# Seeded from my own function-word list plus the top {a.n} English words of the wordfreq package (tools/seed_english_words.py). Keys of english_respell.csv are known too.\n")
    PATH.write_text(header + "\n".join(sorted(words)) + "\n", encoding="utf-8")
    print(len(words), "English words ->", PATH)


if __name__ == "__main__":
    main()
