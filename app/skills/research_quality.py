from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

_GENERIC_TERMS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "into", "is", "of", "on", "or", "the", "to", "using", "via", "with",
    "analysis", "approach", "approaches", "framework", "frameworks", "latest", "method", "methods", "model", "models", "paper", "papers", "recent", "research", "review", "reviews", "study", "studies", "survey", "surveys", "system", "systems",
}
_RELEVANCE_RANK = {"OFF_TOPIC": 0, "TANGENTIAL": 1, "SUPPORTING": 2, "DIRECT": 3}
_NON_CONTENT_TITLE_PREFIXES = (
    r"^\s*decision\s+letter(?:\s+for)?\s*[:\-–—]?\s*",
    r"^\s*editorial\s*[:\-–—]?\s*",
    r"^\s*correction\s+(?:to|for)\s*[:\-–—]?\s*",
    r"^\s*author\s+response\s*[:\-–—]?\s*",
    r"^\s*reviewer\s+report\s*[:\-–—]?\s*",
)


def _stem(token: str) -> str:
    value = token.lower().strip()
    if value.endswith("ies") and len(value) > 4:
        return value[:-3] + "y"
    if value.endswith("s") and len(value) > 4 and not value.endswith(("ss", "us")):
        return value[:-1]
    return value


def concept_tokens(value: Any) -> set[str]:
    text = str(value or "").lower().replace("-", " ")
    values = {_stem(token) for token in re.findall(r"[a-z][a-z0-9]{1,}", text)}
    return {token for token in values if token not in _GENERIC_TERMS}


def build_query_relevance_profiles(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Build deterministic semantic anchors from the plan's query/question bindings.

    When several queries answer the same research question, concepts repeated across
    those queries become domain anchors.  This prevents generic phrase collisions such
    as ``feedback loops`` in hydrology from being treated as evidence for a decision-
    system question merely because two lexical tokens happen to match.
    """

    query_items = [item for item in plan.get("query_items") or [] if isinstance(item, dict)]
    if not query_items:
        query_items = [
            {"query": query, "linked_question_indexes": [index]}
            for index, query in enumerate(plan.get("queries") or [])
        ]

    question_queries: dict[int, list[str]] = defaultdict(list)
    for item in query_items:
        query = str(item.get("query") or "").strip()
        if not query:
            continue
        for raw_index in item.get("linked_question_indexes") or []:
            try:
                index = int(raw_index)
            except (TypeError, ValueError):
                continue
            question_queries[index].append(query)

    anchors_by_question: dict[int, set[str]] = {}
    for index, queries in question_queries.items():
        token_sets = [concept_tokens(query) for query in queries]
        if len(token_sets) < 2:
            anchors_by_question[index] = set()
            continue
        counts = Counter(token for token_set in token_sets for token in token_set)
        anchors_by_question[index] = {token for token, count in counts.items() if count >= 2}

    profiles: dict[str, dict[str, Any]] = {}
    for item in query_items:
        query = str(item.get("query") or "").strip()
        if not query:
            continue
        indexes: list[int] = []
        anchors: set[str] = set()
        for raw_index in item.get("linked_question_indexes") or []:
            try:
                index = int(raw_index)
            except (TypeError, ValueError):
                continue
            indexes.append(index)
            anchors.update(anchors_by_question.get(index) or set())
        profiles[query] = {
            "query_tokens": sorted(concept_tokens(query)),
            "domain_anchors": sorted(anchors),
            "linked_question_indexes": indexes,
        }
    return profiles


def assess_candidate_relevance(
    query: str,
    candidate: dict[str, Any],
    profiles: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    profile = profiles.get(query) or {"query_tokens": sorted(concept_tokens(query)), "domain_anchors": []}
    query_tokens = set(profile.get("query_tokens") or [])
    anchors = set(profile.get("domain_anchors") or [])
    title = str(candidate.get("title") or "")
    cleaned_title = title
    for pattern in _NON_CONTENT_TITLE_PREFIXES:
        cleaned_title = re.sub(pattern, "", cleaned_title, flags=re.IGNORECASE)
    text = f"{cleaned_title} {candidate.get('abstract', '')} {candidate.get('excerpt', '')}"
    text_tokens = concept_tokens(text)
    overlap = query_tokens & text_tokens
    anchor_hits = anchors & text_tokens
    ratio = len(overlap) / max(1, len(query_tokens))

    if anchors:
        # A repeated domain anchor is necessary but not sufficient.  A single generic
        # anchor such as ``decision`` plus two coincidental words caused real false
        # coverage (E3SM versioning, parasite feedback, supply-chain blockchain).
        # Require broader intent overlap, while allowing queries with two independent
        # anchors (for example human + collaboration) to qualify with three terms.
        all_anchor_hit = len(anchors) >= 2 and anchors.issubset(text_tokens)
        if anchor_hits and len(overlap) >= 5:
            label = "DIRECT"
        elif all_anchor_hit and len(overlap) >= 4:
            label = "DIRECT"
        elif all_anchor_hit and len(overlap) >= 3:
            label = "SUPPORTING"
        elif anchor_hits and len(overlap) >= 4:
            label = "SUPPORTING"
        elif len(overlap) >= 1:
            label = "TANGENTIAL"
        else:
            label = "OFF_TOPIC"
    else:
        if len(overlap) >= 4 or (len(overlap) >= 3 and ratio >= 0.45):
            label = "DIRECT"
        elif len(overlap) >= 3:
            label = "SUPPORTING"
        elif len(overlap) >= 1:
            label = "TANGENTIAL"
        else:
            label = "OFF_TOPIC"

    return {
        "label": label,
        "overlap_tokens": sorted(overlap),
        "domain_anchor_hits": sorted(anchor_hits),
        "domain_anchors": sorted(anchors),
        "query_token_count": len(query_tokens),
        "overlap_count": len(overlap),
        "overlap_ratio": round(ratio, 4),
        "qualifies_for_coverage": _RELEVANCE_RANK[label] >= _RELEVANCE_RANK["SUPPORTING"],
    }


def build_retrieval_health(
    discovery_manifest: dict[str, Any] | None,
    *,
    retrieval_provider: str,
    queries: list[str],
) -> dict[str, Any]:
    """Summarize whether the configured discovery channels actually executed.

    The object is deliberately separate from source coverage: many sources from one
    surviving provider must not hide a broken hybrid channel.
    """

    provider = str(retrieval_provider or "").lower()
    if not discovery_manifest or provider not in {"academic", "hybrid"}:
        return {
            "schema_version": "1.0",
            "status": "UNOBSERVED",
            "retrieval_provider": provider or "unknown",
            "required_providers": [],
            "providers": {},
            "reason_codes": [],
        }

    academic = ("openalex", "crossref", "semantic_scholar")
    required = list(academic) + (["searxng"] if provider == "hybrid" else [])
    query_set = {str(query) for query in queries if str(query).strip()}
    stats = {
        name: {
            "attempted_queries": len(query_set),
            "successful_queries": 0,
            "failed_queries": 0,
            "result_count": 0,
            "success_rate": 0.0,
        }
        for name in required
    }

    successful_pairs: set[tuple[str, str]] = set()
    failed_pairs: set[tuple[str, str]] = set()
    global_failed_providers: set[str] = set()

    for run in discovery_manifest.get("provider_runs") or []:
        if not isinstance(run, dict):
            continue
        name = str(run.get("provider") or "").lower()
        if name not in stats:
            continue
        if name == "searxng" and not run.get("query"):
            query_failures = [item for item in run.get("query_failures") or [] if isinstance(item, dict)]
            failures = {str(item.get("query") or "") for item in query_failures if str(item.get("query") or "")}
            try:
                result_count = int(run.get("result_count") or 0)
            except (TypeError, ValueError):
                result_count = 0
            stats[name]["result_count"] += result_count
            if query_failures and not failures and result_count == 0:
                global_failed_providers.add(name)
                for query in query_set:
                    failed_pairs.add((name, query))
            else:
                for query in query_set:
                    if query in failures:
                        failed_pairs.add((name, query))
                    else:
                        successful_pairs.add((name, query))
            continue
        query = str(run.get("query") or "")
        if query in query_set:
            successful_pairs.add((name, query))
        try:
            stats[name]["result_count"] += int(run.get("result_count") or 0)
        except (TypeError, ValueError):
            pass

    for failure in discovery_manifest.get("failures") or []:
        if not isinstance(failure, dict):
            continue
        name = str(failure.get("provider") or "").lower()
        query = str(failure.get("query") or "")
        if name in stats and query in query_set:
            failed_pairs.add((name, query))
        elif name in stats and not query:
            global_failed_providers.add(name)
            for item in query_set:
                failed_pairs.add((name, item))

    for name, item in stats.items():
        successes = {query for provider_name, query in successful_pairs if provider_name == name}
        if name in global_failed_providers:
            successes = set()
        failures = {query for provider_name, query in failed_pairs if provider_name == name and query not in successes}
        item["successful_queries"] = len(successes)
        item["failed_queries"] = len(failures)
        attempted = max(len(query_set), len(successes | failures))
        item["attempted_queries"] = attempted
        item["success_rate"] = round(len(successes) / attempted, 4) if attempted else 0.0

    academic_successful = sum(1 for name in academic if stats.get(name, {}).get("successful_queries", 0) > 0)
    every_query_has_academic = all(
        any((name, query) in successful_pairs for name in academic)
        for query in query_set
    ) if query_set else False
    web_ok = provider != "hybrid" or stats.get("searxng", {}).get("successful_queries", 0) > 0

    reason_codes: list[str] = []
    blocking_reason_codes: list[str] = []
    if academic_successful < 2:
        blocking_reason_codes.append("ACADEMIC_PROVIDER_DIVERSITY_DEGRADED")
    if not every_query_has_academic:
        blocking_reason_codes.append("ACADEMIC_QUERY_EXECUTION_INCOMPLETE")
    if provider == "hybrid" and not web_ok:
        blocking_reason_codes.append("HYBRID_WEB_CHANNEL_UNAVAILABLE")
    reason_codes.extend(blocking_reason_codes)
    for name, item in stats.items():
        if item["successful_queries"] == 0:
            reason_codes.append(f"PROVIDER_UNAVAILABLE:{name}")
        elif item["success_rate"] < 0.5:
            reason_codes.append(f"PROVIDER_LOW_SUCCESS_RATE:{name}")

    status = "PASS" if not blocking_reason_codes else "DEGRADED"
    return {
        "schema_version": "1.0",
        "status": status,
        "retrieval_provider": provider,
        "required_providers": required,
        "providers": stats,
        "academic_successful_provider_count": academic_successful,
        "every_query_has_academic_execution": every_query_has_academic,
        "hybrid_web_channel_available": web_ok if provider == "hybrid" else None,
        "reason_codes": reason_codes,
        "blocking_reason_codes": blocking_reason_codes,
    }
