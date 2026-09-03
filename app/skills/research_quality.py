from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

from .search_providers.base import CHANNEL_ACADEMIC, CHANNEL_WEB_SEARCH, PROVIDER_CHANNELS, provider_channel

_KNOWN_ACADEMIC_PROVIDERS = tuple(
    name for name, channel in PROVIDER_CHANNELS.items() if channel == CHANNEL_ACADEMIC
)

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




def assess_source_priorities(
    candidate: dict[str, Any],
    priorities: list[str] | tuple[str, ...] | None,
) -> dict[str, Any]:
    """Return deterministic alignment with the Plan's source priorities.

    ``source_priorities`` are model-authored search strategy preferences, not a
    security boundary.  Runtime therefore uses them only for ranking/reporting,
    never to fabricate publication status or to hard-reject otherwise relevant
    evidence.  Recognised generic categories are matched from trusted metadata;
    venue-like priorities use conservative substring matching.
    """

    values = [str(item).strip() for item in priorities or [] if str(item).strip()]
    if not values:
        return {
            "matched_priorities": [],
            "match_count": 0,
            "score_bonus": 0.0,
        }

    source_type = str(candidate.get("source_type") or "").upper()
    publication_status = str(candidate.get("publication_status") or "").upper()
    publication_kind = str(candidate.get("publication_kind") or "").lower()
    title = str(candidate.get("title") or "").lower()
    venue = str(candidate.get("venue") or "").lower()
    publisher = str(candidate.get("publisher") or "").lower()
    url = str(candidate.get("url") or "").lower()
    haystack = " ".join((title, venue, publisher, publication_kind, url))

    peer_reviewed = source_type in {"PEER_REVIEWED_PAPER", "CONFERENCE_PAPER"}
    official = source_type in {"OFFICIAL_STANDARD", "GOVERNMENT", "STANDARD", "OFFICIAL_SOURCE"}
    preprint = publication_status == "PREPRINT" or source_type == "ACADEMIC_PREPRINT"
    review = any(term in title for term in ("systematic review", "literature review", "survey", "review"))

    matched: list[str] = []
    for original in values:
        priority = original.lower().strip()
        is_match = False
        if any(token in priority for token in ("peer reviewed", "peer-reviewed", "同行评议", "正式发表")):
            is_match = peer_reviewed
        elif any(token in priority for token in ("official", "government", "standard", "官方", "政府", "标准")):
            is_match = official
        elif any(token in priority for token in ("review", "survey", "综述", "系统评价")):
            is_match = review
        elif any(token in priority for token in ("preprint", "arxiv", "预印本")):
            is_match = preprint or "arxiv" in haystack
        else:
            # Treat a specific venue/publisher priority as a preference only
            # when enough literal signal survives normalisation.
            compact = re.sub(r"[^a-z0-9]+", " ", priority).strip()
            if len(compact) >= 4:
                normalized_haystack = re.sub(r"[^a-z0-9]+", " ", haystack)
                is_match = compact in normalized_haystack
        if is_match and original not in matched:
            matched.append(original)

    return {
        "matched_priorities": matched,
        "match_count": len(matched),
        "score_bonus": float(min(12, len(matched) * 4)),
    }


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
    execution_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Summarize execution health independently from evidence sufficiency.

    Enabled providers are taken from the discovery manifest. Disabled providers are
    never counted as failures. A partially degraded provider set is non-blocking as
    long as every approved query was executed successfully by at least one enabled
    provider.

    When the Phase 3 execution contract declares ``required_channels`` /
    ``provider_execution_requirements``, a required channel whose providers all failed
    is surfaced as ``REQUIRED_CHANNEL_FAILED:<CHANNEL>``; it becomes blocking only for
    the WEB_SEARCH channel when ``require_web_discovery`` is true, so an Academic-only
    success can never mask a mandated web-discovery failure. Required providers that
    were never executed are always blocking contract violations.
    """

    provider = str(retrieval_provider or "").lower()
    if not discovery_manifest or provider not in {"academic", "hybrid"}:
        return {
            "schema_version": "2.0",
            "status": "UNOBSERVED",
            "retrieval_provider": provider or "unknown",
            "enabled_providers": [],
            "disabled_providers": [],
            "providers": {},
            "reason_codes": [],
            "blocking_reason_codes": [],
            "missing_execution_queries": [],
        }

    contract = execution_contract if isinstance(execution_contract, dict) else {}
    required_channels = [
        str(item or "").strip().upper()
        for item in contract.get("required_channels") or []
        if str(item or "").strip()
    ]
    requirements = contract.get("provider_execution_requirements")
    requirements = requirements if isinstance(requirements, dict) else {}
    required_providers = [
        str(item or "").strip().lower()
        for item in requirements.get("required_providers") or []
        if str(item or "").strip()
    ]
    require_web_discovery = bool(contract.get("require_web_discovery"))

    declared = [
        str(item or "").strip().lower()
        for item in discovery_manifest.get("providers") or []
        if str(item or "").strip()
    ]
    # Older manifests may omit providers. Infer only from actual provider runs/failures;
    # if that also yields nothing, fall back to the current default academic pair.
    if not declared:
        for row in [*(discovery_manifest.get("provider_runs") or []), *(discovery_manifest.get("failures") or [])]:
            if not isinstance(row, dict):
                continue
            name = str(row.get("provider") or "").strip().lower()
            if name and name not in declared:
                declared.append(name)
    if not declared:
        declared = ["openalex", "crossref"]
        if provider == "hybrid":
            declared.append("searxng")

    known_academic = _KNOWN_ACADEMIC_PROVIDERS
    enabled = [name for name in declared if provider_channel(name) is not None]
    if provider == "hybrid" and "searxng" not in enabled:
        enabled.append("searxng")
    disabled = [name for name in known_academic if name not in enabled]

    query_set = {str(query) for query in queries if str(query).strip()}
    stats = {
        name: {
            "enabled": True,
            "attempted_queries": len(query_set),
            "successful_queries": 0,
            "failed_queries": 0,
            "result_count": 0,
            "success_rate": 0.0,
        }
        for name in enabled
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

    successful_provider_count = sum(1 for item in stats.values() if item["successful_queries"] > 0)
    missing_execution_queries = sorted(
        query
        for query in query_set
        if not any((name, query) in successful_pairs and name not in global_failed_providers for name in enabled)
    )

    reason_codes: list[str] = []
    blocking_reason_codes: list[str] = []
    if not enabled or successful_provider_count == 0:
        blocking_reason_codes.append("NO_ENABLED_PROVIDER_SUCCEEDED")
    if missing_execution_queries:
        blocking_reason_codes.append("APPROVED_QUERY_EXECUTION_MISSING")
    for name, item in stats.items():
        if item["successful_queries"] == 0:
            reason_codes.append(f"PROVIDER_UNAVAILABLE:{name}")
        elif item["success_rate"] < 0.5:
            reason_codes.append(f"PROVIDER_LOW_SUCCESS_RATE:{name}")
    academic_enabled = [name for name in enabled if name in known_academic]
    academic_successful = sum(1 for name in academic_enabled if stats.get(name, {}).get("successful_queries", 0) > 0)
    if len(academic_enabled) >= 2 and academic_successful < 2:
        reason_codes.append("ACADEMIC_PROVIDER_DIVERSITY_DEGRADED")
    web_enabled = [name for name in enabled if provider_channel(name) == CHANNEL_WEB_SEARCH]
    web_successful = [name for name in web_enabled if stats.get(name, {}).get("successful_queries", 0) > 0]
    if provider == "hybrid" and not web_successful:
        reason_codes.append("HYBRID_WEB_CHANNEL_UNAVAILABLE")
    for channel in required_channels:
        members = [name for name in enabled if provider_channel(name) == channel]
        if not members:
            code = f"REQUIRED_CHANNEL_NOT_EXECUTED:{channel}"
        else:
            successful = [name for name in members if stats.get(name, {}).get("successful_queries", 0) > 0]
            code = "" if successful else f"REQUIRED_CHANNEL_FAILED:{channel}"
        if code:
            # A mandated web-discovery failure must never be masked by Academic
            # success; other required-channel degradations remain observable but
            # non-blocking so technical research can still finish DEGRADED.
            if channel == CHANNEL_WEB_SEARCH and require_web_discovery:
                blocking_reason_codes.append(code)
            else:
                reason_codes.append(code)
    for name in required_providers:
        if name not in enabled:
            blocking_reason_codes.append(f"REQUIRED_PROVIDER_NOT_EXECUTED:{name}")
    reason_codes = list(dict.fromkeys([*blocking_reason_codes, *reason_codes]))

    if blocking_reason_codes:
        status = "BLOCKING_FAILURE"
    elif reason_codes:
        status = "DEGRADED"
    else:
        status = "PASS"
    return {
        "schema_version": "2.0",
        "status": status,
        "retrieval_provider": provider,
        "enabled_providers": enabled,
        "disabled_providers": disabled,
        "providers": stats,
        "successful_provider_count": successful_provider_count,
        "academic_successful_provider_count": academic_successful,
        "every_query_has_execution": not missing_execution_queries,
        "missing_execution_queries": missing_execution_queries,
        "reason_codes": reason_codes,
        "blocking_reason_codes": blocking_reason_codes,
    }


def build_research_sufficiency(
    coverage: dict[str, Any] | None,
    normalized_plan: dict[str, Any] | None,
    retrieval_health: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the deterministic ResearchSufficiency/ResearchGap object.

    Evidence quality can be insufficient without being a provider failure. The
    workflow may continue with explicit gaps when some usable public evidence exists;
    only execution/integrity-equivalent failures or zero usable evidence are blocking.
    """

    coverage = coverage if isinstance(coverage, dict) else {}
    plan = normalized_plan if isinstance(normalized_plan, dict) else {}
    health = retrieval_health if isinstance(retrieval_health, dict) else {"status": "UNOBSERVED"}
    by_query = coverage.get("by_query") if isinstance(coverage.get("by_query"), dict) else {}
    dimensions = coverage.get("dimensions") if isinstance(coverage.get("dimensions"), dict) else {}

    query_meta: dict[str, dict[str, Any]] = {}
    for item in plan.get("query_items") or []:
        if not isinstance(item, dict):
            continue
        query = str(item.get("query") or "").strip()
        if not query:
            continue
        query_meta[query] = {
            "query_id": str(item.get("query_id") or "") or None,
            "linked_question_indexes": [
                int(v) for v in item.get("linked_question_indexes") or []
                if isinstance(v, int) and not isinstance(v, bool)
            ],
        }

    query_min = int((dimensions.get("query_depth") or {}).get("minimum_sources_per_query") or 1)
    authority_min = int((dimensions.get("query_authoritative_depth") or {}).get("minimum_authoritative_sources_per_query") or 1)
    gaps: list[dict[str, Any]] = []
    total_bound_sources: set[str] = set()
    for index, query in enumerate(plan.get("queries") or []):
        query = str(query or "").strip()
        if not query:
            continue
        item = by_query.get(query) if isinstance(by_query.get(query), dict) else {}
        source_ids = [str(v) for v in item.get("source_ids") or [] if str(v)]
        authoritative_ids = [str(v) for v in item.get("authoritative_source_ids") or [] if str(v)]
        total_bound_sources.update(source_ids)
        gap_types: list[str] = []
        if int(item.get("source_count") or 0) < query_min:
            gap_types.append("DEPTH")
        if int(item.get("authoritative_source_count") or 0) < authority_min:
            gap_types.append("AUTHORITY")
        if not gap_types:
            continue
        meta = query_meta.get(query) or {}
        gaps.append({
            "gap_id": f"research-gap-{index + 1:03d}",
            "scope": "QUERY",
            "query_id": meta.get("query_id"),
            "query": query,
            "linked_question_indexes": list(meta.get("linked_question_indexes") or []),
            "gap_types": gap_types,
            "source_count": int(item.get("source_count") or 0),
            "required_source_count": query_min,
            "authoritative_source_count": int(item.get("authoritative_source_count") or 0),
            "required_authoritative_source_count": authority_min,
            "source_ids": source_ids,
            "authoritative_source_ids": authoritative_ids,
            "description": (
                f"Approved query has {int(item.get('source_count') or 0)}/{query_min} qualifying sources "
                f"and {int(item.get('authoritative_source_count') or 0)}/{authority_min} authoritative sources."
            ),
        })

    # Preserve non-query quality deficiencies as explicit global limitations. They do
    # not independently block a run that still has usable evidence.
    for name, value in dimensions.items():
        if name in {"query_depth", "query_authoritative_depth", "query_fulltext_depth", "retrieval_health"}:
            continue
        if not isinstance(value, dict) or value.get("status") == "PASS":
            continue
        gaps.append({
            "gap_id": f"research-gap-global-{len(gaps) + 1:03d}",
            "scope": "GLOBAL",
            "query_id": None,
            "query": None,
            "linked_question_indexes": [],
            "gap_types": [str(name).upper()],
            "source_count": None,
            "required_source_count": None,
            "authoritative_source_count": None,
            "required_authoritative_source_count": None,
            "source_ids": [],
            "authoritative_source_ids": [],
            "description": f"Research quality dimension {name} is insufficient.",
        })

    blocking_reasons = list(health.get("blocking_reason_codes") or [])
    if health.get("status") == "BLOCKING_FAILURE" and not blocking_reasons:
        blocking_reasons.append("RETRIEVAL_HEALTH_BLOCKING_FAILURE")
    if not total_bound_sources and by_query:
        blocking_reasons.append("NO_QUALIFYING_PUBLIC_EVIDENCE")

    if blocking_reasons:
        status = "BLOCKING_FAILURE"
    elif coverage.get("status") == "PASS":
        status = "SUFFICIENT"
    else:
        status = "DEGRADED"

    return {
        "schema_version": "1.0",
        "status": status,
        "coverage_status": str(coverage.get("status") or "UNKNOWN"),
        "research_gaps": gaps,
        "blocking_reasons": list(dict.fromkeys(str(v) for v in blocking_reasons if str(v))),
        "retrieval_health_status": str(health.get("status") or "UNOBSERVED"),
        "may_continue": status in {"SUFFICIENT", "DEGRADED"},
    }


APPLICATION_BACKGROUND_PROFILE = "application_background"
KNOWN_RESEARCH_QUALITY_PROFILES = frozenset(
    {"legacy", "proposal_related_work", APPLICATION_BACKGROUND_PROFILE}
)


def normalize_research_quality_profile(value: Any) -> str:
    profile = str(value or "legacy").strip().lower()
    return profile if profile in KNOWN_RESEARCH_QUALITY_PROFILES else "legacy"


def _record_discovery_providers(record: dict[str, Any]) -> list[str]:
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


def build_background_coverage_dimensions(
    records: list[dict[str, Any]],
    by_query: dict[str, dict[str, Any]],
    *,
    min_sources_per_query: int = 3,
    min_fulltext_sources_per_query: int = 1,
    retrieval_health: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Coverage dimensions for the ``application_background`` quality profile.

    Proposal related-work dimensions (recent work / baselines / limitations)
    do not apply to topic background research.  What matters here is per-query
    evidence depth and — because the WF-3B execution contract always forces
    ``require_web_discovery=true`` — at least one usable evidence record that
    was actually discovered through the WEB_SEARCH channel.  A pure Academic
    source set therefore can never report a PASS for this profile: the web
    evidence dimension stays INSUFFICIENT and surfaces as an explicit research
    gap instead of a silently "sufficient" background run.
    """

    query_min = max(1, min(int(min_sources_per_query), 8))
    fulltext_min = max(0, int(min_fulltext_sources_per_query or 0))
    shallow = [
        query
        for query, item in by_query.items()
        if int(item.get("source_count") or 0) < query_min
    ]
    fulltext_shallow = [
        query
        for query, item in by_query.items()
        if int(item.get("source_count") or 0) < fulltext_min
    ]
    web_sourced = [
        record
        for record in records
        if any(
            provider_channel(name) == CHANNEL_WEB_SEARCH
            for name in _record_discovery_providers(record)
        )
    ]
    return {
        "query_depth": {
            "status": "PASS" if not shallow and bool(by_query) else "INSUFFICIENT",
            "minimum_sources_per_query": query_min,
            "shallow_queries": shallow,
        },
        "query_fulltext_depth": {
            "status": "PASS" if not fulltext_shallow and bool(by_query) else "INSUFFICIENT",
            "minimum_fulltext_sources_per_query": fulltext_min,
            "shallow_queries": fulltext_shallow,
        },
        "web_evidence": {
            "status": "PASS" if web_sourced else "INSUFFICIENT",
            "require_web_discovery": True,
            "source_ids": [str(record.get("source_id")) for record in web_sourced],
            "minimum_source_count": 1,
        },
        "retrieval_health": {
            "status": (
                "PASS"
                if not retrieval_health or retrieval_health.get("status") in {"PASS", "UNOBSERVED"}
                else "INSUFFICIENT"
            ),
            "health": retrieval_health or {"status": "UNOBSERVED"},
        },
    }
