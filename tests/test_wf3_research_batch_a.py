from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.skills.base import SkillContext
from app.skills.public_research import PublicResearchPlanContractError
from app.skills.research_audit import coverage_report
from app.skills.research_execution import (
    ResearchExecutionContractError,
    build_plan_lock,
    validate_plan_transition,
)
from app.skills.research_plan import deduplicate_candidates, normalize_and_validate_plan
from app.skills.research_screening import screen_and_select_candidates
from app.skills.verifiable_public_research import VerifiablePublicResearchArchiveSkill


def _plan(query_count: int = 2) -> dict:
    questions = [f"研究问题{index + 1}：现有工作、基线和局限是什么？" for index in range(query_count)]
    queries = [
        {
            "query_id": f"query-{index + 1:03d}",
            "query": f"adaptive transport scheduling evidence topic {index + 1}",
            "linked_question_indexes": [index],
        }
        for index in range(query_count)
    ]
    return {
        "plan_id": "plan-wf3-batch-a",
        "task_type": "PUBLIC_RESEARCH",
        "binding_contract_version": "1.0",
        "research_questions": questions,
        "queries": queries,
        "source_priorities": ["同行评议论文", "官方来源"],
        "time_scope": "2021-01-01/2025-12-31",
        "evidence_requirements": ["最近工作", "可比较基线", "局限机制"],
        "prohibited_inferences": ["不得推断未公开事实"],
    }


def _settings(tmp_path: Path, connector: Path) -> SimpleNamespace:
    return SimpleNamespace(
        public_search_provider="connector",
        public_search_base_url="",
        public_search_engines="",
        public_research_record_file="",
        public_research_connector_file=str(connector),
        public_search_max_results=14,
        research_fetch_timeout_seconds=5,
        research_max_source_bytes=1024 * 1024,
    )


def test_plan_lock_is_stable_across_deterministic_normalization() -> None:
    plan = _plan()
    normalized, _ = normalize_and_validate_plan(plan, strict=True)
    assert build_plan_lock(plan)["plan_hash"] == build_plan_lock(normalized)["plan_hash"]


def test_plan_lock_rejects_replacing_an_approved_query() -> None:
    plan = _plan()
    lock = build_plan_lock(plan)
    changed = json.loads(json.dumps(plan, ensure_ascii=False))
    changed["queries"][0]["query"] = "completely replaced search task"
    with pytest.raises(ResearchExecutionContractError) as caught:
        validate_plan_transition(lock, changed, allow_additive=True)
    assert caught.value.code == "RESEARCH_PLAN_DESTRUCTIVE_DELTA"
    assert caught.value.details["changed_query_ids"] == ["query-001"]


def test_plan_lock_allows_only_additive_follow_up_queries() -> None:
    plan = _plan(1)
    lock = build_plan_lock(plan)
    expanded = json.loads(json.dumps(plan, ensure_ascii=False))
    expanded["research_questions"].append("研究问题2：补充近邻工作的适用边界是什么？")
    expanded["queries"].append(
        {
            "query_id": "query-002",
            "query": "closest prior work applicability boundary transport scheduling",
            "linked_question_indexes": [1],
        }
    )
    next_lock = validate_plan_transition(lock, expanded, allow_additive=True)
    assert next_lock["plan_hash"] != lock["plan_hash"]
    assert len(next_lock["projection"]["query_items"]) == 2


def test_strict_connector_missing_approved_query_is_plan_contract_failure(tmp_path: Path) -> None:
    plan = _plan(2)
    first = plan["queries"][0]
    connector = tmp_path / "missing-query.json"
    connector.write_text(
        json.dumps(
            {
                "run_id": "connector-missing",
                "connector": "test",
                "agent_generated_queries": [first["query"]],
                "responses": [
                    {
                        "query_id": first["query_id"],
                        "query": first["query"],
                        "results": [
                            {
                                "title": "Adaptive transport scheduling benchmark review",
                                "url": "https://doi.org/10.1000/wf3.1",
                                "doi": "10.1000/wf3.1",
                                "published_at": "2024",
                                "authors": ["A. Author"],
                                "publisher": "Journal A",
                                "content_text": "Adaptive transport scheduling benchmark review limitations evidence.",
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with pytest.raises(PublicResearchPlanContractError) as caught:
        VerifiablePublicResearchArchiveSkill(_settings(tmp_path, connector)).run(
            {
                "provider": "connector",
                "connector_file": str(connector),
                "require_structured_plan": True,
                "plan": plan,
            },
            SkillContext(project_id="p", workflow_id="wf", security_level="PUBLIC", data_dir=str(tmp_path)),
        )
    assert caught.value.details["code"] == "RESEARCH_EXECUTION_MISSING_QUERY"


def test_strict_connector_rejects_query_id_text_mutation(tmp_path: Path) -> None:
    plan = _plan(1)
    approved = plan["queries"][0]
    connector = tmp_path / "mutated-query.json"
    connector.write_text(
        json.dumps(
            {
                "run_id": "connector-mutated",
                "connector": "test",
                "agent_generated_queries": [approved["query"]],
                "responses": [
                    {
                        "query_id": approved["query_id"],
                        "query": "binary neutron star merger observations",
                        "results": [],
                    },
                    {"query": approved["query"], "results": []},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with pytest.raises(PublicResearchPlanContractError) as caught:
        VerifiablePublicResearchArchiveSkill(_settings(tmp_path, connector)).run(
            {
                "provider": "connector",
                "connector_file": str(connector),
                "require_structured_plan": True,
                "plan": plan,
            },
            SkillContext(project_id="p", workflow_id="wf", security_level="PUBLIC", data_dir=str(tmp_path)),
        )
    findings = caught.value.details["findings"]
    assert {item["code"] for item in findings} >= {
        "RESEARCH_EXECUTION_UNAPPROVED_QUERY",
        "RESEARCH_EXECUTION_QUERY_ID_MISMATCH",
    }


def test_screening_rejects_retracted_out_of_scope_and_clear_off_topic_candidates() -> None:
    plan = _plan(1)
    plan["queries"][0]["query"] = "human AI collaboration adaptive transport scheduling"
    normalized, validation = normalize_and_validate_plan(plan, strict=True)
    assert validation["status"] == "PASS"
    query = normalized["queries"][0]
    candidates = [
        {
            "title": "Human AI collaboration for adaptive transport scheduling",
            "url": "https://doi.org/10.1000/relevant",
            "doi": "10.1000/relevant",
            "published_at": "2024",
            "matched_query": query,
            "abstract": "Human AI collaboration supports adaptive transport scheduling under uncertain demand.",
        },
        {
            "title": "Retracted: Human AI collaboration for transport scheduling",
            "url": "https://doi.org/10.1000/retracted",
            "published_at": "2024",
            "matched_query": query,
            "is_retracted": True,
            "abstract": "Human AI collaboration adaptive transport scheduling.",
        },
        {
            "title": "Human AI collaboration for adaptive transport scheduling",
            "url": "https://doi.org/10.1000/future",
            "published_at": "2026",
            "matched_query": query,
            "abstract": "Human AI collaboration adaptive transport scheduling.",
        },
        {
            "title": "Binary neutron star merger gravitational waveform observations",
            "url": "https://doi.org/10.1000/offtopic",
            "published_at": "2024",
            "matched_query": query,
            "abstract": "Binary neutron star mass ejecta gravitational waveform telescope observations astrophysical compact objects.",
        },
    ]
    selected, report = screen_and_select_candidates(
        candidates,
        normalized,
        max_results=4,
        strict=True,
        min_per_query=1,
    )
    assert [item["doi"] for item in selected] == ["10.1000/relevant"]
    rejected = [item for item in report["issues"] if item.get("code") == "CANDIDATE_REJECTED"]
    reasons = {reason for item in rejected for reason in item.get("reason_codes") or []}
    assert {"RETRACTED_OR_WITHDRAWN", "OUTSIDE_TIME_SCOPE", "CLEAR_LEXICAL_OFF_TOPIC"} <= reasons


def test_deduplication_merges_query_and_provider_provenance() -> None:
    candidates = [
        {
            "title": "Same work",
            "url": "https://doi.org/10.1000/same",
            "doi": "10.1000/same",
            "matched_query": "query A",
            "academic_provider": "openalex",
            "authors": ["A"],
        },
        {
            "title": "Same work",
            "url": "https://publisher.example/same",
            "doi": "10.1000/same",
            "matched_query": "query B",
            "academic_provider": "crossref",
            "authors": ["A", "B"],
        },
    ]
    kept, duplicates = deduplicate_candidates(candidates)
    assert len(kept) == 1 and len(duplicates) == 1
    assert kept[0]["matched_queries"] == ["query A", "query B"]
    assert kept[0]["discovery_providers"] == ["openalex", "crossref"]
    assert kept[0]["authors"] == ["A", "B"]


def _coverage_record(index: int, query: str, *, provider: str, publisher: str, category: str = "PEER_REVIEWED_PAPER") -> dict:
    return {
        "source_id": f"src-{index:03d}",
        "title": "Systematic review of transport scheduling baselines and limitations" if index == 1 else f"Transport scheduling method {index}",
        "excerpt": "benchmark comparison limitations challenges" if index <= 3 else "recent transport scheduling evidence",
        "source_category": category,
        "authority_rank": 90 if category == "PEER_REVIEWED_PAPER" else 78,
        "publisher": publisher,
        "authors": [f"Author {index}", f"Coauthor {index}"],
        "is_recent": True,
        "supports_baseline": index <= 3,
        "supports_limitation": index <= 3,
        "matched_query": query,
        "verification": {
            "matched_queries": [query],
            "discovery_provider": provider,
            "discovery_providers": [provider],
        },
    }


def test_strict_coverage_rejects_ten_single_channel_seed_sources() -> None:
    plan = _plan(5)
    normalized, _ = normalize_and_validate_plan(plan, strict=True)
    records = []
    index = 0
    for query in normalized["queries"]:
        for _ in range(2):
            index += 1
            records.append(
                _coverage_record(
                    index,
                    query,
                    provider="openalex",
                    publisher="arXiv",
                    category="ACADEMIC_REPOSITORY",
                )
            )
    report = coverage_report(records, normalized, quality_profile="proposal_related_work")
    assert report["status"] == "INSUFFICIENT"
    assert report["dimensions"]["query_depth"]["status"] == "INSUFFICIENT"
    assert report["dimensions"]["source_volume"]["minimum_source_count"] == 15
    assert report["dimensions"]["discovery_provider_diversity"]["status"] == "INSUFFICIENT"


def test_strict_coverage_accepts_diverse_three_per_query_evidence_set() -> None:
    plan = _plan(4)
    normalized, _ = normalize_and_validate_plan(plan, strict=True)
    providers = ("openalex", "crossref", "semantic_scholar")
    publishers = ("Journal A", "Journal B", "Conference C", "Journal D")
    records = []
    index = 0
    for query_index, query in enumerate(normalized["queries"]):
        for provider_index, provider in enumerate(providers):
            index += 1
            records.append(
                _coverage_record(
                    index,
                    query,
                    provider=provider,
                    publisher=publishers[(query_index + provider_index) % len(publishers)],
                )
            )
    report = coverage_report(records, normalized, quality_profile="proposal_related_work")
    assert report["status"] == "PASS"
    assert report["shallow_queries"] == []
    assert all(item["status"] == "PASS" for item in report["dimensions"].values())


def test_academic_discovery_aggregates_multiple_providers_without_network(monkeypatch) -> None:
    from app.skills.academic_search import AcademicSearchClient

    client = AcademicSearchClient(SimpleNamespace(research_fetch_timeout_seconds=5))

    def result(provider: str):
        return lambda query, limit, time_scope: ([{
            "title": f"{provider} result",
            "url": f"https://example.org/{provider}",
            "matched_query": query,
            "verification": {"discovery_provider": provider},
        }], {"provider": provider, "query": query})

    monkeypatch.setattr(client, "search_openalex", result("openalex"))
    monkeypatch.setattr(client, "search_crossref", result("crossref"))
    monkeypatch.setattr(client, "search_semantic_scholar", result("semantic_scholar"))
    output = client.discover(
        ["adaptive transport scheduling"],
        time_scope="2021-2025",
        per_query_limit=5,
    )
    assert output["providers"] == ["openalex", "crossref", "semantic_scholar"]
    assert [item["title"] for item in output["responses"][0]["results"]] == [
        "openalex result",
        "crossref result",
        "semantic_scholar result",
    ]
    assert [item["provider"] for item in output["provider_runs"]] == [
        "openalex", "crossref", "semantic_scholar"
    ]


def test_openalex_adapter_preserves_retraction_metadata_and_abstract(monkeypatch) -> None:
    from app.skills.academic_search import AcademicSearchClient

    client = AcademicSearchClient(SimpleNamespace(research_fetch_timeout_seconds=5))
    monkeypatch.setattr(
        client,
        "_get_json",
        lambda *args, **kwargs: {
            "results": [{
                "id": "https://openalex.org/W1",
                "display_name": "Adaptive scheduling evidence",
                "doi": "https://doi.org/10.1000/openalex.1",
                "publication_date": "2024-03-02",
                "cited_by_count": 17,
                "is_retracted": True,
                "abstract_inverted_index": {"Adaptive": [0], "scheduling": [1], "evidence": [2]},
                "authorships": [{"author": {"display_name": "A. Author"}}],
                "primary_location": {
                    "landing_page_url": "https://publisher.example/work",
                    "source": {"display_name": "Journal X", "type": "journal"},
                },
            }]
        },
    )
    values, _ = client.search_openalex("adaptive scheduling", 5, "2021-2025")
    assert len(values) == 1
    assert values[0]["doi"] == "10.1000/openalex.1"
    assert values[0]["authors"] == ["A. Author"]
    assert values[0]["citation_count"] == 17
    assert values[0]["is_retracted"] is True
    assert values[0]["abstract"] == "Adaptive scheduling evidence"


def test_strict_related_work_profile_blocks_shallow_connector_before_synthesis(tmp_path: Path) -> None:
    from app.skills.public_research import PublicResearchRetrievalError

    plan = _plan(2)
    responses = []
    for index, item in enumerate(plan["queries"], 1):
        responses.append(
            {
                "query_id": item["query_id"],
                "query": item["query"],
                "results": [{
                    "title": f"Adaptive transport scheduling benchmark review {index}",
                    "url": f"https://doi.org/10.1000/shallow.{index}",
                    "doi": f"10.1000/shallow.{index}",
                    "published_at": "2024",
                    "authors": [f"Author {index}"],
                    "publisher": f"Journal {index}",
                    "content_text": "Recent benchmark comparison review limitations challenges for adaptive transport scheduling evidence.",
                }],
            }
        )
    connector = tmp_path / "shallow.json"
    connector.write_text(
        json.dumps(
            {
                "run_id": "connector-shallow",
                "connector": "test",
                "agent_generated_queries": [item["query"] for item in plan["queries"]],
                "responses": responses,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with pytest.raises(PublicResearchRetrievalError) as caught:
        VerifiablePublicResearchArchiveSkill(_settings(tmp_path, connector)).run(
            {
                "provider": "connector",
                "connector_file": str(connector),
                "require_structured_plan": True,
                "research_quality_profile": "proposal_related_work",
                "plan": plan,
                "max_results": 14,
            },
            SkillContext(project_id="p", workflow_id="wf", security_level="PUBLIC", data_dir=str(tmp_path)),
        )
    assert caught.value.details["coverage"]["status"] == "INSUFFICIENT"
    assert caught.value.details["coverage"]["dimensions"]["query_depth"]["status"] == "INSUFFICIENT"
    assert Path(caught.value.details["archive_manifest"]).exists()


def test_validation_bundle_is_persisted_before_insufficient_gate(tmp_path: Path) -> None:
    from app.skills.public_research import PublicResearchRetrievalError

    plan = _plan(2)
    responses = []
    for index, item in enumerate(plan["queries"], 1):
        responses.append(
            {
                "query_id": item["query_id"],
                "query": item["query"],
                "results": [{
                    "title": f"Adaptive transport scheduling benchmark review {index}",
                    "url": f"https://doi.org/10.1000/validation.{index}",
                    "doi": f"10.1000/validation.{index}",
                    "published_at": "2024",
                    "authors": [f"Author {index}"],
                    "publisher": f"Journal {index}",
                    "content_text": "Recent benchmark comparison review limitations challenges for adaptive transport scheduling evidence.",
                }],
            }
        )
    connector = tmp_path / "validation-shallow.json"
    connector.write_text(
        json.dumps(
            {
                "run_id": "connector-validation-shallow",
                "connector": "test",
                "agent_generated_queries": [item["query"] for item in plan["queries"]],
                "responses": responses,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(PublicResearchRetrievalError) as caught:
        VerifiablePublicResearchArchiveSkill(_settings(tmp_path, connector)).run(
            {
                "provider": "connector",
                "connector_file": str(connector),
                "require_structured_plan": True,
                "research_quality_profile": "proposal_related_work",
                "plan": plan,
                "max_results": 14,
            },
            SkillContext(project_id="p", workflow_id="wf-validation", security_level="PUBLIC", data_dir=str(tmp_path)),
        )

    root = Path(caught.value.details["validation_bundle_dir"])
    assert root.parent.parent == tmp_path / "wf3_validation"
    expected = [
        "00_run_manifest.json",
        "01_input_plan.json",
        "02_normalized_plan.json",
        "03_execution_report.json",
        "04_discovery_manifest.json",
        "05_selection_report.json",
        "06_source_catalog.json",
        "07_coverage.json",
        "08_quality_summary.json",
    ]
    assert all((root / name).exists() for name in expected)
    summary = json.loads((root / "08_quality_summary.json").read_text(encoding="utf-8"))
    assert summary["execution"]["status"] == "PASS"
    assert summary["candidate_funnel"]["selected_candidates"] == 2
    assert summary["candidate_funnel"]["archived_sources"] == 1
    assert summary["coverage"]["status"] == "INSUFFICIENT"
    assert "query_depth" in summary["coverage"]["failing_dimensions"]


def test_synthesis_and_claim_validation_are_appended_to_validation_bundle(tmp_path: Path) -> None:
    from app.research import PublicResearchService

    root = tmp_path / "wf3_validation" / "wf" / "research-1"
    root.mkdir(parents=True)
    source_ref = {
        "source_id": "src-1",
        "source_type": "PUBLIC_SOURCE",
        "document_version_id": None,
        "section_id": None,
        "span_start": None,
        "span_end": None,
        "quoted_text": "Evidence title",
        "source_hash": "a" * 64,
        "authority_rank": 90,
        "security_level": "PUBLIC",
    }
    research_output = {
        "mode": "LIVE_ACADEMIC_MULTI_SOURCE",
        "validation_bundle_dir": str(root),
        "source_catalog": [{
            "source_id": "src-1",
            "title": "Evidence title",
            "excerpt": "Evidence title supports the claim.",
            "snapshot_sha256": "a" * 64,
        }],
        "coverage": {
            "dimensions": {
                "recent_work": {"status": "PASS"},
                "comparable_baselines": {"status": "PASS"},
                "limitation_mechanisms": {"status": "PASS"},
            }
        },
        "issues": [],
    }
    synthesis = {
        "claims": [{
            "claim_id": "claim-1",
            "claim_text": "Evidence-supported conclusion",
            "claim_type": "PUBLIC_CLAIM",
            "subject_id": None,
            "temporal_status": "CURRENT",
            "qualifiers": [],
            "numeric_values": [],
            "source_refs": [source_ref],
            "knowledge_status": "DOCUMENT_EXTRACTED",
            "security_level": "PUBLIC",
        }],
        "source_comparisons": [],
        "conflicts": [],
        "limitations": [],
        "coverage_summary": "covered",
    }
    report = PublicResearchService(SimpleNamespace()).validate_synthesis(synthesis, research_output)
    assert report["status"] == "PASS"
    assert json.loads((root / "09_synthesis.json").read_text(encoding="utf-8"))["claims"][0]["claim_id"] == "claim-1"
    claim_report = json.loads((root / "10_claim_validation.json").read_text(encoding="utf-8"))
    assert claim_report["status"] == "PASS"
    assert claim_report["bindings"][0]["claim_id"] == "claim-1"
