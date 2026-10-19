"""Phone number validation/normalisation and destination policy evaluation."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import phonenumbers

from app.core.errors import Unprocessable

_E164 = re.compile(r"^\+[1-9][0-9]{6,14}$")


def normalize(raw: str, default_region: str = "IN") -> str:
    """Return strict E.164 or raise Unprocessable. Output is safe to embed in dial strings."""
    if not isinstance(raw, str) or len(raw) > 32:
        raise Unprocessable("invalid phone number", code="invalid_number")
    cleaned = re.sub(r"[\s\-().]", "", raw)
    if not re.fullmatch(r"\+?[0-9]+", cleaned):
        raise Unprocessable("invalid phone number", code="invalid_number")
    try:
        num = phonenumbers.parse(cleaned, None if cleaned.startswith("+") else default_region)
    except phonenumbers.NumberParseException:
        raise Unprocessable("invalid phone number", code="invalid_number") from None
    if not phonenumbers.is_possible_number(num):
        raise Unprocessable("invalid phone number", code="invalid_number")
    e164 = phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)
    if not _E164.match(e164):
        raise Unprocessable("invalid phone number", code="invalid_number")
    return e164


def region_of(e164: str) -> str | None:
    try:
        return phonenumbers.region_code_for_number(phonenumbers.parse(e164, None))
    except phonenumbers.NumberParseException:
        return None


@dataclass
class DestinationPolicy:
    allowed_countries: list[str] = field(default_factory=lambda: ["IN"])
    deny_prefixes: list[str] = field(default_factory=list)
    allow_prefixes: list[str] = field(default_factory=list)  # if non-empty, destination must match one
    caller_ids: list[str] = field(default_factory=list)  # approved caller IDs (E.164)
    transfer_destinations: list[str] = field(default_factory=list)  # exact E.164 or prefix ending with '*'

    def check_destination(self, e164: str) -> None:
        for p in self.deny_prefixes:
            if e164.startswith(p):
                raise Unprocessable("destination is denied by policy", code="destination_denied")
        if self.allow_prefixes and not any(e164.startswith(p) for p in self.allow_prefixes):
            raise Unprocessable("destination not in allowlist", code="destination_not_allowed")
        region = region_of(e164)
        if self.allowed_countries and region not in self.allowed_countries:
            raise Unprocessable("destination country not permitted", code="destination_country_denied")

    def check_caller_id(self, e164: str) -> None:
        if e164 not in self.caller_ids:
            raise Unprocessable("caller id not approved for tenant", code="caller_id_not_approved")

    def check_transfer(self, e164: str) -> None:
        self.check_destination(e164)
        for rule in self.transfer_destinations:
            if rule.endswith("*") and e164.startswith(rule[:-1]):
                return
            if rule == e164:
                return
        raise Unprocessable("transfer destination not permitted", code="transfer_not_permitted")
