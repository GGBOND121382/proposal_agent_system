from __future__ import annotations

import copy
import inspect
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.executor import PromptExecutionError, PromptExecutor
from app.pack import PromptPack
from app.runtime_failures import FailureCategory
from app.util import sha256_json
from app.workflow_repair import WorkflowRepairMixin
from app.workflows import WorkflowEngine


PRODUCER = "P-ARGUMENT-ARCHITECTURE"
ROOT = Path(__file__).resolve().parents[1]


class _Policy:
    def __init__(self) -> None:
        self.calls = 0

    def assert_output_unchanged(self, before, after, *, stage: str) -> None:
        self.calls += 1
        assert before == after
        assert stage == "output_normalization"


class _Executor:
    quality_guard_enabled = False

    def __init__(self, *, repaired_errors: list[str] | None = None) -> None:
        self.policy = _Policy()
        self.repaired_errors = repaired_errors or []
        self.normalize_calls = 0
        self.guard_calls = 0
        self.semantic_calls = 0

    def _normalize_output(self, prompt_id, candidate, envelope):
        self.normalize_calls += 1
        assert prompt_id == PRODUCER
        assert envelope["prompt_id"] == PRODUCER
        return copy.deepcopy(candidate)

    def _observe_guard(self, prompt_id, envelope, candidate):
        self.guard_calls += 1
        return {"observation_status": "PASS"}

    def _validate_output_semantics(self, prompt_id, envelope, candidate):
        self.semantic_calls += 1


class _Pack:
    def __init__(self, executor: _Executor) -> None:
        self.executor = executor
        self.calls = 0

    def validate(self, prompt_id, kind, candidate):
        self.calls += 1
        assert prompt_id == PRODUCER
        assert kind == "output"
        return list(self.executor.repaired_errors)

    def entry(self, prompt_id):
        return {"required_environment": "OFFLINE_LOCAL"}


class _DB:
    def __init__(self, row: dict | None) -> None:
        self.row = row

    def fetchone(self, sql, params=()):
        if "FROM prompt_runs" in sql:
            return copy.deepcopy(self.row)
        return None


class _ContextBuilder:
    def __init__(self) -> None:
        self.calls = 0
        self.overrides: dict | None = None

    def build(self, prompt_id, project_id, **kwargs):
        self.calls += 1
        assert prompt_id == "P-TARGETED-REPAIR"
        self.overrides = copy.deepcopy(kwargs["overrides"])
        return {"prompt_id": prompt_id, "payload": {}}


class _RepairHarness(WorkflowRepairMixin):
    def __init__(
        self,
        row: dict | None,
        repaired_candidate: dict,
        *,
        repaired_errors: list[str] | None = None,
    ) -> None:
        self.db = _DB(row)
        self.executor = _Executor(repaired_errors=repaired_errors)
        self.pack = _Pack(self.executor)
        self.context_builder = _ContextBuilder()
        self.repaired_candidate = copy.deepcopy(repaired_candidate)
        self.repair_calls = 0
        self.last_retry_categories = None
        self.persisted: dict | None = None

    def _project_level(self, project_id: str) -> str:
        return "INTERNAL"

    def _inherited_producer_source_catalog(self, wf, state, producer_prompt):
        return []

    async def _execute_prompt_with_provider_retry(self, *args, **kwargs):
        self.repair_calls += 1
        self.last_retry_categories = kwargs.get("retry_categories")
        return {
            "run_id": "run-repair",
            "status": "PASS",
            "route": {
                "environment": "OFFLINE_LOCAL",
                "model_id": "repair-model",
                "endpoint_id": "offline-primary",
            },
            "call_key": "call-repair",
            "output": {
                "status": "PASS",
                "result": {"repaired_object": copy.deepcopy(self.repaired_candidate)},
            },
        }

    def _persist_repair_application(self, **kwargs):
        self.persisted = copy.deepcopy(kwargs)
        return "artifact-repair"

    def _update(self, wf, *, state):
        self.updated_state = copy.deepcopy(state)


class _SuccessExecutor:
    def provider_request_spec_hash(self, prompt_id: str) -> str:
        return "request-spec"

    async def execute(self, prompt_id, envelope, **kwargs):
        return {
            "run_id": "run-success",
            "prompt_id": prompt_id,
            "status": "PASS",
            "route": {
                "environment": "OFFLINE_LOCAL",
                "model_id": "producer-model",
                "endpoint_id": "offline-primary",
            },
            "output": {"status": "PASS", "result": {"value": "valid"}},
            "call_key": kwargs["call_key"],
        }


class _ContractFailingExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def provider_request_spec_hash(self, prompt_id: str) -> str:
        return "request-spec"

    async def execute(self, prompt_id, envelope, **kwargs):
        self.calls += 1
        raise PromptExecutionError(
            "contract failure",
            validation_errors=["/result/value: invalid reference"],
            run_id="run-producer",
        )


class _RetryHarness(WorkflowEngine):
    def __init__(self) -> None:
        self.executor = _SuccessExecutor()
        self.repair_calls = 0

    def _update(self, wf, **kwargs):
        if "state" in kwargs:
            wf["state"] = kwargs["state"]

    async def _repair_producer_contract_failure(self, *args, **kwargs):
        self.repair_calls += 1
        return {"attempted": False, "result": None}


class _ContractRetryHarness(_RetryHarness):
    def __init__(self) -> None:
        self.executor = _ContractFailingExecutor()
        self.repair_calls = 0

    def _record_runtime_failure(self, wf, state, *, prompt_id, exc):
        return {"workflow_status": "BLOCKED_CONTRACT"}

    async def _repair_producer_contract_failure(self, *args, **kwargs):
        self.repair_calls += 1
        return {
            "attempted": True,
            "result": {
                "run_id": "run-repair",
                "prompt_id": PRODUCER,
                "status": "PASS",
                "route": {
                    "environment": "OFFLINE_LOCAL",
                    "model_id": "repair-model",
                    "endpoint_id": "offline-primary",
                },
                "output": {"status": "PASS", "result": {"value": "valid"}},
                "call_key": "call-repair",
                "contract_repair": {
                    "source_run_id": "run-producer",
                    "repair_run_id": "run-repair",
                },
            },
        }


class _RetryAfterFailedRepairExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def provider_request_spec_hash(self, prompt_id: str) -> str:
        return "request-spec"

    async def execute(self, prompt_id, envelope, **kwargs):
        self.calls += 1
        if self.calls == 1:
            error = PromptExecutionError(
                "provider-authored contract failure",
                validation_errors=["/result/value: invalid"],
                run_id="run-producer-1",
            )
            error.provider_failure_kind = "RESPONSE_SHAPE"
            error.provider_phase = "output_structure_validation"
            error.retryable_hint = False
            raise error
        return {
            "run_id": "run-producer-2",
            "prompt_id": prompt_id,
            "status": "PASS",
            "route": {
                "environment": "OFFLINE_LOCAL",
                "model_id": "producer-model",
                "endpoint_id": "offline-primary",
            },
            "output": {"status": "PASS", "result": {"value": "valid"}},
            "call_key": kwargs["call_key"],
        }


class _ParseThenSuccessExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def provider_request_spec_hash(self, prompt_id: str) -> str:
        return "request-spec"

    async def execute(self, prompt_id, envelope, **kwargs):
        self.calls += 1
        if self.calls == 1:
            error = PromptExecutionError(
                "MiniMax assistant JSON parse failed",
                run_id="run-repair-parse-1",
            )
            error.provider_failure_kind = "RESPONSE_PARSE"
            error.provider_phase = "assistant_json_parse"
            error.retryable_hint = False
            raise error
        return {
            "run_id": "run-repair-parse-2",
            "prompt_id": prompt_id,
            "status": "PASS",
            "route": {
                "environment": "OFFLINE_LOCAL",
                "model_id": "repair-model",
                "endpoint_id": "offline-primary",
            },
            "output": {"status": "PASS", "result": {"value": "valid"}},
            "call_key": kwargs["call_key"],
        }


class _ParseRetryHarness(WorkflowEngine):
    def __init__(self) -> None:
        self.executor = _ParseThenSuccessExecutor()

    def _update(self, wf, **kwargs):
        if "state" in kwargs:
            wf["state"] = kwargs["state"]

    def _record_runtime_failure(self, wf, state, *, prompt_id, exc):
        return {"workflow_status": "BLOCKED_CONTRACT"}

    async def _repair_producer_contract_failure(self, *args, **kwargs):
        return {"attempted": False, "result": None}


class _FailedRepairThenRetryHarness(WorkflowEngine):
    def __init__(self) -> None:
        self.executor = _RetryAfterFailedRepairExecutor()
        self.repair_calls = 0

    def _update(self, wf, **kwargs):
        if "state" in kwargs:
            wf["state"] = kwargs["state"]

    def _record_runtime_failure(self, wf, state, *, prompt_id, exc):
        return {"workflow_status": "BLOCKED_CONTRACT"}

    async def _repair_producer_contract_failure(self, *args, **kwargs):
        self.repair_calls += 1
        return {"attempted": True, "result": None}


class _SupersedeHarness(WorkflowRepairMixin):
    def __init__(self) -> None:
        self.deactivated: list[str] = []

    def _deactivate_repair_application(self, state, producer_prompt):
        self.deactivated.append(producer_prompt)


def _producer_row(candidate: object) -> dict:
    return {
        "id": "run-producer",
        "input_json": json.dumps({"prompt_id": PRODUCER}),
        "output_json": json.dumps(candidate),
    }


def _classification(*, category=FailureCategory.OUTPUT_CONTRACT, kind=None):
    return SimpleNamespace(category=category, failure_kind=kind)


@pytest.mark.asyncio
async def test_valid_producer_result_does_not_call_targeted_repair() -> None:
    engine = _RetryHarness()
    state = {"options": {}, "provider_call_cycles": {}}
    wf = {
        "id": "wf-generic",
        "project_id": "project-1",
        "current_step": 4,
        "state": state,
    }

    result = await engine._execute_prompt_with_provider_retry(
        wf,
        state,
        prompt_id=PRODUCER,
        envelope={"prompt_id": PRODUCER},
    )

    assert result["status"] == "PASS"
    assert engine.repair_calls == 0


@pytest.mark.asyncio
async def test_contract_repair_success_does_not_regenerate_the_producer() -> None:
    engine = _ContractRetryHarness()
    state = {"options": {}, "provider_call_cycles": {}}
    wf = {
        "id": "wf-generic",
        "project_id": "project-1",
        "current_step": 4,
        "state": state,
    }

    result = await engine._execute_prompt_with_provider_retry(
        wf,
        state,
        prompt_id=PRODUCER,
        envelope={"prompt_id": PRODUCER},
    )

    assert result["run_id"] == "run-repair"
    assert engine.executor.calls == 1
    assert engine.repair_calls == 1
    cycle = state["provider_call_cycles"][f"4:{PRODUCER}"]
    assert cycle["successful_contract_repair_run_id"] == "run-repair"
    assert "successful_call_key" not in cycle


@pytest.mark.asyncio
async def test_response_parse_failure_can_regenerate_at_repair_provider_boundary() -> None:
    engine = _ParseRetryHarness()
    state = {
        "options": {
            "provider_retry_limit": 2,
            "provider_retry_base_delay_seconds": 0,
            "provider_retry_max_delay_seconds": 0,
        },
        "provider_call_cycles": {},
    }
    wf = {
        "id": "wf-generic",
        "project_id": "project-1",
        "current_step": 0,
        "state": state,
    }

    result = await engine._execute_prompt_with_provider_retry(
        wf,
        state,
        prompt_id="P-TARGETED-REPAIR",
        envelope={"prompt_id": "P-TARGETED-REPAIR"},
        retry_categories=frozenset({
            FailureCategory.PROVIDER_TRANSIENT,
            FailureCategory.OUTPUT_CONTRACT,
        }),
    )

    assert result["run_id"] == "run-repair-parse-2"
    assert engine.executor.calls == 2
    cycle = state["provider_call_cycles"]["0:P-TARGETED-REPAIR"]
    assert cycle["completed_attempts"] == 2
    assert cycle["successful_attempt"] == 2
    assert "provider_wait" not in state


@pytest.mark.asyncio
async def test_failed_contract_repair_does_not_consume_remaining_producer_retry() -> None:
    engine = _FailedRepairThenRetryHarness()
    state = {
        "options": {
            "provider_retry_limit": 2,
            "provider_retry_base_delay_seconds": 0,
            "provider_retry_max_delay_seconds": 0,
        },
        "provider_call_cycles": {},
    }
    wf = {
        "id": "wf-generic",
        "project_id": "project-1",
        "current_step": 4,
        "state": state,
    }

    result = await engine._execute_prompt_with_provider_retry(
        wf,
        state,
        prompt_id=PRODUCER,
        envelope={"prompt_id": PRODUCER},
    )

    assert result["run_id"] == "run-producer-2"
    assert engine.executor.calls == 2
    assert engine.repair_calls == 1
    cycle = state["provider_call_cycles"][f"4:{PRODUCER}"]
    assert cycle["completed_attempts"] == 2
    assert cycle["successful_attempt"] == 2
    assert "provider_wait" not in state


@pytest.mark.asyncio
async def test_v8_argument_contract_error_without_authoritative_state_escalates_instead_of_local_repair() -> None:
    original = {
        "schema_version": "2.0",
        "prompt_id": PRODUCER,
        "status": "PASS",
        "result": {"value": "missing-id"},
        "findings": [],
        "unresolved_items": [],
        "user_questions": [],
    }
    repaired = copy.deepcopy(original)
    repaired["result"]["value"] = "known-id"
    original_snapshot = copy.deepcopy(original)
    harness = _RepairHarness(_producer_row(original), repaired)
    exc = PromptExecutionError(
        "contract failure",
        validation_errors=["/result/value: reference id does not exist"],
        run_id="run-producer",
    )

    state: dict = {}
    outcome = await harness._repair_producer_contract_failure(
        {"id": "wf-generic", "project_id": "project-1"},
        state,
        prompt_id=PRODUCER,
        envelope={"prompt_id": PRODUCER},
        exc=exc,
        classification=_classification(kind="RESPONSE_SHAPE"),
    )

    assert outcome == {"attempted": True, "result": None}
    assert harness.repair_calls == 0
    assert harness.persisted is None
    assert original == original_snapshot
    assert state["contract_repair_escalations"][-1]["reason"] == "AUTHORITATIVE_RUNTIME_CONTRACT_REGENERATION_REQUIRED"



@pytest.mark.asyncio
async def test_v8_argument_contract_retry_is_not_attempted_without_authoritative_state() -> None:
    original = {"status": "PASS", "result": {"value": "bad"}}
    repaired = {"status": "PASS", "result": {"value": "still-bad"}}
    harness = _RepairHarness(
        _producer_row(original),
        repaired,
        repaired_errors=["/result/value: reference id still does not exist"],
    )
    exc = PromptExecutionError(
        "contract failure",
        validation_errors=["/result/value: reference id does not exist"],
        run_id="run-producer",
    )

    outcome = await harness._repair_producer_contract_failure(
        {"id": "wf-generic", "project_id": "project-1"},
        {},
        prompt_id=PRODUCER,
        envelope={"prompt_id": PRODUCER},
        exc=exc,
        classification=_classification(),
    )

    assert outcome == {"attempted": True, "result": None}
    assert harness.repair_calls == 0
    assert harness.persisted is None



@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("candidate", "classification"),
    [
        ({"result": {}}, _classification(category=FailureCategory.PROVIDER_TRANSIENT, kind="TRANSPORT")),
        ({"result": {}}, _classification(category=FailureCategory.PROVIDER_TRANSIENT, kind="TIMEOUT")),
        ({"result": {}}, _classification(kind="OUTPUT_TRUNCATED")),
        ({"result": {}}, _classification(kind="RESPONSE_PARSE")),
        ("not-an-object", _classification()),
    ],
)
async def test_provider_or_unparsed_failures_never_enter_targeted_repair(
    candidate,
    classification,
) -> None:
    harness = _RepairHarness(_producer_row(candidate), {"result": {}})
    exc = PromptExecutionError(
        "provider failure",
        validation_errors=["/result: invalid"],
        run_id="run-producer",
    )

    outcome = await harness._repair_producer_contract_failure(
        {"id": "wf-generic", "project_id": "project-1"},
        {},
        prompt_id=PRODUCER,
        envelope={"prompt_id": PRODUCER},
        exc=exc,
        classification=classification,
    )

    assert outcome == {"attempted": False, "result": None}
    assert harness.repair_calls == 0


def test_error_mapping_requires_a_safe_pointer_and_limits_status_closure() -> None:
    candidate = {"status": "PASS", "result": {"value": "x"}}
    assert WorkflowRepairMixin._contract_repair_findings(
        PRODUCER,
        candidate,
        ["reference id does not exist"],
    ) is None

    first = WorkflowRepairMixin._contract_repair_findings(
        PRODUCER,
        candidate,
        ["/status: blocking question requires NEED_USER_INPUT"],
    )
    second = WorkflowRepairMixin._contract_repair_findings(
        PRODUCER,
        candidate,
        ["/status: blocking question requires NEED_USER_INPUT"],
    )
    assert first == second
    assert first[1] == [
        "/status",
        "/findings",
        "/user_questions",
        "/unresolved_items",
    ]
    assert "/" not in first[1]


def test_generated_repair_contract_obeys_existing_schema_and_scope_validator() -> None:
    pack = PromptPack(ROOT / "prompt_pack")
    envelope = pack.replay_input("P-TARGETED-REPAIR")
    original = {"status": "PASS", "result": {"value": "bad"}}
    repaired = {"status": "PASS", "result": {"value": "good"}}
    findings, validator_paths = WorkflowRepairMixin._contract_repair_findings(
        PRODUCER,
        original,
        ["/result/value: invalid reference"],
    )
    allowed_paths = [f"/content{path}" for path in validator_paths]
    protected_paths, protected_hashes = (
        WorkflowRepairMixin._contract_repair_protection(
            original,
            validator_paths,
        )
    )
    original_hash = sha256_json(original)
    envelope["payload"].update(
        {
            "original_object": {
                "object_type": "ARGUMENT_ARCHITECTURE",
                "object_id": "contract-run-1",
                "object_hash": original_hash,
                "content": original,
            },
            "original_producer": "ARGUMENT_ARCHITECTURE_AGENT",
            "findings_to_repair": findings,
            "allowed_paths": allowed_paths,
            "protected_paths": protected_paths,
            "protected_hashes": protected_hashes,
            "original_input_refs": [
                {
                    "object_id": "contract-run-1",
                    "object_type": "ARGUMENT_ARCHITECTURE",
                    "version": 1,
                    "object_hash": original_hash,
                    "security_level": "INTERNAL",
                    "display_name": "failed producer output",
                }
            ],
            "inherited_source_catalog": [],
        }
    )
    output = {
        "schema_version": "2.0",
        "prompt_id": "P-TARGETED-REPAIR",
        "prompt_version": "8.0.0",
        "status": "PASS",
        "result": {
            "repaired_object": {"content": repaired},
            "changed_paths": ["/content/result/value"],
            "unchanged_protected_hashes": protected_hashes,
            "resolved_finding_ids": [findings[0]["finding_instance_id"]],
            "unresolved_finding_ids": [],
        },
        "findings": [],
        "unresolved_items": [],
        "user_questions": [],
        "source_refs": [],
        "warnings": [],
    }

    assert pack.validate("P-TARGETED-REPAIR", "input", envelope) == []
    assert pack.validate("P-TARGETED-REPAIR", "output", output) == []
    PromptExecutor._validate_output_semantics(
        "P-TARGETED-REPAIR",
        envelope,
        output,
    )



def test_user_routed_repairable_scalar_is_narrowly_repairable_and_then_passes_full_contract() -> None:
    pack = PromptPack(ROOT / "prompt_pack")
    executor = PromptExecutor.__new__(PromptExecutor)
    executor.pack = pack
    envelope = pack.replay_input(PRODUCER)
    candidate = pack.replay_output(PRODUCER, "normal")
    candidate["status"] = "REVISE"
    candidate["findings"] = [
        {
            "finding_instance_id": "F-USER-FOUNDATION-001",
            "code": "FOUNDATION_EVIDENCE_MISSING",
            "severity": "P0",
            "category": "ARGUMENT",
            "target_type": "ARGUMENT_NODE",
            "target_path_or_span": "/result/argument_architecture/nodes/0",
            "description": "团队研究基础缺少必须由用户确认的可核验来源。",
            "evidence_refs": [],
            "repairable": True,
            "repair_instruction": "由用户提供或确认团队研究基础来源。",
            "suggested_route": "USER",
            "blocking": True,
        }
    ]
    candidate["user_questions"] = [
        {
            "question_id": "UQ-FOUNDATION-001",
            "question_type": "MISSING_INFORMATION",
            "question": "请提供或确认团队前期研究基础的可核验来源。",
            "reason": "该信息不能由自动化生产者补造。",
            "target_paths": ["/payload/confirmed_facts"],
            "answer_schema": {"type": "ARRAY", "allowed_values": []},
            "blocking": True,
            "priority": "P0",
        }
    ]

    with pytest.raises(PromptExecutionError) as captured:
        executor._normalize_output(PRODUCER, candidate, envelope)

    assert captured.value.validation_errors == [
        "/findings/0/repairable: blocking USER-routed Finding cannot be marked "
        "repairable by an automated producer"
    ]
    mapped = WorkflowRepairMixin._contract_repair_findings(
        PRODUCER,
        candidate,
        captured.value.validation_errors,
    )
    assert mapped is not None
    findings, validator_paths = mapped
    assert validator_paths == ["/findings/0/repairable"]
    assert findings[0]["target_path_or_span"] == "/findings/0/repairable"

    repaired = copy.deepcopy(candidate)
    repaired["findings"][0]["repairable"] = False
    normalized = executor._normalize_output(PRODUCER, repaired, envelope)

    assert normalized["status"] == "NEED_USER_INPUT"
    assert pack.validate(PRODUCER, "output", normalized) == []
    executor._validate_output_semantics(PRODUCER, envelope, normalized)


def test_contract_repair_orchestration_has_no_step_or_argument_prompt_special_case() -> None:
    source = inspect.getsource(WorkflowRepairMixin._repair_producer_contract_failure)
    assert "current_step == 0" not in source
    assert 'prompt_id == "P-ARGUMENT-ARCHITECTURE"' not in source
    assert re.search(r"wf-[0-9a-f]{12,}", source, flags=re.IGNORECASE) is None


def test_section_contract_repair_override_is_not_mistaken_for_a_fresh_candidate() -> None:
    harness = _SupersedeHarness()
    state = {
        "active_section_id": "section-1",
        "section_progress": {
            "section-1": {"runs": [{"run_id": "run-repair"}]}
        },
        "producer_contract_repair_markers": {
            "section:section-1:P-WRITE-CONTENT": {
                "section_id": "section-1",
                "repair_run_id": "run-repair",
            }
        },
    }

    harness._supersede_repair_subject(
        state,
        critic_prompt="P-WRITE-CRITIC",
        producer_prompt="P-WRITE-CONTENT",
        reason="FRESH_PRODUCER_PASS",
    )

    assert harness.deactivated == []
    assert "producer_contract_repair_markers" not in state
