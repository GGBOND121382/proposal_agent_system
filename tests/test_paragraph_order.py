from __future__ import annotations

from app.context_base import ContextBuilder
from app.paragraph_order import (
    canonical_candidate_text,
    ordered_paragraphs,
    paragraph_sequence_error,
)


def _paragraph(paragraph_id: str, sequence: int, text: str) -> dict:
    return {
        "paragraph_id": paragraph_id,
        "sequence": sequence,
        "text": text,
    }


def test_ordered_paragraphs_uses_sequence_not_array_order():
    candidate = {
        "candidate_text": "stale array-order text",
        "paragraphs": [
            _paragraph("p-2", 2, "second"),
            _paragraph("p-1", 1, "first"),
        ],
    }

    assert [item["paragraph_id"] for item in ordered_paragraphs(candidate["paragraphs"])] == ["p-1", "p-2"]
    assert canonical_candidate_text(candidate) == "first\n\nsecond"
    assert paragraph_sequence_error(candidate["paragraphs"]) is None


def test_paragraph_sequence_reports_duplicate_and_gap():
    duplicate = [_paragraph("p-1", 1, "first"), _paragraph("p-2", 1, "second")]
    gap = [_paragraph("p-1", 1, "first"), _paragraph("p-2", 3, "second")]

    assert "重复" in paragraph_sequence_error(duplicate)
    assert "连续" in paragraph_sequence_error(gap)


def test_integration_and_security_context_use_export_order():
    candidate = {
        "candidate_id": "candidate-1",
        "candidate_text": "wrong order",
        "paragraphs": [
            _paragraph("p-2", 2, "second"),
            _paragraph("p-1", 1, "first"),
        ],
        "trace_links": [],
        "term_usage": [],
        "unresolved_items": [],
        "claim_advancement": {},
    }

    integration = ContextBuilder._integration_candidate(candidate)
    document = ContextBuilder._candidate_document(
        None,
        {"id": "project-1", "security_level": "INTERNAL"},
        [{
            "section": {"section_id": "section-1", "title": "Section", "level": 1},
            "candidate": candidate,
        }],
    )

    assert [item["paragraph_id"] for item in integration["paragraphs"]] == ["p-1", "p-2"]
    assert integration["candidate_text"] == "first\n\nsecond"
    assert document["sections"][0]["text"] == "first\n\nsecond"
    assert document["sections"][0]["block_ids"] == ["p-1", "p-2"]
