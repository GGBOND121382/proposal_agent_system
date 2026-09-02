from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..util import new_id, sha256_bytes, sha256_text, utc_now, write_json
from .research_plan import canonical_url, normalize_doi, parse_year
from .public_research import PublicResearchIntegrityError
from .research_quality import build_research_sufficiency

_BASELINE_TERMS = {"baseline", "benchmark", "comparison", "comparative", "survey", "review", "基线", "对比", "比较", "综述", "评测", "现有方法"}
_LIMITATION_TERMS = {"limitation", "limitations", "challenge", "challenges", "gap", "open problem", "drawback", "局限", "不足", "挑战", "差距", "瓶颈"}


def _contains_any(text: str, terms: set[str]) -> bool:
    lowered = str(text or "").lower()
    return any(term in lowered for term in terms)


def source_category(record: dict[str, Any]) -> str:
    domain = str(record.get("domain") or urlparse(str(record.get("final_url") or record.get("url") or "")).netloc).lower()
    publisher = str(record.get("publisher") or "").lower()
    title = str(record.get("title") or "").lower()
    declared = str(record.get("source_type") or "").strip().upper()
    publication_status = str(record.get("publication_status") or "").strip().upper()

    if any(token in domain for token in ("iso.org", "iec.ch", "rfc-editor.org", "itu.int", "standards.")):
        return "OFFICIAL_STANDARD"
    if domain.endswith(".gov") or domain.endswith(".gov.cn") or ".gov." in domain:
        return "GOVERNMENT"
    if any(term in f"{title} {publisher} {domain}" for term in ("preprint", "research square", "ssrn", "arxiv")) or publication_status == "PREPRINT":
        return "ACADEMIC_PREPRINT"
    if any(term in title for term in ("decision letter", "editorial", "corrigendum", "erratum", "retraction notice")) or publication_status == "EDITORIAL":
        return "EDITORIAL"

    trusted_declared = {
        "PEER_REVIEWED_PAPER", "CONFERENCE_PAPER", "BOOK_CHAPTER", "BOOK",
        "REPORT", "DATASET", "THESIS", "ACADEMIC_PREPRINT", "EDITORIAL",
        "SCHOLARLY_PUBLICATION_UNVERIFIED",
    }
    if declared in trusted_declared:
        return declared
    if any(token in domain for token in ("arxiv.org", "openreview.net", "semanticscholar.org")):
        return "ACADEMIC_REPOSITORY"
    if domain.endswith(".edu") or domain.endswith(".edu.cn") or "ac.cn" in domain:
        return "ACADEMIC_REPOSITORY"
    if any(token in domain for token in ("docs.", "readthedocs", "developer.", "github.com")):
        return "TECHNICAL_DOCUMENTATION"
    if any(token in publisher for token in ("ministry", "commission", "department", "研究院", "委员会", "政府")):
        return "GOVERNMENT"
    if record.get("doi"):
        # A DOI proves persistent identity, not peer review.  Without provider-level
        # publication metadata keep the source usable but do not grant authority 90.
        return "SCHOLARLY_PUBLICATION_UNVERIFIED"
    return "OTHER"


def _record_queries(record: dict[str, Any]) -> list[str]:
    values: list[str] = []
    verification = record.get("verification") if isinstance(record.get("verification"), dict) else {}
    for raw in verification.get("matched_queries") or []:
        value = str(raw or "").strip()
        if value and value not in values:
            values.append(value)
    matched = str(record.get("matched_query") or "").strip()
    if matched and matched not in values:
        values.append(matched)
    return values


def _record_query_qualifies(record: dict[str, Any], query: str) -> bool:
    verification = record.get("verification") if isinstance(record.get("verification"), dict) else {}
    relevance = verification.get("semantic_relevance_by_query") if isinstance(verification.get("semantic_relevance_by_query"), dict) else {}
    assessment = relevance.get(query) if isinstance(relevance.get(query), dict) else None
    if assessment is None:
        return True
    return bool(assessment.get("qualifies_for_coverage")) or str(assessment.get("label") or "") in {"DIRECT", "SUPPORTING"}


def _record_providers(record: dict[str, Any]) -> list[str]:
    values: list[str] = []
    verification = record.get("verification") if isinstance(record.get("verification"), dict) else {}
    for raw in verification.get("discovery_providers") or []:
        value = str(raw or "").strip().lower()
        if value and value not in values:
            values.append(value)
    single = str(verification.get("discovery_provider") or "").strip().lower()
    if single and single not in values:
        values.append(single)
    return values


def _author_team_key(record: dict[str, Any]) -> str:
    authors = [str(item or "").strip().lower() for item in record.get("authors") or [] if str(item or "").strip()]
    return "|".join(authors[:3])


def _is_review_source(record: dict[str, Any]) -> bool:
    searchable = f"{record.get('title', '')}\n{record.get('excerpt', '')}".lower()
    terms = ("systematic review", "literature review", "survey", "review", "综述", "系统评价")
    return any(term in searchable for term in terms)


def coverage_report(
    records: list[dict[str, Any]],
    plan: dict[str, Any],
    *,
    quality_profile: str = "legacy",
    min_sources_per_query: int = 3,
    retrieval_health: dict[str, Any] | None = None,
) -> dict[str, Any]:
    queries = list(plan.get("queries") or [])
    by_query: dict[str, dict[str, Any]] = {}
    for query in queries:
        matched = [
            record for record in records
            if query in _record_queries(record) and _record_query_qualifies(record, query)
        ]
        authoritative = [record for record in matched if int(record.get("authority_rank") or 0) >= 80]
        by_query[query] = {
            "source_count": len(matched),
            "authoritative_source_count": len(authoritative),
            "source_ids": [record["source_id"] for record in matched],
            "authoritative_source_ids": [record["source_id"] for record in authoritative],
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

    profile = str(quality_profile or "legacy").strip().lower()
    if profile != "proposal_related_work":
        return {
            "status": "PASS" if not uncovered and all(item["status"] == "PASS" for item in dimensions.values()) else "INSUFFICIENT",
            "quality_profile": profile or "legacy",
            "by_query": by_query,
            "uncovered_queries": uncovered,
            "shallow_queries": [],
            "dimensions": dimensions,
        }

    # Proposal related-work research needs breadth, depth, and diversity.  The old
    # existential checks (>=1 recent/baseline/limitation source) remain available in
    # legacy mode for replay/backward compatibility but are not sufficient here.
    query_min = max(1, min(int(min_sources_per_query), 8))
    query_count = len(queries)
    source_min = max(10, query_count * query_min) if query_count else 10
    peer_or_official = [
        record for record in records
        if record.get("source_category") in {"PEER_REVIEWED_PAPER", "CONFERENCE_PAPER", "OFFICIAL_STANDARD", "GOVERNMENT"}
    ]
    reviews = [record for record in records if _is_review_source(record)]
    providers = sorted({provider for record in records for provider in _record_providers(record)})
    publishers = sorted({str(record.get("publisher") or "").strip().lower() for record in records if str(record.get("publisher") or "").strip()})
    author_teams = sorted({key for record in records if (key := _author_team_key(record))})
    shallow = [query for query, item in by_query.items() if item["source_count"] < query_min]
    authority_shallow = [query for query, item in by_query.items() if item["authoritative_source_count"] < 1]
    authoritative_total = sum(1 for record in records if int(record.get("authority_rank") or 0) >= 80)

    provider_counts: dict[str, float] = {}
    for record in records:
        record_providers = _record_providers(record)
        if not record_providers:
            continue
        weight = 1.0 / len(record_providers)
        for provider in record_providers:
            provider_counts[provider] = provider_counts.get(provider, 0.0) + weight
    provider_concentration = (max(provider_counts.values()) / len(records)) if records and provider_counts else 1.0

    dimensions.update({
        "query_depth": {
            "status": "PASS" if not shallow and bool(queries) else "INSUFFICIENT",
            "minimum_sources_per_query": query_min,
            "shallow_queries": shallow,
        },
        "query_authoritative_depth": {
            "status": "PASS" if not authority_shallow and bool(queries) else "INSUFFICIENT",
            "minimum_authoritative_sources_per_query": 1,
            "shallow_queries": authority_shallow,
        },
        "source_volume": {
            "status": "PASS" if len(records) >= source_min else "INSUFFICIENT",
            "source_count": len(records),
            "minimum_source_count": source_min,
        },
        "peer_reviewed_or_official": {
            "status": "PASS" if len(peer_or_official) >= max(4, query_count) else "INSUFFICIENT",
            "source_ids": [record["source_id"] for record in peer_or_official],
            "minimum_source_count": max(4, query_count),
        },
        "review_synthesis": {
            "status": "PASS" if reviews else "INSUFFICIENT",
            "source_ids": [record["source_id"] for record in reviews],
        },
        "author_team_diversity": {
            "status": "PASS" if len(author_teams) >= 3 else "INSUFFICIENT",
            "distinct_author_teams": len(author_teams),
        },
        "publisher_diversity": {
            "status": "PASS" if len(publishers) >= 3 else "INSUFFICIENT",
            "distinct_publishers": len(publishers),
        },
        "discovery_provider_diversity": {
            # Connector imports may not expose their upstream search-engine identity.
            # In that case publisher/category diversity remains enforceable and this
            # dimension is explicitly marked unobserved rather than making connector
            # mode impossible to pass.
            "status": "PASS" if not providers or len(providers) >= 2 else "INSUFFICIENT",
            "providers": providers,
            "observed": bool(providers),
        },
        "provider_concentration": {
            "status": "PASS" if not provider_counts or provider_concentration <= 0.8 else "INSUFFICIENT",
            "provider_counts": provider_counts,
            "observed": bool(provider_counts),
            "max_share": round(provider_concentration, 4) if provider_counts else None,
            "maximum_allowed_share": 0.8,
        },
        "retrieval_health": {
            "status": (
                "PASS"
                if not retrieval_health or retrieval_health.get("status") in {"PASS", "UNOBSERVED"}
                else "INSUFFICIENT"
            ),
            "health": retrieval_health or {"status": "UNOBSERVED"},
        },
        "authoritative_depth": {
            "status": "PASS" if authoritative_total >= max(4, query_count) else "INSUFFICIENT",
            "authoritative_source_count": authoritative_total,
            "minimum_source_count": max(4, query_count),
        },
        "recent_work": {
            "status": "PASS" if len(recent) >= max(3, query_count) else "INSUFFICIENT",
            "source_ids": [r["source_id"] for r in recent],
            "minimum_source_count": max(3, query_count),
        },
        "comparable_baselines": {
            "status": "PASS" if len(baselines) >= 2 else "INSUFFICIENT",
            "source_ids": [r["source_id"] for r in baselines],
            "minimum_source_count": 2,
        },
        "limitation_mechanisms": {
            "status": "PASS" if len(limitations) >= 2 else "INSUFFICIENT",
            "source_ids": [r["source_id"] for r in limitations],
            "minimum_source_count": 2,
        },
    })
    return {
        "status": "PASS" if not uncovered and all(item["status"] == "PASS" for item in dimensions.values()) else "INSUFFICIENT",
        "quality_profile": "proposal_related_work",
        "by_query": by_query,
        "uncovered_queries": uncovered,
        "shallow_queries": shallow,
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


def upgrade_archive_result(
    result,
    normalized_plan: dict[str, Any],
    plan_validation: dict[str, Any],
    duplicate_issues: list[dict[str, Any]],
    *,
    quality_profile: str = "legacy",
    selection_report: dict[str, Any] | None = None,
    execution_report: dict[str, Any] | None = None,
    min_sources_per_query: int = 3,
    retrieval_health: dict[str, Any] | None = None,
):
    manifest_path = Path(result.output["archive_manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = list(manifest.get("records") or [])
    issues: list[dict[str, Any]] = []
    warnings = list(result.output.get("warnings") or []) + list(plan_validation.get("warnings") or [])
    if selection_report:
        issues.extend(list(selection_report.get("issues") or []))
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
    scope_years = [
        int(value)
        for value in re.findall(r"(?:19|20)\d{2}", str(normalized_plan.get("time_scope") or ""))
    ]
    recent_reference_year = max(scope_years) if scope_years else current_year
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
            "CONFERENCE_PAPER": 88,
            "BOOK_CHAPTER": 76,
            "BOOK": 74,
            "ACADEMIC_PREPRINT": 72,
            "ACADEMIC_REPOSITORY": 70,
            "REPORT": 72,
            "THESIS": 68,
            "SCHOLARLY_PUBLICATION_UNVERIFIED": 68,
            "TECHNICAL_DOCUMENTATION": 66,
            "DATASET": 60,
            "EDITORIAL": 52,
            "OTHER": 55,
        }[record["source_category"]]
        record["authority_rank"] = category_rank
        year = parse_year(record.get("published_at"))
        record["published_year"] = year
        searchable = f"{record.get('title', '')}\n{record.get('excerpt', '')}"
        record["accessed_at"] = record.get("retrieved_at") or utc_now()
        record["is_recent"] = bool(year and year >= recent_reference_year - 5)
        record["supports_baseline"] = _contains_any(searchable, _BASELINE_TERMS)
        record["supports_limitation"] = _contains_any(searchable, _LIMITATION_TERMS)
        record["evidence_layers"] = {
            "raw_snapshot": {"kind": "ORIGINAL_SNAPSHOT", "path": record.get("raw_path"), "sha256": record.get("snapshot_sha256")},
            "extracted_text": {"kind": "SOURCE_EXTRACT", "path": record.get("text_path"), "sha256": record.get("text_sha256")},
        }
        write_json(Path(record["metadata_path"]), record)
        unique_records.append(record)

    unique_records.sort(key=lambda item: (-int(item.get("authority_rank") or 0), -(item.get("published_year") or 0), item.get("canonical_url") or ""))
    evidence_records = [
        record
        for record in unique_records
        if str(record.get("fetch_mode") or "").upper() != "SNIPPET_ONLY"
    ]
    snippet_only_records = [
        record
        for record in unique_records
        if str(record.get("fetch_mode") or "").upper() == "SNIPPET_ONLY"
    ]
    for record in snippet_only_records:
        issues.append(
            {
                "type": "SOURCE_FETCH_FAILURE",
                "code": "SNIPPET_ONLY_NOT_EVIDENCE",
                "url": record.get("url"),
                "blockage_type": record.get("browser_blockage_type"),
            }
        )
    coverage = coverage_report(
        evidence_records,
        normalized_plan,
        quality_profile=quality_profile,
        min_sources_per_query=min_sources_per_query,
        retrieval_health=retrieval_health,
    )
    research_sufficiency = build_research_sufficiency(
        coverage,
        normalized_plan,
        retrieval_health or {"status": "UNOBSERVED"},
    )
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
        "research_quality_profile": str(quality_profile or "legacy"),
        "selection_report": selection_report,
        "execution_report": execution_report,
        "retrieval_health": retrieval_health or {"status": "UNOBSERVED"},
        "research_sufficiency": research_sufficiency,
        "research_gaps": research_sufficiency.get("research_gaps", []),
        "connector_response_sha256": connector_hash,
    })
    write_json(manifest_path, manifest)
    _write_csv(Path(result.output["source_index"]), unique_records)

    sources: list[dict[str, Any]] = []
    passages: list[dict[str, Any]] = []
    catalog: list[dict[str, Any]] = []
    for record in unique_records:
        is_snippet_only = str(record.get("fetch_mode") or "").upper() == "SNIPPET_ONLY"
        source_ref = {
            "source_id": record["source_id"], "source_type": "PUBLIC_SOURCE",
            "document_version_id": None, "section_id": None, "span_start": None, "span_end": None,
            "quoted_text": str(record.get("excerpt") or "")[:500],
            "source_hash": record["snapshot_sha256"], "authority_rank": record["authority_rank"], "security_level": "PUBLIC",
        }
        if not is_snippet_only:
            sources.append(source_ref)
            passages.append({"passage_id": new_id("passage"), "source_ref": source_ref, "text": str(record.get("excerpt") or "")[:6000], "relevance": record.get("matched_query") or "公开资料检索"})
        catalog.append({
            "source_id": record["source_id"], "title": record.get("title"), "url": record.get("url"),
            "canonical_url": record.get("canonical_url"), "doi": record.get("doi"), "source_type": record.get("source_category"),
            "publication_status": record.get("publication_status"), "publication_kind": record.get("publication_kind"), "venue": record.get("venue"),
            "authority_rank": record.get("authority_rank"), "published_at": record.get("published_at"), "is_recent": record.get("is_recent"),
            "matched_query": record.get("matched_query"), "matched_queries": _record_queries(record),
            "discovery_providers": _record_providers(record), "snapshot_sha256": record.get("snapshot_sha256"),
            "text_sha256": record.get("text_sha256"), "excerpt": record.get("excerpt"),
            "text_length": record.get("text_length"),
            "full_text_available": bool(
                not is_snippet_only and int(record.get("text_length") or 0) >= 1000
            ),
            "fetch_mode": record.get("fetch_mode"),
        })
    verification = verify_research_archive(manifest_path)
    if verification["status"] != "PASS":
        raise PublicResearchIntegrityError(
            "Archive verification failed immediately after creation",
            details={"verification": verification},
        )
    result.output.update({
        "sources": sources, "passages": passages, "queries": normalized_plan["queries"],
        "normalized_plan": normalized_plan, "plan_validation": plan_validation,
        "source_catalog": catalog, "coverage": coverage, "issues": issues,
        "research_quality_profile": str(quality_profile or "legacy"),
        "selection_report": selection_report, "execution_report": execution_report,
        "retrieval_health": retrieval_health or {"status": "UNOBSERVED"},
        "research_sufficiency": research_sufficiency,
        "research_gaps": research_sufficiency.get("research_gaps", []),
        "archive_verification": verification, "warnings": warnings,
    })
    result.warnings = warnings
    return result
