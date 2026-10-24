# -*- coding: utf-8 -*-
"""
tools/lexicon_coverage.py - Step 1: word-frequency list of the Latin tokens of a reply corpus and how much of it the lexicon covers.

  python tools/lexicon_coverage.py lexicon/corpus/counselling_corpus.jsonl            (jsonl with "text", or a plain text file, one sentence per line)
  python tools/lexicon_coverage.py replies.jsonl --out lexicon/reports

Writes <out>/coverage.md, <out>/token_frequency.csv and <out>/uncovered_for_review.csv (tokens a native speaker must classify: hindi / english / name / ignore).

How to read the numbers (be careful, they are optimistic on a corpus the tables were written from):
  hindi_covered   Latin tokens found in the Hindi lexicon (frequency-weighted)
  english_known   Latin tokens in the English list
  uncovered       Latin tokens in neither. In Roman/mixed sentences some of them are Hindi words the lexicon misses. Hindi coverage by frequency is reported as
                  hindi_covered / (hindi_covered + uncovered), i.e. it treats EVERY uncovered token as a possible Hindi word (pessimistic on purpose).
The 98%-of-Hindi-tokens target can only be confirmed on the real Brain replies; the 342-sentence corpus was the source of the old table (circular).
"""
import argparse, csv, json, re, sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from mme_lexicon import Lexicon, _TOKEN  # noqa: E402


def load(path):
    p = Path(path)
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("{"):
            r = json.loads(line)
            out.append((r.get("text") or r.get("raw") or "", r.get("cat", "")))
        else:
            out.append((line, ""))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus")
    ap.add_argument("--out", default=str(ROOT / "lexicon" / "reports"))
    ap.add_argument("--skip-cat", default="indian_english", help="comma list of categories ignored (plain English never goes through the lexicon)")
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    skip = set(a.skip_cat.split(",")) if a.skip_cat else set()
    lx = Lexicon()
    rows = [(t, c) for t, c in load(a.corpus) if c not in skip]
    freq, cls_of, example = Counter(), {}, {}
    sent_total = sent_hin = sent_conv = sent_gated = 0
    for text, cat in rows:
        toks = lx._classify(text)
        for t in toks:
            if t[0] != "lat":
                continue
            freq[t[3]] += 1
            cls_of[t[3]] = t[4]
            example.setdefault(t[3], text)
        res = lx.convert(text)
        if re.search(r"[A-Za-z]", text) and any(t[4] == "hin" for t in toks if t[0] == "lat"):
            sent_hin += 1
            sent_conv += 1 if res.changed else 0
            sent_gated += 1 if res.gated else 0
        sent_total += 1
    kinds = Counter()
    for w, n in freq.items():
        kinds[{"hin": "hindi_covered", "hom": "hindi_covered", "eng": "english_known"}.get(cls_of[w], "uncovered")] += n
    total = sum(kinds.values()) or 1
    hin_cov = kinds["hindi_covered"] / ((kinds["hindi_covered"] + kinds["uncovered"]) or 1)
    with open(out / "token_frequency.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f); w.writerow(["token", "count", "class"])
        for t, n in freq.most_common():
            w.writerow([t, n, cls_of[t]])
    with open(out / "uncovered_for_review.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f); w.writerow(["token", "count", "example sentence", "native decision (hindi / english / name / ignore)", "devanagari if hindi"])
        for t, n in freq.most_common():
            if cls_of[t] in ("unk", "name"):
                w.writerow([t, n, example[t], "", ""])
    md = [f"# Lexicon coverage on {Path(a.corpus).name}", "",
          f"- sentences analysed: {sent_total} (skipped categories: {sorted(skip) or 'none'})",
          f"- Latin tokens: {total} ({len(freq)} distinct)",
          f"- Hindi covered: {kinds['hindi_covered']} ({kinds['hindi_covered'] / total:.1%} of all Latin tokens)",
          f"- English known: {kinds['english_known']} ({kinds['english_known'] / total:.1%})",
          f"- uncovered (not Hindi-covered, not English-known): {kinds['uncovered']} ({kinds['uncovered'] / total:.1%}), {sum(1 for t in freq if cls_of[t] in ('unk', 'name'))} distinct",
          f"- **Hindi coverage by frequency (pessimistic, every uncovered token counted as Hindi): {hin_cov:.1%}** (target >= 98%)",
          f"- sentences with Roman Hindi: {sent_hin}; converted: {sent_conv}; left unchanged by the gate: {sent_gated}", "",
          "Caveat: the table was written from this same corpus, so these numbers are optimistic. Re-run on the real Brain replies.", "",
          "## Top uncovered tokens", ""]
    md += [f"- {t} ({n})" for t, n in freq.most_common() if cls_of[t] in ("unk", "name")][:40] or ["(none)"]
    (out / "coverage.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md[:12]))


if __name__ == "__main__":
    main()
