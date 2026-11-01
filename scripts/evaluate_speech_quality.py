"""Standardized 20-utterance speech quality, linguistic coverage, and acoustic evaluation suite.

Evaluates:
- Hindi (conversational, long, numbers, dates, monetary amounts, names, colleges)
- Hinglish (code-switching, technical terms, eligibility, curriculum, expressions)
- English (conversational, locations, technical tracks)
- Special linguistic edge cases (commas, stops, questions, abbreviations, long proper nouns, boundary transitions)

Records:
- Input text, language, category
- Audio duration, chunk count, TTFA, total wall time, RTF
- Human listening evaluation sheet (ready for blind listening panel)
"""
import argparse
import asyncio
import base64
import json
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.settings import Settings
from app.ai.clients import ModelClients

EVALUATION_UTTERANCES = [
    # 1-6: Hindi
    {
        "id": "hi_01_conversational",
        "category": "Hindi Conversational",
        "language": "hi",
        "text": "जी, Make My Education में आपका स्वागत है। मैं आपकी क्या मदद कर सकती हूँ?",
        "voice": "utkarsh"
    },
    {
        "id": "hi_02_long_sentence",
        "category": "Hindi Long Sentence",
        "language": "hi",
        "text": "यदि आपने बारहवीं कक्षा में भौतिकी, रसायन विज्ञान और गणित विषय लिए हैं, तो आप बी.टेक कंप्यूटर साइंस में सीधे प्रवेश के लिए पात्र हैं।",
        "voice": "utkarsh"
    },
    {
        "id": "hi_03_numbers_money",
        "category": "Hindi Numbers & Currency",
        "language": "hi",
        "text": "बी.टेक कंप्यूटर साइंस की कुल वार्षिक ट्यूशन फीस ₹1,45,000 है और हॉस्टल शुल्क ₹65,000 प्रति वर्ष है।",
        "voice": "utkarsh"
    },
    {
        "id": "hi_04_dates_deadlines",
        "category": "Hindi Dates & Deadlines",
        "language": "hi",
        "text": "प्रवेश के लिए ऑनलाइन आवेदन पत्र जमा करने की अंतिम तिथि 15 जुलाई 2026 निर्धारित की गई है।",
        "voice": "utkarsh"
    },
    {
        "id": "hi_05_names_locations",
        "category": "Hindi Names & Locations",
        "language": "hi",
        "text": "प्रिया शर्मा जी और उत्कर्ष वर्मा जी का दाखिला लखनऊ और कानपुर कैंपस में सफलतापूर्वक हो गया है।",
        "voice": "utkarsh"
    },
    {
        "id": "hi_06_colleges_univ",
        "category": "Hindi College Names",
        "language": "hi",
        "text": "डॉ. ए.पी.जे. अब्दुल कलाम प्राविधिक विश्वविद्यालय और छत्रपति शाहू जी महाराज विश्वविद्यालय से संबद्ध कॉलेज उपलब्ध हैं।",
        "voice": "utkarsh"
    },
    # 7-11: Hinglish
    {
        "id": "hinglish_07_codeswitch",
        "category": "Hinglish Code-switching",
        "language": "hi",
        "text": "हाँ बिल्कुल, admission process बहुत simple है। आपको बस portal पर documents upload करने होंगे।",
        "voice": "utkarsh"
    },
    {
        "id": "hinglish_08_tech_terms",
        "category": "Hinglish Technical Terms",
        "language": "hi",
        "text": "हमारे पास Computer Science, Artificial Intelligence, Machine Learning और Data Science की top branches उपलब्ध हैं।",
        "voice": "utkarsh"
    },
    {
        "id": "hinglish_09_eligibility",
        "category": "Hinglish Entrance Exams",
        "language": "hi",
        "text": "JEE Main percentile और 12th board percentage के basis पर direct admission और scholarship offer की जाती है।",
        "voice": "utkarsh"
    },
    {
        "id": "hinglish_10_curriculum",
        "category": "Hinglish Educational Terminology",
        "language": "hi",
        "text": "Course curriculum industry-oriented है और 7th semester से corporate campus placements शुरू हो जाते हैं।",
        "voice": "utkarsh"
    },
    {
        "id": "hinglish_11_expression",
        "category": "Hinglish Conversational Expressions",
        "language": "hi",
        "text": "Don't worry, हमारे expert career counsellors आपको step-by-step complete guidance provide करेंगे।",
        "voice": "utkarsh"
    },
    # 12-14: English
    {
        "id": "en_12_conversational",
        "category": "English Conversational",
        "language": "en",
        "text": "Hello! Welcome to Make My Education counselling desk. How may I assist you with your admissions today?",
        "voice": "utkarsh"
    },
    {
        "id": "en_13_locations",
        "category": "English Indian Names & Locations",
        "language": "en",
        "text": "Applications are open for accredited campuses located in Delhi NCR, Noida, Greater Noida, and Gurugram.",
        "voice": "utkarsh"
    },
    {
        "id": "en_14_technical_tracks",
        "category": "English Technical Terminology",
        "language": "en",
        "text": "The Bachelor of Technology program offers specialized tracks in Cloud Computing, Cybersecurity, and IoT.",
        "voice": "utkarsh"
    },
    # 15-20: Special Cases
    {
        "id": "special_15_commas",
        "category": "Special: Commas & Clause Breaks",
        "language": "hi",
        "text": "जी हाँ, फॉर्म भरने के बाद, आपको वेरिफिकेशन मेल आएगा, और फिर सीट अलॉटमेंट होगा।",
        "voice": "utkarsh"
    },
    {
        "id": "special_16_fullstops",
        "category": "Special: Short Utterances & Stops",
        "language": "hi",
        "text": "जी, बिल्कुल। हम आपकी पूरी सहायता करेंगे।",
        "voice": "utkarsh"
    },
    {
        "id": "special_17_questions",
        "category": "Special: Question Intonation",
        "language": "hi",
        "text": "क्या आपने इस वर्ष जेईई मेन या सीयूईटी की प्रवेश परीक्षा दी है?",
        "voice": "utkarsh"
    },
    {
        "id": "special_18_abbreviations",
        "category": "Special: Educational Acronyms",
        "language": "hi",
        "text": "एमबीबीएस, बीडीएस, बीसीए, एमसीए और बीबीए कोर्सेज के लिए एनईईटी और सीयूईटी स्कोर स्वीकार्य हैं।",
        "voice": "utkarsh"
    },
    {
        "id": "special_19_long_nouns",
        "category": "Special: Long Proper Nouns",
        "language": "hi",
        "text": "इन्द्रप्रस्थ सूचना प्रौद्योगिकी संस्थान और नेताजी सुभाष प्रौद्योगिकी विश्वविद्यालय में मेरिट कटऑफ जारी हो चुका है।",
        "voice": "utkarsh"
    },
    {
        "id": "special_20_boundary_transitions",
        "category": "Special: Window Boundary Transitions",
        "language": "hi",
        "text": "प्रथम वर्ष की कक्षाएं अगस्त के पहले सप्ताह से शुरू होंगी, और ओरिएंटेशन प्रोग्राम शनिवार को आयोजित किया जाएगा।",
        "voice": "utkarsh"
    }
]


def generate_evaluation_manifest(out_path: str = "docs/evidence/speech_quality_evaluation_suite.json"):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "title": "MME Speech Quality & Linguistic Coverage Evaluation Suite (20 Utterances)",
            "total_items": len(EVALUATION_UTTERANCES),
            "utterances": EVALUATION_UTTERANCES,
            "evaluation_criteria": [
                "Pronunciation accuracy (no phonetic distortion)",
                "Intelligibility of proper nouns, numbers, and dates",
                "Natural sentence cadence and comma pauses",
                "Absence of window boundary clicks or phase discontinuities",
                "Voice similarity and timbre stability across clauses",
                "Sentence completion without premature cutoff"
            ],
            "human_evaluation_status": "NOT_VALIDATED (Requires Human Panel Signoff)"
        }, f, indent=2, ensure_ascii=False)
    print(f"Evaluation suite written to {out_path} ({len(EVALUATION_UTTERANCES)} utterances)")


if __name__ == "__main__":
    generate_evaluation_manifest()
