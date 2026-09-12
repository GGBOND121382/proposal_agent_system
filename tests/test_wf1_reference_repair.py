"""WF-1 survey-intake defaults and targeted-repair reference binding tests.

Covers docs/WF1_REFERENCE_REPAIR_CHANGE_SPEC_20260911.md section 6:
deterministic survey defaults (cases 1-5), repair reference scope bound to
the original producer request (cases 6-10, 13), and native JSON patch value
typing (cases 11-12).
"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

from app.executor import PromptExecutor, PromptExecutionError
from app.model_semantic_contracts import (
    apply_semantic_model_output_defaults,
    apply_wf1_survey_intake_defaults,
    build_semantic_model_input,
    expand_semantic_model_output,
    semantic_model_reference_errors,
    targeted_repair_semantic_errors,
)
from app.output_integrity import attach_trusted_source_catalog
from app.pack import PromptPack
from app.workflow_repair import WorkflowRepairMixin

ROOT = Path(__file__).resolve().parents[1]
PACK = PromptPack(ROOT / "prompt_pack")
PD_EXTRACT = "P-PROJECT-DEFINITION-EXTRACT"


def _executor() -> PromptExecutor:
    executor = PromptExecutor.__new__(PromptExecutor)
    executor.pack = PACK
    executor.quality_guard_enabled = False
    return executor


class _RepairHarness(WorkflowRepairMixin):
    def __init__(self) -> None:
        self.pack = PACK
        self.executor = _executor()


def _envelope(*, document_type: str | None = None) -> dict:
    envelope = attach_trusted_source_catalog(PACK.replay_input(PD_EXTRACT))
    if document_type is not None:
        envelope.setdefault("payload", {})["document_type"] = document_type
    return envelope


def _survey_semantic_output() -> dict:
    """Minimal SURVEY_REPORT-shaped PD-EXTRACT semantic output.

    Deliberately omits gap_keys and the two contract ceiling fields, mirroring
    the wf-11d5a0f0ef814cfc failure candidate.
    """
    return {
        "status": "PASS",
        "document_kind": "TECHNICAL_REPORT",
        "project_name": "美空军DASH系统调研分析报告",
        "items": [
            {"local_key": "I1", "item_type": "DEMAND", "domain": "BACKGROUND_AND_DEMAND",
             "summary": "调研DASH系统的能力边界", "attributes": {}, "evidence_ids": ["S1"]},
            {"local_key": "I2", "item_type": "GAP", "domain": "STATE_GAP_ROOT_CAUSE",
             "summary": "DASH公开资料缺少内部评估细节", "attributes": {}, "evidence_ids": ["S1"]},
        ],
        "relations": [],
        "proposal_contract": {
            "primary_evaluation_logic": "TECHNICAL_INNOVATION",
            "target_evaluators": [],
            "mandatory_sections": [],
            "appendix_only_topics": [],
            "forbidden_main_body_topics": [],
        },
        "argument_seed": {
            "central_question": {
                "statement": "DASH系统的能力边界与公开证据是什么",
                "proposition_type": "DESIGN_PROPOSITION",
                "falsifiable_or_comparable": True,
                "boundary_conditions": [],
                "evidence_ids": ["S1"],
            },
            "research_questions": [
                {
                    "statement": "DASH系统的决策加速效果如何",
                    "question_type": "TECHNICAL",
                    "answerability": "COMPARABLE",
                    "success_evidence": ["公开实验报道"],
                    "evidence_ids": ["S1"],
                },
                {
                    "statement": "DASH系统的人机协同流程如何组织",
                    "question_type": "TECHNICAL",
                    "answerability": "TESTABLE",
                    "success_evidence": ["公开流程描述"],
                    "evidence_ids": ["S1"],
                },
                {
                    "statement": "DASH系统的已知局限是什么",
                    "question_type": "TECHNICAL",
                    "answerability": "UNCLEAR",
                    "success_evidence": ["公开局限说明"],
                    "evidence_ids": ["S1"],
                },
            ],
            "in_scope": ["DASH系统"],
            "out_of_scope": [],
        },
        "findings": [],
        "unresolved_items": [],
        "user_questions": [],
    }


def _normalized_survey_candidate(envelope: dict) -> tuple[dict, list]:
    schema = PACK.model_schema(PD_EXTRACT, "output")
    candidate = apply_semantic_model_output_defaults(schema, _survey_semantic_output())
    candidate, report = apply_wf1_survey_intake_defaults(PD_EXTRACT, envelope, candidate)
    return candidate, report


# --- 确定性缺省与事实边界（规格用例 1-5） ---------------------------------


def test_survey_defaults_fill_missing_gap_keys_and_contract_nulls() -> None:
    """Case 1: missing gap_keys/max fields are filled and pass validation."""
    envelope = _envelope(document_type="SURVEY_REPORT")
    candidate, report = _normalized_survey_candidate(envelope)
    paths = {entry["path"] for entry in report}
    assert "/proposal_contract/max_main_pages" in paths
    assert "/proposal_contract/max_core_research_questions" in paths
    for index in range(3):
        assert f"/argument_seed/research_questions/{index}/gap_keys" in paths
    assert all(entry["rule"] == "WF1_SURVEY_INTAKE_DEFAULT" for entry in report)
    assert PACK.validate_model(PD_EXTRACT, "output", candidate) == []
    assert semantic_model_reference_errors(PD_EXTRACT, envelope, candidate) == []
    expanded = expand_semantic_model_output(PD_EXTRACT, envelope, candidate)
    normalized = _executor()._normalize_output(PD_EXTRACT, expanded, envelope)
    assert PACK.validate(PD_EXTRACT, "output", normalized) == []
    questions = normalized["result"]["argument_graph_seed"]["research_questions"]
    assert len(questions) == 3
    assert all(question["linked_gap_ids"] == [] for question in questions)


def test_survey_defaults_never_override_confirmed_limits() -> None:
    """Case 2/3: authored limits stay binding; no fabricated caps."""
    envelope = _envelope(document_type="SURVEY_REPORT")
    output = _survey_semantic_output()
    output["proposal_contract"]["max_main_pages"] = 20
    schema = PACK.model_schema(PD_EXTRACT, "output")
    candidate = apply_semantic_model_output_defaults(schema, output)
    candidate, report = apply_wf1_survey_intake_defaults(PD_EXTRACT, envelope, candidate)
    assert candidate["proposal_contract"]["max_main_pages"] == 20
    # Three authored questions do not become a fabricated "max three" rule.
    assert candidate["proposal_contract"]["max_core_research_questions"] is None
    assert "/proposal_contract/max_main_pages" not in {entry["path"] for entry in report}


def test_survey_defaults_are_idempotent() -> None:
    """Case 4: applying the rule twice yields the identical object."""
    envelope = _envelope(document_type="SURVEY_REPORT")
    once, first_report = _normalized_survey_candidate(envelope)
    twice, second_report = apply_wf1_survey_intake_defaults(PD_EXTRACT, envelope, once)
    assert first_report
    assert second_report == []
    assert twice == once


def test_research_proposal_gets_no_survey_defaults() -> None:
    """Case 5: RESEARCH_PROPOSAL intake still requires authored gap links."""
    envelope = _envelope(document_type="RESEARCH_PROPOSAL")
    candidate, report = _normalized_survey_candidate(envelope)
    assert report == []
    errors = PACK.validate_model(PD_EXTRACT, "output", candidate)
    assert any("gap_keys" in error for error in errors)
    assert any("max_main_pages" in error for error in errors)


def test_defaults_require_survey_document_type_marker() -> None:
    envelope = _envelope()
    _candidate, report = _normalized_survey_candidate(envelope)
    assert report == []


# --- 编号范围与修复（规格用例 6-10、13） -----------------------------------


def test_repair_revalidation_accepts_original_run_evidence_ids() -> None:
    """Case 6/13: S-ids from the original producer request survive repair.

    This is the wf-11d5a0f0ef814cfc regression: the persisted semantic
    candidate used to go straight into canonical reference validation, where
    S1…S8 were rejected as unknown ids.  The repaired candidate must instead
    re-enter the semantic chain bound to the original producer input.
    """
    envelope = _envelope(document_type="SURVEY_REPORT")
    candidate, _report = _normalized_survey_candidate(envelope)
    harness = _RepairHarness()
    validated, _guard = harness._validate_repaired_producer_output(
        prompt_id=PD_EXTRACT,
        candidate=candidate,
        producer_input=envelope,
        quality_input=envelope,
    )
    assert PACK.validate(PD_EXTRACT, "output", validated) == []
    assert validated["result"]["project_definition"]["items"]


def test_repair_revalidation_rejects_unknown_evidence_id() -> None:
    """Cases 6/10: S99 never becomes legal just because the output cites it."""
    envelope = _envelope(document_type="SURVEY_REPORT")
    candidate, _report = _normalized_survey_candidate(envelope)
    candidate["items"][0]["evidence_ids"] = ["S99"]
    harness = _RepairHarness()
    with pytest.raises(PromptExecutionError) as excinfo:
        harness._validate_repaired_producer_output(
            prompt_id=PD_EXTRACT,
            candidate=candidate,
            producer_input=envelope,
            quality_input=envelope,
        )
    assert any("S99" in error for error in excinfo.value.validation_errors)


def test_repair_scope_is_bound_to_original_producer_request() -> None:
    """Case 7: an S-id legal in another run's request is dangling here."""
    envelope = _envelope(document_type="SURVEY_REPORT")
    candidate, _report = _normalized_survey_candidate(envelope)
    card_count = len(build_semantic_model_input(PD_EXTRACT, envelope)["evidence_cards"])
    beyond = f"S{card_count + 1}"
    candidate["items"][0]["evidence_ids"] = [beyond]
    harness = _RepairHarness()
    with pytest.raises(PromptExecutionError) as excinfo:
        harness._validate_repaired_producer_output(
            prompt_id=PD_EXTRACT,
            candidate=candidate,
            producer_input=envelope,
            quality_input=envelope,
        )
    assert any(beyond in error for error in excinfo.value.validation_errors)


def test_repair_revalidation_rejects_unknown_and_wrong_typed_gap_keys() -> None:
    """Case 8: undeclared I37 fails; a DEMAND item cannot serve as a gap."""
    envelope = _envelope(document_type="SURVEY_REPORT")
    candidate, _report = _normalized_survey_candidate(envelope)
    candidate["argument_seed"]["research_questions"][0]["gap_keys"] = ["I37"]
    errors = semantic_model_reference_errors(PD_EXTRACT, envelope, candidate)
    assert any("I37" in error for error in errors)

    candidate, _report = _normalized_survey_candidate(envelope)
    candidate["argument_seed"]["research_questions"][0]["gap_keys"] = ["I1"]
    errors = semantic_model_reference_errors(PD_EXTRACT, envelope, candidate)
    assert any("gap_keys" in error and "DEMAND" in error for error in errors)

    # A GAP/PROBLEM item remains legal.
    candidate, _report = _normalized_survey_candidate(envelope)
    candidate["argument_seed"]["research_questions"][0]["gap_keys"] = ["I2"]
    assert semantic_model_reference_errors(PD_EXTRACT, envelope, candidate) == []


def test_missing_evidence_catalog_reports_context_error() -> None:
    """Case 9: without the original evidence catalog the error names it."""
    envelope = _envelope(document_type="SURVEY_REPORT")
    envelope["payload"]["source_documents"] = []
    candidate, _report = _normalized_survey_candidate(envelope)
    errors = semantic_model_reference_errors(PD_EXTRACT, envelope, candidate)
    assert errors
    assert all("evidence_cards" in error for error in errors)


# --- 修复值的原生 JSON 类型（规格用例 11-12） -------------------------------


def _repair_envelope(original: dict, *allowed: str) -> dict:
    return {
        "schema_version": "2.0",
        "prompt_id": "P-TARGETED-REPAIR",
        "prompt_version": "8.0.0",
        "payload": {
            "original_object": {
                "object_type": "PROJECT_DEFINITION_EXTRACT",
                "object_id": "contract-run-test",
                "object_hash": "0" * 64,
                "content": copy.deepcopy(original),
            },
            "original_producer": "PROJECT_KNOWLEDGE_AGENT",
            "findings_to_repair": [
                {
                    "finding_instance_id": "finding-test-001",
                    "code": "OUTPUT_CONTRACT_VIOLATION",
                    "target_path_or_span": path,
                    "description": "缺字段。",
                }
                for path in allowed
            ],
            "allowed_paths": ["/content" + path for path in allowed],
            "protected_paths": [],
            "protected_hashes": [],
            "human_resolutions": [],
        },
    }


def _repair_decision(*changes: dict) -> dict:
    return {
        "decision": "APPLY",
        "changes": list(changes),
        "findings": [],
        "unresolved_items": [],
        "user_questions": [],
    }


def test_patch_value_must_use_native_json_types() -> None:
    """Case 11: "[]" is a string, not an array; native [] and null are legal."""
    original = _survey_semantic_output()
    envelope = _repair_envelope(
        original,
        "/argument_seed/research_questions/0/gap_keys",
        "/proposal_contract/max_main_pages",
    )
    stringy = _repair_decision(
        {"path": "/argument_seed/research_questions/0/gap_keys", "value": "[]"},
    )
    errors = targeted_repair_semantic_errors(envelope, stringy)
    assert any("does not match target field" in error for error in errors)

    native = _repair_decision(
        {"path": "/argument_seed/research_questions/0/gap_keys", "value": []},
        {"path": "/proposal_contract/max_main_pages", "value": None},
    )
    assert targeted_repair_semantic_errors(envelope, native) == []

    wrong_scalar = _repair_decision(
        {"path": "/proposal_contract/max_main_pages", "value": "null"},
    )
    errors = targeted_repair_semantic_errors(envelope, wrong_scalar)
    assert any("does not match target field" in error for error in errors)

    integer = _repair_decision(
        {"path": "/proposal_contract/max_main_pages", "value": 20},
    )
    assert targeted_repair_semantic_errors(envelope, integer) == []


def test_patch_outside_authorized_scope_is_rejected_atomically() -> None:
    """Case 12: any out-of-scope or invalid path rejects the whole patch."""
    original = _survey_semantic_output()
    envelope = _repair_envelope(
        original,
        "/argument_seed/research_questions/0/gap_keys",
    )
    patch = _repair_decision(
        {"path": "/argument_seed/research_questions/0/gap_keys", "value": []},
        {"path": "/project_name", "value": "篡改标题"},
    )
    errors = targeted_repair_semantic_errors(envelope, patch)
    assert any("outside authorized repair targets" in error for error in errors)
    # Nothing is applied while errors exist; the original stays untouched.
    assert original["project_name"] == "美空军DASH系统调研分析报告"
