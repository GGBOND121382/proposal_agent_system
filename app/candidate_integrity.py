from __future__ import annotations

import copy
import re
from typing import Any, Iterable

from .paragraph_order import ordered_paragraphs
from .util import sha256_json, sha256_text


def _normalized_visible_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def canonical_paragraph_texts(candidate: dict[str, Any]) -> list[str]:
    paragraphs = ordered_paragraphs(candidate.get("paragraphs"))
    if paragraphs:
        return [
            str(paragraph.get("text") or "").strip()
            for paragraph in paragraphs
            if str(paragraph.get("text") or "").strip()
        ]
    fallback = str(candidate.get("candidate_text") or "").strip()
    return [fallback] if fallback else []


def paragraph_identity_error(value: Iterable[Any] | None) -> str | None:
    paragraphs = [item for item in (value or []) if isinstance(item, dict)]
    if not paragraphs:
        return None
    identifiers = [str(item.get("paragraph_id") or "").strip() for item in paragraphs]
    missing = [index + 1 for index, paragraph_id in enumerate(identifiers) if not paragraph_id]
    if missing:
        return f"段落缺少paragraph_id，位置为{missing}。"
    duplicates = sorted({item for item in identifiers if identifiers.count(item) > 1})
    if duplicates:
        return f"paragraph_id存在重复值：{duplicates}。"
    return None


def candidate_text_divergence(candidate: dict[str, Any]) -> str | None:
    paragraphs = ordered_paragraphs(candidate.get("paragraphs"))
    legacy = str(candidate.get("candidate_text") or "").strip()
    if not paragraphs or not legacy:
        return None
    canonical = "\n\n".join(
        str(paragraph.get("text") or "").strip()
        for paragraph in paragraphs
        if str(paragraph.get("text") or "").strip()
    )
    if _normalized_visible_text(legacy) != _normalized_visible_text(canonical):
        return "candidate_text与按sequence排序后的paragraphs正文不一致。"
    return None


def canonical_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(candidate)
    result["paragraphs"] = ordered_paragraphs(candidate.get("paragraphs"))
    texts = canonical_paragraph_texts(candidate)
    result["candidate_text"] = "\n\n".join(texts)
    return result


def visible_candidate_snapshot(
    candidates: Iterable[dict[str, Any]],
    *,
    document_section_map: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Hash the exact section text/order visible to review and export.

    Candidate items may be Context Builder objects of the form
    ``{section_id, candidate}`` or exporter records containing ``paragraphs`` as
    already-normalized text strings.  Run/provenance identifiers are omitted on
    purpose: this identity protects the reviewed visible candidate set, while
    separate manifests continue to bind the producing runs.
    """

    by_section: dict[str, dict[str, Any]] = {}
    input_order: list[str] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        section = item.get("section") if isinstance(item.get("section"), dict) else {}
        section_id = str(item.get("section_id") or section.get("section_id") or "").strip()
        candidate = item.get("candidate") if isinstance(item.get("candidate"), dict) else item
        candidate_id = str((candidate or {}).get("candidate_id") or item.get("candidate_id") or "").strip()
        title = str(
            item.get("title")
            or item.get("section_title")
            or section.get("title")
            or section.get("section_key")
            or ""
        ).strip()
        raw_paragraphs = (candidate or {}).get("paragraphs")
        if raw_paragraphs and all(isinstance(value, str) for value in raw_paragraphs):
            paragraph_texts = [str(value).strip() for value in raw_paragraphs if str(value).strip()]
        else:
            paragraph_texts = canonical_paragraph_texts(candidate or {})
        if not section_id or not candidate_id:
            continue
        raw_ids = (candidate or {}).get("paragraph_ids") or item.get("paragraph_ids")
        if raw_ids:
            paragraph_ids = [str(value).strip() for value in raw_ids if str(value).strip()]
        else:
            paragraph_ids = [
                str(paragraph.get("paragraph_id") or "").strip()
                for paragraph in ordered_paragraphs((candidate or {}).get("paragraphs"))
                if str(paragraph.get("paragraph_id") or "").strip()
            ]
        record = {
            "section_id": section_id,
            "section_title": title,
            "candidate_id": candidate_id,
            "paragraph_ids": paragraph_ids,
            "paragraph_hashes": [sha256_text(text) for text in paragraph_texts],
            "candidate_visible_hash": sha256_json(paragraph_texts),
        }
        by_section[section_id] = record
        if section_id not in input_order:
            input_order.append(section_id)

    ordered_ids: list[str] = []
    for item in document_section_map or []:
        if not isinstance(item, dict):
            continue
        section_id = str(item.get("section_id") or "").strip()
        if section_id in by_section and section_id not in ordered_ids:
            ordered_ids.append(section_id)
            if not by_section[section_id]["section_title"]:
                by_section[section_id]["section_title"] = str(item.get("title") or "").strip()
    ordered_ids.extend(section_id for section_id in input_order if section_id not in ordered_ids)
    sections = [by_section[section_id] for section_id in ordered_ids]
    core = {"section_count": len(sections), "sections": sections}
    return {**core, "visible_candidate_set_hash": sha256_json(core)}


def visible_document_snapshot(value: Any) -> dict[str, Any]:
    """Hash the ordered section titles and visible text reviewed or exported."""

    records: list[dict[str, Any]] = []
    if isinstance(value, dict) and isinstance(value.get("sections"), list):
        for item in value.get("sections") or []:
            if not isinstance(item, dict):
                continue
            section_id = str(item.get("section_id") or "").strip()
            title = str(item.get("title") or item.get("section_title") or "").strip()
            text = str(item.get("text") or "").strip()
            if not section_id:
                continue
            block_ids = [
                str(block_id).strip()
                for block_id in item.get("block_ids") or []
                if str(block_id).strip()
            ]
            records.append({
                "section_id": section_id,
                "section_title": title,
                "block_ids": block_ids,
                "visible_text_hash": sha256_text(text),
            })
    else:
        for item in value or []:
            if not isinstance(item, dict):
                continue
            section_id = str(item.get("section_id") or "").strip()
            title = str(item.get("title") or item.get("section_title") or "").strip()
            candidate = item.get("candidate") if isinstance(item.get("candidate"), dict) else item
            raw_paragraphs = (candidate or {}).get("paragraphs")
            if raw_paragraphs and all(isinstance(block, str) for block in raw_paragraphs):
                text = "\n\n".join(str(block).strip() for block in raw_paragraphs if str(block).strip())
            else:
                text = "\n\n".join(canonical_paragraph_texts(candidate or {}))
            if not section_id:
                continue
            raw_ids = (candidate or {}).get("paragraph_ids") or item.get("paragraph_ids")
            if raw_ids:
                block_ids = [str(value).strip() for value in raw_ids if str(value).strip()]
            else:
                block_ids = [
                    str(paragraph.get("paragraph_id") or "").strip()
                    for paragraph in ordered_paragraphs((candidate or {}).get("paragraphs"))
                    if str(paragraph.get("paragraph_id") or "").strip()
                ]
            records.append({
                "section_id": section_id,
                "section_title": title,
                "block_ids": block_ids,
                "visible_text_hash": sha256_text(text),
            })
    core = {"section_count": len(records), "sections": records}
    return {**core, "reviewed_document_hash": sha256_json(core)}
