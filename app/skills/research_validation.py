from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
from typing import Any

from .research_execution import build_plan_lock
from ..util import safe_filename, utc_now, write_json

_VALIDATION_SCHEMA_VERSION = "1.0"


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


def _record_providers(record: dict[str, Any]) -> list[str]:
    verification = record.get("verification") if isinstance(record.get("verification"), dict) else {}
    values: list[str] = []
    for raw in verification.get("discovery_providers") or []:
        value = str(raw or "").strip().lower()
        if value and value not in values:
            values.append(value)
    single = str(verification.get("discovery_provider") or "").strip().lower()
    if single and single not in values:
        values.append(single)
    return values


def _author_team(record: dict[str, Any]) -> str:
    authors = [str(item or "").strip() for item in record.get("authors") or [] if str(item or "").strip()]
    return " | ".join(authors[:3])


def _source_catalog(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = []
    for record in records:
        catalog.append(
            {
                "source_id": record.get("source_id"),
                "title": record.get("title"),
                "url": record.get("url"),
                "final_url": record.get("final_url"),
                "canonical_url": record.get("canonical_url"),
                "doi": record.get("doi"),
                "source_category": record.get("source_category"),
                "publication_status": record.get("publication_status"),
                "publication_kind": record.get("publication_kind"),
                "venue": record.get("venue"),
                "authority_rank": record.get("authority_rank"),
                "publisher": record.get("publisher"),
                "authors": list(record.get("authors") or []),
                "published_at": record.get("published_at"),
                "published_year": record.get("published_year"),
                "matched_query": record.get("matched_query"),
                "matched_queries": _record_queries(record),
                "semantic_relevance_by_query": dict((record.get("verification") or {}).get("semantic_relevance_by_query") or {}),
                "time_scope_status": (record.get("verification") or {}).get("time_scope_status"),
                "published_date_precision": (record.get("verification") or {}).get("published_date_precision"),
                "discovery_providers": _record_providers(record),
                "is_recent": bool(record.get("is_recent")),
                "supports_baseline": bool(record.get("supports_baseline")),
                "supports_limitation": bool(record.get("supports_limitation")),
                "text_length": int(record.get("text_length") or 0),
                "content_type": record.get("content_type"),
                "snapshot_sha256": record.get("snapshot_sha256"),
                "text_sha256": record.get("text_sha256"),
                "excerpt": record.get("excerpt"),
            }
        )
    return catalog


def _quality_summary(
    *,
    normalized_plan: dict[str, Any],
    execution_report: dict[str, Any] | None,
    discovery_manifest: dict[str, Any] | None,
    selection_report: dict[str, Any] | None,
    coverage: dict[str, Any] | None,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    selection = selection_report or {}
    coverage_value = coverage or {}
    rejection_reasons: Counter[str] = Counter()
    screening_issue_codes: Counter[str] = Counter()
    for issue in selection.get("issues") or []:
        code = str(issue.get("code") or "UNKNOWN")
        screening_issue_codes[code] += 1
        if code == "CANDIDATE_REJECTED":
            for reason in issue.get("reason_codes") or []:
                rejection_reasons[str(reason)] += 1

    source_categories = Counter(str(record.get("source_category") or "UNKNOWN") for record in records)
    publication_statuses = Counter(str(record.get("publication_status") or "UNKNOWN") for record in records)
    providers = Counter()
    for record in records:
        row_providers = _record_providers(record)
        if not row_providers:
            providers["UNOBSERVED"] += 1
        else:
            for provider in row_providers:
                providers[provider] += 1
    publishers = sorted(
        {
            str(record.get("publisher") or "").strip()
            for record in records
            if str(record.get("publisher") or "").strip()
        }
    )
    author_teams = sorted({team for record in records if (team := _author_team(record))})

    discovery_provider_counts: dict[str, int] = {}
    discovery_query_counts: dict[str, int] = {}
    if discovery_manifest:
        for run in discovery_manifest.get("provider_runs") or []:
            if not isinstance(run, dict):
                continue
            provider = str(run.get("provider") or "UNKNOWN").lower()
            try:
                count = int(run.get("result_count") or 0)
            except (TypeError, ValueError):
                count = 0
            discovery_provider_counts[provider] = discovery_provider_counts.get(provider, 0) + count
        for response in discovery_manifest.get("responses") or []:
            if not isinstance(response, dict):
                continue
            query = str(response.get("query") or "")
            results = response.get("results") if isinstance(response.get("results"), list) else []
            discovery_query_counts[query] = len(results)

    failing_dimensions = sorted(
        dimension
        for dimension, value in (coverage_value.get("dimensions") or {}).items()
        if isinstance(value, dict) and value.get("status") != "PASS"
    )
    short_text_count = sum(1 for record in records if int(record.get("text_length") or 0) < 1200)
    return {
        "schema_version": _VALIDATION_SCHEMA_VERSION,
        "generated_at": utc_now(),
        "plan": {
            "plan_id": normalized_plan.get("plan_id"),
            "plan_hash": build_plan_lock(normalized_plan)["plan_hash"],
            "query_count": len(normalized_plan.get("queries") or []),
            "queries": list(normalized_plan.get("queries") or []),
            "research_question_count": len(normalized_plan.get("research_questions") or []),
        },
        "execution": execution_report or {},
        "candidate_funnel": {
            "discovered_input_candidates": int(selection.get("input_candidate_count") or 0),
            "screened_candidates": int(selection.get("screened_candidate_count") or 0),
            "deduplicated_candidates": int(selection.get("deduplicated_candidate_count") or 0),
            "selected_candidates": int(selection.get("selected_candidate_count") or 0),
            "archived_sources": len(records),
            "selected_by_query": dict(selection.get("selected_by_query") or {}),
        },
        "discovery": {
            "providers": list((discovery_manifest or {}).get("providers") or []),
            "retrieval_health": (coverage_value.get("dimensions") or {}).get("retrieval_health", {}).get("health", {"status": "UNOBSERVED"}),
            "provider_candidate_counts": discovery_provider_counts,
            "candidate_counts_by_query": discovery_query_counts,
            "failure_count": len((discovery_manifest or {}).get("failures") or []),
            "failures": list((discovery_manifest or {}).get("failures") or []),
        },
        "screening": {
            "issue_counts": dict(sorted(screening_issue_codes.items())),
            "rejection_reason_counts": dict(sorted(rejection_reasons.items())),
            "semantic_relevance_counts": dict(selection.get("semantic_relevance_counts") or {}),
        },
        "accepted_sources": {
            "source_count": len(records),
            "source_categories": dict(sorted(source_categories.items())),
            "publication_statuses": dict(sorted(publication_statuses.items())),
            "discovery_provider_occurrences": dict(sorted(providers.items())),
            "distinct_publishers": len(publishers),
            "publishers": publishers,
            "distinct_author_teams": len(author_teams),
            "author_teams": author_teams,
            # Observation only. Batch A deliberately does not gate on evidence depth;
            # this makes abstract-only behavior visible before Batch B adds full-text gates.
            "short_text_under_1200_chars": short_text_count,
        },
        "coverage": {
            "status": coverage_value.get("status"),
            "quality_profile": coverage_value.get("quality_profile"),
            "uncovered_queries": list(coverage_value.get("uncovered_queries") or []),
            "shallow_queries": list(coverage_value.get("shallow_queries") or []),
            "failing_dimensions": failing_dimensions,
        },
    }


def write_validation_bundle(
    *,
    context,
    original_plan: dict[str, Any],
    normalized_plan: dict[str, Any],
    plan_validation: dict[str, Any],
    provider: str,
    quality_profile: str,
    configured_max_results: int,
    effective_max_results: int,
    discovery_manifest: dict[str, Any] | None,
    discovery_input: str | None,
    result_output: dict[str, Any],
) -> Path:
    """Persist a self-contained WF-3 quality-observation bundle before any gate raises."""

    session_id = str(result_output.get("archive_session_id") or "research-session")
    root = (
        Path(context.data_dir)
        / "wf3_validation"
        / safe_filename(context.workflow_id or "workflow")
        / safe_filename(session_id)
    )
    root.mkdir(parents=True, exist_ok=True)

    archive_manifest_path = Path(str(result_output.get("archive_manifest") or ""))
    archive_manifest: dict[str, Any] = {}
    if archive_manifest_path.exists():
        try:
            archive_manifest = json.loads(archive_manifest_path.read_text(encoding="utf-8"))
        except Exception:
            archive_manifest = {}
    records = [item for item in archive_manifest.get("records") or [] if isinstance(item, dict)]
    execution_report = result_output.get("execution_report") if isinstance(result_output.get("execution_report"), dict) else None
    selection_report = result_output.get("selection_report") if isinstance(result_output.get("selection_report"), dict) else None
    coverage = result_output.get("coverage") if isinstance(result_output.get("coverage"), dict) else None

    plan_lock = build_plan_lock(normalized_plan)
    run_manifest = {
        "schema_version": _VALIDATION_SCHEMA_VERSION,
        "created_at": utc_now(),
        "project_id": context.project_id,
        "workflow_id": context.workflow_id,
        "research_session_id": session_id,
        "retrieval_provider": provider,
        "research_quality_profile": quality_profile,
        "configured_max_results": configured_max_results,
        "effective_max_results": effective_max_results,
        "plan_id": normalized_plan.get("plan_id"),
        "plan_hash": plan_lock["plan_hash"],
        "archive_manifest": str(archive_manifest_path),
        "archive_root": result_output.get("archive_root"),
        "discovery_input": discovery_input,
        "validation_bundle_dir": str(root),
        "expected_files": [
            "00_run_manifest.json",
            "01_input_plan.json",
            "02_normalized_plan.json",
            "03_execution_report.json",
            "04_discovery_manifest.json",
            "05_selection_report.json",
            "06_source_catalog.json",
            "07_coverage.json",
            "07b_research_sufficiency.json",
            "08_quality_summary.json",
            "09_synthesis.json (written only after synthesis executes)",
            "10_claim_validation.json (written only after synthesis validation executes)",
        ],
    }
    write_json(root / "00_run_manifest.json", run_manifest)
    write_json(root / "01_input_plan.json", original_plan)
    write_json(
        root / "02_normalized_plan.json",
        {
            "normalized_plan": normalized_plan,
            "plan_validation": plan_validation,
            "plan_lock": plan_lock,
        },
    )
    write_json(root / "03_execution_report.json", execution_report or {})
    write_json(root / "04_discovery_manifest.json", discovery_manifest or {})
    write_json(root / "05_selection_report.json", selection_report or {})
    write_json(root / "06_source_catalog.json", _source_catalog(records))
    write_json(root / "07_coverage.json", coverage or {})
    write_json(root / "07b_research_sufficiency.json", result_output.get("research_sufficiency") or {})
    quality_summary = _quality_summary(
        normalized_plan=normalized_plan,
        execution_report=execution_report,
        discovery_manifest=discovery_manifest,
        selection_report=selection_report,
        coverage=coverage,
        records=records,
    )
    quality_summary["research_sufficiency"] = result_output.get("research_sufficiency") or {}
    quality_summary["research_gaps"] = list(result_output.get("research_gaps") or [])
    write_json(root / "08_quality_summary.json", quality_summary)
    return root


def write_synthesis_validation_bundle(
    *,
    validation_bundle_dir: str | Path,
    synthesis: dict[str, Any],
    claim_validation: dict[str, Any],
) -> None:
    root = Path(validation_bundle_dir)
    if not root.exists() or not root.is_dir():
        return
    write_json(root / "09_synthesis.json", synthesis)
    write_json(root / "10_claim_validation.json", claim_validation)
