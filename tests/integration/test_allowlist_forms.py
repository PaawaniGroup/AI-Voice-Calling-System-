"""The caller allowlist must match the operator's number however the carrier presents it: +91..., 91... and national 0... forms, and a
call with NO caller ID must be rejected. Plivo and the models are MOCKS (same harness as test_plivo_voice)."""
import xml.etree.ElementTree as ET

import pytest

from tests.integration.test_plivo_voice import form, post_answer, pv  # noqa: F401  (`pv` is a fixture)

ALLOWED = "+919876543210"       # a test number; the same 10 digits are presented in each form below


async def _arm(pv):
    await pv.repos.put_policy("t1", {"enabled": True, "max_active_calls": 5, "allowed_callers": [ALLOWED]})


def _answered(text: str) -> bool:
    return ET.fromstring(text).find("Stream") is not None


@pytest.mark.parametrize("presented", ["+919876543210", "919876543210", "09876543210", "+91 98765 43210", "0 98765 43210"],
                         ids=["plus91", "91", "national0", "plus91-spaced", "national0-spaced"])
async def test_the_allowed_number_is_answered_in_every_presentation(pv, presented):
    await _arm(pv)
    r = await post_answer(pv, form(**{"From": presented}))
    assert _answered(r.text), f"the allowed caller presented as {presented!r} was REJECTED"


@pytest.mark.parametrize("presented", ["", "anonymous", "Anonymous", "restricted", "unavailable", "0", "+"], ids=lambda v: repr(v) or "empty")
async def test_a_call_without_a_usable_caller_id_is_rejected(pv, presented):
    await _arm(pv)
    r = await post_answer(pv, form(**{"From": presented}))
    assert not _answered(r.text) and await pv.db["calls"].count_documents({}) == 0 and pv.models.calls == []


async def test_a_call_with_the_From_field_missing_is_rejected(pv):
    await _arm(pv)
    params = form()
    params.pop("From")
    r = await post_answer(pv, params)
    assert not _answered(r.text) and await pv.db["calls"].count_documents({}) == 0


async def test_other_numbers_in_any_presentation_stay_rejected(pv):
    await _arm(pv)
    for presented in ("919000000009", "+919000000009", "09000000009"):
        r = await post_answer(pv, form(**{"From": presented}))
        assert not _answered(r.text), presented
    assert await pv.db["calls"].count_documents({}) == 0
