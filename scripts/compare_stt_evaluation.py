"""STT Evaluation Comparison Script (Offline).

Compares heard transcripts from an evaluation session against the 12 reference
counselling lines and canonical glossary terms.
Constraint: Outputs counts and terms only (never prints phone numbers, keys, or raw personal data).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# The 12 canonical target terms tested during the evaluation reading
CANONICAL_TARGET_TERMS = [
    "B.Tech",
    "JEE Main",
    "percentile",
    "BCA",
    "BC category",
    "New Zealand",
    "abroad",
    "scholarship",
    "hostel",
    "NEET",
    "MBA",
    "BBA",
]

# Known raw phonetic variants observed before glossary/worker correction
PHONETIC_VARIANTS = {
    "B.Tech": ["beetaik", "bee taik", "bee tek", "btek"],
    "JEE Main": ["je ee men", "jee mane", "je ee main"],
    "percentile": ["parsentaail", "parsentaile", "percentail"],
    "BCA": ["bi si"],
    "New Zealand": ["nyoo jeelaind", "new zeeland", "nyu ziland"],
    "abroad": ["eprod"],
}


def evaluate_transcripts(heard_lines: list[str]) -> dict:
    """Evaluates a list of heard lines against canonical terms and phonetic variants.

    Returns summary metrics (counts and terms only).
    """
    total_heard_turns = len(heard_lines)
    canonical_hits = {term: 0 for term in CANONICAL_TARGET_TERMS}
    phonetic_hits = {term: 0 for term in PHONETIC_VARIANTS}
    unrecognized_terms_count = 0

    combined_text = " ".join(heard_lines).lower()

    for term in CANONICAL_TARGET_TERMS:
        # Check canonical occurrence (case-insensitive)
        pattern = r"\b" + re.escape(term.lower()) + r"\b"
        matches = len(re.findall(pattern, combined_text))
        canonical_hits[term] = matches

    for term, variants in PHONETIC_VARIANTS.items():
        for var in variants:
            pattern = r"\b" + re.escape(var.lower()) + r"\b"
            matches = len(re.findall(pattern, combined_text))
            phonetic_hits[term] += matches

    # Calculate overall terms recognized vs phonetic
    total_canonical_detected = sum(canonical_hits.values())
    total_phonetic_raw_detected = sum(phonetic_hits.values())

    return {
        "turns_evaluated_count": total_heard_turns,
        "total_canonical_detected_count": total_canonical_detected,
        "total_phonetic_raw_detected_count": total_phonetic_raw_detected,
        "canonical_terms_summary": {k: v for k, v in canonical_hits.items() if v > 0},
        "phonetic_unconverted_summary": {k: v for k, v in phonetic_hits.items() if v > 0},
    }


def main():
    parser = argparse.ArgumentParser(description="Evaluate STT heard lines against canonical glossary terms.")
    parser.add_argument("--file", help="Path to JSON file containing heard utterances: ['line 1', ...]")
    parser.add_argument("--demo", action="store_true", help="Run with demonstration reference test lines")
    args = parser.parse_args()

    if args.demo:
        sample_heard = [
            "Mujhe beetaik mein admission lena hai",
            "je ee men ka parsentaail kitna chahiye",
            "bi si course ke liye eligibility kya hai",
            "nyoo jeelaind mein study eprod options hain kya",
            "hostel fee aur scholarship milti hai kya",
            "NEET MBA BBA admissions kab start hain",
        ]
        results = evaluate_transcripts(sample_heard)
        print("=== STT EVALUATION REPORT (COUNTS AND TERMS ONLY) ===")
        print(f"Turns Evaluated: {results['turns_evaluated_count']}")
        print(f"Canonical Terms Detected: {results['total_canonical_detected_count']}")
        print(f"Phonetic Unconverted Detected: {results['total_phonetic_raw_detected_count']}")
        print("Canonical Detected Terms:", list(results['canonical_terms_summary'].keys()))
        print("Phonetic Detected Terms:", list(results['phonetic_unconverted_summary'].keys()))
        return

    if not args.file:
        print("Provide --file <path_to_json> or --demo")
        sys.exit(1)

    p = Path(args.file)
    if not p.exists():
        print(f"Error: file not found: {p}")
        sys.exit(1)

    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Accepts either list of strings or list of turn dicts with "heard"
    lines = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, str):
                lines.append(item)
            elif isinstance(item, dict) and "heard" in item:
                lines.append(item["heard"])

    report = evaluate_transcripts(lines)
    print("=== STT EVALUATION REPORT (COUNTS AND TERMS ONLY) ===")
    print(f"Turns Evaluated: {report['turns_evaluated_count']}")
    print(f"Canonical Terms Detected: {report['total_canonical_detected_count']}")
    print(f"Phonetic Raw Detected: {report['total_phonetic_raw_detected_count']}")
    print("Canonical Detected Terms:", list(report['canonical_terms_summary'].keys()))
    print("Phonetic Unconverted Terms:", list(report['phonetic_unconverted_summary'].keys()))


if __name__ == "__main__":
    main()
