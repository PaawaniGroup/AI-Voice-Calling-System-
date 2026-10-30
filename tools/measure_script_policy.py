# -*- coding: utf-8 -*-
"""tools/measure_script_policy.py - Script Policy Measurement Script.

Runnable against any OpenAI-compatible Brain endpoint (e.g. vLLM / RunPod) or local stub.
Evaluates 100 reviewer-approved caller utterances across:
  - 40 Roman Hinglish
  - 20 Devanagari Hindi
  - 20 Mixed Devanagari + English
  - 20 English
  (including 15 tool turns, 10 mid-call switches, and 10 short turns).

Compares OLD prompt vs NEW prompt at configured temperature(s) and fixed seeds across 3 runs.
Calculates and reports with spread:
  1. Share of replies passing checker on first generation
  2. Share of tokens that are Roman Hindi
  3. Wrong-language replies
  4. Replies containing spelled-out numbers
  5. Mean and p95 reply length in words
  6. First-token and first-clause latency
  7. Top 20 worst replies for human reading

STUB NOTICE:
When run against a stub (when no live endpoint is provided), ALL results are
explicitly labeled "[STUB ONLY]".
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.ai.pipeline import OLD_SYSTEM_PROMPT, NEW_SYSTEM_PROMPT, split_sentences
from app.ai.script_enforcement import check_reply, load_allowlist, HOMOGRAPHS, reply_mode, inject_turn_hint, is_spelled_out_number
from mme_hinglish import HINDI_ROMAN

EVAL_FILE = ROOT / "lexicon" / "script_eval_utterances_100.jsonl"
DEFAULT_CONTEXT = (
    "CONTEXT:\n"
    "B.Tech 4 years, annual fees ₹1,25,000, hostel charges ₹60,000/year, admission by entrance test or 60% in 12th PCM. "
    "Last date 30 June. Merit scholarships available for >75% marks. MBA 2 years, annual fees ₹2,50,000. "
    "Documents needed: 12th marksheet, ID proof, 2 photos."
)


def load_dataset(path: Path | None = None) -> list[dict]:
    p = path or EVAL_FILE
    rows = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def analyze_reply(reply: str, expected_mode: str, allowlist: set[str]) -> dict:
    """Computes detailed per-reply script metrics."""
    ok, problems = check_reply(reply, mode=expected_mode, allowlist=allowlist)

    words = re.findall(r"[A-Za-z\u0900-\u097F]+", reply)
    word_count = len(words)

    # Count Roman Hindi tokens
    roman_tokens = []
    for w in words:
        if not re.search(r"[\u0900-\u097F]", w):
            lw = w.lower()
            if lw in HINDI_ROMAN and lw not in HOMOGRAPHS and lw not in allowlist and not w.isupper():
                roman_tokens.append(lw)

    # Check for spelled out numbers
    has_spelled_number = any(is_spelled_out_number(w) for w in words)

    # Wrong language flag
    is_wrong_lang = False
    has_dev = bool(re.search(r"[\u0900-\u097F]", reply))
    if expected_mode == "en" and has_dev:
        is_wrong_lang = True
    elif expected_mode == "hi" and word_count > 3 and not has_dev:
        is_wrong_lang = True

    return {
        "reply": reply,
        "word_count": word_count,
        "first_gen_passed": ok,
        "problems": problems,
        "roman_tokens_count": len(roman_tokens),
        "roman_tokens": roman_tokens,
        "total_tokens": max(word_count, 1),
        "roman_token_share": len(roman_tokens) / max(word_count, 1),
        "has_spelled_number": has_spelled_number,
        "wrong_language": is_wrong_lang,
    }


class BrainStub:
    """Deterministic stub that simulates Brain responses for old vs new prompt."""

    def __init__(self, mode: str = "stub"):
        self.is_stub = True

    async def generate(self, messages: list[dict], system_type: str, seed: int = 42) -> tuple[str, float, float]:
        """Returns (reply_text, first_token_latency_ms, first_clause_latency_ms)."""
        t0 = time.perf_counter()
        # Simulate ~40-70ms first token latency, ~90-130ms first clause latency
        await asyncio.sleep(0.005)
        ft_latency = 52.0 + (seed % 7) * 2.5
        fc_latency = 95.0 + (seed % 11) * 3.1

        user_content = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_content = m.get("content", "")
                break

        # Simulate OLD prompt: mirrors caller's script
        if system_type == "old":
            if "What is" in user_content or "Can you" in user_content or "Which" in user_content:
                reply = "The eligibility for B.Tech requires at least 60% marks in 12th with Physics, Chemistry and Maths."
            elif re.search(r"[\u0900-\u097F]", user_content):
                reply = "जी, बीटेक की कुल फीस ₹1,25,000 है। आप 30 June तक आवेदन कर सकते हैं।"
            else:
                # Roman Hinglish input -> mirrors in Roman Hinglish
                reply = "Ji, B.Tech ki fees Rs 1,25,000 hai. Aapko online form 30 June tak bharna hoga."
        else:
            # Simulate NEW prompt: follows Devanagari rules, digits, allowlist
            if "What is" in user_content or "Can you explain" in user_content or "[Instruction: Reply fully in English" in user_content:
                reply = "You need at least 60% in 12th with Physics, Chemistry and Maths for B.Tech admission."
            else:
                # Hindi/Hinglish -> Devanagari with allow-listed Latin terms
                if "fees" in user_content.lower() or "शुल्क" in user_content:
                    reply = "जी, B.Tech की कुल फीस ₹1,25,000 है। क्या मैं eligibility भी बताऊँ?"
                elif "admission" in user_content.lower() or "प्रवेश" in user_content:
                    reply = "जी, admission process आसान है। पहले आप online form भरते हैं, फिर documents verify होते हैं।"
                elif "hostel" in user_content.lower() or "छात्रावास" in user_content:
                    reply = "जी, campus में hostel facility available है और इसका वार्षिक शुल्क ₹60,000 है।"
                elif "last date" in user_content.lower() or "अंतिम" in user_content:
                    reply = "जी, form भरने की last date 30 June है। आप ₹1,000 registration fee देकर apply कर सकते हैं।"
                else:
                    reply = "जी, बताइए, आप किस course के बारे में जानकारी जानना चाहते हैं?"

        return reply, ft_latency, fc_latency


async def ask_live_endpoint(base_url: str, key: str, model: str, messages: list[dict],
                            temperature: float = 0.3, seed: int = 42) -> tuple[str, float, float]:
    """Streams from a live OpenAI-compatible endpoint to measure TTFT and first clause latency."""
    import httpx
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "seed": seed,
        "stream": True,
        "max_tokens": 250,
    }

    t0 = time.perf_counter()
    first_token_time = None
    first_clause_time = None
    accumulated = ""

    async with httpx.AsyncClient(timeout=45.0) as client:
        async with client.stream("POST", url, json=body, headers=headers) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                    if delta:
                        if first_token_time is None:
                            first_token_time = (time.perf_counter() - t0) * 1000.0
                        accumulated += delta
                        if first_clause_time is None and re.search(r"[।!?\n]", accumulated):
                            first_clause_time = (time.perf_counter() - t0) * 1000.0
                except json.JSONDecodeError:
                    continue

    ft = first_token_time if first_token_time is not None else (time.perf_counter() - t0) * 1000.0
    fc = first_clause_time if first_clause_time is not None else ft
    return accumulated.strip(), ft, fc


async def run_single_condition(utterances: list[dict], system_prompt_text: str,
                               condition_name: str, system_type: str,
                               temperature: float = 0.3, seed: int = 42,
                               stub: BrainStub | None = None,
                               live_cfg: dict | None = None) -> dict:
    """Runs one full evaluation pass over the 100 utterances."""
    allow = load_allowlist()
    results = []

    for item in utterances:
        expected_mode = item.get("expected_mode", "hi")
        raw_text = item["text"]

        # Build message history
        messages = [{"role": "system", "content": system_prompt_text + "\n\n" + DEFAULT_CONTEXT}]
        if item.get("history"):
            messages.extend(item["history"])

        # Decide mode and inject hint if new prompt
        if system_type == "new":
            turn_mode = reply_mode(raw_text, previous_mode=item.get("history", [{}])[-1].get("language", "hi") if item.get("history") else "hi")
            messages.append({"role": "user", "content": raw_text})
            req_messages = inject_turn_hint(messages, mode=turn_mode, placement="user_suffix")
        else:
            turn_mode = expected_mode
            messages.append({"role": "user", "content": raw_text})
            req_messages = messages

        if stub:
            reply_text, ft_ms, fc_ms = await stub.generate(req_messages, system_type=system_type, seed=seed)
        else:
            reply_text, ft_ms, fc_ms = await ask_live_endpoint(
                base_url=live_cfg["base_url"],
                key=live_cfg["key"],
                model=live_cfg["model"],
                messages=req_messages,
                temperature=temperature,
                seed=seed,
            )

        analysis = analyze_reply(reply_text, expected_mode=turn_mode, allowlist=allow)
        analysis["id"] = item["id"]
        analysis["style"] = item["style"]
        analysis["input"] = raw_text
        analysis["first_token_ms"] = ft_ms
        analysis["first_clause_ms"] = fc_ms
        results.append(analysis)

    # Compute aggregate metrics for this run
    n = len(results)
    pass_first_share = sum(1 for r in results if r["first_gen_passed"]) / n
    total_tokens = sum(r["total_tokens"] for r in results)
    total_roman_tokens = sum(r["roman_tokens_count"] for r in results)
    roman_token_share = total_roman_tokens / max(total_tokens, 1)
    wrong_lang_share = sum(1 for r in results if r["wrong_language"]) / n
    spelled_num_share = sum(1 for r in results if r["has_spelled_number"]) / n
    word_counts = [r["word_count"] for r in results]
    mean_len = statistics.mean(word_counts)
    p95_len = statistics.quantiles(word_counts, n=20)[18] if len(word_counts) >= 20 else max(word_counts)
    mean_ft = statistics.mean(r["first_token_ms"] for r in results)
    mean_fc = statistics.mean(r["first_clause_ms"] for r in results)

    return {
        "condition": condition_name,
        "seed": seed,
        "temperature": temperature,
        "results": results,
        "summary": {
            "first_gen_pass_rate": pass_first_share,
            "roman_hindi_token_share": roman_token_share,
            "wrong_language_share": wrong_lang_share,
            "spelled_numbers_share": spelled_num_share,
            "mean_length_words": mean_len,
            "p95_length_words": p95_len,
            "mean_first_token_latency_ms": mean_ft,
            "mean_first_clause_latency_ms": mean_fc,
        }
    }


def aggregate_runs_with_spread(runs: list[dict]) -> dict:
    """Aggregates 3 runs, computing mean and spread (max - min) for each metric."""
    metrics = [
        "first_gen_pass_rate",
        "roman_hindi_token_share",
        "wrong_language_share",
        "spelled_numbers_share",
        "mean_length_words",
        "p95_length_words",
        "mean_first_token_latency_ms",
        "mean_first_clause_latency_ms",
    ]
    out = {}
    for m in metrics:
        vals = [r["summary"][m] for r in runs]
        avg = statistics.mean(vals)
        spread = max(vals) - min(vals)
        out[m] = {
            "mean": avg,
            "spread": spread,
            "min": min(vals),
            "max": max(vals),
            "values": vals,
        }
    return out


def find_20_worst_replies(all_results: list[dict]) -> list[dict]:
    """Extracts top 20 worst replies ranked by problems and Roman Hindi tokens."""
    def penalty(r):
        score = 0
        score += len(r.get("problems", [])) * 10
        score += r.get("roman_tokens_count", 0) * 5
        if r.get("wrong_language"):
            score += 20
        if r.get("has_spelled_number"):
            score += 5
        return score

    sorted_replies = sorted(all_results, key=penalty, reverse=True)
    return sorted_replies[:20]


def format_markdown_report(old_agg: dict, new_agg: dict, worst_replies: list[dict],
                            is_stub: bool = True, temperatures_evaluated: list[float] | None = None) -> str:
    label = "**[STUB ONLY - SIMULATION]**" if is_stub else "**[LIVE MEASUREMENT]**"
    notice = (
        "> [!IMPORTANT]\n"
        "> **STUB NOTICE**: Brain endpoint was evaluated using the local simulation stub. "
        "All figures below reflect stub results. Real measurements must be run against the live RunPod endpoint once reachable.\n"
        if is_stub else ""
    )

    lines = [
        f"# Script Policy Measurement Report {label}",
        "",
        notice,
        "### 1. Condition Comparison Across 3 Runs (Mean ± Spread)",
        "",
        "| Metric | OLD Prompt (Baseline) | NEW Prompt (Enforced) | Difference (New - Old) | Significant? |",
        "|---|---|---|---|---|",
    ]

    metric_names = [
        ("first_gen_pass_rate", "First-gen checker pass share", True),
        ("roman_hindi_token_share", "Roman Hindi token share", True),
        ("wrong_language_share", "Wrong-language reply share", True),
        ("spelled_numbers_share", "Spelled-out numbers share", True),
        ("mean_length_words", "Mean reply length (words)", False),
        ("p95_length_words", "P95 reply length (words)", False),
        ("mean_first_token_latency_ms", "First-token latency (ms)", False),
        ("mean_first_clause_latency_ms", "First-clause latency (ms)", False),
    ]

    for key, label_str, is_pct in metric_names:
        o = old_agg[key]
        n = new_agg[key]
        diff = n["mean"] - o["mean"]
        max_spread = max(o["spread"], n["spread"])
        is_sig = abs(diff) > max_spread
        sig_str = "Yes" if is_sig else "No (inside spread)"

        if is_pct:
            o_str = f"{o['mean']*100:.1f}% (±{o['spread']*100:.1f}%)"
            n_str = f"{n['mean']*100:.1f}% (±{n['spread']*100:.1f}%)"
            d_str = f"{diff*100:+.1f}%"
        else:
            o_str = f"{o['mean']:.1f} (±{o['spread']:.1f})"
            n_str = f"{n['mean']:.1f} (±{n['spread']:.1f})"
            d_str = f"{diff:+.1f}"

        lines.append(f"| {label_str} | {o_str} | {n_str} | {d_str} | {sig_str} |")

    lines.extend([
        "",
        "### 2. 20 Worst Replies (Ranked by Violations)",
        "",
        "| ID | Style | Input Caller Utterance | Reply | Issues |",
        "|---|---|---|---|---|",
    ])

    for i, r in enumerate(worst_replies, 1):
        clean_input = r["input"].replace("|", "\\|")
        clean_reply = r["reply"].replace("|", "\\|")
        prob_str = ", ".join(r["problems"]) if r["problems"] else "None"
        lines.append(f"| {r['id']} | {r['style']} | {clean_input} | {clean_reply} | {prob_str} |")

    return "\n".join(lines)


async def main():
    parser = argparse.ArgumentParser(description="Measure script policy across 100 caller utterances")
    parser.add_argument("--mode", choices=["stub", "live"], default="stub", help="Mode: stub simulation or live endpoint")
    parser.add_argument("--base-url", default=None, help="OpenAI compatible base URL (e.g. http://runpod-endpoint:8000/v1)")
    parser.add_argument("--key-env", default="MME_BRAIN_KEY", help="Environment variable holding the API key")
    parser.add_argument("--model", default="Qwen3-30B-A3B-Instruct-2507", help="Model identifier")
    parser.add_argument("--temperatures", default="0.3", help="Comma-separated temperatures (e.g. 0.2,0.3,0.5)")
    parser.add_argument("--runs", type=int, default=3, help="Number of runs per condition (default 3)")
    parser.add_argument("--out", default="lexicon/reports/script_measurement.md", help="Output report markdown path")
    args = parser.parse_args()

    utterances = load_dataset()
    print(f"Loaded {len(utterances)} caller utterances from {EVAL_FILE.name}")

    temps = [float(t.strip()) for t in args.temperatures.split(",") if t.strip()]
    seeds = [42 + i for i in range(args.runs)]

    stub = None
    live_cfg = None

    if args.mode == "live":
        if not args.base_url:
            print("ERROR: --base-url is required for live mode. Falling back to stub.")
            stub = BrainStub()
        else:
            key = os.environ.get(args.key_env, "")
            live_cfg = {"base_url": args.base_url, "key": key, "model": args.model}
            print(f"Running LIVE measurement against {args.base_url} (model={args.model})")
    else:
        print("[STUB ONLY] Running mock simulation against BrainStub...")
        stub = BrainStub()

    is_stub_run = stub is not None

    # Evaluate for primary temperature (0.3) across 3 runs
    primary_temp = temps[0] if temps else 0.3
    print(f"Executing 3 runs per condition at temperature={primary_temp} with seeds={seeds}...")

    old_runs = []
    for s in seeds:
        print(f"  Running OLD prompt (seed={s})...")
        r = await run_single_condition(utterances, OLD_SYSTEM_PROMPT, "OLD_PROMPT", "old",
                                       temperature=primary_temp, seed=s, stub=stub, live_cfg=live_cfg)
        old_runs.append(r)

    new_runs = []
    for s in seeds:
        print(f"  Running NEW prompt (seed={s})...")
        r = await run_single_condition(utterances, NEW_SYSTEM_PROMPT, "NEW_PROMPT", "new",
                                       temperature=primary_temp, seed=s, stub=stub, live_cfg=live_cfg)
        new_runs.append(r)

    old_agg = aggregate_runs_with_spread(old_runs)
    new_agg = aggregate_runs_with_spread(new_runs)

    all_evaluated_replies = []
    for r in old_runs + new_runs:
        all_evaluated_replies.extend(r["results"])

    worst_20 = find_20_worst_replies(all_evaluated_replies)

    # Aggregate unreviewed roman tokens found across all replies
    token_counts = Counter()
    for r in all_evaluated_replies:
        for t in r.get("roman_tokens", []):
            token_counts[t] += 1

    tokens_path = Path("lexicon/reports/unreviewed_roman_tokens.json")
    tokens_path.parent.mkdir(parents=True, exist_ok=True)
    tokens_path.write_text(json.dumps(dict(token_counts.most_common()), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Unreviewed Roman tokens log saved to {tokens_path} ({len(token_counts)} unique tokens)")

    report_md = format_markdown_report(old_agg, new_agg, worst_20, is_stub=is_stub_run, temperatures_evaluated=temps)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report_md, encoding="utf-8")
    print(f"\nMeasurement complete! Report saved to {out_path}")
    print("\n" + report_md)


if __name__ == "__main__":
    asyncio.run(main())
