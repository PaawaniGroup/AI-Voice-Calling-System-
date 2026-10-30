# -*- coding: utf-8 -*-
"""
tools/apply_review.py - fold the filled-in review sheets (tools/export_review_sheet.py) back into lexicon/hindi_lexicon.csv.

  python tools/apply_review.py sheet_ravi.csv sheet_anita.csv sheet_sunil.csv [--min-reviewers 2] [--dry-run]

One sheet per reviewer (the "reviewer" column, or the file name, identifies them). Per entry:
  all answers Y and at least --min-reviewers of them     -> status "verified", reviewed_by "a; b; c"
  any N with a correction                                -> devanagari replaced by the MAJORITY correction, status stays "unreviewed" (the new spelling needs a fresh pass)
  any N without a correction, or reviewers disagree      -> status "disputed" (kept in use, listed in the printout, decide by hand)
  not enough answers                                     -> unchanged
Nothing is verified by this script's own judgement: it only counts what the reviewers wrote.
"""
import argparse, csv, sys
from collections import Counter, defaultdict
from pathlib import Path

LEX = Path(__file__).resolve().parent.parent / "lexicon" / "hindi_lexicon.csv"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sheets", nargs="+")
    ap.add_argument("--min-reviewers", type=int, default=2)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    votes = defaultdict(list)       # roman -> [(reviewer, ok, correction)]
    for path in a.sheets:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            sheet = list(csv.DictReader(fh))
        for r in sheet:
            ok = (r.get("ok? (Y/N)") or "").strip().upper()[:1]
            if ok not in ("Y", "N"):
                continue
            who = (r.get("reviewer") or "").strip() or Path(path).stem
            votes[r["roman (canonical)"].strip().lower()].append((who, ok, (r.get("correct Devanagari (if N)") or "").strip()))
    rows = list(csv.DictReader(open(LEX, encoding="utf-8-sig", newline="")))
    changed = Counter()
    for row in rows:
        v = votes.get(row["roman"])
        if not v:
            continue
        who = sorted({x[0] for x in v})
        oks = [x for x in v if x[1] == "Y"]
        nos = [x for x in v if x[1] == "N"]
        if not nos and len({x[0] for x in oks}) >= a.min_reviewers:
            row["status"], row["reviewed_by"] = "verified", "; ".join(who); changed["verified"] += 1
        elif nos:
            fixes = Counter(x[2] for x in nos if x[2])
            if fixes and not oks and len(fixes) == 1:
                row["devanagari"] = fixes.most_common(1)[0][0]; row["status"] = "unreviewed"; row["reviewed_by"] = ""
                row["notes"] = (row["notes"] + "; corrected by " + ", ".join(who)).strip("; "); changed["corrected"] += 1
            else:
                row["status"] = "disputed"; row["notes"] = (row["notes"] + f"; reviewers disagree: {v}").strip("; "); changed["disputed"] += 1
    print(dict(changed))
    if not a.dry_run:
        with open(LEX, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader(); w.writerows(rows)


if __name__ == "__main__":
    main()
