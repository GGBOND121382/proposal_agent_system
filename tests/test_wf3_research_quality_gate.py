from __future__ import annotations

from types import SimpleNamespace

from app.skills.academic_search import AcademicSearchClient
from app.skills.research_audit import coverage_report, source_category
from app.skills.research_plan import normalize_and_validate_plan, parse_time_scope_bounds
from app.skills.research_quality import build_research_sufficiency, build_retrieval_health
from app.skills.research_screening import screen_and_select_candidates


def _realistic_plan() -> dict:
    return {
        "plan_id": "plan-quality",
        "task_type": "PUBLIC_RESEARCH",
        "binding_contract_version": "1.0",
        "research_questions": [
            "How are state, facts, evidence, versions, temporal dependencies and feedback represented in decision systems?",
            "How are human and AI roles dynamically allocated under uncertainty?",
        ],
        "queries": [
            {
                "query_id": "query-001",
                "query": "multi-agent decision workflow explicit contract responsibility decomposition",
                "linked_question_indexes": [0],
            },
            {
                "query_id": "query-002",
                "query": "decision provenance tracking unified state fact evidence version model",
                "linked_question_indexes": [0],
            },
            {
                "query_id": "query-003",
                "query": "temporal dependencies feedback loops incremental impact propagation decision systems",
                "linked_question_indexes": [0],
            },
            {
                "query_id": "query-004",
                "query": "dynamic role allocation human-AI collaboration attention budget uncertainty",
                "linked_question_indexes": [1],
            },
            {
                "query_id": "query-005",
                "query": "human-in-the-loop short-cycle collaboration rollback oversight mechanism agent",
                "linked_question_indexes": [1],
            },
        ],
        "source_priorities": ["peer reviewed papers"],
        "time_scope": "2021-08-28/2026-08-28",
        "evidence_requirements": ["recent work", "baselines", "limitations"],
        "prohibited_inferences": ["no internal inference"],
    }


def test_exact_time_scope_bounds_preserve_day_precision() -> None:
    start, end = parse_time_scope_bounds("2021-08-28/2026-08-28")
    assert start and start.isoformat() == "2021-08-28"
    assert end and end.isoformat() == "2026-08-28"


def test_academic_adapters_send_exact_date_filters(monkeypatch) -> None:
    client = AcademicSearchClient(SimpleNamespace(research_fetch_timeout_seconds=5))
    calls: list[tuple[str, dict]] = []

    def fake(url, *, params, headers=None):
        calls.append((url, dict(params)))
        if "openalex" in url:
            return {"results": []}
        if "crossref" in url:
            return {"message": {"items": []}}
        return {"data": []}

    monkeypatch.setattr(client, "_get_json", fake)
    scope = "2021-08-28/2026-08-28"
    client.search_openalex("decision provenance", 5, scope)
    client.search_crossref("decision provenance", 5, scope)
    client.search_semantic_scholar("decision provenance", 5, scope)

    assert calls[0][1]["filter"] == "from_publication_date:2021-08-28,to_publication_date:2026-08-28"
    assert calls[1][1]["filter"] == "from-pub-date:2021-08-28,until-pub-date:2026-08-28"
    # Semantic Scholar supports a year filter only; exact dates are enforced after retrieval.
    assert calls[2][1]["year"] == "2021-2026"


def test_semantic_relevance_rejects_real_keyword_collision_examples() -> None:
    normalized, validation = normalize_and_validate_plan(_realistic_plan(), strict=True)
    assert validation["status"] == "PASS"
    q_provenance = normalized["queries"][1]
    q_feedback = normalized["queries"][2]
    q_roles = normalized["queries"][3]
    candidates = [
        {
            "title": "The DOE E3SM Model Version 2: Overview of the physical model and initial model evaluation",
            "url": "https://doi.org/10.1000/e3sm",
            "published_at": "2024-01-01",
            "matched_query": q_provenance,
            "abstract": "Earth system model version two evaluates atmosphere ocean and climate components.",
        },
        {
            "title": "Drought as a continuum – memory effects in hydrological ecological and social systems",
            "url": "https://doi.org/10.1000/drought",
            "published_at": "2024-01-01",
            "matched_query": q_feedback,
            "abstract": "Drought feedback loops and impacts propagate across hydrological and ecological processes.",
        },
        {
            "title": "Dating Apps and Feedback Loops",
            "url": "https://doi.org/10.1000/dating",
            "published_at": "2024-01-01",
            "matched_query": q_feedback,
            "abstract": "Dating applications create social feedback loops among users.",
        },
        {
            "title": "AI-driven adaptive learning for educational transformation",
            "url": "https://doi.org/10.1000/education",
            "published_at": "2024-01-01",
            "matched_query": q_roles,
            "abstract": "AI has a role in adaptive learning and personalised education.",
        },
        {
            "title": "Decision provenance and evidence versioning for auditable multi-agent workflows",
            "url": "https://doi.org/10.1000/relevant",
            "published_at": "2024-01-01",
            "matched_query": q_provenance,
            "abstract": "A decision workflow tracks provenance, evidence, state and version changes for auditable multi-agent decisions.",
        },
    ]
    selected, report = screen_and_select_candidates(
        candidates,
        normalized,
        max_results=10,
        strict=True,
        min_per_query=1,
        enforce_semantic_relevance=True,
    )
    assert [item["url"] for item in selected] == ["https://doi.org/10.1000/relevant"]
    rejected = [item for item in report["issues"] if item.get("code") == "CANDIDATE_REJECTED"]
    assert sum("LOW_SEMANTIC_RELEVANCE" in (item.get("reason_codes") or []) for item in rejected) >= 3


def test_exact_time_scope_rejects_before_and_after_boundary_dates() -> None:
    normalized, _ = normalize_and_validate_plan(_realistic_plan(), strict=True)
    query = normalized["queries"][1]
    base = {
        "title": "Decision provenance and evidence versioning for auditable decision workflows",
        "abstract": "Decision provenance tracking keeps evidence state fact and version history in decision systems.",
        "matched_query": query,
    }
    candidates = [
        {**base, "url": "https://doi.org/10.1000/early", "published_at": "2021-05-31"},
        {**base, "url": "https://doi.org/10.1000/inside", "published_at": "2024-02-01"},
        {**base, "url": "https://doi.org/10.1000/future", "published_at": "2026-09-01"},
    ]
    selected, report = screen_and_select_candidates(
        candidates,
        normalized,
        max_results=10,
        strict=True,
        min_per_query=1,
        enforce_semantic_relevance=True,
    )
    assert [item["url"] for item in selected] == ["https://doi.org/10.1000/inside"]
    reasons = [reason for item in report["issues"] for reason in item.get("reason_codes") or []]
    assert reasons.count("OUTSIDE_TIME_SCOPE") == 2


def test_publication_status_does_not_equate_doi_with_peer_review(monkeypatch) -> None:
    client = AcademicSearchClient(SimpleNamespace(research_fetch_timeout_seconds=5))
    payload = {
        "message": {
            "items": [
                {
                    "DOI": "10.21203/rs.3.rs-3258718/v1",
                    "title": ["Human-AI Collaborative Decision-making: A Cognitive Ergonomics Approach"],
                    "type": "posted-content",
                    "subtype": "preprint",
                    "publisher": "Research Square Platform LLC",
                    "published": {"date-parts": [[2023, 8, 22]]},
                    "URL": "https://doi.org/10.21203/rs.3.rs-3258718/v1",
                },
                {
                    "DOI": "10.1000/journal",
                    "title": ["Peer reviewed decision support article"],
                    "type": "journal-article",
                    "publisher": "Journal X",
                    "published": {"date-parts": [[2024, 2, 3]]},
                    "URL": "https://doi.org/10.1000/journal",
                },
            ]
        }
    }
    monkeypatch.setattr(client, "_get_json", lambda *args, **kwargs: payload)
    values, _ = client.search_crossref("human AI decision", 5, "2021-08-28/2026-08-28")
    assert values[0]["source_type"] == "ACADEMIC_PREPRINT"
    assert values[0]["publication_status"] == "PREPRINT"
    assert values[1]["source_type"] == "PEER_REVIEWED_PAPER"
    assert source_category({"doi": "10.1000/unknown", "domain": "doi.org"}) == "SCHOLARLY_PUBLICATION_UNVERIFIED"
    assert source_category({"title": "Decision letter for a published paper", "doi": "10.1000/letter", "source_type": "PEER_REVIEWED_PAPER"}) == "EDITORIAL"


def test_hybrid_retrieval_health_exposes_missing_web_and_rate_limited_channels() -> None:
    queries = [f"query {index}" for index in range(8)]
    provider_runs = []
    failures = []
    for query in queries:
        provider_runs.append({"provider": "openalex", "query": query, "result_count": 8})
    for query in queries[:3]:
        provider_runs.append({"provider": "crossref", "query": query, "result_count": 8})
    for query in queries[3:]:
        failures.append({"provider": "crossref", "query": query, "error_code": "429"})
    for query in queries:
        failures.append({"provider": "semantic_scholar", "query": query, "error_code": "429"})
    provider_runs.append({"provider": "searxng", "result_count": 0, "query_failures": [{"error_code": "ALL_FAILED"}]})
    failures.append({"provider": "searxng", "error_code": "HYBRID_SEARXNG_DISCOVERY_ERROR"})
    health = build_retrieval_health(
        {"provider_runs": provider_runs, "failures": failures},
        retrieval_provider="hybrid",
        queries=queries,
    )
    assert health["status"] == "DEGRADED"
    assert health["providers"]["openalex"]["success_rate"] == 1.0
    assert health["providers"]["crossref"]["success_rate"] == 0.375
    assert health["providers"]["semantic_scholar"]["successful_queries"] == 0
    assert health["providers"]["searxng"]["successful_queries"] == 0
    assert "HYBRID_WEB_CHANNEL_UNAVAILABLE" in health["reason_codes"]
    assert "HYBRID_WEB_CHANNEL_UNAVAILABLE" not in health["blocking_reason_codes"]


def test_strict_coverage_uses_relevance_and_retrieval_health_not_search_binding_alone() -> None:
    normalized, _ = normalize_and_validate_plan(_realistic_plan(), strict=True)
    query = normalized["queries"][1]
    records = []
    for index, label in enumerate(("DIRECT", "TANGENTIAL", "OFF_TOPIC"), 1):
        records.append(
            {
                "source_id": f"src-{index}",
                "title": "Decision research",
                "excerpt": "benchmark review limitations",
                "source_category": "PEER_REVIEWED_PAPER",
                "authority_rank": 90,
                "publisher": f"Journal {index}",
                "authors": [f"Author {index}"],
                "is_recent": True,
                "supports_baseline": True,
                "supports_limitation": True,
                "matched_query": query,
                "verification": {
                    "matched_queries": [query],
                    "discovery_provider": "openalex",
                    "semantic_relevance_by_query": {
                        query: {"label": label, "qualifies_for_coverage": label == "DIRECT"}
                    },
                },
            }
        )
    report = coverage_report(
        records,
        {**normalized, "queries": [query], "query_items": [normalized["query_items"][1]]},
        quality_profile="proposal_related_work",
        min_sources_per_query=3,
        retrieval_health={"status": "DEGRADED", "reason_codes": ["HYBRID_WEB_CHANNEL_UNAVAILABLE"]},
    )
    assert report["by_query"][query]["source_count"] == 1
    assert report["dimensions"]["query_depth"]["status"] == "INSUFFICIENT"
    assert report["dimensions"]["retrieval_health"]["status"] == "INSUFFICIENT"
    assert report["status"] == "INSUFFICIENT"


def test_relevance_ignores_document_type_prefix_and_requires_broader_anchor_context() -> None:
    normalized, _ = normalize_and_validate_plan(_realistic_plan(), strict=True)
    q_provenance = normalized["queries"][1]
    q_feedback = normalized["queries"][2]
    candidates = [
        {
            "title": 'Decision letter for "Immunological feedback loops generate parasite persistence thresholds"',
            "url": "https://doi.org/10.1000/decision-letter",
            "published_at": "2024-01-01",
            "matched_query": q_feedback,
            "abstract": "Feedback loops explain parasite persistence in infection dynamics.",
        },
        {
            "title": "Artificial intelligence and blockchain implementation in supply chains",
            "url": "https://doi.org/10.1000/supply-chain",
            "published_at": "2024-01-01",
            "matched_query": q_provenance,
            "abstract": "A unified supply-chain platform uses evidence for operational decisions.",
        },
        {
            "title": "Decision provenance and evidence versioning for auditable multi-agent workflows",
            "url": "https://doi.org/10.1000/provenance-good",
            "published_at": "2024-01-01",
            "matched_query": q_provenance,
            "abstract": "Decision provenance tracking records state facts evidence and version changes in an auditable workflow.",
        },
    ]
    selected, report = screen_and_select_candidates(
        candidates,
        normalized,
        max_results=10,
        strict=True,
        min_per_query=1,
        enforce_semantic_relevance=True,
    )
    assert [item["url"] for item in selected] == ["https://doi.org/10.1000/provenance-good"]
    reasons = [reason for item in report["issues"] for reason in item.get("reason_codes") or []]
    assert reasons.count("LOW_SEMANTIC_RELEVANCE") >= 2


def test_strict_coverage_requires_authoritative_depth_per_query() -> None:
    normalized, _ = normalize_and_validate_plan(_realistic_plan(), strict=True)
    query = normalized["queries"][1]
    records = []
    for index in range(3):
        records.append(
            {
                "source_id": f"preprint-{index}",
                "title": "Decision provenance review",
                "excerpt": "review baseline limitation evidence",
                "source_category": "ACADEMIC_PREPRINT",
                "authority_rank": 72,
                "publisher": f"Repository {index}",
                "authors": [f"Author {index}"],
                "is_recent": True,
                "supports_baseline": True,
                "supports_limitation": True,
                "matched_query": query,
                "verification": {
                    "matched_queries": [query],
                    "discovery_provider": "openalex",
                    "semantic_relevance_by_query": {
                        query: {"label": "DIRECT", "qualifies_for_coverage": True}
                    },
                },
            }
        )
    report = coverage_report(
        records,
        {**normalized, "queries": [query], "query_items": [normalized["query_items"][1]]},
        quality_profile="proposal_related_work",
        min_sources_per_query=3,
        retrieval_health={"status": "PASS"},
    )
    assert report["dimensions"]["query_depth"]["status"] == "PASS"
    assert report["dimensions"]["query_authoritative_depth"]["status"] == "INSUFFICIENT"
    assert report["by_query"][query]["authoritative_source_count"] == 0
    assert report["status"] == "INSUFFICIENT"


def test_source_priorities_are_consumed_by_runtime_ranking_not_left_as_dead_plan_fields() -> None:
    plan = _realistic_plan()
    plan["research_questions"] = ["How is decision provenance represented?"]
    plan["queries"] = [{
        "query_id": "query-priority",
        "query": "decision provenance evidence version auditable workflow",
        "linked_question_indexes": [0],
    }]
    plan["source_priorities"] = ["peer reviewed papers", "IEEE TKDE"]
    normalized, validation = normalize_and_validate_plan(plan, strict=True)
    assert validation["status"] == "PASS"
    query = normalized["queries"][0]
    candidates = [
        {
            "title": "Decision provenance evidence versioning for auditable workflows",
            "url": "https://doi.org/10.1000/preprint",
            "doi": "10.1000/preprint",
            "published_at": "2024-01-01",
            "matched_query": query,
            "abstract": "Decision provenance evidence versioning supports auditable workflow decisions.",
            "source_type": "ACADEMIC_PREPRINT",
            "publication_status": "PREPRINT",
            "venue": "Research Square",
            "citation_count": 20,
        },
        {
            "title": "Decision provenance evidence versioning for auditable workflows",
            "url": "https://doi.org/10.1000/peer",
            "doi": "10.1000/peer",
            "published_at": "2024-01-01",
            "matched_query": query,
            "abstract": "Decision provenance evidence versioning supports auditable workflow decisions.",
            "source_type": "PEER_REVIEWED_PAPER",
            "publication_status": "PUBLISHED",
            "venue": "IEEE Transactions on Knowledge and Data Engineering",
            "publisher": "IEEE",
            "citation_count": 0,
        },
    ]
    selected, report = screen_and_select_candidates(
        candidates,
        normalized,
        max_results=1,
        strict=True,
        min_per_query=1,
        enforce_semantic_relevance=True,
    )
    assert len(selected) == 1
    assert selected[0]["url"] == "https://doi.org/10.1000/peer"
    assessment = selected[0]["verification"]["source_priority_assessment"]
    assert "peer reviewed papers" in assessment["matched_priorities"]
    assert report["source_priorities"] == ["peer reviewed papers", "IEEE TKDE"]
    assert report["priority_matched_candidate_count"] == 1
    assert report["selected_priority_match_counts"]["peer reviewed papers"] == 1


def test_doi_does_not_restore_peer_review_authority_bonus_for_preprints() -> None:
    plan = _realistic_plan()
    plan["research_questions"] = ["How is decision provenance represented?"]
    plan["queries"] = [{
        "query_id": "query-doi",
        "query": "decision provenance evidence version auditable workflow",
        "linked_question_indexes": [0],
    }]
    plan["source_priorities"] = ["peer reviewed papers"]
    normalized, _ = normalize_and_validate_plan(plan, strict=True)
    query = normalized["queries"][0]
    candidates = [
        {
            "title": "Decision provenance evidence versioning for auditable workflows",
            "url": "https://doi.org/10.1000/preprint-doi",
            "doi": "10.1000/preprint-doi",
            "published_at": "2024-01-01",
            "matched_query": query,
            "abstract": "Decision provenance evidence versioning supports auditable workflow decisions.",
            "source_type": "ACADEMIC_PREPRINT",
            "publication_status": "PREPRINT",
            "citation_count": 0,
        },
        {
            "title": "Decision provenance evidence versioning for auditable workflows",
            "url": "https://example.org/peer-no-doi",
            "published_at": "2024-01-01",
            "matched_query": query,
            "abstract": "Decision provenance evidence versioning supports auditable workflow decisions.",
            "source_type": "PEER_REVIEWED_PAPER",
            "publication_status": "PUBLISHED",
            "citation_count": 0,
        },
    ]
    selected, _ = screen_and_select_candidates(
        candidates,
        normalized,
        max_results=1,
        strict=True,
        min_per_query=1,
        enforce_semantic_relevance=True,
    )
    # DOI contributes traceability only; it cannot impersonate peer-review authority.
    assert selected[0]["url"] == "https://example.org/peer-no-doi"



def test_research_sufficiency_marks_shallow_query_degraded_not_blocking() -> None:
    plan = {
        "queries": ["q1", "q2"],
        "query_items": [
            {"query_id": "query-001", "query": "q1", "linked_question_indexes": [0]},
            {"query_id": "query-002", "query": "q2", "linked_question_indexes": [1]},
        ],
    }
    coverage = {
        "status": "INSUFFICIENT",
        "by_query": {
            "q1": {"source_count": 3, "source_ids": ["s1", "s2", "s3"], "authoritative_source_count": 1, "authoritative_source_ids": ["s1"]},
            "q2": {"source_count": 1, "source_ids": ["s4"], "authoritative_source_count": 0, "authoritative_source_ids": []},
        },
        "dimensions": {
            "query_depth": {"status": "INSUFFICIENT", "minimum_sources_per_query": 3},
            "query_authoritative_depth": {"status": "INSUFFICIENT", "minimum_authoritative_sources_per_query": 1},
        },
    }
    value = build_research_sufficiency(
        coverage,
        plan,
        {"status": "PASS", "blocking_reason_codes": []},
    )
    assert value["status"] == "DEGRADED"
    assert value["may_continue"] is True
    assert len(value["research_gaps"]) == 1
    gap = value["research_gaps"][0]
    assert gap["query_id"] == "query-002"
    assert set(gap["gap_types"]) == {"DEPTH", "AUTHORITY"}
    assert gap["linked_question_indexes"] == [1]


def test_research_sufficiency_blocks_when_no_query_has_qualifying_evidence() -> None:
    plan = {
        "queries": ["q1"],
        "query_items": [{"query_id": "query-001", "query": "q1", "linked_question_indexes": [0]}],
    }
    coverage = {
        "status": "INSUFFICIENT",
        "by_query": {
            "q1": {"source_count": 0, "source_ids": [], "authoritative_source_count": 0, "authoritative_source_ids": []},
        },
        "dimensions": {
            "query_depth": {"status": "INSUFFICIENT", "minimum_sources_per_query": 3},
            "query_authoritative_depth": {"status": "INSUFFICIENT", "minimum_authoritative_sources_per_query": 1},
        },
    }
    value = build_research_sufficiency(
        coverage,
        plan,
        {"status": "PASS", "blocking_reason_codes": []},
    )
    assert value["status"] == "BLOCKING_FAILURE"
    assert value["may_continue"] is False
    assert "NO_QUALIFYING_PUBLIC_EVIDENCE" in value["blocking_reasons"]
