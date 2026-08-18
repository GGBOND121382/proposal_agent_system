from __future__ import annotations

import copy

from app.executor import PromptExecutionError
from app.model_semantic_contracts import (
    _critic_chain_checks,
    _critic_design_matrix_checks,
    build_semantic_model_input,
    expand_argument_architecture_model_output,
)
from app.runtime_executor import RuntimePromptExecutor
from app.workflows import WorkflowEngine
from tests.test_semantic_model_contracts_v1 import (
    PACK,
    _argument_envelope_with_evidence,
    _semantic_argument_output,
)


def test_foundation_is_truly_optional_when_no_supported_foundation_exists() -> None:
    envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(envelope)
    semantic["research_threads"][0]["foundation"] = []

    canonical = expand_argument_architecture_model_output(envelope, semantic)

    assert canonical["status"] == "PASS"
    assert PACK.validate("P-ARGUMENT-ARCHITECTURE", "output", canonical) == []
    row = canonical["result"]["research_design_matrix"][0]
    assert row["foundation_evidence_ids"] == []
    chain_checks = _critic_chain_checks(canonical["result"])
    assert all(item["complete"] for item in chain_checks)
    foundation_check = next(
        item
        for item in chain_checks
        if item["chain_type"] == "FOUNDATION_TO_FEASIBILITY"
    )
    assert foundation_check["source_ids"] == []
    assert "不适用" in foundation_check["evidence"]
    assert all(item["complete"] for item in _critic_design_matrix_checks(canonical["result"]))


def test_present_foundation_still_requires_explicit_support_relation() -> None:
    envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(envelope)
    canonical = expand_argument_architecture_model_output(envelope, semantic)
    candidate = copy.deepcopy(canonical["result"])
    candidate["argument_architecture"]["edges"] = [
        edge
        for edge in candidate["argument_architecture"]["edges"]
        if edge.get("relation") != "SUPPORTS"
    ]

    foundation_check = next(
        item
        for item in _critic_chain_checks(candidate)
        if item["chain_type"] == "FOUNDATION_TO_FEASIBILITY"
    )
    assert foundation_check["source_ids"]
    assert foundation_check["complete"] is False



def test_foundation_optional_semantics_are_declared_in_registry() -> None:
    from app.contracts.semantic_contract import get_semantic_contract

    contract = get_semantic_contract()
    matrix = contract.rule("SC-ARGUMENT-DESIGN-MATRIX-COMPLETENESS").config
    assert "foundation_evidence_ids" not in set(matrix.get("required_fields") or [])
    assert "foundation_evidence_ids" in set(matrix.get("optional_fields") or [])

    chains = contract.rule("SC-ARGUMENT-DETERMINISTIC-CHAINS").config.get("chains") or []
    foundation_chain = next(
        item for item in chains if item.get("chain_type") == "FOUNDATION_TO_FEASIBILITY"
    )
    assert foundation_chain.get("source_presence") == "IF_PRESENT"

def test_semantic_retry_feedback_is_minimal_and_model_schema_valid() -> None:
    cause = RuntimeError("provider semantic object rejected")
    cause.provider_phase = "output_structure_validation"
    exc = PromptExecutionError(
        "provider output contract validation failed",
        validation_errors=[
            "/research_threads/0/work_packages/0: unknown evidence id E404",
            "/research_threads/0/work_packages/0: unknown evidence id E404",
        ],
    )
    exc.__cause__ = cause

    issues = WorkflowEngine._semantic_retry_issues("P-ARGUMENT-ARCHITECTURE", exc)
    assert len(issues) == 1
    assert set(issues[0]) == {
        "problem", "severity", "component", "required_action", "evidence_ids"
    }

    envelope = _argument_envelope_with_evidence()
    semantic_input = build_semantic_model_input("P-ARGUMENT-ARCHITECTURE", envelope)
    merged = RuntimePromptExecutor._merge_semantic_retry_issues(
        "P-ARGUMENT-ARCHITECTURE", semantic_input, issues
    )
    assert PACK.validate_model("P-ARGUMENT-ARCHITECTURE", "input", merged) == []
    assert merged["revision_issues"][-1] == issues[0]


def test_semantic_retry_feedback_does_not_leak_into_unrelated_failures() -> None:
    cause = RuntimeError("timeout")
    cause.provider_phase = "transport"
    exc = PromptExecutionError("timeout", validation_errors=["some error"])
    exc.__cause__ = cause
    assert WorkflowEngine._semantic_retry_issues("P-ARGUMENT-ARCHITECTURE", exc) == []
    assert WorkflowEngine._semantic_retry_issues("P-WRITE-CONTENT", exc) == []



def test_semantic_retry_feedback_is_size_bounded() -> None:
    cause = RuntimeError("provider semantic object rejected")
    cause.provider_phase = "output_structure_validation"
    exc = PromptExecutionError(
        "provider output contract validation failed",
        validation_errors=[
            f"/research_threads/{idx}: " + ("错误" * 400)
            for idx in range(8)
        ],
    )
    exc.__cause__ = cause

    issues = WorkflowEngine._semantic_retry_issues("P-ARGUMENT-ARCHITECTURE", exc)
    assert len(issues) == 3
    assert all(len(item["problem"]) < 320 for item in issues)

def test_provider_retry_passes_semantic_feedback_only_after_argument_contract_failure(tmp_path) -> None:
    import asyncio
    import json
    from types import SimpleNamespace

    from app.llm import ProviderError
    from app.runtime_failures import ProviderFailureKind
    from tests.test_runtime_recovery import SequencePromptExecutor, make_executor_db

    db = make_executor_db(tmp_path)
    state = {
        "workflow_type": "WF-4_PROPOSAL_AUTHORING",
        "options": {
            "provider_retry_limit": 1,
            "provider_retry_base_delay_seconds": 0,
        },
        "step_results": {},
    }
    now = __import__("app.util", fromlist=["utc_now"]).utc_now()
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            "wf-v11-retry",
            "project-1",
            "WF-4_PROPOSAL_AUTHORING",
            "RUNNING",
            0,
            json.dumps(state),
            now,
            now,
        ),
    )
    wf = {
        "id": "wf-v11-retry",
        "project_id": "project-1",
        "workflow_type": "WF-4_PROPOSAL_AUTHORING",
        "status": "RUNNING",
        "current_step": 0,
        "state": state,
    }

    provider = ProviderError(
        "semantic output contract failed",
        kind=ProviderFailureKind.RESPONSE_SHAPE,
        phase="output_structure_validation",
        retryable_hint=False,
        validation_errors=["/research_threads/0: unknown evidence id E404"],
    )
    first = PromptExecutionError(
        "prompt execution failed",
        validation_errors=["/research_threads/0: unknown evidence id E404"],
    )
    first.__cause__ = provider
    success = {"run_id": "run-v11-ok", "status": "PASS", "output": {"status": "PASS"}}
    executor = SequencePromptExecutor([first, success])
    engine = WorkflowEngine(
        db,
        SimpleNamespace(),
        SimpleNamespace(),
        executor,
        SimpleNamespace(),
    )

    result = asyncio.run(
        engine._execute_prompt_with_provider_retry(
            wf,
            state,
            prompt_id="P-ARGUMENT-ARCHITECTURE",
            envelope={"payload": {}},
        )
    )

    assert result is success
    assert "semantic_retry_issues" not in executor.call_kwargs[0]
    retry_feedback = executor.call_kwargs[1]["semantic_retry_issues"]
    assert len(retry_feedback) == 1
    assert "E404" in retry_feedback[0]["problem"]
    cycle = state["provider_call_cycles"]["0:P-ARGUMENT-ARCHITECTURE"]
    assert "semantic_retry_issues" not in cycle
    assert "provider_wait" not in state
