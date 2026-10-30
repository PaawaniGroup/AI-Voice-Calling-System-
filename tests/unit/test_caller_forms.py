"""phone.normalize_caller: the operator's own number is recognised however it is presented, and nothing else is ever mistaken for it.
Used by the Plivo inbound caller allowlist."""
import pytest

from app.core.phone import normalize_caller

ME = "+919876543210"   # a test number


@pytest.mark.parametrize("presented", [
    "+919876543210", "919876543210", "09876543210", "00919876543210",                         # the forms a carrier may present
    "+91 98765 43210", "+91-98765-43210", "91 98765-43210", "0 98765 43210", "09876-543 210",  # whitespace / punctuation
    "+91 (987) 654-3210", "0(98765)43210", " 919876543210 ", "+91.98765.43210", "(0)98765 43210",
])
def test_every_presentation_of_the_number_resolves_to_it(presented):
    assert normalize_caller(presented) == ME, presented


@pytest.mark.parametrize("presented", [
    "+919876543211", "919876543200", "09876543211", "09876543201", "08876543210", "919876543310", "+917876543210",
    "00919876543211", "+91 98765 43211", "0 98765 43211",
])
def test_numbers_differing_by_one_digit_never_match(presented):
    assert normalize_caller(presented) != ME, presented


@pytest.mark.parametrize("presented", [
    "+449876543210", "+19876543210", "+14159876543210", "+61987654321", "+971509876543", "+8613876543210",    # same ending digits, other countries
    "+4409876543210", "0044 9876543210", "001 987 654 3210", "+49 9876543210", "+33 987654321",
])
def test_another_countrys_number_with_the_same_ending_digits_never_matches(presented):
    assert normalize_caller(presented) != ME, presented


@pytest.mark.parametrize("presented", [
    "", " ", "+", "0", "00", "000", "9", "abc", "anonymous", "Restricted", "unavailable", "0+9876543210", "+91+9876543210",
    "09876x43210", "91987654321O", "+91 98765 43210 ext 5", "९८७६५४३२१०", None, 9876543210, b"919876543210",
    "9198765432109999999999999999999999",
])
def test_unreadable_caller_ids_are_none(presented):
    assert normalize_caller(presented) is None, repr(presented)


@pytest.mark.parametrize("presented", ["91", "+91", "098765", "09876", "+91987", "91 98765", "0987654321", "919876543", "+9198765432", "09876543210999"])
def test_short_or_over_long_numbers_never_match(presented):
    assert normalize_caller(presented) != ME, presented


def test_nothing_without_a_leading_zero_is_ever_read_in_the_default_region():
    # a bare 10-digit number (no country code, no trunk 0) is NOT assumed to be Indian
    assert normalize_caller("9876543210") != ME
    assert normalize_caller("7302983395") != "+917302983395"


def test_default_region_only_applies_to_the_national_form():
    assert normalize_caller("09876543210", "IN") == ME
    assert normalize_caller("09876543210", "US") != ME
