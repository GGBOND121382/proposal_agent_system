from __future__ import annotations

import copy
from pathlib import Path

from app.executor import PromptExecutionError
from app.model_semantic_contracts import (
    build_argument_architecture_critic_model_input,
    expand_argument_architecture_critic_model_output,
    semantic_model_reference_errors,
)
from app.pack import PromptPack
from app.workflows import WorkflowEngine


ROOT = Path(__file__).resolve().parents[1]
PACK = PromptPack(ROOT / "prompt_pack")
PROMPT = "P-ARGUMENT-ARCHITECTURE-CRITIC"
DIMENSIONS = [
    "CENTRAL_THESIS",
    "ARGUMENT_CHAIN",
    "EVIDENCE_SUPPORT",
    "METHOD_SUBSTANCE",
    "INNOVATION_BASELINE",
    "FEASIBILITY_FOUNDATION",
    "METRIC_JUSTIFICATION",
]


def _envelope_without_foundation() -> dict:
    envelope = copy.deepcopy(PACK.replay_input(PROMPT))
    for thread in envelope["payload"]["architecture_candidate"]["authored_state"][
        "research_threads"
    ]:
        thread["foundation"] = []
    return envelope


def _pass_output() -> dict:
    return {
        "quality_dimensions": [
            {"dimension": dimension, "score": 4, "evidence": ["满足要求"]}
            for dimension in DIMENSIONS
        ],
        "issues": [],
        "user_questions": [],
    }


def test_model_input_is_one_typed_review_graph_without_duplicate_candidate_view() -> None:
    model_input = build_argument_architecture_critic_model_input(
        PACK.replay_input(PROMPT)
    )

    assert PACK.validate_model(PROMPT, "input", model_input) == []
    assert set(model_input["candidate"]) == {
        "review_graph",
        "evidence_gaps",
        "readiness_summary",
    }
    assert "review_units" not in model_input["candidate"]
    assert "research_threads" not in model_input["candidate"]
    assert all(
        card["evidence_ref"].startswith("EV-")
        for card in model_input["evidence_cards"]
    )
    assert all(
        unit["review_ref"].startswith("RU-")
        for unit in model_input["candidate"]["review_graph"]["units"]
    )


def test_every_configured_thread_slot_is_addressable_as_present_or_missing() -> None:
    model_input = build_argument_architecture_critic_model_input(
        _envelope_without_foundation()
    )
    units = model_input["candidate"]["review_graph"]["units"]
    thread_indexes = {
        unit["thread_index"]
        for unit in units
        if unit["semantic_component"] == "THREAD"
    }
    required = {
        "THREAD",
        "GAP",
        "QUESTION",
        "LIMITATION",
        "OBJECTIVE",
        "WORK_PACKAGE",
        "METHOD",
        "EVALUATION",
        "SUCCESS_CRITERION",
        "INNOVATION",
        "PRIOR_WORK",
        "FOUNDATION",
    }

    for thread_index in thread_indexes:
        components = {
            unit["semantic_component"]
            for unit in units
            if unit["thread_index"] == thread_index
        }
        assert required <= components
    assert all(
        unit["presence"] == "MISSING"
        for unit in units
        if unit["semantic_component"] == "FOUNDATION"
    )


def test_missing_foundation_has_legal_single_handle_target_and_runtime_locator() -> None:
    envelope = _envelope_without_foundation()
    model_input = build_argument_architecture_critic_model_input(envelope)
    foundation = next(
        unit
        for unit in model_input["candidate"]["review_graph"]["units"]
        if unit["semantic_component"] == "FOUNDATION"
        and unit["thread_index"] == 0
    )
    output = _pass_output()
    output["issues"] = [
        {
            "code": "FOUNDATION_EVIDENCE_MISSING",
            "review_ref": foundation["review_ref"],
            "description": "该线程没有可核验研究基础。",
            "evidence_refs": [],
            "repair_instruction": "由原 Producer 补充证据；不存在时保持未知。",
            "needs_user_input": False,
            "requires_structure_change": True,
        }
    ]

    assert PACK.validate_model(PROMPT, "output", output) == []
    assert semantic_model_reference_errors(PROMPT, envelope, output) == []
    canonical = expand_argument_architecture_critic_model_output(envelope, output)
    finding = next(
        item
        for item in canonical["findings"]
        if item["defect_namespace"] == "SEMANTIC_OBSERVATION"
    )
    assert finding["semantic_component"] == "FOUNDATION"
    assert finding["semantic_thread"] == 0
    assert finding["semantic_review_unit_key"] == "SLOT:FOUNDATION:THREAD:0"
    assert finding["target_path_or_span"] == "/result/research_design_matrix/0"
    assert PACK.validate(PROMPT, "output", canonical) == []


def test_review_ref_and_evidence_ref_namespaces_cannot_be_interchanged() -> None:
    envelope = PACK.replay_input(PROMPT)
    model_input = build_argument_architecture_critic_model_input(envelope)
    method = next(
        unit
        for unit in model_input["candidate"]["review_graph"]["units"]
        if unit["semantic_component"] == "METHOD"
    )
    output = _pass_output()
    output["issues"] = [
        {
            "code": "ARGUMENT_METHOD_SUBSTANCE_WEAK",
            "review_ref": method["review_ref"],
            "description": "方法机制不足。",
            "evidence_refs": [method["review_ref"]],
            "repair_instruction": "补充方法机制。",
            "needs_user_input": False,
            "requires_structure_change": False,
        }
    ]

    assert PACK.validate_model(PROMPT, "output", output)
    errors = semantic_model_reference_errors(PROMPT, envelope, output)
    assert any("unknown EvidenceRef" in error for error in errors)


def test_issue_code_component_compatibility_is_derived_from_review_ref() -> None:
    envelope = PACK.replay_input(PROMPT)
    model_input = build_argument_architecture_critic_model_input(envelope)
    method = next(
        unit
        for unit in model_input["candidate"]["review_graph"]["units"]
        if unit["semantic_component"] == "METHOD"
    )
    output = _pass_output()
    output["issues"] = [
        {
            "code": "FOUNDATION_EVIDENCE_MISSING",
            "review_ref": method["review_ref"],
            "description": "错误定位。",
            "evidence_refs": [],
            "repair_instruction": "修复定位。",
            "needs_user_input": False,
            "requires_structure_change": False,
        }
    ]

    errors = semantic_model_reference_errors(PROMPT, envelope, output)
    assert any("cannot target" in error and "METHOD" in error for error in errors)


def test_successful_call_is_runtime_coverage_receipt_without_key_echo() -> None:
    envelope = PACK.replay_input(PROMPT)
    output = _pass_output()

    assert "reviewed_unit_keys" not in output
    assert PACK.validate_model(PROMPT, "output", output) == []
    canonical = expand_argument_architecture_critic_model_output(envelope, output)
    assert canonical["result"]["checked_node_ids"]
    assert len(canonical["result"]["checked_node_ids"]) == len(
        set(canonical["result"]["checked_node_ids"])
    )


def test_critic_contract_retry_receives_bounded_exact_feedback() -> None:
    error = PromptExecutionError(
        "Semantic model output validation failed",
        validation_errors=[
            "/issues/0/review_ref: unknown ReviewRef 'RU-999'",
            "/issues/0/evidence_refs/0: unknown EvidenceRef 'RU-001'",
        ],
    )
    error.provider_phase = "output_structure_validation"

    feedback = WorkflowEngine._contract_retry_feedback(PROMPT, error)

    assert feedback == [
        "/issues/0/review_ref: unknown ReviewRef 'RU-999'",
        "/issues/0/evidence_refs/0: unknown EvidenceRef 'RU-001'",
    ]
