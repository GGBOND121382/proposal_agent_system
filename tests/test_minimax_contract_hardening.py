from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.contracts.semantic_contract import (
    annotate_schema_reference_semantics,
    assert_schema_reference_coverage,
)
from app.db import Database
from app.executor import PromptExecutionError, PromptExecutor
from app.llm import ModelGateway
from app.output_integrity import bind_trusted_source_refs
from app.pack import PromptPack
from app.security import SecurityRouter


@pytest.fixture()
def hardening_runtime(tmp_path: Path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("MODEL_RUNTIME_MODE", "SIMULATED")
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PROMPT_PACK_DIR", str(root / "prompt_pack"))
    settings = Settings.load()
    pack = PromptPack(settings.prompt_pack_dir)
    db = Database(settings.db_path)
    router = SecurityRouter(pack)
    gateway = ModelGateway(settings, pack)
    executor = PromptExecutor(db, pack, router, gateway)
    return pack, executor


def test_argument_revise_with_blocking_user_question_routes_to_need_user_input(hardening_runtime):
    pack, executor = hardening_runtime
    output = pack.replay_output("P-ARGUMENT-ARCHITECTURE", "normal")
    output["status"] = "REVISE"
    output["user_questions"] = [{
        "question_id": "UQ-HARDEN-001",
        "question_type": "MISSING_INFORMATION",
        "question": "请提供团队前期研究基础的可核验来源。",
        "reason": "缺少必须由项目负责人提供的团队基础证据。",
        "target_paths": ["/payload/confirmed_facts"],
        "answer_schema": {"type": "ARRAY", "allowed_values": []},
        "blocking": True,
        "priority": "P0",
    }]

    normalized = executor._normalize_output(
        "P-ARGUMENT-ARCHITECTURE",
        output,
        pack.replay_input("P-ARGUMENT-ARCHITECTURE"),
    )

    assert normalized["status"] == "NEED_USER_INPUT"
    assert normalized["user_questions"][0]["answer_schema"] == {"type": "STRING"}


def test_blocking_user_routed_finding_without_question_is_rejected(hardening_runtime):
    pack, executor = hardening_runtime
    output = pack.replay_output("P-ARGUMENT-ARCHITECTURE", "normal")
    output["status"] = "REVISE"
    output["user_questions"] = []
    output["findings"] = [{
        "finding_instance_id": "F-HARDEN-001",
        "code": "FOUNDATION_EVIDENCE_MISSING",
        "severity": "P0",
        "category": "ARGUMENT",
        "target_type": "ARGUMENT_NODE",
        "target_path_or_span": "/result/argument_architecture/nodes/0",
        "description": "团队研究基础缺少可核验来源。",
        "evidence_refs": [],
        "repairable": False,
        "repair_instruction": "由项目负责人提供论文、专利、项目或预实验来源。",
        "suggested_route": "USER",
        "blocking": True,
    }]

    with pytest.raises(PromptExecutionError, match="human-gate contract") as exc_info:
        executor._normalize_output(
            "P-ARGUMENT-ARCHITECTURE",
            output,
            pack.replay_input("P-ARGUMENT-ARCHITECTURE"),
        )

    assert any(
        "requires at least one blocking" in error
        and "runtime will not invent that question" in error
        for error in exc_info.value.validation_errors
    )


def test_blocking_user_finding_with_question_routes_to_need_user_input(hardening_runtime):
    pack, executor = hardening_runtime
    output = pack.replay_output("P-ARGUMENT-ARCHITECTURE", "normal")
    output["status"] = "REVISE"
    output["findings"] = [{
        "finding_instance_id": "F-HARDEN-002",
        "code": "FOUNDATION_EVIDENCE_MISSING",
        "severity": "P0",
        "category": "ARGUMENT",
        "target_type": "ARGUMENT_NODE",
        "target_path_or_span": "/result/argument_architecture/nodes/0",
        "description": "团队研究基础缺少可核验来源。",
        "evidence_refs": [],
        "repairable": False,
        "repair_instruction": "由项目负责人提供论文、专利、项目或预实验来源。",
        "suggested_route": "USER",
        "blocking": True,
    }]
    output["user_questions"] = [{
        "question_id": "UQ-HARDEN-002",
        "question_type": "MISSING_INFORMATION",
        "question": "请提供团队前期研究基础的可核验来源。",
        "reason": "缺少必须由项目负责人提供的团队基础证据。",
        "target_paths": ["/payload/confirmed_facts"],
        "answer_schema": {"type": "ARRAY", "allowed_values": []},
        "blocking": True,
        "priority": "P0",
    }]

    normalized = executor._normalize_output(
        "P-ARGUMENT-ARCHITECTURE",
        output,
        pack.replay_input("P-ARGUMENT-ARCHITECTURE"),
    )

    assert normalized["status"] == "NEED_USER_INPUT"


def test_argument_dangling_design_id_error_explains_node_definition_location(hardening_runtime):
    pack, executor = hardening_runtime
    output = pack.replay_output("P-ARGUMENT-ARCHITECTURE", "normal")
    envelope = pack.replay_input("P-ARGUMENT-ARCHITECTURE")
    output["result"]["research_design_matrix"][0]["method_ids"] = ["PRD-METHOD-001"]

    with pytest.raises(PromptExecutionError) as exc_info:
        executor._normalize_output("P-ARGUMENT-ARCHITECTURE", output, envelope)

    assert any(
        "PRD-METHOD-001" in error
        and "/result/argument_architecture/nodes" in error
        for error in exc_info.value.validation_errors
    )


def test_argument_scalar_matrix_reference_cannot_authorize_itself(hardening_runtime):
    pack, executor = hardening_runtime
    output = pack.replay_output("P-ARGUMENT-ARCHITECTURE", "normal")
    envelope = pack.replay_input("P-ARGUMENT-ARCHITECTURE")
    output["result"]["research_design_matrix"][0]["research_question_id"] = "FAKE-RQ-999"

    with pytest.raises(PromptExecutionError) as exc_info:
        executor._normalize_output("P-ARGUMENT-ARCHITECTURE", output, envelope)

    assert any(
        "/result/research_design_matrix/0/research_question_id" in error
        and "FAKE-RQ-999" in error
        for error in exc_info.value.validation_errors
    )


def test_known_source_with_alias_section_rebinds_by_exact_hash():
    trusted_hash = "f" * 64
    envelope = {
        "security_context": {"input_max_security_level": "INTERNAL"},
        "payload": {
            "sources": [
                {
                    "source_id": "doc-001",
                    "source_type": "CURRENT_PROPOSAL",
                    "document_version_id": "docv-001",
                    "section_id": "sec-canonical",
                    "source_hash": trusted_hash,
                    "authority_rank": 85,
                    "security_level": "INTERNAL",
                },
                {
                    "source_id": "doc-001",
                    "source_type": "CURRENT_PROPOSAL",
                    "document_version_id": "docv-001",
                    "section_id": "sec-other",
                    "source_hash": "e" * 64,
                    "authority_rank": 85,
                    "security_level": "INTERNAL",
                },
                {
                    "source_id": "sec-alias",
                    "source_type": "CURRENT_PROPOSAL",
                    "document_version_id": "docv-001",
                    "section_id": "sec-alias",
                    "source_hash": trusted_hash,
                    "authority_rank": 85,
                    "security_level": "INTERNAL",
                },
            ]
        },
    }
    output = {
        "source_refs": [{
            "source_id": "doc-001",
            "section_id": "sec-alias",
            "source_hash": trusted_hash,
        }]
    }

    normalized, report = bind_trusted_source_refs(output, envelope)

    assert report["errors"] == []
    assert normalized["source_refs"][0]["source_id"] == "doc-001"
    assert normalized["source_refs"][0]["section_id"] == "sec-canonical"
    assert report["changes"][0]["alias_kind"] == "SOURCE_HASH_SECTION"



def test_singular_source_ref_uses_the_same_trusted_binder():
    trusted_hash = "a" * 64
    envelope = {
        "security_context": {"input_max_security_level": "INTERNAL"},
        "payload": {
            "sources": [{
                "source_id": "doc-002",
                "source_type": "GUIDE",
                "document_version_id": "docv-002",
                "section_id": "sec-002",
                "source_hash": trusted_hash,
                "authority_rank": 100,
                "security_level": "INTERNAL",
            }]
        },
    }
    output = {
        "source_ref": {
            "source_id": "doc-002",
            "source_type": "MODEL_INFERENCE",
            "document_version_id": "fake-version",
            "section_id": "sec-002",
            "source_hash": "b" * 64,
            "authority_rank": 1,
            "security_level": "PUBLIC",
        }
    }

    normalized, report = bind_trusted_source_refs(output, envelope)

    assert report["errors"] == []
    assert report["normalized_count"] == 1
    assert normalized["source_ref"]["source_id"] == "doc-002"
    assert normalized["source_ref"]["document_version_id"] == "docv-002"
    assert normalized["source_ref"]["source_hash"] == trusted_hash
    assert normalized["source_ref"]["authority_rank"] == 100
    assert normalized["source_ref"]["security_level"] == "INTERNAL"


def test_project_relation_scalar_reference_cannot_authorize_itself(hardening_runtime):
    pack, executor = hardening_runtime
    output = pack.replay_output("P-PROJECT-DEFINITION-EXTRACT", "normal")
    envelope = pack.replay_input("P-PROJECT-DEFINITION-EXTRACT")
    output["result"]["project_definition"]["relations"] = [{
        "relation_id": "REL-HARDEN-001",
        "source_item_id": "FAKE-ITEM-999",
        "source_item_type": "OBJECTIVE",
        "relation_type": "SUPPORTS",
        "target_item_id": "item-001",
        "target_item_type": "OBJECTIVE",
        "status": "CANDIDATE",
        "confidence": "MEDIUM",
        "source_refs": [],
        "security_level": "INTERNAL",
        "relation_hash": "c" * 64,
    }]

    with pytest.raises(PromptExecutionError) as exc_info:
        executor._normalize_output("P-PROJECT-DEFINITION-EXTRACT", output, envelope)

    assert any(
        "/result/project_definition/relations/0/source_item_id" in error
        and "FAKE-ITEM-999" in error
        for error in exc_info.value.validation_errors
    )


def test_write_content_summary_paragraph_is_a_reference_not_a_definition(hardening_runtime):
    pack, executor = hardening_runtime
    output = pack.replay_output("P-WRITE-CONTENT", "normal")
    envelope = pack.replay_input("P-WRITE-CONTENT")
    output["result"]["source_preservation_summary"][0]["paragraph_id"] = "FAKE-P-999"

    with pytest.raises(PromptExecutionError) as exc_info:
        executor._normalize_output("P-WRITE-CONTENT", output, envelope)

    assert any(
        "/result/source_preservation_summary/0/paragraph_id" in error
        and "FAKE-P-999" in error
        for error in exc_info.value.validation_errors
    )


def test_path_aware_schema_annotation_audit_accepts_overrides(hardening_runtime):
    pack, _ = hardening_runtime
    schema = annotate_schema_reference_semantics(
        pack.inlined_schema("P-ARGUMENT-ARCHITECTURE", "output")
    )

    assert_schema_reference_coverage([schema])


def test_planning_profile_declares_task_budget_while_model_owns_hard_limits(hardening_runtime):
    pack, _ = hardening_runtime
    profile = pack.model_profile("P-ARGUMENT-ARCHITECTURE")
    capability = pack.model_capability("MiniMax-M3")
    endpoint = next(
        item
        for item in pack.endpoints["endpoints"]
        if item["endpoint_id"] == "offline-primary"
    )

    assert profile["desired_output_tokens"] == 131072
    assert capability["context_window_tokens"] == 1_000_000
    assert capability["recommended_output_tokens"] == 131_072
    assert capability["hard_max_output_tokens"] == 524_288
    assert capability["output_parameter"] == "max_completion_tokens"
    assert "max_input_tokens" not in endpoint["limits"]
    assert "max_output_tokens" not in endpoint["limits"]


def test_argument_prompt_keeps_business_boundary_without_duplicating_runtime_validators(
    hardening_runtime,
):
    pack, executor = hardening_runtime
    prompt_id = "P-ARGUMENT-ARCHITECTURE"
    prompt = pack.prompt_text(prompt_id)
    shared = pack.shared_prompt
    envelope = pack.replay_input(prompt_id)
    system_prompt = executor._system_prompt(prompt_id, pack.inlined_schema(prompt_id, "output"), envelope)

    forbidden_role_template = "不得替代其他智能体完成事实确认、论证架构、章节规划、证据写作、表达编辑或全篇评价"
    assert forbidden_role_template not in prompt
    for candidate_prompt_id in pack.prompt_ids():
        assert forbidden_role_template not in pack.prompt_text(candidate_prompt_id)

    # The model sees a research-design task, not runtime validation code.
    assert "科研论证架构设计" in prompt
    assert "现有差距及其限制机制 → 研究问题 → 研究目标" in prompt
    assert "语义任务通则" in system_prompt
    assert "result.argument_architecture.nodes[]" not in prompt
    assert "user_questions[*].blocking=true" not in prompt
    assert "最终`status`必须为`NEED_USER_INPUT`" not in prompt
    assert "source_hash" not in system_prompt
    assert "protected_hash" not in system_prompt


def test_wf3b_plan_fabricated_source_refs_are_cleared(hardening_runtime):
    pack, executor = hardening_runtime
    output = pack.replay_output("P-BACKGROUND-RESEARCH-PLAN", "normal")
    output["source_refs"] = [
        {
            "source_id": "src-invented-by-model",
            "source_type": "EVIDENCE_MATERIAL",
            "security_level": "PUBLIC",
            "authority_rank": 1,
        }
    ]

    normalized = executor._normalize_output(
        "P-BACKGROUND-RESEARCH-PLAN",
        output,
        pack.replay_input("P-BACKGROUND-RESEARCH-PLAN"),
    )

    assert normalized["source_refs"] == []
    assert any(
        "SYSTEM_WF3B_SOURCE_REFS_RUNTIME_OWNED" in warning
        for warning in normalized["warnings"]
    )


def test_wf3b_plan_critic_fabricated_source_refs_are_cleared(hardening_runtime):
    pack, executor = hardening_runtime
    output = pack.replay_output("P-BACKGROUND-RESEARCH-PLAN-CRITIC", "normal")
    output["source_refs"] = [{"source_id": "src-invented-by-model"}]

    normalized = executor._normalize_output(
        "P-BACKGROUND-RESEARCH-PLAN-CRITIC",
        output,
        pack.replay_input("P-BACKGROUND-RESEARCH-PLAN-CRITIC"),
    )

    assert normalized["source_refs"] == []


def test_wf3b_synthesis_source_refs_stay_provider_accountable(hardening_runtime):
    pack, executor = hardening_runtime
    output = pack.replay_output("P-BACKGROUND-RESEARCH-SYNTHESIS", "normal")
    output["source_refs"] = [{"source_id": "src-invented-by-model"}]

    with pytest.raises(PromptExecutionError, match="provenance"):
        executor._normalize_output(
            "P-BACKGROUND-RESEARCH-SYNTHESIS",
            output,
            pack.replay_input("P-BACKGROUND-RESEARCH-SYNTHESIS"),
        )


def test_wf3b_synthesis_uses_direct_tool_arguments(hardening_runtime):
    from app.runtime_executor import RuntimePromptExecutor

    pack, _ = hardening_runtime
    executor = RuntimePromptExecutor.__new__(RuntimePromptExecutor)
    executor.pack = pack
    executor.runtime_mode = "LIVE"

    synthesis_spec = executor._model_request_spec("P-BACKGROUND-RESEARCH-SYNTHESIS")
    assert synthesis_spec["semantic_model_contract"]["enabled"] is False
    assert synthesis_spec["semantic_model_contract"]["direct_tool_arguments"] is True

    plan_spec = executor._model_request_spec("P-BACKGROUND-RESEARCH-PLAN")
    assert plan_spec["semantic_model_contract"]["direct_tool_arguments"] is False
