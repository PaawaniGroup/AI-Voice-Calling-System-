# -*- coding: utf-8 -*-
"""
tools/measure_roman_hindi.py - Approach A measurement: how often does the Brain still write Hindi words in Roman letters?

LIVE (needs the Brain endpoint, not available yet): asks the Brain the 100 synthetic student utterances of lexicon/brain_test_utterances.jsonl once with the current
SYSTEM_PROMPT ("before") and once with SYSTEM_PROMPT_DEVANAGARI ("after"), saves the replies, and prints the Roman-Hindi share for both.

    set MME_BRAIN_KEY=...                       (the key is read from the environment variable NAMED by --key-env; it is never printed or written)
    python tools/measure_roman_hindi.py live --base-url https://<endpoint>/v1 --model Qwen3-30B-A3B-Instruct-2507 --key-env MME_BRAIN_KEY --out lexicon/reports/brain

OFFLINE: measure replies you already have (one per line, or jsonl with "reply"):
    python tools/measure_roman_hindi.py replies path/to/replies.jsonl

DRY RUN (no network): python tools/measure_roman_hindi.py live --dry-run   prints the request that would be sent for the first utterance.

Metrics per reply set (and per input style roman / devanagari / mixed / english):
  roman_hindi_share   Roman Hindi tokens / (Roman Hindi tokens + Devanagari words)         Roman Hindi = Latin token in the Hindi lexicon (neutral loanwords like lakh excluded)
  upper_share         the same with unknown Latin tokens counted as Hindi too (upper bound: unknown tokens are mostly English words, so this is pessimistic)
  replies_with_roman  share of replies containing at least one Roman Hindi token
Only English-style inputs are expected to give an all-Latin reply; for them the interesting number is Devanagari leakage, reported as devanagari_replies.
Nothing is generated if the endpoint fails: errors are counted, never replaced by invented text.
"""
import argparse, asyncio, json, os, re, sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from mme_lexicon import Lexicon, NEUTRAL_LOAN  # noqa: E402

UTT = ROOT / "lexicon" / "brain_test_utterances.jsonl"
CONTEXT = ("CONTEXT: (demo) B.Tech 4 years, annual fees Rs. 1,20,000, hostel available, admission by entrance exam or 12th marks, last date 30 June. "
           "Scholarships for merit students. Documents: 12th marksheet, ID proof, 2 photos.")


def stats(replies, lx=None):
    """replies: list of (style, text). Returns {'all': {...}, style: {...}}."""
    lx = lx or Lexicon()
    agg = defaultdict(Counter)
    for style, text in replies:
        toks = lx._classify(text)
        roman = unk = dev = 0
        for i, t in enumerate(toks):
            if t[0] == "dev":
                dev += 1
            elif t[0] == "lat":
                w = lx._word(toks, i)
                if t[4] == "hin" and w not in NEUTRAL_LOAN:
                    roman += 1
                elif t[4] == "unk":
                    unk += 1
        for key in ("all", style):
            a = agg[key]
            a["replies"] += 1; a["roman"] += roman; a["unk"] += unk; a["dev"] += dev
            a["replies_with_roman"] += 1 if roman else 0
            a["devanagari_replies"] += 1 if dev else 0
    out = {}
    for key, a in agg.items():
        den = a["roman"] + a["dev"]
        out[key] = {"replies": a["replies"], "roman_tokens": a["roman"], "devanagari_tokens": a["dev"], "unknown_latin_tokens": a["unk"],
                    "roman_hindi_share": round(a["roman"] / den, 4) if den else None,
                    "upper_share": round((a["roman"] + a["unk"]) / (den + a["unk"]), 4) if den + a["unk"] else None,
                    "replies_with_roman": round(a["replies_with_roman"] / a["replies"], 4), "devanagari_replies": round(a["devanagari_replies"] / a["replies"], 4)}
    return out


def load_utterances(n=100):
    return [json.loads(l) for l in open(UTT, encoding="utf-8") if l.strip()][:n]


def build_body(model, system, user, max_tokens=300, temperature=0.3):
    return {"model": model, "messages": [{"role": "system", "content": system + " " + CONTEXT}, {"role": "user", "content": user}],
            "temperature": temperature, "max_tokens": max_tokens}


async def ask_all(base_url, key, model, system, utts, concurrency=4, timeout=40):
    import httpx
    sem = asyncio.Semaphore(concurrency)
    errors = Counter()
    async with httpx.AsyncClient(timeout=timeout) as http:
        async def one(u):
            async with sem:
                try:
                    r = await http.post(base_url.rstrip("/") + "/chat/completions", json=build_body(model, system, u["text"]), headers={"Authorization": f"Bearer {key}"})
                    r.raise_for_status()
                    return u, r.json()["choices"][0]["message"]["content"].strip()
                except Exception as e:                                   # counted, not hidden, never replaced by invented text
                    errors[type(e).__name__] += 1
                    return u, None
        res = await asyncio.gather(*(one(u) for u in utts))
    return res, errors


def table(name, st):
    lines = [f"### {name}", "", "| input style | replies | Roman Hindi tokens | Devanagari tokens | roman_hindi_share | upper_share | replies with Roman Hindi |", "|---|---|---|---|---|---|---|"]
    for k in ("all", "roman", "devanagari", "mixed", "english"):
        s = st.get(k)
        if s:
            lines.append(f"| {k} | {s['replies']} | {s['roman_tokens']} | {s['devanagari_tokens']} | {s['roman_hindi_share']} | {s['upper_share']} | {s['replies_with_roman']} |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    lv = sub.add_parser("live")
    lv.add_argument("--base-url"); lv.add_argument("--model", default="Qwen3-30B-A3B-Instruct-2507"); lv.add_argument("--key-env", default="MME_BRAIN_KEY")
    lv.add_argument("--n", type=int, default=100); lv.add_argument("--out", default=str(ROOT / "lexicon" / "reports" / "brain")); lv.add_argument("--dry-run", action="store_true")
    rp = sub.add_parser("replies"); rp.add_argument("file")
    a = ap.parse_args()
    lx = Lexicon()
    if a.cmd == "replies":
        reps = []
        for line in open(a.file, encoding="utf-8"):
            line = line.strip()
            if line:
                r = json.loads(line) if line.startswith("{") else {"reply": line}
                reps.append((r.get("style", "all"), r.get("reply") or r.get("text", "")))
        print(table(Path(a.file).name, stats(reps, lx)))
        return
    from app.ai.pipeline import SYSTEM_PROMPT, SYSTEM_PROMPT_DEVANAGARI
    utts = load_utterances(a.n)
    if a.dry_run:
        print(json.dumps(build_body(a.model, SYSTEM_PROMPT_DEVANAGARI, utts[0]["text"]), ensure_ascii=False, indent=1)); return
    key = os.environ.get(a.key_env, "")
    if not a.base_url or not key:
        raise SystemExit(f"need --base-url and the key in environment variable {a.key_env} (the Brain endpoint is not deployed yet: nothing was measured)")
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    report = []
    for name, system in (("before (current SYSTEM_PROMPT)", SYSTEM_PROMPT), ("after (SYSTEM_PROMPT_DEVANAGARI)", SYSTEM_PROMPT_DEVANAGARI)):
        res, errors = asyncio.run(ask_all(a.base_url, key, a.model, system, utts))
        ok = [(u["style"], r) for u, r in res if r]
        with open(out / (name.split()[0] + "_replies.jsonl"), "w", encoding="utf-8") as f:
            for u, r in res:
                f.write(json.dumps({"id": u["id"], "style": u["style"], "reply": r}, ensure_ascii=False) + "\n")
        report.append(table(f"{name}: {len(ok)} replies, {sum(errors.values())} errors {dict(errors)}", stats(ok, lx)))
    (out / "summary.md").write_text("\n\n".join(report) + "\n", encoding="utf-8")
    print("\n\n".join(report))


if __name__ == "__main__":
    main()
