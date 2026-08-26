from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from app.db import Database
from app.llm import LLMError, LLMResult
from app.runtime_evidence import ModelCallEvidenceStore
from app.runtime_executor import RuntimePromptExecutor, _RuntimeArgumentStageGateway
from app.runtime_failures import FailureCategory, classify_runtime_failure
from app.security import Route
from app.util import sha256_json, utc_now
from app.workflow_defs import WORKFLOWS
from app.argument_two_stage_orchestration import (
    ARGUMENT_DESIGN_STAGE,
    ARGUMENT_SKELETON_STAGE,
    _apply_stage_repair_changes,
    _drop_reported_unknown_evidence_ids,
    _normalize_stage_mechanical_defaults,
    _normalize_stage_mechanical_defaults_with_provenance,
    _normalize_stage_structural_artifacts,
    _normalize_skeleton_mechanical_artifacts,
    _project_stage_user_questions,
    _stage_candidate_has_substantive_draft,
    argument_stage_output_schema,
    argument_stage_repair_scope_paths,
    merge_argument_stage_repair_candidate,
)
from app.model_semantic_contracts import (
    assemble_argument_authored_state,
    expand_argument_architecture_model_output,
)
from app.workflows import semantic_gap_revision_finding
from tests.test_argument_lifecycle_composition_v9 import CRITIC, _LifecycleHarness, _scope_finding
from tests.test_semantic_contract_final_closure_v7 import _critic_context
from tests.test_semantic_model_contracts_v1 import (
    PACK,
    _argument_envelope_with_evidence,
    _flat_design_output,
    _flat_skeleton_output,
)


class _ArgumentRuntimeRouter:
    def __init__(self, environment="OFFLINE_LOCAL"):
        self.environment = environment

    def route(self, prompt_id, envelope, original_environment=None):
        return Route(
            prompt_id=prompt_id,
            environment=self.environment,
            model_id="offline-general-primary",
            endpoint_id="offline-primary",
            provider_model_name="MiniMax-M3",
            endpoint={"base_url": "https://example.invalid"},
            profile=copy.deepcopy(PACK.model_profile(prompt_id)),
        )


class _ArgumentRuntimeGateway:
    supports_runtime_evidence = True

    def __init__(self, tmp_path, responses):
        self.settings = SimpleNamespace(runtime_mode="LIVE")
        self.evidence_store = ModelCallEvidenceStore(tmp_path / "model_calls")
        self.responses = list(responses)
        self.calls = []

    async def invoke(
        self,
        route,
        prompt_id,
        system_prompt,
        envelope,
        output_schema,
        *,
        call_key=None,
        direct_tool_arguments=False,
    ):
        self.calls.append({
            "route": route,
            "prompt_id": prompt_id,
            "system_prompt": system_prompt,
            "envelope": copy.deepcopy(envelope),
            "output_schema": copy.deepcopy(output_schema),
            "call_key": call_key,
            "direct_tool_arguments": direct_tool_arguments,
        })
        response = self.responses[len(self.calls) - 1]
        if isinstance(response, BaseException):
            raise response
        if hasattr(response, "output") and hasattr(response, "model_id"):
            return response
        raw = json.dumps(response, ensure_ascii=False)
        return LLMResult(
            output=copy.deepcopy(response),
            raw_text=raw,
            model_id=route.model_id,
            endpoint_id=route.endpoint_id,
            response_contract_mode="TEST_STRICT_JSON",
        )


def _runtime_db(tmp_path):
    db = Database(tmp_path / "runtime.sqlite3")
    now = utc_now()
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        ("project-1", "Project", "Description", "INTERNAL", "{}", now, now),
    )
    return db


def _executor(tmp_path, responses, *, environment="OFFLINE_LOCAL"):
    gateway = _ArgumentRuntimeGateway(tmp_path, responses)
    executor = RuntimePromptExecutor(
        _runtime_db(tmp_path),
        PACK,
        _ArgumentRuntimeRouter(environment),
        gateway,
        quality_guard_enabled=False,
    )
    return executor, gateway


def _stage_question(
    label: str,
    *,
    question_type: str = "MISSING_INFORMATION",
    allowed_values: list[str] | None = None,
) -> dict:
    return {
        "target_area": "RESEARCH_DESIGN",
        "question_type": question_type,
        "question": f"Question {label}?",
        "reason": f"Reason {label}.",
        "answer_shape": "STRING",
        "allowed_values": list(allowed_values or []),
        "blocking": True,
        "priority": "P0",
    }


def _repair_response(*changes: tuple[str, object]) -> dict:
    return {
        "decision": "APPLY",
        "changes": [
            {"path": path, "value": copy.deepcopy(value)}
            for path, value in changes
        ],
        "escalation_reason": None,
    }


def _fragment_second_question(questions: list[dict]) -> list[dict]:
    second = questions[1]
    return [
        copy.deepcopy(questions[0]),
        {
            "target_area": second["target_area"],
            "question_type": second["question_type"],
            "question": second["question"],
        },
        {"$text": second["answer_shape"]},
        {},
        {"$text": "true"},
        {"$text": second["priority"]},
        *copy.deepcopy(questions[2:]),
    ]


def test_step4b_runtime_argument_uses_only_two_internal_stage_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPABILITY_ACCEPTANCE_MODE", "false")
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    design = _flat_design_output(envelope)
    executor, gateway = _executor(tmp_path, [skeleton, design])

    result = asyncio.run(executor.execute(
        "P-ARGUMENT-ARCHITECTURE",
        envelope,
        project_id="project-1",
        workflow_id="wf-step4b",
        call_key="outer-step4b",
    ))

    assert result["prompt_id"] == "P-ARGUMENT-ARCHITECTURE"
    assert result["output"]["result"]["authored_state"]["central_proposition"] == skeleton["central_proposition"]
    assert len(gateway.calls) == 2
    assert [call["route"].profile["desired_output_tokens"] for call in gateway.calls] == [
        8_192, 65_536
    ]
    assert [
        call["route"].profile["argument_two_stage_internal_stage"]
        for call in gateway.calls
    ] == [ARGUMENT_SKELETON_STAGE, ARGUMENT_DESIGN_STAGE]
    assert gateway.calls[0]["envelope"].get("skeleton_seed") is not None
    assert "frozen_skeleton" not in gateway.calls[0]["envelope"]
    assert gateway.calls[1]["envelope"]["frozen_skeleton"] == skeleton
    assert all(call["direct_tool_arguments"] is True for call in gateway.calls)
    assert gateway.calls[0]["call_key"] != gateway.calls[1]["call_key"]

    row = executor.db.fetchone(
        "SELECT COUNT(*) AS n FROM prompt_runs WHERE prompt_id='P-ARGUMENT-ARCHITECTURE'"
    )
    assert int(row["n"]) == 1


def test_step4b_runtime_semantic_regeneration_uses_exact_baseline_and_only_design(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CAPABILITY_ACCEPTANCE_MODE", "false")
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    complete_design = _flat_design_output(envelope)
    baseline_design = copy.deepcopy(complete_design)
    baseline_design["evaluations"] = []
    baseline_design["baselines"] = []
    baseline_design["ablations"] = []
    baseline_design["innovation_evaluation_refs"] = []
    baseline_authored = assemble_argument_authored_state(
        skeleton, baseline_design
    )
    baseline_output = expand_argument_architecture_model_output(
        envelope, baseline_authored
    )
    assert baseline_output["status"] == "REVISE"

    finding = semantic_gap_revision_finding(
        {
            "defect_key": "ARGUMENT:TEST:HARD:0:EVALUATION",
            "finding_code": "RESEARCH_DESIGN_INCOMPLETE",
            "semantic_component": "EVALUATION",
            "thread_index": 0,
            "reason": "The method lacks a validation/evaluation closure.",
            "suggested_source_or_question": "Add the missing evaluation closure.",
        },
        producer_prompt="P-ARGUMENT-ARCHITECTURE",
        round_number=1,
        index=1,
    )
    envelope["payload"]["revision_findings"] = [finding]
    executor, gateway = _executor(tmp_path, [complete_design])
    baseline_run_id = "run-stage0-baseline"
    now = utc_now()
    executor.db.execute(
        """INSERT INTO prompt_runs(
               id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,
               input_hash,output_hash,input_json,output_json,error,duration_ms,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            baseline_run_id,
            "project-1",
            "wf-stage0-regeneration",
            "P-ARGUMENT-ARCHITECTURE",
            "REVISE",
            "offline-general-primary",
            "offline-primary",
            sha256_json(envelope),
            sha256_json(baseline_output),
            json.dumps(envelope, ensure_ascii=False),
            json.dumps(baseline_output, ensure_ascii=False),
            None,
            1,
            now,
        ),
    )

    result = asyncio.run(executor.execute(
        "P-ARGUMENT-ARCHITECTURE",
        envelope,
        project_id="project-1",
        workflow_id="wf-stage0-regeneration",
        call_key="outer-stage0-regeneration",
        semantic_regeneration_baseline_run_id=baseline_run_id,
    ))

    assert result["status"] == "PASS"
    assert [
        call["route"].profile["argument_two_stage_internal_stage"]
        for call in gateway.calls
    ] == [ARGUMENT_DESIGN_STAGE]
    revise_call = gateway.calls[0]
    retry_context = revise_call["envelope"]["retry_context"]
    assert retry_context["mode"] == "WHOLE_DESIGN_REVISE"
    assert retry_context["required_output"] == "COMPLETE_DESIGN"
    assert retry_context["previous_candidate"] == baseline_design
    assert "WHOLE_DESIGN_REVISE" in revise_call["system_prompt"]


def test_step4b_runtime_design_retry_never_regenerates_skeleton(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPABILITY_ACCEPTANCE_MODE", "false")
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    broken_design = _flat_design_output(envelope)
    broken_design["methods"][0]["work_package_index"] = 99
    valid_design = _flat_design_output(envelope)
    executor, gateway = _executor(
        tmp_path,
        [
            skeleton,
            broken_design,
            valid_design,
        ],
    )

    result = asyncio.run(executor.execute(
        "P-ARGUMENT-ARCHITECTURE",
        envelope,
        project_id="project-1",
        workflow_id="wf-step4b-retry",
        call_key="outer-step4b-retry",
    ))

    assert result["status"] in {"PASS", "NEED_USER_INPUT"}
    assert len(gateway.calls) == 3
    first, design1, design_retry = gateway.calls
    assert first["envelope"].get("skeleton_seed") is not None
    assert design1["envelope"]["frozen_skeleton"] == skeleton
    assert design_retry["prompt_id"] == "P-ARGUMENT-ARCHITECTURE"
    assert design_retry["envelope"]["frozen_skeleton"] == skeleton
    retry_context = design_retry["envelope"]["retry_context"]
    assert retry_context["recovery_mode"] == "FULL_STAGE_RETRY"
    assert any(
        "unresolved parent index" in error
        for error in retry_context["validation_errors"]
    )
    assert "previous_candidate" not in retry_context
    assert "repair_targets" not in design_retry["envelope"]
    full_design_chars = len(json.dumps(design1["envelope"], ensure_ascii=False))
    retry_chars = len(json.dumps(design_retry["envelope"], ensure_ascii=False))
    assert retry_chars < full_design_chars + 4_000


def test_step4b_runtime_allows_a_third_internal_stage_attempt(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPABILITY_ACCEPTANCE_MODE", "false")
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    incomplete = {
        "central_proposition": copy.deepcopy(skeleton["central_proposition"]),
        "scope": copy.deepcopy(skeleton["scope"]),
    }
    design = _flat_design_output(envelope)
    executor, gateway = _executor(
        tmp_path, [incomplete, incomplete, skeleton, design]
    )

    result = asyncio.run(executor.execute(
        "P-ARGUMENT-ARCHITECTURE",
        envelope,
        project_id="project-1",
        workflow_id="wf-step4b-third-attempt",
        call_key="outer-step4b-third-attempt",
    ))

    assert result["status"] in {"PASS", "NEED_USER_INPUT"}
    assert len(gateway.calls) == 4
    assert all(
        call["prompt_id"] == "P-ARGUMENT-ARCHITECTURE"
        for call in gateway.calls
    )
    assert [
        call["route"].profile["argument_two_stage_internal_stage"]
        for call in gateway.calls
    ] == [
        ARGUMENT_SKELETON_STAGE,
        ARGUMENT_SKELETON_STAGE,
        ARGUMENT_SKELETON_STAGE,
        ARGUMENT_DESIGN_STAGE,
    ]
    for retry in gateway.calls[1:3]:
        assert retry["envelope"]["retry_context"]["recovery_mode"] == "FULL_STAGE_RETRY"
        assert "previous_candidate" not in retry["envelope"]["retry_context"]
    assert gateway.calls[3]["envelope"]["frozen_skeleton"] == skeleton


def test_empty_skeleton_response_retries_the_full_stage_without_a_fake_draft(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CAPABILITY_ACCEPTANCE_MODE", "false")
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    design = _flat_design_output(envelope)
    executor, gateway = _executor(tmp_path, [{}, skeleton, design])

    result = asyncio.run(executor.execute(
        "P-ARGUMENT-ARCHITECTURE",
        envelope,
        project_id="project-1",
        workflow_id="wf-empty-skeleton-retry",
        call_key="outer-empty-skeleton-retry",
    ))

    assert result["status"] in {"PASS", "NEED_USER_INPUT"}
    assert [
        call["route"].profile["argument_two_stage_internal_stage"]
        for call in gateway.calls
    ] == [ARGUMENT_SKELETON_STAGE, ARGUMENT_SKELETON_STAGE, ARGUMENT_DESIGN_STAGE]
    retry = gateway.calls[1]
    assert retry["prompt_id"] == "P-ARGUMENT-ARCHITECTURE"
    assert retry["envelope"]["retry_context"]["recovery_mode"] == "FULL_STAGE_RETRY"
    assert "previous_candidate" not in retry["envelope"]["retry_context"]
    assert len(retry["envelope"]["retry_context"]["validation_errors"]) == 1
    assert len(json.dumps(retry["envelope"]["retry_context"])) < 300
    assert "FULL_STAGE_RETRY" in retry["system_prompt"]
    assert gateway.calls[2]["envelope"]["frozen_skeleton"] == skeleton


def test_empty_design_response_retries_only_design_with_skeleton_frozen(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CAPABILITY_ACCEPTANCE_MODE", "false")
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    design = _flat_design_output(envelope)
    executor, gateway = _executor(tmp_path, [skeleton, {}, design])

    result = asyncio.run(executor.execute(
        "P-ARGUMENT-ARCHITECTURE",
        envelope,
        project_id="project-1",
        workflow_id="wf-empty-design-retry",
        call_key="outer-empty-design-retry",
    ))

    assert result["status"] in {"PASS", "NEED_USER_INPUT"}
    assert [
        call["route"].profile["argument_two_stage_internal_stage"]
        for call in gateway.calls
    ] == [ARGUMENT_SKELETON_STAGE, ARGUMENT_DESIGN_STAGE, ARGUMENT_DESIGN_STAGE]
    assert gateway.calls[1]["envelope"]["frozen_skeleton"] == skeleton
    assert gateway.calls[2]["envelope"]["frozen_skeleton"] == skeleton
    assert gateway.calls[2]["envelope"]["retry_context"]["recovery_mode"] == "FULL_STAGE_RETRY"
    assert "previous_candidate" not in gateway.calls[2]["envelope"]["retry_context"]


def test_partial_stage_failure_never_enters_targeted_repair(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CAPABILITY_ACCEPTANCE_MODE", "false")
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    incomplete = {
        "central_proposition": copy.deepcopy(skeleton["central_proposition"]),
        "scope": copy.deepcopy(skeleton["scope"]),
    }
    design = _flat_design_output(envelope)
    executor, gateway = _executor(
        tmp_path, [incomplete, skeleton, design]
    )

    result = asyncio.run(executor.execute(
        "P-ARGUMENT-ARCHITECTURE",
        envelope,
        project_id="project-1",
        workflow_id="wf-fragmented-repair-retry",
        call_key="outer-fragmented-repair-retry",
    ))

    assert result["status"] in {"PASS", "NEED_USER_INPUT"}
    assert len(gateway.calls) == 3
    assert all(
        call["prompt_id"] == "P-ARGUMENT-ARCHITECTURE"
        for call in gateway.calls
    )
    retry = gateway.calls[1]["envelope"]["retry_context"]
    assert retry["recovery_mode"] == "FULL_STAGE_RETRY"
    assert "previous_candidate" not in retry
    assert "repair_targets" not in gateway.calls[1]["envelope"]


def test_repeated_empty_stage_responses_exhaust_full_retries_without_targeted_repair(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CAPABILITY_ACCEPTANCE_MODE", "false")
    envelope = _argument_envelope_with_evidence()
    executor, gateway = _executor(tmp_path, [{}, {}, {}])

    with pytest.raises(Exception) as raised:
        asyncio.run(executor.execute(
            "P-ARGUMENT-ARCHITECTURE",
            envelope,
            project_id="project-1",
            workflow_id="wf-empty-skeleton-exhausted",
            call_key="outer-empty-skeleton-exhausted",
        ))

    classification = classify_runtime_failure(raised.value)
    assert classification.category == FailureCategory.OUTPUT_CONTRACT
    assert classification.retryable is False
    assert len(gateway.calls) == 3
    assert all(call["prompt_id"] == "P-ARGUMENT-ARCHITECTURE" for call in gateway.calls)
    assert all(
        call["route"].profile["argument_two_stage_internal_stage"]
        == ARGUMENT_SKELETON_STAGE
        for call in gateway.calls
    )
    assert [
        call["envelope"].get("retry_context", {}).get("recovery_mode")
        for call in gateway.calls
    ] == [None, "FULL_STAGE_RETRY", "FULL_STAGE_RETRY"]


def test_stage_retry_classifier_preserves_any_authored_partial_draft():
    assert not _stage_candidate_has_substantive_draft(ARGUMENT_SKELETON_STAGE, {})
    assert not _stage_candidate_has_substantive_draft(
        ARGUMENT_SKELETON_STAGE,
        {"evidence_gaps": [], "user_questions": [], "cannot_proceed_reason": None},
    )
    assert _stage_candidate_has_substantive_draft(
        ARGUMENT_SKELETON_STAGE,
        {"central_proposition": {"statement": "Keep this authored text."}},
    )
    assert _stage_candidate_has_substantive_draft(
        ARGUMENT_SKELETON_STAGE,
        {"user_questions": [{"question": "Keep this blocking question."}]},
    )
    assert _stage_candidate_has_substantive_draft(
        ARGUMENT_DESIGN_STAGE,
        {"methods": [{"method_statement": "Keep this method."}]},
    )


def test_stage_defaults_do_not_enrich_untyped_question_fragments():
    candidate = {
        "user_questions": [
            {"$text": "STRING"},
            {},
            {"question_type": "MISSING_INFORMATION"},
            {"question_type": "CHOICE"},
        ]
    }

    normalized = _normalize_stage_mechanical_defaults(candidate)

    assert normalized["user_questions"][0] == {"$text": "STRING"}
    assert normalized["user_questions"][1] == {}
    assert normalized["user_questions"][2] == {
        "question_type": "MISSING_INFORMATION",
        "allowed_values": [],
    }
    assert normalized["user_questions"][3] == {"question_type": "CHOICE"}
    assert candidate == {
        "user_questions": [
            {"$text": "STRING"},
            {},
            {"question_type": "MISSING_INFORMATION"},
            {"question_type": "CHOICE"},
        ]
    }


def test_user_question_gate_metadata_is_runtime_owned_and_gap_derived():
    candidate = {
        "evidence_gaps": [
            {
                "kind": "FOUNDATION",
                "thread_index": 0,
                "reason": "Foundation evidence is missing.",
                "blocking": True,
                "suggested_question": "Please provide foundation evidence.",
            }
        ],
        "user_questions": [
            {
                "target_area": "OTHER",
                "question_type": "CONFIRMATION",
                "question": "Please provide foundation evidence.",
                "reason": "Foundation evidence is missing.",
                "answer_shape": "OBJECT",
                "allowed_values": [],
                "blocking": False,
                "priority": "P3",
            },
            {
                "target_area": "FOUNDATION_EVIDENCE",
                "question_type": "MISSING_INFORMATION",
                "question": "Choose the evaluation mode.",
                "reason": "One mode is required.",
                "answer_shape": "OBJECT",
                "allowed_values": ["A", "B"],
                "blocking": False,
                "priority": "P0",
            },
        ],
        "cannot_proceed_reason": "provider-authored blocker",
    }

    projected = _project_stage_user_questions(ARGUMENT_DESIGN_STAGE, candidate)

    assert projected["user_questions"] == [
        {
            "target_area": "FOUNDATION_EVIDENCE",
            "question_type": "MISSING_INFORMATION",
            "question": "Please provide foundation evidence.",
            "reason": "Foundation evidence is missing.",
            "answer_shape": "STRING",
            "allowed_values": [],
            "blocking": True,
            "priority": "P0",
        },
        {
            "target_area": "RESEARCH_DESIGN",
            "question_type": "CHOICE",
            "question": "Choose the evaluation mode.",
            "reason": "One mode is required.",
            "answer_shape": "STRING",
            "allowed_values": ["A", "B"],
            "blocking": False,
            "priority": "P2",
        },
    ]
    assert projected["cannot_proceed_reason"] is None


def test_skeleton_collection_fragment_is_compacted_without_model_retry():
    candidate = {
        "central_proposition": {"statement": "Central proposition", "evidence_ids": []},
        "scope": {"in_scope": ["scope"], "out_of_scope": [], "boundary_conditions": []},
        "research_threads": [
            {
                "gap_statement": "Gap",
                "gap_evidence_ids": ["E1"],
                "limitation_mechanism_statement": "Limitation",
            },
            {"$text": "E1"},
            {"$text": "Research question?"},
            {"$text": "ENGINEERING"},
            {"$text": "DESIGN_VERIFIABLE"},
            {"item": ["Success evidence"]},
            {"$text": "Objective"},
            {"item": ["E1"]},
            {"item": ["Assumption"]},
            {"$text": "Falsification rule"},
        ],
        "evidence_gaps": [],
        "user_questions": [],
        "cannot_proceed_reason": None,
    }

    normalized = _normalize_stage_structural_artifacts(
        ARGUMENT_SKELETON_STAGE,
        candidate,
        frozen_skeleton=None,
    )

    assert len(normalized["research_threads"]) == 1
    assert normalized["research_threads"][0]["limitation_mechanism_evidence_ids"] == ["E1"]
    assert normalized["research_threads"][0]["question_statement"] == "Research question?"
    assert normalized["research_threads"][0]["falsification_or_comparison_rule"] == "Falsification rule"


def test_retry_default_provenance_allows_only_the_exact_default_value():
    previous = {
        "user_questions": [
            {
                "target_area": "RESEARCH_DESIGN",
                "question_type": "MISSING_INFORMATION",
                "question": "What information is missing?",
            }
        ]
    }
    _validation_projection, defaults = (
        _normalize_stage_mechanical_defaults_with_provenance(previous)
    )
    errors = [
        "/user_questions/0: 'reason' is a required property",
        "/user_questions/0: 'answer_shape' is a required property",
        "/user_questions/0: 'blocking' is a required property",
        "/user_questions/0: 'priority' is a required property",
    ]
    exact = {
        "user_questions": [
            {
                **previous["user_questions"][0],
                "reason": "Needed to continue.",
                "answer_shape": "STRING",
                "allowed_values": [],
                "blocking": True,
                "priority": "P0",
            }
        ]
    }
    nondefault = copy.deepcopy(exact)
    nondefault["user_questions"][0]["allowed_values"] = ["unauthorized"]

    assert merge_argument_stage_repair_candidate(
        previous,
        exact,
        errors,
        deterministic_defaults=defaults,
    ) == exact
    assert merge_argument_stage_repair_candidate(
        previous,
        nondefault,
        errors,
        deterministic_defaults=defaults,
    ) == nondefault


def test_skeleton_retry_compacts_fragmented_question_rows_without_default_pollution(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CAPABILITY_ACCEPTANCE_MODE", "false")
    envelope = _argument_envelope_with_evidence()
    valid_skeleton = _flat_skeleton_output(envelope)
    valid_skeleton["user_questions"] = [
        _stage_question("foundation"),
        _stage_question("metrics"),
        _stage_question(
            "decomposition",
            question_type="CHOICE",
            allowed_values=["retain", "adapt", "replace"],
        ),
        _stage_question(
            "constraints",
            question_type="CHOICE",
            allowed_values=["hard", "soft", "later"],
        ),
    ]
    malformed_skeleton = copy.deepcopy(valid_skeleton)
    malformed_skeleton["user_questions"] = _fragment_second_question(
        valid_skeleton["user_questions"]
    )
    thread = valid_skeleton["research_threads"][0]
    malformed_skeleton["research_threads"] = [
        {
            "gap_statement": thread["gap_statement"],
            "gap_evidence_ids": copy.deepcopy(thread["gap_evidence_ids"]),
            "limitation_mechanism_statement": thread[
                "limitation_mechanism_statement"
            ],
            "limitation_mechanism_evidence_ids": copy.deepcopy(
                thread["limitation_mechanism_evidence_ids"]
            ),
        },
        {"$text": thread["question_statement"]},
        {"$text": thread["question_type"]},
        {"$text": thread["answerability"]},
        {"item": copy.deepcopy(thread["success_evidence"])},
        {"$text": thread["objective_statement"]},
        {"item": copy.deepcopy(thread["objective_evidence_ids"])},
        {"item": copy.deepcopy(thread["assumptions"])},
        {"$text": thread["falsification_or_comparison_rule"]},
    ]
    design = _flat_design_output(envelope)
    executor, gateway = _executor(
        tmp_path,
        [
            malformed_skeleton,
            valid_skeleton,
            design,
        ],
    )

    result = asyncio.run(executor.execute(
        "P-ARGUMENT-ARCHITECTURE",
        envelope,
        project_id="project-1",
        workflow_id="wf-step4b-fragmented-skeleton-questions",
        call_key="outer-step4b-fragmented-skeleton-questions",
    ))

    assert result["status"] == "NEED_USER_INPUT"
    assert len(gateway.calls) == 3
    retry = gateway.calls[1]["envelope"]
    assert retry["retry_context"]["recovery_mode"] == "FULL_STAGE_RETRY"
    assert "previous_candidate" not in retry["retry_context"]
    assert "repair_targets" not in retry
    authored_questions = result["output"]["user_questions"]
    assert len(authored_questions) == 4
    assert [item["question"] for item in authored_questions] == [
        item["question"] for item in valid_skeleton["user_questions"]
    ]


def test_design_retry_compacts_fragmented_question_rows_without_default_pollution(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CAPABILITY_ACCEPTANCE_MODE", "false")
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    valid_design = _flat_design_output(envelope)
    valid_design["user_questions"] = [
        _stage_question("foundation"),
        _stage_question("metrics"),
        _stage_question(
            "decomposition",
            question_type="CHOICE",
            allowed_values=["retain", "adapt", "replace"],
        ),
        _stage_question(
            "constraints",
            question_type="CHOICE",
            allowed_values=["hard", "soft", "later"],
        ),
    ]
    malformed_design = copy.deepcopy(valid_design)
    malformed_design["user_questions"] = _fragment_second_question(
        valid_design["user_questions"]
    )
    executor, gateway = _executor(
        tmp_path, [skeleton, malformed_design, valid_design]
    )

    result = asyncio.run(executor.execute(
        "P-ARGUMENT-ARCHITECTURE",
        envelope,
        project_id="project-1",
        workflow_id="wf-step4b-fragmented-design-questions",
        call_key="outer-step4b-fragmented-design-questions",
    ))

    assert result["status"] == "NEED_USER_INPUT"
    assert len(gateway.calls) == 2
    authored_questions = result["output"]["user_questions"]
    assert len(authored_questions) == 3


def test_design_retry_repairs_draft_before_frozen_skeleton_deduplication(
    tmp_path, monkeypatch
):
    """Replay the failure mode where pre-merge dedupe discarded the repair."""

    monkeypatch.setenv("CAPABILITY_ACCEPTANCE_MODE", "false")
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    inherited_question = _stage_question("inherited")
    skeleton["user_questions"] = [copy.deepcopy(inherited_question)]

    malformed_design = _flat_design_output(envelope)
    malformed_design["user_questions"] = [
        {
            "target_area": inherited_question["target_area"],
            "question_type": inherited_question["question_type"],
        }
    ]
    repaired_design = _flat_design_output(envelope)
    repaired_design["user_questions"] = [copy.deepcopy(inherited_question)]

    executor, gateway = _executor(
        tmp_path, [skeleton, malformed_design, repaired_design]
    )
    result = asyncio.run(executor.execute(
        "P-ARGUMENT-ARCHITECTURE",
        envelope,
        project_id="project-1",
        workflow_id="wf-step4b-draft-before-dedupe",
        call_key="outer-step4b-draft-before-dedupe",
    ))

    assert result["status"] == "NEED_USER_INPUT"
    assert len(gateway.calls) == 2
    assert len(result["output"]["user_questions"]) == 1
    assert result["output"]["user_questions"][0]["question"] == inherited_question[
        "question"
    ]


def test_structurally_invalid_question_row_is_not_immutable_draft_content():
    previous = {
        "user_questions": [
            {
                "target_area": "RESEARCH_DESIGN",
                "question_type": "CHOICE",
            }
        ]
    }
    repaired = {
        "user_questions": [
            {
                "target_area": "FOUNDATION_EVIDENCE",
                "question_type": "MISSING_INFORMATION",
                "question": "Please provide the missing foundation evidence.",
                "reason": "The evidence is required to support the claim.",
                "answer_shape": "STRING",
                "allowed_values": [],
                "blocking": True,
                "priority": "P0",
            }
        ]
    }
    errors = [
        "/user_questions/0: 'question' is a required property",
        "/user_questions/0: 'reason' is a required property",
        "/user_questions/0: 'answer_shape' is a required property",
    ]

    assert merge_argument_stage_repair_candidate(previous, repaired, errors) == repaired


def test_step4b_runtime_drops_explicit_unknown_evidence_without_model_retry(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CAPABILITY_ACCEPTANCE_MODE", "false")
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    design = _flat_design_output(envelope)
    design["work_packages"][0]["evidence_ids"] = [
        "RC-PROJ-002",
        *design["work_packages"][0]["evidence_ids"],
    ]
    design["methods"][0]["evidence_ids"] = [
        *design["methods"][0]["evidence_ids"],
        "RC-PROJ-003",
    ]
    executor, gateway = _executor(tmp_path, [skeleton, design])

    result = asyncio.run(executor.execute(
        "P-ARGUMENT-ARCHITECTURE",
        envelope,
        project_id="project-1",
        workflow_id="wf-step4b-reference-prune",
        call_key="outer-step4b-reference-prune",
    ))

    assert result["status"] in {"PASS", "NEED_USER_INPUT"}
    assert len(gateway.calls) == 2
    authored = result["output"]["result"]["authored_state"]
    assert "RC-PROJ-002" not in json.dumps(authored, ensure_ascii=False)
    assert "RC-PROJ-003" not in json.dumps(authored, ensure_ascii=False)


def test_design_retry_uses_reference_pruned_structural_candidate(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CAPABILITY_ACCEPTANCE_MODE", "false")
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    valid_design = _flat_design_output(envelope)
    broken_design = copy.deepcopy(valid_design)
    broken_design["methods"][0]["method_type"] = "NOT_A_METHOD_TYPE"
    broken_design["work_packages"][0]["evidence_ids"] = [
        "UNKNOWN-RETRY-EVIDENCE",
        *broken_design["work_packages"][0]["evidence_ids"],
    ]
    executor, gateway = _executor(
        tmp_path,
        [
            skeleton,
            broken_design,
            valid_design,
        ],
    )

    result = asyncio.run(executor.execute(
        "P-ARGUMENT-ARCHITECTURE",
        envelope,
        project_id="project-1",
        workflow_id="wf-step4b-prune-before-retry",
        call_key="outer-step4b-prune-before-retry",
    ))

    assert result["status"] in {"PASS", "NEED_USER_INPUT"}
    assert len(gateway.calls) == 3
    retry_input = gateway.calls[2]["envelope"]
    assert retry_input["retry_context"]["recovery_mode"] == "FULL_STAGE_RETRY"
    assert "previous_candidate" not in retry_input["retry_context"]
    assert "UNKNOWN-RETRY-EVIDENCE" not in json.dumps(retry_input)
    assert "UNKNOWN-RETRY-EVIDENCE" not in json.dumps(result["output"])


def test_argument_stage_repair_scope_is_minimal_and_root_errors_fail_closed():
    errors = [
        "/: Additional properties are not allowed ('evidence_ids', 'item', 'statement' were unexpected)",
        "/: 'innovation_evaluation_refs' is a required property",
        "/theoretical_properties/5: 'property_index' is a required property",
        "/theoretical_properties/5: 'statement' is a required property",
        "/theoretical_properties/5: 'evidence_ids' is a required property",
        "/: opaque cross-stage failure",
    ]

    assert set(argument_stage_repair_scope_paths(errors)) == {
        ("evidence_ids",),
        ("item",),
        ("statement",),
        ("innovation_evaluation_refs",),
        ("theoretical_properties", "5"),
    }
    assert argument_stage_repair_scope_paths(
        ["/: opaque cross-stage failure"]
    ) == ()


def test_stage_repair_null_deletes_only_uncontracted_properties():
    candidate = {
        "item": "invalid root transport property",
        "cannot_proceed_reason": "authored blocker",
    }
    repaired = _apply_stage_repair_changes(
        stage=ARGUMENT_SKELETON_STAGE,
        candidate=candidate,
        repair_output=_repair_response(
            ("/item", None),
            ("/cannot_proceed_reason", None),
        ),
        scopes=(("item",), ("cannot_proceed_reason",)),
    )

    assert "item" not in repaired
    assert "cannot_proceed_reason" in repaired
    assert repaired["cannot_proceed_reason"] is None


def test_argument_stage_repair_restores_unreported_success_criteria_regression():
    previous = {
        "evidence_ids": ["unexpected-root-field"],
        "item": "unexpected-root-field",
        "statement": "unexpected-root-field",
        "innovation_evaluation_refs": [],
        "theoretical_properties": [
            {
                "thread_index": 2,
                "work_package_index": 0,
                "method_index": 0,
            }
        ],
        "evaluations": [
            {
                "thread_index": 2,
                "work_package_index": 0,
                "method_index": 0,
                "evaluation_index": 0,
                "statement": "验证候选生成质量。",
                "evidence_ids": ["RC-PROJ-001", "METRIC-PROJ-001"],
                "success_criteria": [
                    "候选多样性可被量化且在多约束条件下保持",
                    "候选表达标注完整且不确定性不被掩盖",
                ],
            }
        ],
    }
    repaired = copy.deepcopy(previous)
    for key in ("evidence_ids", "item", "statement"):
        repaired.pop(key)
    repaired["innovation_evaluation_refs"] = [
        {"thread_index": 2, "innovation_index": 0, "work_package_index": 0}
    ]
    repaired["theoretical_properties"][0].update(
        {
            "property_index": 0,
            "statement": "候选生成保持约束可行性。",
            "evidence_ids": ["RC-PROJ-001"],
        }
    )
    criteria = repaired["evaluations"][0].pop("success_criteria")
    repaired["evaluations"][0]["evidence_ids"].extend(criteria)

    errors = [
        "/: Additional properties are not allowed ('evidence_ids', 'item', 'statement' were unexpected)",
        "/: 'innovation_evaluation_refs' is a required property",
        "/theoretical_properties/0: 'property_index' is a required property",
        "/theoretical_properties/0: 'statement' is a required property",
        "/theoretical_properties/0: 'evidence_ids' is a required property",
    ]
    merged = merge_argument_stage_repair_candidate(previous, repaired, errors)

    assert "evidence_ids" not in merged
    assert "item" not in merged
    assert "statement" not in merged
    assert merged["innovation_evaluation_refs"] == repaired[
        "innovation_evaluation_refs"
    ]
    assert merged["theoretical_properties"] == repaired[
        "theoretical_properties"
    ]
    assert merged["evaluations"] == previous["evaluations"]


def test_argument_stage_repair_does_not_apply_positional_patch_after_row_reorder():
    previous = {
        "evaluations": [
            {
                "thread_index": 0,
                "evaluation_index": 0,
                "statement": "first",
                "success_criteria": [],
            },
            {
                "thread_index": 0,
                "evaluation_index": 1,
                "statement": "second",
                "success_criteria": ["already valid"],
            },
        ]
    }
    repaired = {"evaluations": list(reversed(copy.deepcopy(previous["evaluations"])))}
    repaired["evaluations"][0]["success_criteria"] = ["wrong row"]

    merged = merge_argument_stage_repair_candidate(
        previous,
        repaired,
        ["/evaluations/0/success_criteria: [] should be non-empty"],
    )

    assert merged == previous


def test_argument_stage_repair_discards_unreported_appended_rows():
    previous = {
        "theoretical_properties": [
            {
                "thread_index": 1,
                "work_package_index": 0,
                "method_index": 0,
            }
        ]
    }
    repaired = {
        "theoretical_properties": [
            {
                "thread_index": 1,
                "work_package_index": 0,
                "method_index": 0,
                "property_index": 0,
                "statement": "authorized repair",
                "evidence_ids": ["RC-PROJ-001"],
            },
            {
                "thread_index": 2,
                "work_package_index": 0,
                "method_index": 0,
                "property_index": 0,
                "statement": "unreported appended row",
                "evidence_ids": ["RC-PROJ-001"],
            },
        ]
    }
    errors = [
        "/theoretical_properties/0: 'property_index' is a required property",
        "/theoretical_properties/0: 'statement' is a required property",
        "/theoretical_properties/0: 'evidence_ids' is a required property",
    ]

    merged = merge_argument_stage_repair_candidate(previous, repaired, errors)

    assert merged["theoretical_properties"] == repaired["theoretical_properties"][:1]


def test_argument_stage_repair_discards_unreported_object_rows_at_any_position():
    previous = {
        "methods": [
            {"thread_index": 0, "work_package_index": 0},
            {
                "thread_index": 0,
                "work_package_index": 1,
                "method_index": 0,
                "statement": "stable-b",
            },
        ]
    }
    repaired = {
        "methods": [
            {
                "thread_index": 9,
                "work_package_index": 9,
                "method_index": 0,
                "statement": "discard-prefix",
            },
            {
                "thread_index": 0,
                "work_package_index": 0,
                "method_index": 0,
                "statement": "authorized-a",
            },
            {
                "thread_index": 8,
                "work_package_index": 8,
                "method_index": 0,
                "statement": "discard-middle",
            },
            {
                "thread_index": 0,
                "work_package_index": 1,
                "method_index": 0,
                "statement": "stable-b",
            },
        ]
    }
    errors = [
        "/methods/0: 'method_index' is a required property",
        "/methods/0: 'statement' is a required property",
    ]

    assert merge_argument_stage_repair_candidate(previous, repaired, errors) == {
        "methods": [repaired["methods"][1], repaired["methods"][3]]
    }


def test_argument_stage_repair_replaces_or_deletes_any_structurally_invalid_row():
    assert merge_argument_stage_repair_candidate(
        {"methods": [{}]},
        {"methods": []},
        ["/methods/0: 'thread_index' is a required property"],
    ) == {"methods": []}

    previous_with_valid_content = {"methods": [{"statement": "preserve me"}]}
    assert merge_argument_stage_repair_candidate(
        previous_with_valid_content,
        {"methods": []},
        ["/methods/0: 'thread_index' is a required property"],
    ) == {"methods": []}

    assert merge_argument_stage_repair_candidate(
        {"foundation_supports": [{"thread_index": 0}]},
        {"foundation_supports": []},
        ["/foundation_supports/0: unresolved parent index (0, 0)"],
    ) == {"foundation_supports": []}


def test_argument_stage_repair_accepts_reported_array_item_deletion_with_row_repairs():
    previous = {
        "research_threads": [
            {
                "gap_statement": "stable row identity",
                "limitation_mechanism_evidence_ids": [
                    "RC-PROJ-001",
                    {"question_statement": "misnested thread"},
                ],
            }
        ]
    }
    repaired = {
        "research_threads": [
            {
                "gap_statement": "stable row identity",
                "limitation_mechanism_evidence_ids": ["RC-PROJ-001"],
                "question_statement": "restored question",
                "question_type": "ENGINEERING",
            }
        ]
    }
    errors = [
        "/research_threads/0: 'question_statement' is a required property",
        "/research_threads/0: 'question_type' is a required property",
        "/research_threads/0/limitation_mechanism_evidence_ids/1: "
        "{'question_statement': 'misnested thread'} is not of type 'string'",
    ]

    assert merge_argument_stage_repair_candidate(previous, repaired, errors) == repaired


def test_argument_stage_repair_deletes_multiple_scoped_scalar_items_without_index_drift():
    previous = {
        "evidence_ids": [
            "bad-start",
            "keep-a",
            "bad-middle",
            "keep-b",
            "bad-end",
        ]
    }
    repaired = {"evidence_ids": ["keep-a", "keep-b"]}
    errors = [
        "/evidence_ids/0: evidence_id 'bad-start' is not present in evidence_cards",
        "/evidence_ids/2: evidence_id 'bad-middle' is not present in evidence_cards",
        "/evidence_ids/4: evidence_id 'bad-end' is not present in evidence_cards",
    ]

    assert merge_argument_stage_repair_candidate(previous, repaired, errors) == repaired


def test_argument_stage_repair_replaces_one_scoped_scalar_and_discards_append():
    previous = {"evidence_ids": ["keep-a", "bad", "keep-b"]}
    repaired = {
        "evidence_ids": ["keep-a", "replacement", "keep-b", "unreported-append"]
    }

    assert merge_argument_stage_repair_candidate(
        previous,
        repaired,
        ["/evidence_ids/1: evidence_id 'bad' is not present in evidence_cards"],
    ) == {"evidence_ids": ["keep-a", "replacement", "keep-b"]}


@pytest.mark.parametrize(
    "repaired",
    [
        {"evidence_ids": ["keep-b", "replacement", "keep-a"]},
        {"evidence_ids": ["changed-a", "replacement", "keep-b"]},
        {"evidence_ids": ["inserted", "keep-a", "replacement", "keep-b"]},
    ],
)
def test_argument_stage_repair_rejects_unscoped_scalar_reorder_change_or_prefix_insert(
    repaired,
):
    previous = {"evidence_ids": ["keep-a", "bad", "keep-b"]}

    assert merge_argument_stage_repair_candidate(
        previous,
        repaired,
        ["/evidence_ids/1: evidence_id 'bad' is not present in evidence_cards"],
    ) == previous


def test_argument_stage_repair_deletes_fully_scoped_middle_object_row_without_shift():
    previous = {
        "methods": [
            {"method_index": 0, "statement": "keep-a"},
            {"method_index": 1, "statement": "bad"},
            {"method_index": 2, "statement": "keep-b"},
        ]
    }
    repaired = {
        "methods": [
            {"method_index": 0, "statement": "keep-a"},
            {"method_index": 2, "statement": "keep-b"},
        ]
    }

    assert merge_argument_stage_repair_candidate(
        previous,
        repaired,
        ["/methods/1: unresolved parent index (0, 1)"],
    ) == repaired


def test_argument_stage_repair_handles_nested_middle_scalar_deletion_in_object_row():
    previous = {
        "evaluations": [
            {
                "evaluation_index": 0,
                "statement": "stable identity",
                "evidence_ids": ["keep-a", "bad", "keep-b"],
                "success_criteria": ["keep criterion"],
            }
        ]
    }
    repaired = copy.deepcopy(previous)
    repaired["evaluations"][0]["evidence_ids"] = ["keep-a", "keep-b"]

    assert merge_argument_stage_repair_candidate(
        previous,
        repaired,
        [
            "/evaluations/0/evidence_ids/1: evidence_id 'bad' is not present in evidence_cards"
        ],
    ) == repaired


def test_unknown_evidence_pruner_deletes_only_exact_reported_values_in_reverse_order():
    candidate = {
        "work_packages": [
            {
                "evidence_ids": [
                    "bad-start",
                    "keep-a",
                    "bad-middle",
                    "keep-b",
                    "bad-end",
                ]
            }
        ]
    }
    errors = [
        "/work_packages/0/evidence_ids/0: evidence_id 'bad-start' is not present in evidence_cards",
        "/work_packages/0/evidence_ids/2: evidence_id 'bad-middle' is not present in evidence_cards",
        "/work_packages/0/evidence_ids/4: evidence_id 'bad-end' is not present in evidence_cards",
    ]

    repaired, resolved = _drop_reported_unknown_evidence_ids(candidate, errors)

    assert candidate["work_packages"][0]["evidence_ids"] == [
        "bad-start",
        "keep-a",
        "bad-middle",
        "keep-b",
        "bad-end",
    ]
    assert repaired == {
        "work_packages": [{"evidence_ids": ["keep-a", "keep-b"]}]
    }
    assert resolved == errors


def test_unknown_evidence_pruner_preserves_mismatched_or_non_evidence_paths():
    candidate = {
        "evidence_ids": ["actual"],
        "success_criteria": ["bad"],
    }
    errors = [
        "/evidence_ids/0: evidence_id 'different' is not present in evidence_cards",
        "/success_criteria/0: evidence_id 'bad' is not present in evidence_cards",
    ]

    repaired, resolved = _drop_reported_unknown_evidence_ids(candidate, errors)

    assert repaired == candidate
    assert resolved == []


def test_skeleton_normalizer_lifts_exact_completion_object_from_evidence_array():
    envelope = _argument_envelope_with_evidence()
    expected = _flat_skeleton_output(envelope)
    malformed = copy.deepcopy(expected)
    thread = malformed["research_threads"][0]
    completion_fields = {
        "question_statement",
        "question_type",
        "answerability",
        "success_evidence",
        "objective_statement",
        "objective_evidence_ids",
        "assumptions",
        "falsification_or_comparison_rule",
    }
    displaced = {field: thread.pop(field) for field in completion_fields}
    for field in ("success_evidence", "objective_evidence_ids", "assumptions"):
        displaced[field] = {"item": displaced[field]}
    thread["limitation_mechanism_evidence_ids"] = [
        *thread["limitation_mechanism_evidence_ids"],
        displaced,
    ]

    assert _normalize_skeleton_mechanical_artifacts(malformed) == expected


def test_skeleton_normalizer_lifts_exact_wrapped_completion_object():
    envelope = _argument_envelope_with_evidence()
    expected = _flat_skeleton_output(envelope)
    malformed = copy.deepcopy(expected)
    thread = malformed["research_threads"][0]
    completion_fields = {
        "question_statement",
        "question_type",
        "answerability",
        "success_evidence",
        "objective_statement",
        "objective_evidence_ids",
        "assumptions",
        "falsification_or_comparison_rule",
    }
    displaced = {field: thread.pop(field) for field in completion_fields}
    evidence_ids = thread["limitation_mechanism_evidence_ids"]
    thread["limitation_mechanism_evidence_ids"] = {
        "item": evidence_ids[0] if len(evidence_ids) == 1 else evidence_ids,
        "limitation_mechanism_evidence_ids": displaced,
    }

    assert _normalize_skeleton_mechanical_artifacts(malformed) == expected


def test_skeleton_normalizer_preserves_ambiguous_displaced_payload():
    candidate = {
        "research_threads": [
            {
                "gap_statement": "gap",
                "gap_evidence_ids": [],
                "limitation_mechanism_statement": "limitation",
                "limitation_mechanism_evidence_ids": [
                    "RC-PROJ-001",
                    {"question_statement": "only one displaced field"},
                ],
            }
        ]
    }

    assert _normalize_skeleton_mechanical_artifacts(candidate) == candidate


def test_argument_stage_repair_ambiguous_root_failure_accepts_no_changes():
    previous = {"evaluations": [{"statement": "keep"}]}
    repaired = {"evaluations": [{"statement": "rewrite"}], "new": True}

    assert merge_argument_stage_repair_candidate(
        previous,
        repaired,
        ["/: opaque cross-stage failure"],
    ) == previous


def test_step4b_runtime_exhausted_design_failure_is_nonretryable_contract_failure(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CAPABILITY_ACCEPTANCE_MODE", "false")
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    broken = _flat_design_output(envelope)
    broken["methods"][0]["work_package_index"] = 99
    executor, gateway = _executor(
        tmp_path, [skeleton, broken, broken, broken]
    )

    with pytest.raises(Exception) as raised:
        asyncio.run(executor.execute(
            "P-ARGUMENT-ARCHITECTURE",
            envelope,
            project_id="project-1",
            workflow_id="wf-step4b-fail",
            call_key="outer-step4b-fail",
        ))

    classification = classify_runtime_failure(raised.value)
    assert classification.category == FailureCategory.OUTPUT_CONTRACT
    assert classification.retryable is False
    assert len(gateway.calls) == 4
    assert gateway.calls[1]["envelope"]["frozen_skeleton"] == skeleton
    assert all(
        call["prompt_id"] == "P-ARGUMENT-ARCHITECTURE"
        for call in gateway.calls
    )
    assert all(
        call["envelope"]["frozen_skeleton"] == skeleton
        for call in gateway.calls[1:]
    )


def test_step4b_provider_request_identity_includes_two_stage_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPABILITY_ACCEPTANCE_MODE", "false")
    executor, _ = _executor(tmp_path, [])
    spec = executor._model_request_spec("P-ARGUMENT-ARCHITECTURE")

    contract = spec["argument_two_stage_contract"]
    assert contract["version"] == "ARGUMENT_TWO_STAGE_V15"
    assert set(contract["stages"]) == {"SKELETON", "DESIGN"}
    assert contract["stages"]["SKELETON"]["desired_output_tokens"] == 8_192
    assert contract["stages"]["DESIGN"]["desired_output_tokens"] == 65_536
    assert contract["stages"]["SKELETON"]["output_schema"]["properties"]["research_threads"]["maxItems"] == 4


def test_step4b_stage_call_identity_changes_with_actual_stage_input(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CAPABILITY_ACCEPTANCE_MODE", "false")
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    executor, gateway = _executor(tmp_path, [skeleton, skeleton])
    route = _ArgumentRuntimeRouter().route("P-ARGUMENT-ARCHITECTURE", envelope)

    first = _RuntimeArgumentStageGateway(
        executor,
        route=route,
        outer_call_key="call-provider-input-cycle-a-attempt-1",
    )
    asyncio.run(first.invoke_stage(
        ARGUMENT_SKELETON_STAGE,
        {"skeleton_seed": {"revision": 1}},
        argument_stage_output_schema(ARGUMENT_SKELETON_STAGE),
        desired_output_tokens=8_192,
    ))
    first_key = gateway.calls[-1]["call_key"]

    second = _RuntimeArgumentStageGateway(
        executor,
        route=route,
        outer_call_key="call-provider-input-cycle-a-attempt-2",
    )
    asyncio.run(second.invoke_stage(
        ARGUMENT_SKELETON_STAGE,
        {"skeleton_seed": {"revision": 2}},
        argument_stage_output_schema(ARGUMENT_SKELETON_STAGE),
        desired_output_tokens=8_192,
    ))
    second_key = gateway.calls[-1]["call_key"]

    assert first_key != second_key


def test_step4b_stage_call_identity_reuses_success_but_refreshes_failed_stage(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CAPABILITY_ACCEPTANCE_MODE", "false")
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    design = _flat_design_output(envelope)
    executor, gateway = _executor(tmp_path, [skeleton, skeleton, RuntimeError("design transport"), design])
    route = _ArgumentRuntimeRouter().route("P-ARGUMENT-ARCHITECTURE", envelope)

    first = _RuntimeArgumentStageGateway(
        executor,
        route=route,
        outer_call_key="call-provider-x-cycle-y-attempt-1",
    )
    asyncio.run(first.invoke_stage(
        ARGUMENT_SKELETON_STAGE,
        {"skeleton_seed": {}},
        argument_stage_output_schema(ARGUMENT_SKELETON_STAGE),
        desired_output_tokens=8_192,
    ))
    skeleton_key = gateway.calls[-1]["call_key"]
    gateway.evidence_store.write_request(skeleton_key, {"stage": "SKELETON"})
    gateway.evidence_store.write_response(
        skeleton_key,
        raw_text=json.dumps(skeleton, ensure_ascii=False),
        parsed_output=skeleton,
        raw_parsed_output=skeleton,
        metadata={"model_id": "offline-general-primary", "endpoint_id": "offline-primary"},
    )

    second = _RuntimeArgumentStageGateway(
        executor,
        route=route,
        outer_call_key="call-provider-x-cycle-y-attempt-2",
    )
    asyncio.run(second.invoke_stage(
        ARGUMENT_SKELETON_STAGE,
        {"skeleton_seed": {}},
        argument_stage_output_schema(ARGUMENT_SKELETON_STAGE),
        desired_output_tokens=8_192,
    ))
    assert gateway.calls[-1]["call_key"] == skeleton_key

    failing = _RuntimeArgumentStageGateway(
        executor,
        route=route,
        outer_call_key="call-provider-z-cycle-q-attempt-1",
    )
    with pytest.raises(RuntimeError, match="design transport"):
        asyncio.run(failing.invoke_stage(
            ARGUMENT_DESIGN_STAGE,
            {"frozen_skeleton": skeleton},
            argument_stage_output_schema(ARGUMENT_DESIGN_STAGE),
            desired_output_tokens=65_536,
        ))
    failed_design_key = gateway.calls[-1]["call_key"]
    gateway.evidence_store.write_request(failed_design_key, {"stage": "DESIGN"})
    gateway.evidence_store.write_failed_response(
        failed_design_key,
        rejected_text=None,
        metadata={"error": "design transport", "failure_kind": "TRANSPORT"},
    )

    retried = _RuntimeArgumentStageGateway(
        executor,
        route=route,
        outer_call_key="call-provider-z-cycle-q-attempt-2",
    )
    asyncio.run(retried.invoke_stage(
        ARGUMENT_DESIGN_STAGE,
        {"frozen_skeleton": skeleton},
        argument_stage_output_schema(ARGUMENT_DESIGN_STAGE),
        desired_output_tokens=65_536,
    ))
    assert gateway.calls[-1]["call_key"] != failed_design_key


def test_step4b_outer_retry_reuses_retry_specific_successful_skeleton(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CAPABILITY_ACCEPTANCE_MODE", "false")
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    executor, gateway = _executor(
        tmp_path,
        [
            RuntimeError("skeleton transport"),
            skeleton,
            RuntimeError("design transport"),
            skeleton,
        ],
    )
    route = _ArgumentRuntimeRouter().route("P-ARGUMENT-ARCHITECTURE", envelope)

    first = _RuntimeArgumentStageGateway(
        executor,
        route=route,
        outer_call_key="call-provider-chain-cycle-a-attempt-1",
    )
    with pytest.raises(RuntimeError, match="skeleton transport"):
        asyncio.run(first.invoke_stage(
            ARGUMENT_SKELETON_STAGE,
            {"skeleton_seed": {}},
            argument_stage_output_schema(ARGUMENT_SKELETON_STAGE),
            desired_output_tokens=8_192,
        ))
    failed_stable_skeleton_key = gateway.calls[-1]["call_key"]
    gateway.evidence_store.write_request(
        failed_stable_skeleton_key, {"stage": "SKELETON"}
    )
    gateway.evidence_store.write_failed_response(
        failed_stable_skeleton_key,
        rejected_text=None,
        metadata={"error": "skeleton transport", "failure_kind": "TRANSPORT"},
    )

    second = _RuntimeArgumentStageGateway(
        executor,
        route=route,
        outer_call_key="call-provider-chain-cycle-a-attempt-2",
    )
    asyncio.run(second.invoke_stage(
        ARGUMENT_SKELETON_STAGE,
        {"skeleton_seed": {}},
        argument_stage_output_schema(ARGUMENT_SKELETON_STAGE),
        desired_output_tokens=8_192,
    ))
    successful_retry_skeleton_key = gateway.calls[-1]["call_key"]
    assert successful_retry_skeleton_key != failed_stable_skeleton_key
    gateway.evidence_store.write_request(
        successful_retry_skeleton_key, {"stage": "SKELETON"}
    )
    gateway.evidence_store.write_response(
        successful_retry_skeleton_key,
        raw_text=json.dumps(skeleton, ensure_ascii=False),
        parsed_output=skeleton,
        raw_parsed_output=skeleton,
        metadata={
            "model_id": "offline-general-primary",
            "endpoint_id": "offline-primary",
        },
    )

    with pytest.raises(RuntimeError, match="design transport"):
        asyncio.run(second.invoke_stage(
            ARGUMENT_DESIGN_STAGE,
            {"frozen_skeleton": skeleton},
            argument_stage_output_schema(ARGUMENT_DESIGN_STAGE),
            desired_output_tokens=65_536,
        ))
    failed_design_key = gateway.calls[-1]["call_key"]
    gateway.evidence_store.write_request(failed_design_key, {"stage": "DESIGN"})
    gateway.evidence_store.write_failed_response(
        failed_design_key,
        rejected_text=None,
        metadata={"error": "design transport", "failure_kind": "TRANSPORT"},
    )

    third = _RuntimeArgumentStageGateway(
        executor,
        route=route,
        outer_call_key="call-provider-chain-cycle-a-attempt-3",
    )
    asyncio.run(third.invoke_stage(
        ARGUMENT_SKELETON_STAGE,
        {"skeleton_seed": {}},
        argument_stage_output_schema(ARGUMENT_SKELETON_STAGE),
        desired_output_tokens=8_192,
    ))
    assert gateway.calls[-1]["call_key"] == successful_retry_skeleton_key


def test_step4b_external_workflow_still_exposes_one_argument_producer():
    steps = WORKFLOWS["WF-4_PROPOSAL_AUTHORING"]
    prompt_ids = [step.get("prompt_id") for step in steps if step.get("prompt_id")]
    assert prompt_ids.count("P-ARGUMENT-ARCHITECTURE") == 1
    assert not any(
        prompt_id in {"P-ARGUMENT-SKELETON", "P-ARGUMENT-DESIGN"}
        for prompt_id in prompt_ids
    )



def test_step4c_live_argument_two_stage_is_independent_of_legacy_semantic_registry(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CAPABILITY_ACCEPTANCE_MODE", "false")
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    design = _flat_design_output(envelope)
    executor, gateway = _executor(tmp_path, [skeleton, design])
    monkeypatch.setattr(executor, "_uses_semantic_model_contract", lambda _prompt_id: False)

    spec = executor._model_request_spec("P-ARGUMENT-ARCHITECTURE")
    assert spec["semantic_model_contract"]["enabled"] is False
    assert spec["argument_two_stage_contract"]["version"] == "ARGUMENT_TWO_STAGE_V15"
    assert spec["prompt_text"] is None
    assert spec["output_schema"] is None

    result = asyncio.run(executor.execute(
        "P-ARGUMENT-ARCHITECTURE",
        envelope,
        project_id="project-1",
        workflow_id="wf-step4c-no-fallback",
        call_key="outer-step4c-no-fallback",
    ))
    assert result["status"] in {"PASS", "NEED_USER_INPUT"}
    assert [call["envelope"].get("frozen_skeleton") is not None for call in gateway.calls] == [False, True]
    assert len(gateway.calls) == 2


def test_step4c_online_public_safety_guard_receives_exact_stage_payloads(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CAPABILITY_ACCEPTANCE_MODE", "false")
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    design = _flat_design_output(envelope)
    executor, gateway = _executor(
        tmp_path, [skeleton, design], environment="ONLINE_PUBLIC"
    )
    checked = []

    def _spy(payload, project_config):
        checked.append(copy.deepcopy(payload))

    monkeypatch.setattr("app.runtime_executor.assert_online_payload_safe", _spy)
    result = asyncio.run(executor.execute(
        "P-ARGUMENT-ARCHITECTURE",
        envelope,
        project_id="project-1",
        workflow_id="wf-step4c-online",
        call_key="outer-step4c-online",
    ))

    assert result["status"] in {"PASS", "NEED_USER_INPUT"}
    assert checked == [call["envelope"] for call in gateway.calls]
    assert len(checked) == 2
    # The two-stage semantic builders are themselves the outbound business
    # projection: canonical runtime wrappers are not sent to either stage.
    assert all("prompt_id" not in payload for payload in checked)
    assert all("trusted_source_catalog" not in payload for payload in checked)


def test_step4c_transport_failure_persists_stage_audit_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPABILITY_ACCEPTANCE_MODE", "false")
    envelope = _argument_envelope_with_evidence()
    executor, _ = _executor(tmp_path, [LLMError("simulated skeleton transport failure")])

    with pytest.raises(Exception):
        asyncio.run(executor.execute(
            "P-ARGUMENT-ARCHITECTURE",
            envelope,
            project_id="project-1",
            workflow_id="wf-step4c-transport",
            call_key="outer-step4c-transport",
        ))

    row = executor.db.fetchone(
        "SELECT content_json FROM artifacts "
        "WHERE prompt_id='P-ARGUMENT-ARCHITECTURE' AND artifact_type='PROMPT_TRACE' "
        "AND status='ERROR' ORDER BY version DESC LIMIT 1"
    )
    assert row is not None
    trace = json.loads(row["content_json"])
    two_stage = trace["model_call_evidence"]["argument_two_stage"]
    assert two_stage["stage_invocations"] == 1
    assert two_stage["provider_attempts"] is None
    assert two_stage["provider_attempts_known"] == 0
    assert two_stage["provider_attempts_complete"] is False
    assert two_stage["stage_calls"][0]["provider_attempts"] is None
    assert two_stage["stage_calls"][0]["stage"] == "SKELETON"
    assert two_stage["stage_calls"][0]["outcome"] == "ERROR"
    assert two_stage["provider_or_outbound_failure"]["error_type"] == "LLMError"


def test_step4c_provider_attempts_count_only_current_non_replayed_provider_work(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CAPABILITY_ACCEPTANCE_MODE", "false")
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    design = _flat_design_output(envelope)

    skeleton_result = SimpleNamespace(
        output=skeleton,
        raw_text=json.dumps(skeleton, ensure_ascii=False),
        model_id="offline-general-primary",
        endpoint_id="offline-primary",
        reused_response=True,
        provider_attempts=4,
        response_contract_mode="TEST_REPLAY",
        evidence={},
    )
    design_result = SimpleNamespace(
        output=design,
        raw_text=json.dumps(design, ensure_ascii=False),
        model_id="offline-general-primary",
        endpoint_id="offline-primary",
        reused_response=False,
        provider_attempts=3,
        response_contract_mode="TEST_LIVE",
        evidence={},
    )
    executor, _ = _executor(tmp_path, [skeleton_result, design_result])

    result = asyncio.run(executor.execute(
        "P-ARGUMENT-ARCHITECTURE",
        envelope,
        project_id="project-1",
        workflow_id="wf-step4c-attempts",
        call_key="outer-step4c-attempts",
    ))
    assert result["status"] in {"PASS", "NEED_USER_INPUT"}

    row = executor.db.fetchone(
        "SELECT content_json FROM artifacts "
        "WHERE prompt_id='P-ARGUMENT-ARCHITECTURE' AND artifact_type='PROMPT_TRACE' "
        "AND status!='ERROR' ORDER BY version DESC LIMIT 1"
    )
    assert row is not None
    trace = json.loads(row["content_json"])
    two_stage = trace["model_call_evidence"]["argument_two_stage"]
    assert two_stage["stage_invocations"] == 2
    assert two_stage["provider_attempts"] == 3
    assert two_stage["provider_attempts_known"] == 3
    assert two_stage["provider_attempts_complete"] is True
    assert two_stage["stage_calls"][0]["reported_provider_attempts"] == 4
    assert two_stage["stage_calls"][0]["provider_attempts"] == 0
    assert two_stage["stage_calls"][1]["provider_attempts"] == 3



def test_step4c_two_stage_runtime_output_composes_with_critic_and_targeted_repair(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CAPABILITY_ACCEPTANCE_MODE", "false")
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    design = _flat_design_output(envelope)
    executor, _ = _executor(tmp_path, [skeleton, design])

    produced = asyncio.run(executor.execute(
        "P-ARGUMENT-ARCHITECTURE",
        envelope,
        project_id="project-1",
        workflow_id="wf-step4c-compose-producer",
        call_key="outer-step4c-compose-producer",
    ))
    canonical = produced["output"]

    lifecycle_dir = tmp_path / "lifecycle"
    lifecycle_dir.mkdir()
    harness = _LifecycleHarness(lifecycle_dir, canonical, envelope)
    wf = harness.workflow()
    state = {"options": {"targeted_repair_contract_retry_limit": 0}}
    critic_input, _, _ = _critic_context(canonical, envelope)
    assert harness.pack.validate(CRITIC, "input", critic_input) == []

    repaired = asyncio.run(harness._auto_repair(
        wf,
        CRITIC,
        critic_input,
        {"status": "REVISE", "findings": [_scope_finding()]},
        state,
    ))
    assert repaired is not None

    persisted_state = harness.workflow()["state"]
    value = harness.context_builder._repair_override(
        persisted_state, "P-ARGUMENT-ARCHITECTURE", workflow_id="wf-v9"
    )
    assert value["authored_state"]["scope"]["in_scope"] == ["仅保留动态重规划核心问题"]

    checkpoint = harness._workflow_repair_rereview_checkpoint(persisted_state, CRITIC)
    assert checkpoint is not None
    assert harness._start_repair_rereview(
        persisted_state, checkpoint, critic_prompt=CRITIC
    ) == 1
    round_tripped = json.loads(json.dumps(persisted_state, ensure_ascii=False))
    checkpoint2 = harness._workflow_repair_rereview_checkpoint(round_tripped, CRITIC)
    assert checkpoint2 is not None
    assert harness._start_repair_rereview(
        round_tripped, checkpoint2, critic_prompt=CRITIC
    ) == 1

def test_step4d_original_producer_regeneration_returns_to_two_stage_runtime(tmp_path, monkeypatch):
    from app.workflows import WorkflowEngine

    monkeypatch.setenv("CAPABILITY_ACCEPTANCE_MODE", "false")
    envelope = _argument_envelope_with_evidence()
    skeleton1 = _flat_skeleton_output(envelope)
    design1 = _flat_design_output(envelope)
    skeleton2 = copy.deepcopy(skeleton1)
    skeleton2["central_proposition"]["statement"] = skeleton1["central_proposition"]["statement"] + "（再生成）"
    design2 = _flat_design_output(envelope)
    executor, gateway = _executor(tmp_path, [skeleton1, design1, skeleton2, design2])

    first = asyncio.run(executor.execute(
        "P-ARGUMENT-ARCHITECTURE",
        envelope,
        project_id="project-1",
        workflow_id="wf-step4d-regeneration",
        call_key="outer-step4d-generation-1",
    ))
    assert first["output"]["result"]["authored_state"]["central_proposition"] == skeleton1["central_proposition"]
    assert len(gateway.calls) == 2

    class _PackStub:
        @staticmethod
        def entry(prompt_id):
            assert prompt_id == CRITIC
            return {"model_contract_mode": "SEMANTIC"}

    class _DBStub:
        def audit(self, event, **kwargs):
            return None

    class _RegenerationHarness(WorkflowEngine):
        def __init__(self):
            self.pack = _PackStub()
            self.db = _DBStub()

        def get(self, workflow_id):
            assert workflow_id == "wf-step4d-regeneration"
            return {
                "id": workflow_id,
                "steps": [
                    {"prompt_id": "P-ARGUMENT-ARCHITECTURE"},
                    {"prompt_id": CRITIC},
                ],
            }

        def _update(self, wf, **kwargs):
            for key in ("current_step", "status", "state"):
                if key in kwargs:
                    wf[key] = kwargs[key]

    state = {
        "options": {"original_producer_regeneration_limit": 2},
        "step_results": {"0": copy.deepcopy(first), "1": {"status": "REVISE"}},
    }
    wf = {
        "id": "wf-step4d-regeneration",
        "project_id": "project-1",
        "current_step": 1,
        "status": "RUNNING",
        "state": state,
    }
    routing = {
        "findings": [{
            "finding_instance_id": "F-STEP4D-REGENERATE",
            "code": "RESEARCH_DESIGN_INCOMPLETE",
            "blocking": True,
            "suggested_route": "ORIGINAL_PRODUCER",
        }]
    }
    assert _RegenerationHarness()._prepare_original_producer_regeneration(
        wf, state, critic_prompt=CRITIC, output=routing
    ) == "SCHEDULED"
    assert wf["current_step"] == 0
    assert state["producer_regeneration_rounds"][CRITIC] == 1
    assert state["step_results"] == {}

    regenerated = asyncio.run(executor.execute(
        "P-ARGUMENT-ARCHITECTURE",
        envelope,
        project_id="project-1",
        workflow_id="wf-step4d-regeneration",
        call_key="outer-step4d-generation-2",
    ))
    assert len(gateway.calls) == 4
    assert gateway.calls[2]["envelope"].get("frozen_skeleton") is None
    assert gateway.calls[3]["envelope"]["frozen_skeleton"] == skeleton2
    assert regenerated["output"]["result"]["authored_state"]["central_proposition"] == skeleton2["central_proposition"]
