from __future__ import annotations

from typing import Any, Iterable


def ordered_paragraphs(value: Iterable[Any] | None) -> list[dict[str, Any]]:
    """Return paragraph objects in canonical sequence order.

    Old or partially migrated records may lack a valid integer ``sequence``.
    Keep those records deterministic and last, preserving their original order,
    while the quality guard reports the contract defect separately.
    """

    indexed = [
        (index, item)
        for index, item in enumerate(value or [])
        if isinstance(item, dict)
    ]

    def key(entry: tuple[int, dict[str, Any]]) -> tuple[int, int, int]:
        index, paragraph = entry
        sequence = paragraph.get("sequence")
        if isinstance(sequence, int) and not isinstance(sequence, bool) and sequence >= 1:
            return (0, sequence, index)
        return (1, index, index)

    return [item for _index, item in sorted(indexed, key=key)]


def paragraph_sequence_error(value: Iterable[Any] | None) -> str | None:
    """Describe a non-unique or non-contiguous paragraph sequence."""

    paragraphs = [item for item in (value or []) if isinstance(item, dict)]
    if not paragraphs:
        return None
    sequences = [item.get("sequence") for item in paragraphs]
    if any(
        not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence < 1
        for sequence in sequences
    ):
        return "段落序号必须是从1开始的正整数。"
    duplicates = sorted({sequence for sequence in sequences if sequences.count(sequence) > 1})
    if duplicates:
        return f"段落序号存在重复值：{duplicates}。"
    expected = list(range(1, len(paragraphs) + 1))
    actual = sorted(sequences)
    if actual != expected:
        return f"段落序号必须连续覆盖{expected}，实际为{actual}。"
    return None


def canonical_candidate_text(candidate: dict[str, Any], *, separator: str = "\n\n") -> str:
    """Build visible candidate text using the same order as final export."""

    paragraphs = ordered_paragraphs(candidate.get("paragraphs"))
    if paragraphs:
        return separator.join(
            str(paragraph.get("text") or "").strip()
            for paragraph in paragraphs
            if str(paragraph.get("text") or "").strip()
        )
    return str(candidate.get("candidate_text") or "")
