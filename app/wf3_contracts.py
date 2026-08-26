from __future__ import annotations

import copy
import json
from typing import Any, Mapping

from .util import sha256_json


WF3_PRODUCER_PROMPTS = frozenset(
    {
        "P-SAFE-ONLINE-PACKAGE",
        "P-PUBLIC-RESEARCH-PLAN",
        "P-PUBLIC-RESEARCH-SYNTHESIS",
    }
)
WF3_RESEARCH_CRITIC = "P-PUBLIC-RESEARCH-CRITIC"


def _blocking_items(output: Mapping[str, Any], field: str) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in output.get(field) or []
        if isinstance(item, Mapping) and item.get("blocking") is True
    ]


def canonicalize_wf3_producer_status(
    prompt_id: str,
    output: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Derive a WF-3 Producer status from authored blocking objects.

    This is a control-plane projection only.  It never changes a Finding,
    unresolved item, question, or business result.  In particular, advisory
    Findings cannot consume a semantic retry or turn a usable research package
    into a content block merely because the model wrote ``REVISE``.
    """

    if prompt_id not in WF3_PRODUCER_PROMPTS:
        return output, None
    normalized = copy.deepcopy(output)
    before = str(normalized.get("status") or "").upper()
    blockers = [
        *_blocking_items(normalized, "findings"),
        *_blocking_items(normalized, "unresolved_items"),
    ]
    questions = _blocking_items(normalized, "user_questions")
    after = before
    reason = "UNCHANGED"
    if questions:
        after = "NEED_USER_INPUT"
        reason = "BLOCKING_USER_QUESTION"
    elif blockers and before == "PASS":
        after = "REVISE"
        reason = "BLOCKING_CONTENT_ITEM"
    elif not blockers and before == "REVISE":
        after = "PASS"
        reason = "ADVISORY_ONLY"

    if after == before:
        return normalized, None
    normalized["status"] = after
    normalized.setdefault("warnings", []).append(
        f"SYSTEM_WF3_STATUS_CANONICALIZATION: {before}->{after}; reason={reason}"
    )
    return normalized, {
        "prompt_id": prompt_id,
        "before": before,
        "after": after,
        "reason": reason,
        "blocking_item_count": len(blockers),
        "blocking_question_count": len(questions),
    }


def wf3_finding_route(finding: Mapping[str, Any]) -> str:
    """Return the only WF-3 capability allowed to resolve one Finding."""

    suggested = str(finding.get("suggested_route") or "").upper()
    target = " ".join(
        str(finding.get(key) or "")
        for key in ("target_type", "target_path_or_span", "code")
    ).lower()
    narrative = " ".join(
        str(finding.get(key) or "")
        for key in ("description", "repair_instruction")
    ).lower()

    if suggested == "USER":
        return "USER"
    if suggested == "PLANNING_AGENT":
        return "PLAN"
    if any(
        token in target
        for token in (
            "retrieved_source",
            "public_search",
            "source_catalog",
            "search_result",
            "archive",
        )
    ):
        return "RETRIEVAL"
    if any(
        token in narrative
        for token in (
            "重新检索",
            "补充检索",
            "获取全文",
            "re-search",
            "rerun search",
            "retrieve full text",
            "fetch full text",
        )
    ):
        return "RETRIEVAL"
    if any(token in target for token in ("research_plan", "research_question", "query")):
        return "PLAN"
    if any(token in target for token in ("synthesis_candidate", "claim", "limitation", "conflict")):
        return "SYNTHESIS"
    return "BLOCK"


def wf3_critic_routing_report(output: Mapping[str, Any]) -> dict[str, Any]:
    routed: list[dict[str, Any]] = []
    for finding in output.get("findings") or []:
        if not isinstance(finding, Mapping) or finding.get("blocking") is not True:
            continue
        routed.append(
            {
                "finding_instance_id": str(finding.get("finding_instance_id") or ""),
                "code": str(finding.get("code") or ""),
                "target_path_or_span": str(finding.get("target_path_or_span") or ""),
                "route": wf3_finding_route(finding),
            }
        )
    counts = {
        route: sum(1 for item in routed if item["route"] == route)
        for route in ("RETRIEVAL", "PLAN", "SYNTHESIS", "USER", "BLOCK")
    }
    return {
        "schema_version": "1.0",
        "prompt_id": WF3_RESEARCH_CRITIC,
        "blocking_finding_count": len(routed),
        "route_counts": counts,
        "routes": routed,
        "synthesis_only": bool(routed) and counts["SYNTHESIS"] == len(routed),
        "has_non_synthesis_route": any(item["route"] != "SYNTHESIS" for item in routed),
    }


def summarize_public_search(candidate: Mapping[str, Any]) -> dict[str, Any]:
    sources = [item for item in candidate.get("sources") or [] if isinstance(item, Mapping)]
    catalog = [item for item in candidate.get("source_catalog") or [] if isinstance(item, Mapping)]
    issues = [item for item in candidate.get("issues") or [] if isinstance(item, Mapping)]
    coverage = candidate.get("coverage") if isinstance(candidate.get("coverage"), Mapping) else {}
    by_query = coverage.get("by_query") if isinstance(coverage.get("by_query"), Mapping) else {}
    dimensions = coverage.get("dimensions") if isinstance(coverage.get("dimensions"), Mapping) else {}
    queries = {str(item).strip() for item in candidate.get("queries") or [] if str(item).strip()}
    covered_queries = {
        str(query)
        for query, item in by_query.items()
        if isinstance(item, Mapping) and int(item.get("source_count") or 0) > 0
    }
    passed_dimensions = {
        str(name)
        for name, item in dimensions.items()
        if isinstance(item, Mapping) and str(item.get("status") or "").upper() == "PASS"
    }
    critical_issues = [
        item
        for item in issues
        if str(item.get("type") or "").upper()
        in {"EVIDENCE_GAP", "SOURCE_CONFLICT", "SOURCE_FETCH_FAILURE", "SECURITY"}
    ]
    source_ids = {str(item.get("source_id")) for item in sources if item.get("source_id")}
    authoritative = sum(1 for item in catalog if int(item.get("authority_rank") or 0) >= 80)
    full_text = sum(
        1
        for item in catalog
        if int(item.get("text_length") or 0) >= 2000 or bool(item.get("full_text_available"))
    )
    return {
        "candidate_hash": sha256_json(candidate),
        "queries": sorted(queries),
        "covered_queries": sorted(covered_queries or (queries - set(coverage.get("uncovered_queries") or []))),
        "passed_dimensions": sorted(passed_dimensions),
        "source_ids": sorted(source_ids),
        "source_count": len(source_ids),
        "authoritative_source_count": authoritative,
        "full_text_source_count": full_text,
        "critical_issue_count": len(critical_issues),
        "archive_verified": str(
            ((candidate.get("archive_verification") or {}).get("status") or "PASS")
        ).upper()
        == "PASS",
    }


def compare_public_search_candidates(
    accepted: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply conservative whole-candidate non-regression acceptance."""

    baseline = summarize_public_search(accepted)
    proposed = summarize_public_search(candidate)
    regressions: list[str] = []
    baseline_queries = set(baseline["queries"])
    proposed_queries = set(proposed["queries"])
    if not baseline_queries.issubset(proposed_queries):
        regressions.append("QUERY_SET_SHRANK")
    if not set(baseline["covered_queries"]).issubset(set(proposed["covered_queries"])):
        regressions.append("QUERY_COVERAGE_REGRESSED")
    if not set(baseline["passed_dimensions"]).issubset(set(proposed["passed_dimensions"])):
        regressions.append("COVERAGE_DIMENSION_REGRESSED")
    if baseline["archive_verified"] and not proposed["archive_verified"]:
        regressions.append("ARCHIVE_VERIFICATION_REGRESSED")
    if (
        proposed["source_count"] < baseline["source_count"]
        and proposed["critical_issue_count"] >= baseline["critical_issue_count"]
    ):
        regressions.append("SOURCE_SET_SHRANK_WITHOUT_ISSUE_REDUCTION")
    improvements = [
        key
        for key in (
            "source_count",
            "authoritative_source_count",
            "full_text_source_count",
        )
        if proposed[key] > baseline[key]
    ]
    if proposed["critical_issue_count"] < baseline["critical_issue_count"]:
        improvements.append("critical_issue_count")
    return {
        "accepted": not regressions,
        "regressions": regressions,
        "improvements": improvements,
        "baseline": baseline,
        "candidate": proposed,
    }


def compact_wf3_research_envelope(
    prompt_id: str,
    envelope: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Remove duplicated passage text from the model projection only."""

    if prompt_id not in {
        "P-PUBLIC-RESEARCH-SYNTHESIS",
        "P-PUBLIC-RESEARCH-CRITIC",
        "P-ONLINE-RESULT-IMPORT-CRITIC",
    }:
        return envelope, None
    compact = copy.deepcopy(envelope)
    payload = compact.get("payload")
    if not isinstance(payload, dict):
        return envelope, None
    passages = [item for item in payload.get("extracted_passages") or [] if isinstance(item, dict)]
    passage_source_ids = {
        str((item.get("source_ref") or {}).get("source_id") or "")
        for item in passages
        if isinstance(item.get("source_ref"), dict)
    }
    removed_fields = 0
    for passage in passages:
        source_ref = passage.get("source_ref")
        if not isinstance(source_ref, dict):
            continue
        passage["source_ref"] = {
            key: source_ref[key]
            for key in (
                "source_id",
                "source_type",
                "source_hash",
                "authority_rank",
                "security_level",
            )
            if key in source_ref
        }
        removed_fields += max(0, len(source_ref) - len(passage["source_ref"]))
    for source_ref in payload.get("retrieved_sources") or []:
        if not isinstance(source_ref, dict):
            continue
        if str(source_ref.get("source_id") or "") in passage_source_ids and source_ref.get("quoted_text"):
            source_ref.pop("quoted_text", None)
            removed_fields += 1
    if not removed_fields:
        return envelope, None
    return compact, {
        "strategy": "WF3_DEDUPLICATE_RESEARCH_SOURCE_TEXT",
        "original_chars": len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))),
        "model_chars": len(json.dumps(compact, ensure_ascii=False, separators=(",", ":"))),
        "removed_duplicate_source_fields": removed_fields,
        "quality_guard_uses_full_context": True,
    }
