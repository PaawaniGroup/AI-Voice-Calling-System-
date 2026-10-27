import datetime
import pytest
from app.ai.faq import FAQEntry, load_faq_knowledge_base
from app.ai.faq_retriever import FAQRetriever


def test_faq_loader_validates_and_excludes_expired(tmp_path):
    # Valid file with 1 active, 1 expired, 1 duplicate to test
    valid_file = tmp_path / "faq_valid.json"
    valid_file.write_text("""[
        {
            "id": "faq_test_1",
            "questions": ["What is Course X?"],
            "answer": "Course X is a test course.",
            "valid_until": "2030-01-01"
        },
        {
            "id": "faq_test_expired",
            "questions": ["Expired query"],
            "answer": "Expired answer",
            "valid_until": "2020-01-01"
        }
    ]""", encoding="utf-8")

    entries = load_faq_knowledge_base(valid_file, ref_date=datetime.date(2026, 1, 1))
    assert len(entries) == 1
    assert entries[0].id == "faq_test_1"
    assert entries[0].answer == "Course X is a test course."


def test_faq_loader_rejects_duplicate_id(tmp_path):
    dup_file = tmp_path / "faq_dup.json"
    dup_file.write_text("""[
        {"id": "faq_dup", "questions": ["Q1"], "answer": "A1"},
        {"id": "faq_dup", "questions": ["Q2"], "answer": "A2"}
    ]""", encoding="utf-8")

    with pytest.raises(ValueError, match="Duplicate FAQ ID rejected"):
        load_faq_knowledge_base(dup_file)


def test_faq_loader_rejects_empty_answer(tmp_path):
    empty_file = tmp_path / "faq_empty.json"
    empty_file.write_text("""[
        {"id": "faq_empty", "questions": ["Q1"], "answer": "   "}
    ]""", encoding="utf-8")

    with pytest.raises(ValueError, match="answer must not be empty"):
        load_faq_knowledge_base(empty_file)


def test_faq_retriever_bm25_and_threshold():
    entries = [
        FAQEntry(id="faq_1", questions=["Course X fees structure", "Course X fee kitni hai"], answer="Fees for Course X is ₹50,000", tags=["course_x", "fee"]),
        FAQEntry(id="faq_2", questions=["Course Y eligibility criteria"], answer="Eligibility for Course Y is 60%", tags=["course_y", "eligibility"])
    ]
    retriever = FAQRetriever(entries, min_score=1.0)

    # Relevant query matches Course X
    matches = retriever.retrieve("Course X fee details", top_k=3)
    assert len(matches) >= 1
    assert matches[0][0].id == "faq_1"

    # Completely irrelevant query falls below min_score threshold -> returns []
    irrelevant = retriever.retrieve("completely unrelated astrophysics spaceship rocket", top_k=3)
    assert irrelevant == []
