"""Evaluation script for FAQ retrieval and reply grounding guard.

Tests 30 caller-style queries (Roman Hinglish, Devanagari Hindi, and observed STT phonetic mis-spellings)
against the FAKE FAQ knowledge base.
Reports retrieval hit rates (top-1, top-3) and share of unsupported numbers intercepted by the guard.
Outputs counts and percentages only (never numbers/keys).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.ai.faq import load_faq_knowledge_base
from app.ai.faq_retriever import FAQRetriever
from app.ai.reply_guard import guard_clause

# 30 caller-style evaluation queries (all targeting FAKE courses/exams or ungrounded topics)
EVALUATION_QUERIES = [
    # Group 1: Course X Eligibility (5 queries)
    {"query": "Course X ki eligibility kya hai?", "expected_id": "faq_course_x_eligibility"},
    {"query": "कोर्स X में admission ke liye kya qualification chahiye?", "expected_id": "faq_course_x_eligibility"},
    {"query": "course x elidjibility criteria batao", "expected_id": "faq_course_x_eligibility"},
    {"query": "kya course x 12th pass wale kar sakte hain?", "expected_id": "faq_course_x_eligibility"},
    {"query": "कोर्स X की योग्यता क्या है?", "expected_id": "faq_course_x_eligibility"},

    # Group 2: Course X Fee (5 queries)
    {"query": "Course X ki fees kitni lagti hai?", "expected_id": "faq_course_x_fee"},
    {"query": "course x tuition fee details", "expected_id": "faq_course_x_fee"},
    {"query": "कोर्स X की सालाना फीस क्या है?", "expected_id": "faq_course_x_fee"},
    {"query": "course x total cost kitna padega?", "expected_id": "faq_course_x_fee"},
    {"query": "fees structure for Course X", "expected_id": "faq_course_x_fee"},

    # Group 3: Course Y Admission (5 queries)
    {"query": "Course Y me admission kaise le?", "expected_id": "faq_course_y_admission"},
    {"query": "course y edmission process kya hai?", "expected_id": "faq_course_y_admission"},
    {"query": "कोर्स Y में ऑनलाइन अप्लाई कैसे करें?", "expected_id": "faq_course_y_admission"},
    {"query": "Course Y admission form kab aayega?", "expected_id": "faq_course_y_admission"},
    {"query": "how to apply for Course Y online?", "expected_id": "faq_course_y_admission"},

    # Group 4: Exam Alpha Cut-off (5 queries)
    {"query": "Exam Alpha ka cutoff kitna gaya tha?", "expected_id": "faq_exam_alpha_cutoff"},
    {"query": "exam alpha parsentaail requirement kya hai?", "expected_id": "faq_exam_alpha_cutoff"},
    {"query": "एग्जाम अल्फा कट ऑफ परसेंटाइल", "expected_id": "faq_exam_alpha_cutoff"},
    {"query": "minimum percentile needed for Exam Alpha", "expected_id": "faq_exam_alpha_cutoff"},
    {"query": "exam alpha cut off marks", "expected_id": "faq_exam_alpha_cutoff"},

    # Group 5: Hostel (5 queries)
    {"query": "Hostel facility available hai kya?", "expected_id": "faq_hostel_charges"},
    {"query": "hostal charges kitne hain?", "expected_id": "faq_hostel_charges"},
    {"query": "हॉस्टल की सुविधा और फीस बताइए", "expected_id": "faq_hostel_charges"},
    {"query": "hostel accommodation fees per year", "expected_id": "faq_hostel_charges"},
    {"query": "kya campus me hostel milega?", "expected_id": "faq_hostel_charges"},

    # Group 6: Out-of-Domain / Ungrounded queries (5 queries -> expected no match / empty context)
    {"query": "What is the fee for Course Z in Canada?", "expected_id": None},
    {"query": "Can you explain EPRoD degree?", "expected_id": None},
    {"query": "Aviation pilot license fees", "expected_id": None},
    {"query": "Medical MBBS cut off in Ukraine", "expected_id": None},
    {"query": "Astrophysics rocket propulsion eligibility", "expected_id": None},
]


def run_evaluation():
    print("=== FAQ GROUNDING & RETRIEVAL EVALUATION ===")
    entries = load_faq_knowledge_base()
    retriever = FAQRetriever(entries, min_score=1.5)

    total_queries = len(EVALUATION_QUERIES)
    top1_hits = 0
    top3_hits = 0
    correct_empty_fallbacks = 0
    total_in_domain = sum(1 for q in EVALUATION_QUERIES if q["expected_id"] is not None)
    total_out_of_domain = total_queries - total_in_domain

    unsupported_replies_detected = 0
    guarded_replacements_applied = 0

    # Simulated LLM generation samples (some hallucinating numbers when context is empty)
    for q_item in EVALUATION_QUERIES:
        q = q_item["query"]
        expected = q_item["expected_id"]

        results = retriever.retrieve(q, top_k=3)
        retrieved_ids = [e[0].id for e in results]

        if expected is not None:
            if retrieved_ids and retrieved_ids[0] == expected:
                top1_hits += 1
            if expected in retrieved_ids:
                top3_hits += 1
        else:
            if not retrieved_ids:
                correct_empty_fallbacks += 1

        # Simulate LLM clause with and without context
        context_texts = [e[0].answer for e in results]
        if not context_texts:
            # Model might hallucinate: "The fee is ₹75,000"
            simulated_reply = "जी, इसकी फीस ₹75,000 है और एडमिशन 15 जुलाई से शुरू है।"
        else:
            simulated_reply = f"जी, {context_texts[0]}"

        guarded, replaced = guard_clause(simulated_reply, context_texts, "hi")
        if replaced:
            unsupported_replies_detected += 1
            guarded_replacements_applied += 1

    print(f"Total Queries Evaluated: {total_queries}")
    print(f"In-Domain Queries Count: {total_in_domain}")
    print(f"Out-of-Domain Queries Count: {total_out_of_domain}")
    print(f"Top-1 Retrieval Hits Count: {top1_hits} / {total_in_domain}")
    print(f"Top-3 Retrieval Hits Count: {top3_hits} / {total_in_domain}")
    print(f"Correct Empty Context Fallbacks: {correct_empty_fallbacks} / {total_out_of_domain}")
    print(f"Unsupported Fact Clauses Intercepted by Guard: {unsupported_replies_detected}")
    print(f"Guarded Safe Replacements Applied: {guarded_replacements_applied}")


if __name__ == "__main__":
    run_evaluation()
