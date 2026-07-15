from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..util import new_id, sha256_bytes, sha256_text, utc_now, write_json
from .research_plan import canonical_url, normalize_doi, parse_year

_BASELINE_TERMS = {"baseline", "benchmark", "comparison", "comparative", "survey", "review", "基线", "对比", "比较", "综述", "评测", "现有方法"}
_LIMITATION_TERMS = {"limitation", "limitations", "challenge", "challenges", "gap", "open problem", "drawback", "局限", "不足", "挑战", "差距", "瓶颈"}


def _contains_any(text: str, terms: set[str]) -> bool:
    lowered = str(text or "").lower()
    return any(term in lowered for term in terms)


def source_category(record: dict[str, Any]) -> str:
    domain = str(record.get("domain") or urlparse(str(record.get("final_url") or record.get("url") or "")).netloc).lower()
    publisher = str(record.get("publisher") or "").lower()
    if any(token in domain for token in ("iso.org", "iec.ch", "rfc-editor.org", "itu.int", "standards.")):
        return "OFFICIAL_STANDARD"
    if domain.endswith(".gov") or domain.endswith(".gov.cn") or ".gov." in domain:
        return "GOVERNMENT"
    if record.get("doi") or any(token in domain for token in ("doi.org", "ieeexplore.ieee.org", "dl.acm.org", "springer.com", "sciencedirect.com")):
        return "PEER_REVIEWED_PAPER"
    if any(token in domain for token in ("arxiv.org", "openreview.net", "semanticscholar.org")):
        return "ACADEMIC_REPOSITORY"
    if domain.endswith(".edu") or domain.endswith(".edu.cn") or "ac.cn" in domain:
        return "ACADEMIC_REPOSITORY"
    if any(token in domain for token in ("docs.", "readthedocs", "developer.", "github.com")):
        return "TECHNICAL_DOCUMENTATION"
    if any(token in publisher for token in ("ministry", "commission", "department", "研究院", "委员会", "政府")):
        return "GOVERNMENT"
    return "OTHER"


def coverage_report(records: list[dict[str, Any]], plan: dict[str, Any]) -> dict[str, Any]:
    by_query: dict[str, dict[str, Any]] = {}
    for query in plan.get("queries", []):
        matched = [record for record in records if record.get("matched_query") == query]
        by_query[query] = {
            "source_count": len(matched),
            "authoritative_source_count": sum(1 for record in matched if int(record.get("authority_rank") or 0) >= 80),
            "source_ids": [record["source_id"] for record in matched],
        }
    recent = [record for record in records if record.get("is_recent")]
    baselines = [record for record in records if record.get("supports_baseline")]
    limitations = [record for record in records if record.get("supports_limitation")]
    uncovered = [query for query, item in by_query.items() if item["source_count"] == 0]
    dimensions = {
        "recent_work": {"status": "PASS" if recent else "INSUFFICIENT", "source_ids": [r["source_id"] for r in recent]},
        "comparable_baselines": {"status": "PASS" if baselines else "INSUFFICIENT", "source_ids": [r["source_id"] for r in baselines]},
        "limitation_mechanisms": {"status": "PASS" if limitations else "INSUFFICIENT", "source_ids": [r["source_id"] for r in limitations]},
    }
    return {
        "status": "PASS" if not uncovered and all(item["status"] == "PASS" for item in dimensions.values()) else "INSUFFICIENT",
        "by_query": by_query,
        "uncovered_queries": uncovered,
        "dimensions": dimensions,
    }


def verify_research_archive(manifest_path: str | Path) -> dict[str, Any]:
    path = Path(manifest_path)
    failures: list[dict[str, Any]] = []
    if not path.exists():
        return {"status": "FAIL", "manifest": str(path), "failures": [{"code": "MANIFEST_MISSING", "path": str(path)}]}
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "FAIL", "manifest": str(path), "failures": [{"code": "MANIFEST_INVALID", "message": str(exc)}]}
    for record in manifest.get("records", []):
        source_id = record.get("source_id")
        for path_key, hash_key, text_mode in (("raw_path", "snapshot_sha256", False), ("text_path", "text_sha256", True)):
            artifact = Path(str(record.get(path_key) or ""))
            if not artifact.exists():
                failures.append({"code": "ARCHIVE_FILE_MISSING", "source_id": source_id, "path": str(artifact)})
                continue
            data = artifact.read_bytes()
            actual = sha256_text(data.decode("utf-8")) if text_mode else sha256_bytes(data)
            if actual != record.get(hash_key):
                failures.append({"code": "ARCHIVE_HASH_MISMATCH", "source_id": source_id, "path": str(artifact), "expected": record.get(hash_key), "actual": actual})
        metadata_path = Path(str(record.get("metadata_path") or ""))
        if not metadata_path.exists():
            failures.append({"code": "ARCHIVE_METADATA_MISSING", "source_id": source_id, "path": str(metadata_path)})
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception as exc:
            failures.append({"code": "ARCHIVE_METADATA_INVALID", "source_id": source_id, "path": str(metadata_path), "message": str(exc)})
            continue
        for key in ("source_id", "snapshot_sha256", "text_sha256"):
            if metadata.get(key) != record.get(key):
                failures.append({"code": "ARCHIVE_METADATA_MISMATCH", "source_id": source_id, "field": key})
    connector_path = manifest.get("connector_response")
    connector_hash = manifest.get("connector_response_sha256")
    if connector_path:
        connector_file = Path(str(connector_path))
        if not connector_file.exists():
            failures.append({"code": "CONNECTOR_RESPONSE_MISSING", "path": str(connector_file)})
        elif connector_hash and sha256_bytes(connector_file.read_bytes()) != connector_hash:
            failures.append({"code": "CONNECTOR_RESPONSE_HASH_MISMATCH", "path": str(connector_file)})
    return {
        "status": "FAIL" if failures else "PASS",
        "manifest": str(path),
        "source_count": len(manifest.get("records", [])),
        "failures": failures,
        "verified_at": utc_now(),
    }


def _remove_orphan(record: dict[str, Any]) -> None:
    for key in ("raw_path", "text_path", "metadata_path"):
        path = Path(str(record.get(key) or ""))
        if path.exists():
            path.unlink()


def _write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    fields = [
        "source_id", "title", "url", "canonical_url", "final_url", "domain", "source_category",
        "published_at", "publisher", "doi", "retrieved_at", "accessed_at", "retrieval_provider",
        "http_status", "content_type", "snapshot_sha256", "text_sha256", "byte_size", "text_length",
        "authority_rank", "is_recent", "supports_baseline", "supports_limitation", "matched_query",
        "raw_path", "text_path", "metadata_path",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


def upgrade_archive_result(result, normalized_plan: dict[str, Any], plan_validation: dict[str, Any], duplicate_issues: list[dict[str, Any]]):
    manifest_path = Path(result.output["archive_manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = list(manifest.get("records") or [])
    issues: list[dict[str, Any]] = []
    warnings = list(result.output.get("warnings") or []) + list(plan_validation.get("warnings") or [])
    for warning in result.output.get("warnings") or []:
        url, _, message = str(warning).partition(": ")
        issues.append({"type": "SOURCE_FETCH_FAILURE", "url": url, "message": message or str(warning)})
    for item in duplicate_issues:
        issues.append({"type": "DUPLICATE_SOURCE", **item})
        if item.get("conflict_fields"):
            issues.append({
                "type": "SOURCE_CONFLICT",
                "code": "DUPLICATE_IDENTITY_METADATA_CONFLICT",
                "identity": item.get("identity"),
                "kept_url": item.get("kept_url"),
                "duplicate_url": item.get("duplicate_url"),
                "conflict_fields": item.get("conflict_fields"),
            })

    current_year = datetime.now(timezone.utc).year
    unique_records: list[dict[str, Any]] = []
    seen_content: dict[str, str] = {}
    for record in records:
        identity = str(record.get("text_sha256") or record.get("snapshot_sha256") or "")
        if identity and identity in seen_content:
            issues.append({"type": "DUPLICATE_SOURCE", "reason": "DUPLICATE_CONTENT", "identity": identity, "kept_source_id": seen_content[identity], "duplicate_url": record.get("url")})
            _remove_orphan(record)
            continue
        if identity:
            seen_content[identity] = str(record.get("source_id"))
        record["canonical_url"] = canonical_url(str(record.get("final_url") or record.get("url") or ""))
        record["doi"] = normalize_doi(record.get("doi"), str(record.get("final_url") or record.get("url") or ""))
        record["source_category"] = source_category(record)
        category_rank = {
            "OFFICIAL_STANDARD": 98,
            "GOVERNMENT": 94,
            "PEER_REVIEWED_PAPER": 90,
            "ACADEMIC_REPOSITORY": 78,
            "TECHNICAL_DOCUMENTATION": 72,
            "OTHER": 60,
        }[record["source_category"]]
        record["authority_rank"] = max(int(record.get("authority_rank") or 0), category_rank)
        year = parse_year(record.get("published_at"))
        record["published_year"] = year
        searchable = f"{record.get('title', '')}\n{record.get('excerpt', '')}"
        record["accessed_at"] = record.get("retrieved_at") or utc_now()
        record["is_recent"] = bool(year and year >= current_year - 5)
        record["supports_baseline"] = _contains_any(searchable, _BASELINE_TERMS)
        record["supports_limitation"] = _contains_any(searchable, _LIMITATION_TERMS)
        record["evidence_layers"] = {
            "raw_snapshot": {"kind": "ORIGINAL_SNAPSHOT", "path": record.get("raw_path"), "sha256": record.get("snapshot_sha256")},
            "extracted_text": {"kind": "SOURCE_EXTRACT", "path": record.get("text_path"), "sha256": record.get("text_sha256")},
        }
        write_json(Path(record["metadata_path"]), record)
        unique_records.append(record)

    unique_records.sort(key=lambda item: (-int(item.get("authority_rank") or 0), -(item.get("published_year") or 0), item.get("canonical_url") or ""))
    coverage = coverage_report(unique_records, normalized_plan)
    for query in coverage["uncovered_queries"]:
        issues.append({"type": "EVIDENCE_GAP", "code": "QUERY_UNCOVERED", "query": query})
    for dimension, item in coverage["dimensions"].items():
        if item["status"] != "PASS":
            issues.append({"type": "EVIDENCE_GAP", "code": "COVERAGE_INSUFFICIENT", "dimension": dimension})

    connector_path = manifest.get("connector_response")
    connector_hash = sha256_bytes(Path(connector_path).read_bytes()) if connector_path and Path(connector_path).exists() else None
    manifest.update({
        "schema_version": "2.0",
        "normalized_plan": normalized_plan,
        "plan_validation": plan_validation,
        "queries": normalized_plan["queries"],
        "records": unique_records,
        "source_count": len(unique_records),
        "issues": issues,
        "issue_count": len(issues),
        "coverage": coverage,
        "connector_response_sha256": connector_hash,
    })
    write_json(manifest_path, manifest)
    _write_csv(Path(result.output["source_index"]), unique_records)

    sources: list[dict[str, Any]] = []
    passages: list[dict[str, Any]] = []
    catalog: list[dict[str, Any]] = []
    for record in unique_records:
        source_ref = {
            "source_id": record["source_id"], "source_type": "PUBLIC_SOURCE",
            "document_version_id": None, "section_id": None, "span_start": None, "span_end": None,
            "quoted_text": str(record.get("excerpt") or "")[:500],
            "source_hash": record["snapshot_sha256"], "authority_rank": record["authority_rank"], "security_level": "PUBLIC",
        }
        sources.append(source_ref)
        passages.append({"passage_id": new_id("passage"), "source_ref": source_ref, "text": str(record.get("excerpt") or "")[:6000], "relevance": record.get("matched_query") or "公开资料检索"})
        catalog.append({
            "source_id": record["source_id"], "title": record.get("title"), "url": record.get("url"),
            "canonical_url": record.get("canonical_url"), "doi": record.get("doi"), "source_type": record.get("source_category"),
            "authority_rank": record.get("authority_rank"), "published_at": record.get("published_at"), "is_recent": record.get("is_recent"),
            "matched_query": record.get("matched_query"), "snapshot_sha256": record.get("snapshot_sha256"),
            "text_sha256": record.get("text_sha256"), "excerpt": record.get("excerpt"),
        })
    verification = verify_research_archive(manifest_path)
    if verification["status"] != "PASS":
        raise RuntimeError("Archive verification failed immediately after creation")
    result.output.update({
        "sources": sources, "passages": passages, "queries": normalized_plan["queries"],
        "normalized_plan": normalized_plan, "plan_validation": plan_validation,
        "source_catalog": catalog, "coverage": coverage, "issues": issues,
        "archive_verification": verification, "warnings": warnings,
    })
    result.warnings = warnings
    return result
