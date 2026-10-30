# -*- coding: utf-8 -*-
"""
tools/export_review_sheet.py - Step 2: the sheet native-Hindi reviewers fill in (opens in Excel / Google Sheets, UTF-8).

  python tools/export_review_sheet.py            ->  lexicon/review_sheet.csv, lexicon/review_sheet_english.csv

One row per lexicon ENTRY (all spelling variants on the row). Reviewer columns are empty: "ok?" (Y / N), "correct Devanagari" (if N), "comment", "reviewer".
Order: words reported by the listener first, then the ambiguous ones, then by frequency in the reply corpus, then the rest.
After the sheets come back, apply them with tools/apply_review.py (sets status to verified/rejected and reviewed_by).
Example sentences come from the corpus when the word occurs there, else from the carrier sentences, else are generated from the entry itself.
"""
import csv, json, re, sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
LEX = ROOT / "lexicon"
REPORTED = ["aapko", "hoon", "han", "sakta", "chahiye", "baare", "main", "aapka", "hota", "hai", "deta"]


def sentences():
    out = []
    for p in (LEX / "corpus" / "counselling_corpus.jsonl", LEX / "carrier_sentences.jsonl"):
        if p.exists():
            out += [json.loads(l)["text"] for l in open(p, encoding="utf-8") if l.strip()]
    return out


def main():
    rows = list(csv.DictReader(open(LEX / "hindi_lexicon.csv", encoding="utf-8-sig", newline="")))
    sents = sentences()
    freq, example = Counter(), {}
    for s in sents:
        for w in re.findall(r"[A-Za-z]+", s):
            lw = w.lower()
            freq[lw] += 1
            example.setdefault(lw, s)

    def keys(r):
        return [r["roman"]] + r["variants"].split()

    def score(r):
        reported = any(k in REPORTED or r["roman"] in REPORTED for k in keys(r))
        return (0 if reported else 1 if r["context_rule"] else 2, -sum(freq[k] for k in keys(r)), r["roman"])

    rows.sort(key=score)
    with open(LEX / "review_sheet.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["roman (canonical)", "other spellings (all map to the same Devanagari)", "proposed Devanagari", "context rule / note", "example sentence",
                    "ok? (Y/N)", "correct Devanagari (if N)", "comment", "reviewer"])
        for r in rows:
            ex = next((example[k] for k in keys(r) if k in example), "")
            if not ex:
                ex = f"({r['roman']})"
            note = (r["context_rule"] + ": " if r["context_rule"] else "") + r["notes"]
            w.writerow([r["roman"], r["variants"], r["devanagari"], note, ex, "", "", "", ""])
    eng = list(csv.DictReader(open(LEX / "english_respell.csv", encoding="utf-8-sig", newline="")))
    with open(LEX / "review_sheet_english.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["English word", "proposed Devanagari respelling (Approach C2 only)", "example sentence", "ok? (Y/N)", "correct Devanagari (if N)", "comment", "reviewer"])
        for r in eng:
            w.writerow([r["roman"], r["devanagari"], example.get(r["roman"], ""), "", "", "", ""])
    print(f"{len(rows)} Hindi entries -> {LEX / 'review_sheet.csv'}; {len(eng)} English respellings -> {LEX / 'review_sheet_english.csv'}")


if __name__ == "__main__":
    main()
