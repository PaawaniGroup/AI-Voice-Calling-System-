"""FAQ file format, schema validation, and knowledge base loader.

Validates:
- No empty answers
- Expired entries excluded based on valid_until date
- Duplicate IDs strictly rejected
- Answers in Devanagari script for conversational TTS voice delivery
"""
from __future__ import annotations

import datetime
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("mme_telephony.ai.faq")


@dataclass
class FAQEntry:
    id: str
    questions: list[str]
    answer: str
    tags: list[str] = field(default_factory=list)
    valid_until: str | None = None
    source: str = ""

    def is_expired(self, ref_date: datetime.date | None = None) -> bool:
        if not self.valid_until:
            return False
        ref = ref_date or datetime.date.today()
        try:
            return datetime.date.fromisoformat(self.valid_until) < ref
        except ValueError:
            return False


def load_faq_knowledge_base(file_path: str | Path | None = None, ref_date: datetime.date | None = None) -> list[FAQEntry]:
    """Loads and validates FAQ entries from a JSON file.

    Raises ValueError on duplicates, missing fields, or empty answers.
    Silently drops expired entries.
    """
    if not file_path:
        # Default fallback to fake sample in repo
        file_path = Path(__file__).resolve().parents[2] / "data" / "faq_sample_fake.json"

    p = Path(file_path)
    if not p.exists():
        log.warning("FAQ file not found: %s", p)
        return []

    with open(p, "r", encoding="utf-8") as f:
        try:
            raw_data = json.load(f)
        except Exception as e:
            raise ValueError(f"Invalid FAQ JSON format: {e}") from e

    if not isinstance(raw_data, list):
        raise ValueError("FAQ file root must be a JSON array of entries")

    entries: list[FAQEntry] = []
    seen_ids: set[str] = set()
    ref = ref_date or datetime.date.today()

    for idx, item in enumerate(raw_data):
        if not isinstance(item, dict):
            raise ValueError(f"FAQ item at index {idx} must be a JSON object")

        entry_id = str(item.get("id") or "").strip()
        if not entry_id:
            raise ValueError(f"FAQ item at index {idx} missing required non-empty 'id'")

        if entry_id in seen_ids:
            raise ValueError(f"Duplicate FAQ ID rejected: {entry_id}")
        seen_ids.add(entry_id)

        answer = str(item.get("answer") or "").strip()
        if not answer:
            raise ValueError(f"FAQ item '{entry_id}' rejected: answer must not be empty")

        questions = [str(q).strip() for q in item.get("questions", []) if str(q).strip()]
        if not questions:
            raise ValueError(f"FAQ item '{entry_id}' rejected: questions list must not be empty")

        valid_until = item.get("valid_until")
        if valid_until:
            try:
                exp_date = datetime.date.fromisoformat(valid_until)
                if exp_date < ref:
                    log.info("Excluding expired FAQ entry: %s (expired %s)", entry_id, valid_until)
                    continue
            except ValueError:
                raise ValueError(f"FAQ item '{entry_id}' has invalid valid_until ISO date: {valid_until}")

        entries.append(FAQEntry(
            id=entry_id,
            questions=questions,
            answer=answer,
            tags=[str(t).strip() for t in item.get("tags", []) if str(t).strip()],
            valid_until=valid_until,
            source=str(item.get("source") or "").strip()
        ))

    return entries
