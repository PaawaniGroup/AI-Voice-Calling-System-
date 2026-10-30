# -*- coding: utf-8 -*-
"""
tools/export_short_review_sheet.py - SHORT review sheet for native reviewers (instead of the 1,257-row full sheet).

  python tools/export_short_review_sheet.py --replies path/to/replies.jsonl --out lexicon/review_short

Writes
  <out>.csv                 1) the reported words   2) the top N (default 150) lexicon words by frequency in the replies file   3) the ambiguous words, each with example
                            sentences showing the reading the rule picked (the reviewer judges the CHOICE, not only the spelling)
  <out>_generated_verbs.csv 60 random machine-generated verb forms (seeded), listed separately: these were produced from stems + suffixes, nobody wrote them by hand
Columns the reviewer fills: ok? (Y/N), correct Devanagari (if N), comment, reviewer. Apply returned sheets with tools/apply_review.py (it reads "roman (canonical)").
--replies: jsonl with "text"/"reply"/"raw", or one sentence per line. If you pass the old 342-sentence corpus the frequencies are those of THAT corpus, not of real replies.
"""
import argparse, csv, json, random, re, sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from mme_lexicon import Lexicon  # noqa: E402

REPORTED = ["aapko", "hoon", "han", "sakta", "chahiye", "baare", "main", "aapka", "hota", "hai", "deta"]
AMBIGUOUS = ["main", "me", "mai", "ki", "to", "hi", "is", "us", "do", "hai", "hain", "par", "per", "are", "man", "bas", "ban", "ho", "mat", "tab", "log", "hum", "the", "men"]
HEAD = ["section", "roman (canonical)", "other spellings", "proposed Devanagari", "rule / note", "example (Roman)", "example (what the module produces)", "ok? (Y/N)", "correct Devanagari (if N)", "comment", "reviewer"]


def load(path):
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            r = json.loads(line) if line.startswith("{") else {"text": line}
            out.append(r.get("text") or r.get("reply") or r.get("raw") or "")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--replies", required=True); ap.add_argument("--out", default=str(ROOT / "lexicon" / "review_short"))
    ap.add_argument("--top", type=int, default=150); ap.add_argument("--verbs", type=int, default=60); ap.add_argument("--seed", type=int, default=11)
    a = ap.parse_args()
    lx = Lexicon()
    sents = load(a.replies)
    carriers = [json.loads(l)["text"] for l in open(ROOT / "lexicon" / "carrier_sentences.jsonl", encoding="utf-8") if l.strip()]
    freq, example = Counter(), {}
    for s in sents:
        for w in re.findall(r"[A-Za-z]+", s):
            lw = w.lower()
            e = lx.entries.get(lw)
            if e is not None:
                freq[e.canon] += 1
                example.setdefault(e.canon, s)
    rows_by_canon = {}
    for e in {id(v): v for v in lx.entries.values()}.values():
        rows_by_canon[e.canon] = e
    variants = {c: [k for k, v in lx.entries.items() if v is e and k != c] for c, e in rows_by_canon.items()}
    notes = {r["roman"]: r["notes"] for r in csv.DictReader(open(ROOT / "lexicon" / "hindi_lexicon.csv", encoding="utf-8-sig"))}

    def row(section, canon, ex=None):
        e = rows_by_canon[canon]
        src = ex or example.get(canon) or next((c for c in carriers if re.search(r"\b" + re.escape(canon) + r"\b", c, re.I)), "")
        out = lx.convert(src).text if src else ""
        rule = (e.rule + ": " if e.rule else "") + notes.get(canon, "")
        return [section, canon, " ".join(variants[canon]), "|".join(e.devanagari), rule, src, out, "", "", "", ""]

    generated = {c for c, e in rows_by_canon.items() if "generated:" in notes.get(c, "")}
    rows, seen = [], set()
    for w in REPORTED:
        c = lx.entries[w].canon
        if c not in seen:
            seen.add(c); rows.append(row("1 reported word", c))
    n = 0
    for c, _ in freq.most_common():
        if c in seen or c in generated or c in AMBIGUOUS:
            continue
        seen.add(c); rows.append(row("2 frequent in replies", c)); n += 1
        if n >= a.top:
            break
    amb = 0
    for w in AMBIGUOUS:
        c = lx.entries[w].canon
        exs = [s for s in sents + carriers if re.search(r"\b" + w + r"\b", s, re.I)]
        picked, per_reading = [], Counter()
        for s in (x for x in dict.fromkeys(exs) if lx.convert(x).changed):                       # up to 2 examples per reading the rule chose (à¤®à¥ˆà¤‚ and à¤®à¥‡à¤‚, à¤•à¤¿ and à¤•à¥€, converted and kept-Latin)
            readings = {c["devanagari"] for c in lx.convert(s).converted if c["roman"] == w} or {"(kept Latin)"}
            if any(per_reading[r] < 2 for r in readings):
                picked.append(s)
                per_reading.update(readings)
        for s in picked or [""]:
            rows.append(row("3 ambiguous", c, s)); amb += 1
    out = Path(a.out)
    with open(str(out) + ".csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f); w.writerow(HEAD); w.writerows(rows)
    gen = sorted(generated)
    random.Random(a.seed).shuffle(gen)
    gen = sorted(gen[:a.verbs])
    with open(str(out) + "_generated_verbs.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f); w.writerow(HEAD)
        for c in gen:
            e = rows_by_canon[c]
            w.writerow(["4 generated verb form (random sample)", c, "", "|".join(e.devanagari), notes[c], "", "", "", "", "", ""])
    print(f"{len(rows)} rows in {out}.csv (reported {len([r for r in rows if r[0].startswith('1')])}, frequent {n}, ambiguous-example rows {amb}); {len(gen)} generated verbs in {out}_generated_verbs.csv")


if __name__ == "__main__":
    main()
