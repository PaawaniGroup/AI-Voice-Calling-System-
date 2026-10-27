"""Tests for the streaming reply guard: selective factual extraction and no-promise fallbacks.

Verifies:
1. General conversational numbers (12th ke baad, 4 saal, 5 minute, B.Tech 2026) are NOT blocked.
2. Rupee, percentage, and date claims ARE blocked when absent from context.
3. Supported numbers and dates in retrieved context pass through safely.
4. Softened fallbacks promise nothing (no callback, contact, or scheduling).
"""
import pytest
from app.ai.reply_guard import (
    guard_clause,
    UNSUPPORTED_REPLACEMENT_HI,
    UNSUPPORTED_REPLACEMENT_EN,
)


def test_general_numbers_are_not_blocked():
    """Verify conversational numbers not representing fees or dates are allowed without context."""
    empty_ctx = []

    # 1. '12th ke baad'
    clause_1 = "12th ke baad aap B.Tech me admission le sakte hain."
    guarded_1, replaced_1 = guard_clause(clause_1, empty_ctx, language_mode="hi")
    assert replaced_1 is False
    assert guarded_1 == clause_1

    # 2. '4 saal ka course'
    clause_2 = "Yeh 4 saal ka undergraduate engineering course hai."
    guarded_2, replaced_2 = guard_clause(clause_2, empty_ctx, language_mode="hi")
    assert replaced_2 is False
    assert guarded_2 == clause_2

    # 3. '5 minute mein'
    clause_3 = "Main 5 minute mein details check karta hoon."
    guarded_3, replaced_3 = guard_clause(clause_3, empty_ctx, language_mode="hi")
    assert replaced_3 is False
    assert guarded_3 == clause_3

    # 4. Year inside course/batch name: 'B.Tech 2026'
    clause_4 = "B.Tech 2026 batch ke liye applications shuru ho chuki hain."
    guarded_4, replaced_4 = guard_clause(clause_4, empty_ctx, language_mode="hi")
    assert replaced_4 is False
    assert guarded_4 == clause_4


def test_rupee_percentage_and_date_claims_are_blocked_when_unsupported():
    """Verify specific factual claims about fees, percentages, or dates are intercepted."""
    empty_ctx = ["General campus info only."]

    # Rupee / fee claim
    clause_fee = "B.Tech ki annual fees ₹65,000 hai."
    guarded_fee, replaced_fee = guard_clause(clause_fee, empty_ctx, language_mode="hi")
    assert replaced_fee is True
    assert guarded_fee == UNSUPPORTED_REPLACEMENT_HI

    # Percentage / cutoff claim
    clause_pct = "Minimum eligibility criteria 75% marks hai."
    guarded_pct, replaced_pct = guard_clause(clause_pct, empty_ctx, language_mode="hi")
    assert replaced_pct is True
    assert guarded_pct == UNSUPPORTED_REPLACEMENT_HI

    # Calendar date claim
    clause_date = "Application form submit karne ki last date 15 August hai."
    guarded_date, replaced_date = guard_clause(clause_date, empty_ctx, language_mode="hi")
    assert replaced_date is True
    assert guarded_date == UNSUPPORTED_REPLACEMENT_HI

    # Standalone month claim
    clause_month = "Admissions July mein shuru honge."
    guarded_month, replaced_month = guard_clause(clause_month, empty_ctx, language_mode="hi")
    assert replaced_month is True
    assert guarded_month == UNSUPPORTED_REPLACEMENT_HI


def test_supported_claims_pass_through():
    """Verify claims matching context pass through unaltered."""
    ctx = [
        "Course X: Total fee ₹65,000, eligibility 75% in PCM, application deadline 15 August."
    ]
    clause = "Course X ki fees ₹65,000 hai aur last date 15 August hai."
    guarded, replaced = guard_clause(clause, ctx, language_mode="hi")
    assert replaced is False
    assert guarded == clause


def test_softened_fallbacks_promise_no_action():
    """Verify English and Hindi fallbacks promise no actions or callbacks."""
    assert UNSUPPORTED_REPLACEMENT_EN == "I don't have confirmed details on that."
    assert UNSUPPORTED_REPLACEMENT_HI == "सही जानकारी हमारे काउंसलर से पक्की करनी होगी।"

    for phrase in ("callback", "will call", "contact you", "संपर्क करेंगे", "कॉल"):
        assert phrase not in UNSUPPORTED_REPLACEMENT_EN.lower()
        assert phrase not in UNSUPPORTED_REPLACEMENT_HI.lower()
