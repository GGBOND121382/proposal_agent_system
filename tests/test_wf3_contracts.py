from __future__ import annotations

import json
from pathlib import Path

from app.executor import PromptExecutor
from app.wf3_contracts import (
    canonicalize_wf3_producer_status,
    compact_wf3_research_envelope,
    compare_public_search_candidates,
    wf3_critic_routing_report,
)


FIXTURE = Path(__file__).parent / "fixtures" / "wf3_historical_regressions_20260826.json"


def _cases():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


def test_historical_advisory_only_synthesis_does_not_block():
    case = _cases()["advisory_only_synthesis"]
    output, report = canonicalize_wf3_producer_status(
        "P-PUBLIC-RESEARCH-SYNTHESIS", case["output"]
    )

    assert case["run_id"] == "run-ef65772880274b45"
    assert case["input_sha256"] == "88416b1cb85e491d3cb29f79a896559371049714349d09ffd79813ef5251513d"
    assert case["output_sha256"] == "40a3e12cfd7a34bd55678332c7d4db7039428aa73feda4699fc07474e8062934"
    assert len(case["output"]["findings"]) == case["historical_counts"]["findings"]
    assert len(case["output"]["unresolved_items"]) == case["historical_counts"]["unresolved_items"]
    assert len(case["output"]["user_questions"]) == case["historical_counts"]["user_questions"]
    assert output["status"] == "PASS"
    assert report["reason"] == "ADVISORY_ONLY"
    assert all(item["blocking"] is False for item in output["findings"])


def test_wf3_producer_pass_with_blocker_is_deterministically_revise():
    output, report = canonicalize_wf3_producer_status(
        "P-PUBLIC-RESEARCH-PLAN",
        {
            "status": "PASS",
            "findings": [{"code": "MISSING_SCOPE", "blocking": True}],
            "unresolved_items": [],
            "user_questions": [],
            "warnings": [],
        },
    )

    assert output["status"] == "REVISE"
    assert report["reason"] == "BLOCKING_CONTENT_ITEM"


def test_historical_critic_retrieval_findings_never_route_to_synthesis():
    case = _cases()["critic_cross_capability"]
    report = wf3_critic_routing_report(case["output"])

    assert case["run_id"] == "run-de8115e860304095"
    assert case["input_sha256"] == "5399c8efb6a7d5b95148c081d19f369ad1a8db36ece8b2b9d38c300e10ddfe09"
    assert case["output_sha256"] == "1b21481dd81b3e6796dd1ff5187dd1315c693b03da0c6431b8209ab42ac222ca"
    assert report["blocking_finding_count"] == 2
    assert report["route_counts"]["RETRIEVAL"] == 2
    assert report["route_counts"]["SYNTHESIS"] == 0
    assert report["has_non_synthesis_route"] is True


def _search_candidate(*, queries, source_ids, uncovered=(), dimensions=(), issues=()):
    return {
        "queries": list(queries),
        "sources": [{"source_id": source_id} for source_id in source_ids],
        "source_catalog": [
            {"source_id": source_id, "authority_rank": 90}
            for source_id in source_ids
        ],
        "coverage": {
            "by_query": {
                query: {"source_count": 0 if query in uncovered else 1}
                for query in queries
            },
            "uncovered_queries": list(uncovered),
            "dimensions": {
                name: {"status": "PASS"} for name in dimensions
            },
        },
        "issues": list(issues),
        "archive_verification": {"status": "PASS"},
    }


def test_search_candidate_cannot_silently_drop_queries_or_coverage():
    accepted = _search_candidate(
        queries=["q1", "q2", "q3"],
        source_ids=["s1", "s2", "s3"],
        dimensions=["recent_work", "comparable_baselines"],
    )
    candidate = _search_candidate(
        queries=["q1", "q2"],
        source_ids=["n1", "n2"],
        dimensions=["recent_work"],
    )

    comparison = compare_public_search_candidates(accepted, candidate)

    assert comparison["accepted"] is False
    assert "QUERY_SET_SHRANK" in comparison["regressions"]
    assert "COVERAGE_DIMENSION_REGRESSED" in comparison["regressions"]


def test_search_candidate_may_remove_bad_source_when_coverage_is_preserved():
    bad_issue = {"type": "SOURCE_CONFLICT", "code": "WITHDRAWN_SOURCE"}
    accepted = _search_candidate(
        queries=["q1", "q2"],
        source_ids=["s1", "s2", "bad"],
        dimensions=["recent_work", "comparable_baselines"],
        issues=[bad_issue],
    )
    candidate = _search_candidate(
        queries=["q1", "q2"],
        source_ids=["s1", "s2"],
        dimensions=["recent_work", "comparable_baselines"],
    )

    comparison = compare_public_search_candidates(accepted, candidate)

    assert comparison["accepted"] is True
    assert "critical_issue_count" in comparison["improvements"]


def test_model_projection_deduplicates_passage_source_text_only():
    envelope = {
        "payload": {
            "retrieved_sources": [
                {
                    "source_id": "s1",
                    "source_type": "PUBLIC_SOURCE",
                    "quoted_text": "duplicate excerpt",
                    "authority_rank": 90,
                }
            ],
            "extracted_passages": [
                {
                    "passage_id": "p1",
                    "source_ref": {
                        "source_id": "s1",
                        "source_type": "PUBLIC_SOURCE",
                        "quoted_text": "duplicate excerpt",
                        "source_hash": "abc",
                    },
                    "text": "complete evidence text",
                }
            ],
        }
    }

    compact, report = compact_wf3_research_envelope(
        "P-PUBLIC-RESEARCH-SYNTHESIS", envelope
    )

    assert compact["payload"]["extracted_passages"][0]["text"] == "complete evidence text"
    assert compact["payload"]["extracted_passages"][0]["source_ref"] == {
        "source_id": "s1",
        "source_type": "PUBLIC_SOURCE",
        "source_hash": "abc",
    }
    assert "quoted_text" not in compact["payload"]["retrieved_sources"][0]
    assert envelope["payload"]["retrieved_sources"][0]["quoted_text"] == "duplicate excerpt"
    assert report["quality_guard_uses_full_context"] is True

    provider, provider_report = PromptExecutor._prepare_provider_envelope(compact)
    provider_ref = provider["payload"]["extracted_passages"][0]["source_ref"]
    assert "source_hash" not in provider_ref
    assert "quoted_text" not in provider["payload"]["retrieved_sources"][0]
    assert provider_report["removed_hash_fields"]["source_hash"] == 1
