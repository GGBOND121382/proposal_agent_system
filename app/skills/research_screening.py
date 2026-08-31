from __future__ import annotations

import math
import re
from calendar import monthrange
from collections import Counter
from datetime import date
from typing import Any
from urllib.parse import urlparse

from .research_plan import deduplicate_candidates, parse_date, parse_time_scope_bounds, parse_year
from .research_quality import (
    assess_candidate_relevance,
    assess_source_priorities,
    build_query_relevance_profiles,
)

_GENERIC_LATIN = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "into", "is", "of", "on", "or", "the", "to", "using", "via", "with",
    "analysis", "approach", "method", "methods", "model", "models", "paper", "research", "review", "study", "system", "systems",
    "recent", "latest", "survey", "benchmark", "evaluation", "framework",
}
_REVIEW_TERMS = ("systematic review", "literature review", "survey", "review", "综述", "系统评价")
_RETRACT_TERMS = ("retracted", "retraction", "withdrawn", "撤稿", "撤回")


def _latin_tokens(value: Any) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z][a-z0-9_-]{1,}", str(value or "").lower())
        if token not in _GENERIC_LATIN
    }



def _time_scope_status(value: Any, start: date | None, end: date | None) -> tuple[str, str]:
    published, precision = parse_date(value)
    if published is None or (start is None and end is None):
        return "UNKNOWN", precision
    interval_start = published
    interval_end = published
    if precision == "MONTH":
        interval_end = date(published.year, published.month, monthrange(published.year, published.month)[1])
    elif precision == "YEAR":
        interval_end = date(published.year, 12, 31)
    if start and interval_end < start:
        return "OUTSIDE", precision
    if end and interval_start > end:
        return "OUTSIDE", precision
    if (start and interval_start < start) or (end and interval_end > end):
        return "UNCERTAIN", precision
    return "INSIDE", precision



def _candidate_queries(candidate: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for raw in candidate.get("matched_queries") or []:
        value = str(raw or "").strip()
        if value and value not in values:
            values.append(value)
    matched = str(candidate.get("matched_query") or "").strip()
    if matched and matched not in values:
        values.append(matched)
    verification = candidate.get("verification") if isinstance(candidate.get("verification"), dict) else {}
    for raw in verification.get("matched_queries") or []:
        value = str(raw or "").strip()
        if value and value not in values:
            values.append(value)
    return values


def _candidate_providers(candidate: dict[str, Any]) -> list[str]:
    values: list[str] = []
    verification = candidate.get("verification") if isinstance(candidate.get("verification"), dict) else {}
    for raw in (
        candidate.get("academic_provider"),
        verification.get("discovery_provider"),
        candidate.get("engine"),
    ):
        value = str(raw or "").strip().lower()
        if value and value not in values:
            values.append(value)
    for raw in candidate.get("discovery_providers") or verification.get("discovery_providers") or []:
        value = str(raw or "").strip().lower()
        if value and value not in values:
            values.append(value)
    return values


def _query_alignment(query: str, candidate: dict[str, Any]) -> tuple[float, int, int]:
    query_tokens = _latin_tokens(query)
    text_tokens = _latin_tokens(
        f"{candidate.get('title', '')} {candidate.get('abstract', '')} {candidate.get('excerpt', '')}"
    )
    if not query_tokens or not text_tokens:
        return 0.0, len(query_tokens), len(text_tokens)
    overlap = len(query_tokens & text_tokens)
    return overlap / max(1, min(len(query_tokens), len(text_tokens))), len(query_tokens), len(text_tokens)


def _score(candidate: dict[str, Any], query: str, *, end_year: int | None) -> float:
    alignment, _, _ = _query_alignment(query, candidate)
    doi_bonus = 12.0 if candidate.get("doi") else 0.0
    source_type = str(candidate.get("source_type") or "").upper()
    publication_status = str(candidate.get("publication_status") or "").upper()
    if source_type in {"OFFICIAL_STANDARD", "GOVERNMENT", "STANDARD", "OFFICIAL_SOURCE"}:
        authority_bonus = 12.0
    elif source_type in {"PEER_REVIEWED_PAPER", "CONFERENCE_PAPER"}:
        authority_bonus = 10.0
    elif publication_status == "PUBLISHED" and source_type == "SCHOLARLY_PUBLICATION_UNVERIFIED":
        authority_bonus = 3.0
    else:
        authority_bonus = 0.0
    abstract = str(candidate.get("abstract") or candidate.get("excerpt") or "")
    abstract_bonus = min(10.0, len(abstract) / 160.0)
    try:
        citations = max(0, int(candidate.get("citation_count") or (candidate.get("verification") or {}).get("citation_count") or 0))
    except (TypeError, ValueError):
        citations = 0
    citation_bonus = min(12.0, math.log1p(citations) * 2.2)
    year = parse_year(candidate.get("published_at"))
    recency_bonus = 0.0
    if year and end_year:
        recency_bonus = max(0.0, 8.0 - 1.2 * max(0, end_year - year))
    title = str(candidate.get("title") or "").lower()
    review_bonus = 6.0 if any(term in title for term in _REVIEW_TERMS) else 0.0
    verification = candidate.get("verification") if isinstance(candidate.get("verification"), dict) else {}
    assessment = (verification.get("semantic_relevance_by_query") or {}).get(query) or {}
    relevance_bonus = {"DIRECT": 18.0, "SUPPORTING": 8.0}.get(str(assessment.get("label") or ""), 0.0)
    priority_assessment = verification.get("source_priority_assessment") if isinstance(verification.get("source_priority_assessment"), dict) else {}
    priority_bonus = float(priority_assessment.get("score_bonus") or 0.0)
    return round(45.0 * alignment + doi_bonus + authority_bonus + abstract_bonus + citation_bonus + recency_bonus + review_bonus + relevance_bonus + priority_bonus, 4)


def screen_and_select_candidates(
    candidates: list[dict[str, Any]],
    normalized_plan: dict[str, Any],
    *,
    max_results: int,
    strict: bool,
    min_per_query: int = 3,
    enforce_semantic_relevance: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Reject clearly invalid/off-scope results and build a deterministic diverse shortlist."""

    queries = list(normalized_plan.get("queries") or [])
    approved = set(queries)
    start_date, end_date = parse_time_scope_bounds(normalized_plan.get("time_scope"))
    end_year = end_date.year if end_date else None
    relevance_profiles = build_query_relevance_profiles(normalized_plan)
    source_priorities = [
        str(item).strip()
        for item in normalized_plan.get("source_priorities") or []
        if str(item).strip()
    ]
    screened: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []

    for index, original in enumerate(candidates):
        candidate = dict(original)
        reasons: list[str] = []
        title = str(candidate.get("title") or "").strip()
        url = str(candidate.get("url") or "").strip()
        matched_queries = _candidate_queries(candidate)
        if not title or not url:
            reasons.append("CANDIDATE_IDENTITY_MISSING")
        if strict and (not matched_queries or any(query not in approved for query in matched_queries)):
            reasons.append("UNAPPROVED_QUERY_BINDING")
        searchable = f"{title} {candidate.get('abstract', '')} {candidate.get('excerpt', '')}".lower()
        if bool(candidate.get("is_retracted")) or any(term in searchable for term in _RETRACT_TERMS):
            reasons.append("RETRACTED_OR_WITHDRAWN")
        time_status, date_precision = _time_scope_status(candidate.get("published_at"), start_date, end_date)
        if strict and time_status == "OUTSIDE":
            reasons.append("OUTSIDE_TIME_SCOPE")

        original_matched_queries = list(matched_queries)
        relevance_by_query: dict[str, dict[str, Any]] = {}
        qualifying_queries: list[str] = []
        for query in original_matched_queries or ([str(candidate.get("matched_query") or "")] if candidate.get("matched_query") else []):
            assessment = assess_candidate_relevance(query, candidate, relevance_profiles)
            relevance_by_query[query] = assessment
            if assessment.get("qualifies_for_coverage"):
                qualifying_queries.append(query)
        if strict and enforce_semantic_relevance and original_matched_queries and not qualifying_queries:
            labels = {str(item.get("label") or "") for item in relevance_by_query.values()}
            reasons.append("CLEAR_LEXICAL_OFF_TOPIC" if labels == {"OFF_TOPIC"} else "LOW_SEMANTIC_RELEVANCE")
        elif strict and not enforce_semantic_relevance:
            # Preserve the legacy Track-C behavior: only reject an obvious same-script
            # lexical miss when enough text exists to make that judgement safely.
            for query in original_matched_queries:
                alignment, query_size, text_size = _query_alignment(query, candidate)
                if query_size >= 4 and text_size >= 8 and alignment == 0.0:
                    reasons.append("CLEAR_LEXICAL_OFF_TOPIC")
                    break
        reasons = list(dict.fromkeys(reasons))
        if reasons:
            issues.append(
                {
                    "type": "CANDIDATE_SCREENING",
                    "code": "CANDIDATE_REJECTED",
                    "reason_codes": reasons,
                    "title": title,
                    "url": url,
                    "matched_queries": original_matched_queries,
                    "semantic_relevance_by_query": relevance_by_query,
                    "time_scope_status": time_status,
                    "published_date_precision": date_precision,
                }
            )
            continue

        if strict and enforce_semantic_relevance and qualifying_queries:
            matched_queries = list(dict.fromkeys(qualifying_queries))
        if not matched_queries:
            matched = str(candidate.get("matched_query") or "").strip()
            matched_queries = [matched] if matched else []
        candidate["matched_queries"] = matched_queries
        if matched_queries:
            candidate["matched_query"] = matched_queries[0]
        providers = _candidate_providers(candidate)
        candidate["discovery_providers"] = providers
        verification = dict(candidate.get("verification") or {})
        verification["matched_queries"] = matched_queries
        verification["discovery_matched_queries"] = original_matched_queries
        verification["discovery_providers"] = providers
        verification["semantic_relevance_by_query"] = relevance_by_query
        verification["time_scope_status"] = time_status
        verification["published_date_precision"] = date_precision
        verification["source_priority_assessment"] = assess_source_priorities(
            candidate, source_priorities
        )
        candidate["verification"] = verification
        per_query_scores = {
            query: _score(candidate, query, end_year=end_year)
            for query in matched_queries
            if query
        }
        candidate["selection_score"] = max(per_query_scores.values(), default=0.0)
        candidate["selection_scores_by_query"] = per_query_scores
        candidate["screening"] = {
            "status": "PASS",
            "matched_queries": matched_queries,
            "semantic_relevance_by_query": relevance_by_query,
            "time_scope_status": time_status,
        }
        screened.append(candidate)

    deduplicated, duplicate_issues = deduplicate_candidates(screened)
    for duplicate in duplicate_issues:
        issues.append({"type": "CANDIDATE_SCREENING", "code": "CANDIDATE_DEDUPLICATED", **duplicate})

    # Dedup may merge query/provider bindings. Recompute scores against all bindings.
    for candidate in deduplicated:
        matched_queries = _candidate_queries(candidate)
        candidate["matched_queries"] = matched_queries
        candidate["selection_scores_by_query"] = {
            query: _score(candidate, query, end_year=end_year)
            for query in matched_queries
        }
        candidate["selection_score"] = max(candidate["selection_scores_by_query"].values(), default=0.0)

    effective_limit = max(1, min(100, int(max_results)))
    if strict and queries:
        effective_limit = max(effective_limit, min(100, len(queries) * max(1, int(min_per_query))))

    selected: list[dict[str, Any]] = []
    selected_ids: set[int] = set()
    coverage_counts = {query: 0 for query in queries}

    # Guarantee query breadth before global quality fill.  A source that supports
    # several approved queries counts for each binding but is archived once.
    for query in queries:
        ranked = sorted(
            (
                (index, candidate)
                for index, candidate in enumerate(deduplicated)
                if query in _candidate_queries(candidate)
            ),
            key=lambda pair: (
                -float(pair[1].get("selection_scores_by_query", {}).get(query, pair[1].get("selection_score") or 0.0)),
                str(pair[1].get("title") or "").lower(),
                str(pair[1].get("url") or ""),
            ),
        )
        for index, candidate in ranked:
            if coverage_counts[query] >= min_per_query or len(selected) >= effective_limit:
                break
            if index not in selected_ids:
                selected.append(candidate)
                selected_ids.add(index)
                for bound_query in _candidate_queries(candidate):
                    if bound_query in coverage_counts:
                        coverage_counts[bound_query] += 1
            elif query in _candidate_queries(candidate):
                # Already selected for another query; it counts once for this query.
                # Continue scanning until the requested number of distinct sources is met.
                continue

    provider_counts: Counter[str] = Counter()
    domain_counts: Counter[str] = Counter()
    for candidate in selected:
        for provider in _candidate_providers(candidate):
            provider_counts[provider] += 1
        domain_counts[urlparse(str(candidate.get("url") or "")).netloc.lower()] += 1

    remaining = [
        (index, candidate)
        for index, candidate in enumerate(deduplicated)
        if index not in selected_ids
    ]
    while remaining and len(selected) < effective_limit:
        def diversity_key(pair: tuple[int, dict[str, Any]]) -> tuple[float, str, str]:
            _, candidate = pair
            providers = _candidate_providers(candidate)
            domain = urlparse(str(candidate.get("url") or "")).netloc.lower()
            provider_penalty = min((provider_counts[p] for p in providers), default=0) * 3.0
            domain_penalty = domain_counts[domain] * 2.0 if domain else 0.0
            adjusted = float(candidate.get("selection_score") or 0.0) - provider_penalty - domain_penalty
            return (-adjusted, str(candidate.get("title") or "").lower(), str(candidate.get("url") or ""))

        remaining.sort(key=diversity_key)
        index, candidate = remaining.pop(0)
        selected.append(candidate)
        selected_ids.add(index)
        for provider in _candidate_providers(candidate):
            provider_counts[provider] += 1
        domain_counts[urlparse(str(candidate.get("url") or "")).netloc.lower()] += 1
        for bound_query in _candidate_queries(candidate):
            if bound_query in coverage_counts:
                coverage_counts[bound_query] += 1

    relevance_counts: Counter[str] = Counter()
    for candidate in deduplicated:
        verification = candidate.get("verification") if isinstance(candidate.get("verification"), dict) else {}
        for assessment in (verification.get("semantic_relevance_by_query") or {}).values():
            if isinstance(assessment, dict):
                relevance_counts[str(assessment.get("label") or "UNKNOWN")] += 1

    selected_priority_matches: Counter[str] = Counter()
    priority_matched_candidates = 0
    for candidate in selected:
        verification = candidate.get("verification") if isinstance(candidate.get("verification"), dict) else {}
        assessment = verification.get("source_priority_assessment") if isinstance(verification.get("source_priority_assessment"), dict) else {}
        matched = [str(item) for item in assessment.get("matched_priorities") or [] if str(item)]
        if matched:
            priority_matched_candidates += 1
        for priority in matched:
            selected_priority_matches[priority] += 1

    selection_report = {
        "schema_version": "1.2",
        "status": "PASS" if selected else "INSUFFICIENT",
        "input_candidate_count": len(candidates),
        "screened_candidate_count": len(screened),
        "deduplicated_candidate_count": len(deduplicated),
        "selected_candidate_count": len(selected),
        "configured_max_results": int(max_results),
        "effective_max_results": effective_limit,
        "min_per_query": int(min_per_query),
        "semantic_relevance_enforced": bool(enforce_semantic_relevance),
        "selected_by_query": coverage_counts,
        "semantic_relevance_counts": dict(sorted(relevance_counts.items())),
        "query_relevance_profiles": relevance_profiles,
        "source_priorities": source_priorities,
        "priority_matched_candidate_count": priority_matched_candidates,
        "selected_priority_match_counts": dict(sorted(selected_priority_matches.items())),
        "issues": issues,
    }
    return selected, selection_report
