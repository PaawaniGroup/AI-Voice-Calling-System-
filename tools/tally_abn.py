# -*- coding: utf-8 -*-
"""
tools/tally_abn.py - count the answers of tools/make_abn_page.py and apply the decision rule.

  python tools/tally_abn.py listening_pack_KEY.json answers_ravi.json answers_anita.json answers_sunil.json [--min-pref 0.70] [--max-word-error 0.05] [--include-non-native]

Per condition against the baseline (or per pair for all-pairs):
  preference share   wins / (wins + losses), ties reported separately, 95% Wilson interval, exact two-sided sign test. Each (listener, sentence) is one comparison; listeners
                     rate the same sentences, so comparisons are NOT independent: the table also gives the majority-vote-per-sentence version, which is the safer number.
  per-word error     marked-wrong target words / target words heard, per condition (a clip heard in several comparisons counts once per listener+sentence, marks averaged)
  N listeners, N sentences
Verdict per condition: PASS only if preference share >= --min-pref AND per-word error < --max-word-error AND at least 3 native listeners AND at least 30 sentences.
Anything else is reported as FAIL or INSUFFICIENT SAMPLE: the script never rounds a verdict up.
Differences in ASR error under 0.05 are noise (12-sentence test: per-sentence swings 0.1-0.25); ASR is not used here.
"""
import argparse, json, math
from collections import defaultdict
from pathlib import Path


def wilson(k, n, z=1.96):
    if n == 0:
        return (None, None)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - m) / d, (c + m) / d)


def sign_test_p(k, n):
    """exact two-sided binomial test against 0.5"""
    if n == 0:
        return None
    k = max(k, n - k)
    tail = sum(math.comb(n, i) for i in range(k, n + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def tally(key: dict, answers: list, min_pref=0.70, max_word_error=0.05, include_non_native=False, min_listeners=3, min_sentences=30):
    used = [a for a in answers if include_non_native or a.get("native") == "yes"]
    skipped = len(answers) - len(used)
    pair_votes = defaultdict(lambda: defaultdict(lambda: {"win": 0, "loss": 0, "tie": 0}))       # (A,B) -> sid -> counts from A's view
    pair_flat = defaultdict(lambda: {"win": 0, "loss": 0, "tie": 0})
    sentences = defaultdict(set)
    heard = defaultdict(set)                                                                        # cond -> listeners who heard it
    werr = defaultdict(lambda: defaultdict(list))                                                   # cond -> (listener, sid, word) -> [0/1 marks]
    other = defaultdict(lambda: defaultdict(list))
    for a in used:
        L = str(a["listener"])
        sides = key["listeners"][L]
        for item_id, ans in a["answers"].items():
            side = sides[item_id]
            sid, ca, cb = side["sid"], side["A"], side["B"]
            if not ans.get("pref"):
                continue
            a_is_left = side["left"] == ca
            pref = ans["pref"]
            outcome = "tie" if pref == "same" else ("win" if (pref == "left") == a_is_left else "loss")
            pair_flat[(ca, cb)][outcome] += 1
            pair_votes[(ca, cb)][sid][outcome] += 1
            for s_name in ("left", "right"):
                cond = side[s_name]
                sentences[cond].add(sid)
                heard[cond].add(L)
                for w in key["sentences"][sid]["words"]:
                    werr[cond][(L, sid, w)].append(1 if w in ans["err"][s_name] else 0)
                other[cond][(L, sid)].append(1 if ans["other"][s_name] else 0)
    listeners = {c: len(heard[c]) for c in key["conditions"]}
    word_error = {}
    for c in key["conditions"]:
        marks = [sum(v) / len(v) for v in werr[c].values()]
        word_error[c] = (sum(marks) / len(marks), len(marks)) if marks else (None, 0)
    rows = []
    for (a, b), t in pair_flat.items():
        n = t["win"] + t["loss"]
        share = t["win"] / n if n else None
        lo, hi = wilson(t["win"], n)
        maj = {"win": 0, "loss": 0, "tie": 0}
        for sid, c in pair_votes[(a, b)].items():
            maj["win" if c["win"] > c["loss"] else "loss" if c["loss"] > c["win"] else "tie"] += 1
        nm = maj["win"] + maj["loss"]
        n_sent = len({sid for sid in pair_votes[(a, b)]})
        rows.append({"A": a, "B": b, "comparisons": sum(t.values()), "A_wins": t["win"], "B_wins": t["loss"], "ties": t["tie"], "pref_A": share, "ci95": (lo, hi), "sign_test_p": sign_test_p(t["win"], n),
                     "majority_A_share": (maj["win"] / nm if nm else None), "majority_counts": maj, "sentences": n_sent})
    verdicts = {}
    for r in rows:
        # the condition under test is B when design == baseline (A is the baseline), else both are reported without a verdict
        if key["design"] != "baseline":
            continue
        cond = r["B"]
        pref = 1 - r["pref_A"] if r["pref_A"] is not None else None
        we, nwe = word_error[cond]
        n_listeners = listeners[cond]
        if n_listeners < min_listeners or r["sentences"] < min_sentences:
            v = f"INSUFFICIENT SAMPLE ({n_listeners} native listeners < {min_listeners} or {r['sentences']} sentences < {min_sentences})"
        elif pref is not None and pref >= min_pref and we is not None and we < max_word_error:
            v = "PASS"
        else:
            v = "FAIL"
        verdicts[cond] = {"pref_vs_baseline": pref, "word_error": we, "word_instances": nwe, "listeners": n_listeners, "sentences": r["sentences"], "verdict": v}
    return {"rows": rows, "word_error": word_error, "listeners": listeners, "verdicts": verdicts, "skipped_non_native": skipped, "answers_used": len(used)}


def fmt(x, pct=True):
    return "n/a" if x is None else (f"{x:.1%}" if pct else f"{x:.3f}")


def report(key, res, min_pref, max_word_error):
    L = [f"# Listening results ({res['answers_used']} listener answer files used, {res['skipped_non_native']} skipped as not native)", "",
         f"Rule: preference >= {min_pref:.0%} against the baseline AND per-word error < {max_word_error:.0%}, >= 3 native listeners, >= 30 sentences.", "",
         "| condition | preference vs baseline (excl. ties) | 95% CI | majority per sentence | ties | per-word error | word instances | N listeners | N sentences | verdict |", "|---|---|---|---|---|---|---|---|---|---|"]
    if key["design"] == "baseline":
        base = key["baseline"]
        we, nwe = res["word_error"][base]
        L.append(f"| {base} (baseline) | - | - | - | - | {fmt(we)} | {nwe} | {res['listeners'][base]} | - | - |")
        for r in res["rows"]:
            v = res["verdicts"][r["B"]]
            pref = v["pref_vs_baseline"]
            ci = (1 - r["ci95"][1], 1 - r["ci95"][0]) if r["ci95"][0] is not None else (None, None)
            maj = None if r["majority_A_share"] is None else 1 - r["majority_A_share"]
            L.append(f"| {r['B']} | {fmt(pref)} ({r['B_wins']}/{r['A_wins'] + r['B_wins']}, p={fmt(r['sign_test_p'], False)}) | {fmt(ci[0])}-{fmt(ci[1])} | {fmt(maj)} | {r['ties']} | {fmt(v['word_error'])} | {v['word_instances']} | {v['listeners']} | {v['sentences']} | **{v['verdict']}** |")
    else:
        L += [f"| {r['A']} vs {r['B']} | {fmt(r['pref_A'])} for {r['A']} ({r['A_wins']}/{r['A_wins'] + r['B_wins']}) | {fmt(r['ci95'][0])}-{fmt(r['ci95'][1])} | {fmt(r['majority_A_share'])} | {r['ties']} | - | - | - | {r['sentences']} | no verdict (all-pairs) |" for r in res["rows"]]
        L += ["", "Per-word error: " + ", ".join(f"{c}: {fmt(v[0])} ({v[1]} word instances)" for c, v in res["word_error"].items())]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("key"); ap.add_argument("answers", nargs="+")
    ap.add_argument("--min-pref", type=float, default=0.70); ap.add_argument("--max-word-error", type=float, default=0.05)
    ap.add_argument("--include-non-native", action="store_true")
    a = ap.parse_args()
    key = json.loads(Path(a.key).read_text(encoding="utf-8"))
    answers = [json.loads(Path(p).read_text(encoding="utf-8")) for p in a.answers]
    res = tally(key, answers, a.min_pref, a.max_word_error, a.include_non_native)
    print(report(key, res, a.min_pref, a.max_word_error))


if __name__ == "__main__":
    main()
