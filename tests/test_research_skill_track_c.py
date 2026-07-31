from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.skills.base import SkillContext
from app.skills.public_research import PublicResearchArchiveError, PublicResearchSecurityError
from app.skills.research_audit import verify_research_archive
from app.skills.research_claims import validate_public_claims
from app.skills.verifiable_public_research import VerifiablePublicResearchArchiveSkill


def _settings(tmp_path: Path, connector_file: Path) -> SimpleNamespace:
    return SimpleNamespace(
        public_search_provider="connector",
        public_search_base_url="",
        public_research_record_file="",
        public_research_connector_file=str(connector_file),
        public_search_max_results=20,
        research_fetch_timeout_seconds=5,
        research_max_source_bytes=1024 * 1024,
    )


def _plan() -> dict:
    return {
        "plan_id": "plan-c-001",
        "task_type": "PUBLIC_RESEARCH",
        "research_questions": [
            "2021—2026年动态运输优化的最近工作、基线方法和局限机制是什么？",
            "官方标准如何规定可核验评价过程？",
        ],
        "binding_contract_version": "1.0",
        "queries": [
            {
                "query_id": "query-001",
                "query": "dynamic transportation optimization benchmark review limitations 2021 2026",
                "linked_question_indexes": [0],
            },
            {
                "query_id": "query-002",
                "query": "official evaluation standard reproducible evidence 2021 2026",
                "linked_question_indexes": [1],
            },
        ],
        "source_priorities": ["官方标准", "同行评议论文", "官方项目页面"],
        "time_scope": "2021-01-01/2026-12-31",
        "evidence_requirements": ["最近工作", "可比较基线", "局限机制"],
        "prohibited_inferences": ["不得推断内部项目"],
    }


def _query_texts(plan: dict) -> list[str]:
    return [item["query"] if isinstance(item, dict) else str(item) for item in plan["queries"]]


def _connector_file(tmp_path: Path) -> Path:
    plan = _plan()
    queries = _query_texts(plan)
    payload = {
        "run_id": "connector-run-c-001",
        "connector": "approved-test-connector",
        "created_at": "2026-07-15T00:00:00Z",
        "agent_generated_queries": queries,
        "responses": [
            {
                "query": queries[0],
                "retrieved_at": "2026-07-15T00:00:00Z",
                "results": [
                    {
                        "title": "Dynamic Transportation Optimization: Benchmark Review and Limitations",
                        "url": "https://doi.org/10.1000/example.dynamic?utm_source=test",
                        "doi": "10.1000/example.dynamic",
                        "authors": ["A. Author"],
                        "publisher": "Peer Reviewed Journal",
                        "published_at": "2025-03-01",
                        "source_type": "PEER_REVIEWED_PAPER",
                        "content_text": "This recent review compares benchmark baselines and explains limitations, open problems, and challenge mechanisms for dynamic transportation optimization.",
                    },
                    {
                        "title": "Dynamic Transportation Optimization: Benchmark Review and Limitations",
                        "url": "https://publisher.example/paper/123",
                        "doi": "https://doi.org/10.1000/example.dynamic",
                        "published_at": "2025",
                        "content_text": "Duplicate copy that must be removed by DOI normalization.",
                    },
                    {
                        "title": "Invalid local source",
                        "url": "http://127.0.0.1/private",
                        "published_at": "2025",
                        "content_text": "This source must be rejected and recorded as an issue.",
                    },
                ],
            },
            {
                "query": queries[1],
                "retrieved_at": "2026-07-15T00:00:00Z",
                "results": [
                    {
                        "title": "Official Evaluation Standard",
                        "url": "https://www.iso.org/standard/99999.html?utm_campaign=test",
                        "publisher": "International Organization for Standardization",
                        "published_at": "2024-01-01",
                        "source_type": "OFFICIAL_STANDARD",
                        "content_text": "The official standard defines a reproducible evaluation process, evidence retention, comparison baselines, and documented limitations.",
                    }
                ],
            },
        ],
    }
    path = tmp_path / "connector.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _run(tmp_path: Path):
    connector = _connector_file(tmp_path)
    skill = VerifiablePublicResearchArchiveSkill(_settings(tmp_path, connector))
    return skill.run(
        {
            "provider": "connector",
            "connector_file": str(connector),
            "require_structured_plan": True,
            "plan": _plan(),
            "max_results": 20,
        },
        SkillContext(project_id="project-c", workflow_id="wf-c", security_level="PUBLIC", data_dir=str(tmp_path)),
    )


def test_c1_rejects_broad_unbound_query_before_retrieval(tmp_path):
    connector = _connector_file(tmp_path)
    bad = _plan()
    bad["queries"] = ["latest research"]
    with pytest.raises(PublicResearchArchiveError, match="RESEARCH_PLAN_BROAD_QUERY"):
        VerifiablePublicResearchArchiveSkill(_settings(tmp_path, connector)).run(
            {"provider": "connector", "connector_file": str(connector), "require_structured_plan": True, "plan": bad},
            SkillContext(project_id="project-c", workflow_id="wf-c", security_level="PUBLIC", data_dir=str(tmp_path)),
        )


def test_c2_c3_archive_hashes_deduplicates_and_ranks_sources(tmp_path):
    result = _run(tmp_path)
    assert result.status == "PASS"
    assert result.output["plan_validation"]["status"] == "PASS"
    assert result.output["archive_verification"]["status"] == "PASS"
    assert len(result.output["source_catalog"]) == 2
    assert result.output["source_catalog"][0]["source_type"] == "OFFICIAL_STANDARD"
    assert result.output["source_catalog"][0]["authority_rank"] > result.output["source_catalog"][1]["authority_rank"]
    issue_types = {item["type"] for item in result.output["issues"]}
    assert "DUPLICATE_SOURCE" in issue_types
    assert "SOURCE_FETCH_FAILURE" in issue_types
    manifest = json.loads(Path(result.output["archive_manifest"]).read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "2.0"
    assert all(Path(record[key]).exists() for record in manifest["records"] for key in ("raw_path", "text_path", "metadata_path"))


def test_c5_recent_work_baseline_and_limitation_coverage(tmp_path):
    result = _run(tmp_path)
    coverage = result.output["coverage"]
    assert coverage["status"] == "PASS"
    assert coverage["dimensions"]["recent_work"]["status"] == "PASS"
    assert coverage["dimensions"]["comparable_baselines"]["status"] == "PASS"
    assert coverage["dimensions"]["limitation_mechanisms"]["status"] == "PASS"
    assert coverage["uncovered_queries"] == []


def test_c4_claim_binding_accepts_archived_hash_and_distinguishes_synthesis(tmp_path):
    result = _run(tmp_path)
    source = result.output["sources"][0]
    synthesis = {
        "claims": [{
            "claim_id": "public-claim-c-001",
            "claim_text": "该公开标准要求保留可复核评价证据。",
            "claim_type": "PUBLIC_CLAIM",
            "subject_id": None,
            "temporal_status": "CURRENT",
            "qualifiers": ["MODEL_SYNTHESIS"],
            "numeric_values": [],
            "source_refs": [source],
            "knowledge_status": "DOCUMENT_EXTRACTED",
            "security_level": "PUBLIC",
        }],
        "source_comparisons": [],
        "conflicts": [],
        "limitations": [],
        "coverage_summary": "覆盖完整",
    }
    report = validate_public_claims(synthesis, result.output)
    assert report["status"] == "PASS"
    assert report["bindings"][0]["evidence_mode"] == "DIRECT_SOURCE_SUPPORTED"


def test_c4_c6_claim_binding_blocks_unknown_source_and_forged_hash(tmp_path):
    result = _run(tmp_path)
    forged = dict(result.output["sources"][0])
    forged["source_id"] = "invented-source"
    forged["source_hash"] = "0" * 64
    synthesis = {
        "claims": [{
            "claim_id": "public-claim-c-002",
            "claim_text": "Invented claim",
            "claim_type": "PUBLIC_CLAIM",
            "subject_id": None,
            "temporal_status": "UNKNOWN",
            "qualifiers": [],
            "numeric_values": [],
            "source_refs": [forged],
            "knowledge_status": "UNKNOWN",
            "security_level": "PUBLIC",
        }],
        "source_comparisons": [],
        "conflicts": [],
        "limitations": [],
        "coverage_summary": "",
    }
    report = validate_public_claims(synthesis, result.output)
    assert report["status"] == "BLOCK"
    assert {item["code"] for item in report["findings"]} >= {"PUBLIC_CLAIM_UNKNOWN_SOURCE"}


def test_c6_restart_verification_detects_tampering(tmp_path):
    result = _run(tmp_path)
    manifest_path = Path(result.output["archive_manifest"])
    assert verify_research_archive(manifest_path)["status"] == "PASS"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    Path(manifest["records"][0]["text_path"]).write_text("tampered", encoding="utf-8")
    report = verify_research_archive(manifest_path)
    assert report["status"] == "FAIL"
    assert "ARCHIVE_HASH_MISMATCH" in {item["code"] for item in report["failures"]}


def test_c6_preserves_conflicting_duplicate_metadata(tmp_path):
    connector = _connector_file(tmp_path)
    payload = json.loads(connector.read_text(encoding="utf-8"))
    payload["responses"][0]["results"][1]["title"] = "Conflicting title for the same DOI"
    payload["responses"][0]["results"][1]["published_at"] = "2022"
    connector.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    result = VerifiablePublicResearchArchiveSkill(_settings(tmp_path, connector)).run(
        {
            "provider": "connector",
            "connector_file": str(connector),
            "require_structured_plan": True,
            "plan": _plan(),
            "max_results": 20,
        },
        SkillContext(project_id="project-c", workflow_id="wf-c", security_level="PUBLIC", data_dir=str(tmp_path)),
    )
    conflicts = [item for item in result.output["issues"] if item.get("type") == "SOURCE_CONFLICT"]
    assert conflicts
    assert set(conflicts[0]["conflict_fields"]) == {"title", "published_year"}
    synthesis = {
        "claims": [],
        "source_comparisons": [],
        "conflicts": [],
        "limitations": [],
        "coverage_summary": "存在待保留冲突",
    }
    report = validate_public_claims(synthesis, result.output)
    assert report["status"] == "BLOCK"
    assert "PUBLIC_SOURCE_CONFLICT_SUPPRESSED" in {item["code"] for item in report["findings"]}


def test_legacy_component_mode_warns_instead_of_blocking_broad_query(tmp_path):
    connector = _connector_file(tmp_path)
    payload = json.loads(connector.read_text(encoding="utf-8"))
    broad = "公开评价方法"
    payload["agent_generated_queries"] = [broad]
    payload["responses"] = [{
        "query": broad,
        "retrieved_at": "2026-07-15T00:00:00Z",
        "results": [payload["responses"][1]["results"][0]],
    }]
    connector.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    result = VerifiablePublicResearchArchiveSkill(_settings(tmp_path, connector)).run(
        {"provider": "connector", "connector_file": str(connector), "plan": {"queries": [broad]}},
        SkillContext(project_id="legacy-project", workflow_id="legacy-wf", security_level="PUBLIC", data_dir=str(tmp_path)),
    )
    assert result.status == "PASS"
    assert result.output["plan_validation"]["status"] == "WARN"
    assert any("RESEARCH_PLAN_BROAD_QUERY" in warning for warning in result.output["warnings"])


def test_c4_requires_snapshot_hash_for_known_source(tmp_path):
    result = _run(tmp_path)
    source = dict(result.output["sources"][0])
    source.pop("source_hash")
    synthesis = {
        "claims": [{
            "claim_id": "public-claim-no-hash",
            "claim_text": "公开来源结论",
            "claim_type": "PUBLIC_CLAIM",
            "subject_id": None,
            "temporal_status": "CURRENT",
            "qualifiers": [],
            "numeric_values": [],
            "source_refs": [source],
            "knowledge_status": "DOCUMENT_EXTRACTED",
            "security_level": "PUBLIC",
        }],
        "source_comparisons": [], "conflicts": [], "limitations": [], "coverage_summary": "覆盖完整",
    }
    report = validate_public_claims(synthesis, result.output)
    assert report["status"] == "BLOCK"
    assert "PUBLIC_CLAIM_HASH_MISSING" in {item["code"] for item in report["findings"]}


def test_c5_innovation_claim_requires_recent_baseline_and_limitation_evidence(tmp_path):
    result = _run(tmp_path)
    result.output["coverage"]["dimensions"]["limitation_mechanisms"]["status"] = "INSUFFICIENT"
    source = result.output["sources"][0]
    synthesis = {
        "claims": [{
            "claim_id": "innovation-claim-c",
            "claim_text": "形成可比较的创新机制。",
            "claim_type": "PUBLIC_CLAIM",
            "subject_id": "innovation-001",
            "temporal_status": "PLANNED",
            "qualifiers": ["INNOVATION_CLAIM"],
            "numeric_values": [],
            "source_refs": [source],
            "knowledge_status": "DOCUMENT_EXTRACTED",
            "security_level": "PUBLIC",
        }],
        "source_comparisons": [], "conflicts": [], "limitations": [], "coverage_summary": "局限证据不足",
    }
    report = validate_public_claims(synthesis, result.output)
    assert report["status"] == "BLOCK"
    assert "PUBLIC_INNOVATION_EVIDENCE_GAP" in {item["code"] for item in report["findings"]}


def test_cross_language_legacy_queries_are_not_rejected_by_token_overlap():
    from app.skills.research_plan import normalize_and_validate_plan

    plan = {
        "plan_id": "legacy-cross-language",
        "task_type": "PUBLIC_RESEARCH",
        "research_questions": [
            "公开研究中常用的评价方法、基线与局限是什么？",
            "如何建立可复核的公开证据链？",
        ],
        "queries": [
            "evaluation methods benchmark limitations systematic review",
            "verifiable public evidence provenance audit trail",
        ],
        "source_priorities": ["官方来源", "同行评议论文"],
        "time_scope": "2021-2026",
        "evidence_requirements": ["可核验来源"],
        "prohibited_inferences": ["不得推断内部项目"],
    }

    normalized, validation = normalize_and_validate_plan(plan, strict=True)

    assert validation["status"] == "WARN"
    assert "RESEARCH_PLAN_UNBOUND_QUERY" not in {
        item["code"] for item in validation["findings"]
    }
    assert all(item["binding_basis"] == "LEGACY_PLAN_SCOPE" for item in normalized["query_items"])


def test_explicit_cross_language_query_bindings_are_language_independent():
    from app.skills.research_plan import normalize_and_validate_plan

    plan = {
        "plan_id": "explicit-cross-language",
        "task_type": "PUBLIC_RESEARCH",
        "binding_contract_version": "1.0",
        "research_questions": ["公开评价方法有哪些？", "如何保留证据链？"],
        "queries": [
            {
                "query_id": "query-001",
                "query": "public evaluation methods benchmark review",
                "linked_question_indexes": [0],
            },
            {
                "query_id": "query-002",
                "query": "provenance evidence retention audit trail",
                "linked_question_indexes": [1],
            },
        ],
        "source_priorities": ["官方来源"],
        "time_scope": "2021-2026",
        "evidence_requirements": ["可核验来源"],
        "prohibited_inferences": ["不得推断内部项目"],
    }

    normalized, validation = normalize_and_validate_plan(plan, strict=True)

    assert validation["status"] == "PASS"
    assert [item["linked_question_indexes"] for item in normalized["query_items"]] == [[0], [1]]
    assert {item["binding_basis"] for item in normalized["query_items"]} == {"EXPLICIT"}


def test_new_binding_contract_rejects_missing_or_invalid_indexes():
    from app.skills.research_plan import normalize_and_validate_plan

    plan = {
        "plan_id": "bad-binding",
        "task_type": "PUBLIC_RESEARCH",
        "binding_contract_version": "1.0",
        "research_questions": ["公开评价方法有哪些？"],
        "queries": [
            {"query_id": "query-001", "query": "public evaluation benchmark", "linked_question_indexes": []},
            {"query_id": "query-002", "query": "evidence audit trail", "linked_question_indexes": [9]},
        ],
        "source_priorities": ["官方来源"],
        "time_scope": "2021-2026",
        "evidence_requirements": ["可核验来源"],
        "prohibited_inferences": ["不得推断内部项目"],
    }

    _, validation = normalize_and_validate_plan(plan, strict=True)
    codes = {item["code"] for item in validation["findings"]}

    assert validation["status"] == "BLOCK"
    assert "RESEARCH_PLAN_UNBOUND_QUERY" in codes
    assert "RESEARCH_PLAN_INVALID_QUERY_BINDING" in codes


def test_legacy_cross_language_plan_reaches_retrieval_without_model_regeneration(tmp_path):
    connector = _connector_file(tmp_path)
    legacy = _plan()
    legacy.pop("binding_contract_version", None)
    legacy["queries"] = _query_texts(legacy)

    result = VerifiablePublicResearchArchiveSkill(_settings(tmp_path, connector)).run(
        {
            "provider": "connector",
            "connector_file": str(connector),
            "require_structured_plan": True,
            "plan": legacy,
            "max_results": 20,
        },
        SkillContext(
            project_id="project-legacy-cross-language",
            workflow_id="wf-legacy-cross-language",
            security_level="PUBLIC",
            data_dir=str(tmp_path),
        ),
    )

    assert result.status == "PASS"
    assert result.output["plan_validation"]["status"] in {"PASS", "WARN"}
    assert "RESEARCH_PLAN_UNBOUND_QUERY" not in {
        item["code"] for item in result.output["plan_validation"]["findings"]
    }
    assert result.output["queries"] == legacy["queries"]


def test_explicit_binding_contract_rejects_unstable_ids_and_indexes():
    from app.skills.research_plan import normalize_and_validate_plan

    plan = {
        "plan_id": "unstable-binding",
        "task_type": "PUBLIC_RESEARCH",
        "binding_contract_version": "1.0",
        "research_questions": ["公开评价方法有哪些？", "公开评价方法有哪些？"],
        "queries": [
            {
                "query_id": "query-001",
                "query": "public evaluation benchmark review",
                "linked_question_indexes": [0],
            },
            {
                "query_id": "query-001",
                "query": "evidence provenance audit trail",
                "linked_question_indexes": [1.5],
            },
        ],
        "source_priorities": ["官方来源"],
        "time_scope": "2021-2026",
        "evidence_requirements": ["可核验来源"],
        "prohibited_inferences": ["不得推断内部项目"],
    }

    _, validation = normalize_and_validate_plan(plan, strict=True)
    codes = {item["code"] for item in validation["findings"]}

    assert validation["status"] == "BLOCK"
    assert "RESEARCH_PLAN_DUPLICATE_QUESTION" in codes
    assert "RESEARCH_PLAN_DUPLICATE_QUERY_ID" in codes
    assert "RESEARCH_PLAN_INVALID_QUERY_BINDING" in codes


def test_public_research_facade_preserves_typed_category_across_nested_causes() -> None:
    from app.research import (
        PublicResearchConfigurationError as FacadeConfigurationError,
        _facade_error,
    )
    from app.skills.executor import SkillExecutionError
    from app.skills.public_research import PublicResearchConfigurationError

    try:
        try:
            raise ValueError("invalid JSON")
        except ValueError as low_level:
            raise PublicResearchConfigurationError(
                "Connector research file is not readable JSON",
                details={"path": "connector.json"},
            ) from low_level
    except PublicResearchConfigurationError as typed:
        try:
            raise SkillExecutionError("public_research.archive failed") from typed
        except SkillExecutionError as boundary:
            mapped = _facade_error(boundary)

    assert isinstance(mapped, FacadeConfigurationError)
    assert mapped.category == "CONFIGURATION"
    assert mapped.details == {"path": "connector.json"}


def test_all_security_rejected_candidates_remain_security_failures(tmp_path: Path) -> None:
    query = "public benchmark"
    connector = tmp_path / "private-only-connector.json"
    connector.write_text(
        json.dumps(
            {
                "run_id": "connector-private-only",
                "connector": "approved-test-connector",
                "responses": [
                    {
                        "query": query,
                        "results": [
                            {
                                "title": "Local-only source",
                                "url": "http://127.0.0.1/private",
                                "content_text": "This must never enter the public archive.",
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(PublicResearchSecurityError) as caught:
        VerifiablePublicResearchArchiveSkill(_settings(tmp_path, connector)).run(
            {
                "provider": "connector",
                "connector_file": str(connector),
                "plan": {"queries": [query]},
            },
            SkillContext(
                project_id="project-private-only",
                workflow_id="wf-private-only",
                security_level="PUBLIC",
                data_dir=str(tmp_path),
            ),
        )

    assert caught.value.category == "SECURITY"
    assert caught.value.details["candidate_failures"][0]["category"] == "SECURITY"
