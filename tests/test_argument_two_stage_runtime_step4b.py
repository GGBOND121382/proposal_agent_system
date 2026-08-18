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
from app.util import utc_now
from app.workflow_defs import WORKFLOWS
from app.argument_two_stage_orchestration import ARGUMENT_DESIGN_STAGE, ARGUMENT_SKELETON_STAGE, argument_stage_output_schema
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
    assert gateway.calls[0]["envelope"].get("skeleton_seed") is not None
    assert "frozen_skeleton" not in gateway.calls[0]["envelope"]
    assert gateway.calls[1]["envelope"]["frozen_skeleton"] == skeleton
    assert all(call["direct_tool_arguments"] is True for call in gateway.calls)
    assert gateway.calls[0]["call_key"] != gateway.calls[1]["call_key"]

    row = executor.db.fetchone(
        "SELECT COUNT(*) AS n FROM prompt_runs WHERE prompt_id='P-ARGUMENT-ARCHITECTURE'"
    )
    assert int(row["n"]) == 1


def test_step4b_runtime_design_retry_never_regenerates_skeleton(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPABILITY_ACCEPTANCE_MODE", "false")
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    broken_design = _flat_design_output(envelope)
    broken_design["methods"][0]["work_package_index"] = 99
    valid_design = _flat_design_output(envelope)
    executor, gateway = _executor(
        tmp_path, [skeleton, broken_design, valid_design]
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
    first, design1, design2 = gateway.calls
    assert first["envelope"].get("skeleton_seed") is not None
    assert design1["envelope"]["frozen_skeleton"] == skeleton
    assert design2["envelope"]["frozen_skeleton"] == skeleton
    assert design2["envelope"]["retry_context"]["previous_candidate"] == broken_design
    assert design2["envelope"]["retry_context"]["validation_errors"]


def test_step4b_runtime_exhausted_design_failure_is_nonretryable_contract_failure(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CAPABILITY_ACCEPTANCE_MODE", "false")
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    broken = _flat_design_output(envelope)
    broken["methods"][0]["work_package_index"] = 99
    executor, gateway = _executor(tmp_path, [skeleton, broken, broken])

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
    assert len(gateway.calls) == 3
    assert gateway.calls[1]["envelope"]["frozen_skeleton"] == skeleton
    assert gateway.calls[2]["envelope"]["frozen_skeleton"] == skeleton


def test_step4b_provider_request_identity_includes_two_stage_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPABILITY_ACCEPTANCE_MODE", "false")
    executor, _ = _executor(tmp_path, [])
    spec = executor._model_request_spec("P-ARGUMENT-ARCHITECTURE")

    contract = spec["argument_two_stage_contract"]
    assert contract["version"] == "ARGUMENT_TWO_STAGE_V2"
    assert set(contract["stages"]) == {"SKELETON", "DESIGN"}
    assert contract["stages"]["SKELETON"]["desired_output_tokens"] == 8_192
    assert contract["stages"]["DESIGN"]["desired_output_tokens"] == 65_536
    assert contract["stages"]["SKELETON"]["output_schema"]["properties"]["research_threads"]["maxItems"] == 4

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
    assert spec["argument_two_stage_contract"]["version"] == "ARGUMENT_TWO_STAGE_V2"
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

