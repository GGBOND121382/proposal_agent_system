from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.db import Database
from app.llm import LLMResult, MODEL_RESPONSE_PROTOCOL_VERSION
from app.runtime_context import LiveContextBlocked, LiveContextBuilder
from app.runtime_evidence import EvidenceIntegrityError, ModelCallEvidenceStore
from app.runtime_executor import RecoverablePromptExecutionError, RuntimePromptExecutor
from app.runtime_policy import CapabilityModeError, CapabilityPolicy, LIVE_ENVELOPE_REGISTRY
from app.runtime_workflows import RecoverableWorkflowEngine
from app.security import Route
from app.util import sha256_json, utc_now


class MinimalPack:
    def __init__(self):
        self.replay_reads = 0

    def replay_input(self, prompt_id: str):
        self.replay_reads += 1
        raise AssertionError("LIVE context must not read Replay")

    def inlined_schema(self, prompt_id: str, kind: str):
        if kind == "output":
            return {"type": "object"}
        return {
            "type": "object",
            "properties": {
                "schema_version": {"const": "2.0"},
                "prompt_id": {"const": prompt_id},
                "prompt_version": {"const": "2.0.0"},
                "task": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string", "minLength": 1},
                        "workflow_type": {"enum": ["PROJECT_INTAKE"]},
                        "current_step": {"type": "string", "minLength": 1},
                        "attempt": {"type": "integer", "minimum": 1},
                        "writing_mode": {"type": ["string", "null"]},
                    },
                    "required": ["task_id", "workflow_type", "current_step", "attempt", "writing_mode"],
                },
                "security_context": {
                    "type": "object",
                    "properties": {
                        "project_security_level": {"enum": ["INTERNAL"]},
                        "input_max_security_level": {"enum": ["INTERNAL"]},
                        "required_environment": {"enum": ["OFFLINE_LOCAL"]},
                        "online_transfer_approval_status": {"enum": ["NOT_REQUIRED"]},
                        "allowed_model_endpoint_ids": {"type": "array", "items": {"type": "string"}},
                        "prohibited_fields": {"type": "array", "items": {"type": "string"}},
                        "recipient_scope": {"type": "array", "items": {"type": "string"}},
                        "policy_version": {"type": "string"},
                    },
                    "required": [
                        "project_security_level", "input_max_security_level", "required_environment",
                        "online_transfer_approval_status", "allowed_model_endpoint_ids", "prohibited_fields",
                        "recipient_scope", "policy_version",
                    ],
                },
                "scope": {
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "target_object_ids": {"type": "array"},
                        "read_only_object_ids": {"type": "array"},
                        "protected_object_ids": {"type": "array"},
                    },
                    "required": ["project_id", "target_object_ids", "read_only_object_ids", "protected_object_ids"],
                },
                "freshness": {"type": "object", "properties": {}},
                "payload": {
                    "type": "object",
                    "properties": {"task_instruction": {"type": "string", "minLength": 1}},
                    "required": ["task_instruction"],
                },
                "expected_output_schema": {"type": "string", "minLength": 1},
            },
            "required": [
                "schema_version", "prompt_id", "prompt_version", "task", "security_context",
                "scope", "freshness", "payload", "expected_output_schema",
            ],
        }

    def entry(self, prompt_id: str):
        return {
            "required_environment": "OFFLINE_LOCAL",
            "output_schema": "schemas/output.json",
            "next_human_gate": None,
        }

    def validate(self, prompt_id: str, kind: str, value):
        return []

    def section_profile_for(self, title):
        return {}


class ContextDB:
    def fetchone(self, sql, params=()):
        if "FROM projects" in sql:
            return {
                "id": "project-1",
                "name": "Project",
                "description": "Real persisted project description",
                "security_level": "INTERNAL",
                "config_json": json.dumps(
                    {
                        "task_instruction": "Use real project material",
                        "allowed_model_endpoint_ids": ["offline-primary"],
                        "recipient_scope": ["内部用户"],
                    }
                ),
            }
        return None

    def fetchall(self, sql, params=()):
        return []


@pytest.fixture(autouse=True)
def clean_runtime_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("CAPABILITY_ACCEPTANCE_MODE", "false")
    monkeypatch.setenv("MODEL_RUNTIME_MODE", "REPLAY")
    monkeypatch.setenv("MODEL_CALL_EVIDENCE_DIR", str(tmp_path / "model_calls"))
    monkeypatch.delenv("RUNTIME_FAULT_POINT", raising=False)
    monkeypatch.delenv("RUNTIME_FAULT_ACTION", raising=False)
    LIVE_ENVELOPE_REGISTRY.clear()


def test_capability_policy_rejects_replay(monkeypatch):
    monkeypatch.setenv("CAPABILITY_ACCEPTANCE_MODE", "true")
    with pytest.raises(CapabilityModeError):
        CapabilityPolicy.from_environment().assert_environment("REPLAY")


def test_capability_policy_allows_validator_annotations(monkeypatch):
    monkeypatch.setenv("CAPABILITY_ACCEPTANCE_MODE", "true")
    policy = CapabilityPolicy.from_environment()
    original = {
        "prompt_id": "P-TEST",
        "status": "PASS",
        "findings": [],
        "result": {"verdict": "ACCEPT", "content": {"claim": "unchanged"}},
    }
    annotated = {
        "prompt_id": "P-TEST",
        "status": "REVISE",
        "findings": [{"code": "QG_TEST"}],
        "result": {"verdict": "REVISE", "content": {"claim": "unchanged"}},
    }
    policy.assert_output_unchanged(original, annotated, stage="proposal_quality_guard")


def test_capability_policy_rejects_semantic_rewrite(monkeypatch):
    monkeypatch.setenv("CAPABILITY_ACCEPTANCE_MODE", "true")
    policy = CapabilityPolicy.from_environment()
    original = {"status": "PASS", "findings": [], "result": {"claim": "original"}}
    rewritten = {"status": "REVISE", "findings": [], "result": {"claim": "changed"}}
    with pytest.raises(CapabilityModeError, match="semantic content"):
        policy.assert_output_unchanged(original, rewritten, stage="proposal_quality_guard")


def test_live_context_does_not_read_replay(monkeypatch):
    monkeypatch.setenv("MODEL_RUNTIME_MODE", "LIVE")
    pack = MinimalPack()
    builder = LiveContextBuilder(ContextDB(), pack)
    envelope = builder.build(
        "P-TEST",
        "project-1",
        workflow_id="wf-1",
        workflow_state={"workflow_type": "WF-1_PROJECT_INTAKE"},
    )
    assert pack.replay_reads == 0
    assert envelope["payload"]["task_instruction"] == "Use real project material"
    assert LIVE_ENVELOPE_REGISTRY.contains_hash(sha256_json(envelope))


def test_live_context_blocks_unresolved_required_field(monkeypatch):
    monkeypatch.setenv("MODEL_RUNTIME_MODE", "LIVE")
    pack = MinimalPack()
    db = ContextDB()
    db.fetchone = lambda sql, params=(): {
        "id": "project-1",
        "name": "",
        "description": "",
        "security_level": "INTERNAL",
        "config_json": "{}",
    } if "FROM projects" in sql else None
    with pytest.raises(LiveContextBlocked):
        LiveContextBuilder(db, pack).build("P-TEST", "project-1")
    assert pack.replay_reads == 0


def test_response_evidence_detects_tampering(tmp_path):
    store = ModelCallEvidenceStore(tmp_path / "evidence")
    store.write_request("call-1", {"prompt": "p"})
    store.write_response(
        "call-1",
        raw_text='{"status":"PASS"}',
        parsed_output={"status": "PASS"},
        raw_parsed_output={"status": "PASS"},
        metadata={"model_id": "m", "endpoint_id": "e"},
    )
    raw_path, _, _ = store.response_paths("call-1")
    raw_path.write_text('{"status":"BLOCK"}', encoding="utf-8")
    with pytest.raises(EvidenceIntegrityError):
        store.load_verified_response("call-1")



def test_failed_response_evidence_preserves_provider_raw_and_rejected_candidate(tmp_path):
    store = ModelCallEvidenceStore(tmp_path / "evidence")
    store.write_request("call-bad", {"prompt": "p"})
    provider_raw = (
        'data: {"choices":[{"delta":{"content":"bad"},"finish_reason":null}]}\n'
        'data: [DONE]'
    )
    store.write_provider_response(
        "call-bad",
        provider_attempt=1,
        raw_text=provider_raw,
        metadata={
            "prompt_id": "P-TEST",
            "failure_surface": "provider_wire",
        },
    )
    store.write_failed_response(
        "call-bad",
        rejected_text='{"status":"PASS"',
        metadata={
            "error": "malformed JSON",
            "failure_kind": "RESPONSE_PARSE",
            "provider_phase": "response_parse",
        },
    )

    provider_raw_path, provider_meta_path = store.provider_response_paths("call-bad", 1)
    rejected_path, failed_meta_path = store.failed_response_paths("call-bad")
    _, parsed_path, success_meta_path = store.response_paths("call-bad")

    assert provider_raw_path.read_text(encoding="utf-8") == provider_raw
    assert provider_meta_path.exists()
    assert rejected_path.read_text(encoding="utf-8") == '{"status":"PASS"'
    assert failed_meta_path.exists()
    assert not parsed_path.exists()
    assert not success_meta_path.exists()

    failed = store.load_failed_response("call-bad")
    assert failed["metadata"]["failure_kind"] == "RESPONSE_PARSE"
    assert failed["metadata"]["provider_response_count"] == 1
    assert failed["rejected_text"] == '{"status":"PASS"'
    assert failed["provider_responses"][0]["raw_response_sha256"]



def test_response_evidence_survives_json_parser_upgrade(tmp_path, monkeypatch):
    store = ModelCallEvidenceStore(tmp_path / "evidence")
    store.write_request("call-parser-upgrade", {"prompt": "p"})
    store.write_response(
        "call-parser-upgrade",
        raw_text='```json\n{"status":"PASS"}\n```',
        parsed_output={"status": "PASS"},
        raw_parsed_output={"status": "PASS"},
        metadata={
            "model_id": "m",
            "endpoint_id": "e",
            "json_parser_version": "old-parser",
        },
    )

    def changed_parser(_text):
        raise AssertionError("immutable evidence must not be reparsed after parser upgrade")

    monkeypatch.setattr("app.runtime_evidence._extract_json_object", changed_parser)
    verified = store.load_verified_response("call-parser-upgrade")
    assert verified.parsed_output == {"status": "PASS"}
    assert verified.metadata["raw_parsed_object_sha256"] == verified.metadata["parsed_object_sha256"]


class ExecutorPack:
    def validate(self, prompt_id, kind, value):
        return []

    def inlined_schema(self, prompt_id, kind):
        return {"type": "object"}

    @property
    def shared_prompt(self):
        return "shared"

    def prompt_text(self, prompt_id):
        return "prompt"

    def entry(self, prompt_id):
        return {"next_human_gate": None}


class ExecutorRouter:
    def route(self, prompt_id, envelope, original_environment=None):
        return Route(
            prompt_id=prompt_id,
            environment="OFFLINE_LOCAL",
            model_id="model-1",
            endpoint_id="endpoint-1",
            provider_model_name="provider-model",
            endpoint={},
            profile={},
        )


class CountingGateway:
    supports_runtime_evidence = False

    def __init__(self):
        self.calls = 0
        self.settings = SimpleNamespace(runtime_mode="REPLAY")

    async def invoke(self, route, prompt_id, system_prompt, envelope, output_schema):
        self.calls += 1
        output = {"status": "PASS", "result": {"value": 1}, "warnings": [], "user_questions": []}
        return LLMResult(output=output, raw_text=json.dumps(output), model_id=route.model_id, endpoint_id=route.endpoint_id)


class SequenceGateway:
    supports_runtime_evidence = False

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.settings = SimpleNamespace(runtime_mode="REPLAY")

    async def invoke(self, route, prompt_id, system_prompt, envelope, output_schema):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def make_executor_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "runtime.sqlite3")
    now = utc_now()
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        ("project-1", "Project", "Description", "INTERNAL", "{}", now, now),
    )
    return db


def test_executor_reuses_atomic_commit_after_fault(monkeypatch, tmp_path):
    db = make_executor_db(tmp_path)
    gateway = CountingGateway()
    executor = RuntimePromptExecutor(
        db,
        ExecutorPack(),
        ExecutorRouter(),
        gateway,
        quality_guard_enabled=False,
    )
    envelope = {"security_context": {"input_max_security_level": "INTERNAL"}, "payload": {}}
    monkeypatch.setenv("RUNTIME_FAULT_POINT", "after_db_transaction")

    async def scenario():
        with pytest.raises(RecoverablePromptExecutionError):
            await executor.execute("P-TEST", envelope, project_id="project-1", workflow_id="wf-1")
        return await executor.execute("P-TEST", envelope, project_id="project-1", workflow_id="wf-1")

    result = asyncio.run(scenario())
    assert result["reused_committed_result"] is True
    assert gateway.calls == 1
    assert db.fetchone("SELECT COUNT(*) AS n FROM prompt_runs")["n"] == 1
    assert db.fetchone("SELECT COUNT(*) AS n FROM artifacts WHERE artifact_type='PROMPT_OUTPUT'")["n"] == 1


def test_runtime_requested_call_key_rejects_identity_collision(tmp_path):
    db = make_executor_db(tmp_path)
    gateway = CountingGateway()
    executor = RuntimePromptExecutor(
        db,
        ExecutorPack(),
        ExecutorRouter(),
        gateway,
        quality_guard_enabled=False,
    )
    first_envelope = {
        "security_context": {"input_max_security_level": "INTERNAL"},
        "payload": {"value": "first"},
    }
    changed_envelope = {
        "security_context": {"input_max_security_level": "INTERNAL"},
        "payload": {"value": "changed"},
    }

    asyncio.run(
        executor.execute(
            "P-TEST",
            first_envelope,
            project_id="project-1",
            workflow_id="wf-1",
            call_key="call-fixed",
        )
    )

    with pytest.raises(EvidenceIntegrityError, match="identity mismatch"):
        asyncio.run(
            executor.execute(
                "P-TEST",
                changed_envelope,
                project_id="project-1",
                workflow_id="wf-1",
                call_key="call-fixed",
            )
        )
    assert gateway.calls == 1


def test_runtime_requested_call_key_rejects_provider_request_spec_collision(
    monkeypatch, tmp_path
):
    db = make_executor_db(tmp_path)
    gateway = CountingGateway()
    pack = ExecutorPack()
    executor = RuntimePromptExecutor(
        db,
        pack,
        ExecutorRouter(),
        gateway,
        quality_guard_enabled=False,
    )
    envelope = {
        "security_context": {"input_max_security_level": "INTERNAL"},
        "payload": {"value": "same"},
    }

    asyncio.run(
        executor.execute(
            "P-TEST",
            envelope,
            project_id="project-1",
            workflow_id="wf-1",
            call_key="call-fixed-spec",
        )
    )
    monkeypatch.setattr(
        executor, "_model_request_spec_hash", lambda _prompt_id: "changed-spec"
    )

    with pytest.raises(EvidenceIntegrityError, match="provider request spec mismatch"):
        asyncio.run(
            executor.execute(
                "P-TEST",
                envelope,
                project_id="project-1",
                workflow_id="wf-1",
                call_key="call-fixed-spec",
            )
        )
    assert gateway.calls == 1


def test_recoverable_technical_block_resumes_same_step(tmp_path):
    blocked_status = "BLOCKED_TECHNICAL"
    db = make_executor_db(tmp_path)
    now = utc_now()
    state = {
        "workflow_type": "WF-1_PROJECT_INTAKE",
        "options": {},
        "step_results": {},
        "repair_attempts": {},
        "public_search_results": None,
        "runtime_recoverable": True,
        "runtime_failure_point": "after_db_transaction",
        "last_error": "INJECTED_FAILURE:after_db_transaction:call-1",
    }
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        ("wf-1", "project-1", "WF-1_PROJECT_INTAKE", blocked_status, 3, json.dumps(state), now, now),
    )
    engine = RecoverableWorkflowEngine(db, SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
    recovered = engine._recover_status(engine.get("wf-1"))
    assert recovered["status"] == "RUNNING"
    assert recovered["current_step"] == 3
    assert recovered["state"]["recovered_from"] == "after_db_transaction"


@pytest.mark.parametrize(
    "blocked_status",
    ["BLOCKED", "BLOCKED_PROVIDER", "BLOCKED_CONTRACT", "BLOCKED_CONTENT"],
)
def test_runtime_recoverable_flag_does_not_reopen_non_technical_block(
    tmp_path, blocked_status
):
    db = make_executor_db(tmp_path)
    now = utc_now()
    state = {
        "workflow_type": "WF-1_PROJECT_INTAKE",
        "options": {},
        "step_results": {},
        "repair_attempts": {},
        "public_search_results": None,
        "runtime_recoverable": True,
        "runtime_failure_point": "after_db_transaction",
        "last_error": "persisted classified failure",
    }
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        ("wf-1", "project-1", "WF-1_PROJECT_INTAKE", blocked_status, 3, json.dumps(state), now, now),
    )
    engine = RecoverableWorkflowEngine(
        db, SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace()
    )

    recovered = engine._recover_status(engine.get("wf-1"))

    assert recovered["status"] == blocked_status
    assert recovered["state"]["runtime_recoverable"] is True


@pytest.mark.parametrize("existing_status", ["WAITING_PROVIDER", "BLOCKED_CONTRACT"])
def test_duplicate_start_is_rejected_for_every_nonterminal_status(tmp_path, existing_status):
    db = make_executor_db(tmp_path)
    now = utc_now()
    state = {
        "workflow_type": "WF-1_PROJECT_INTAKE",
        "options": {},
        "step_results": {},
        "repair_attempts": {},
        "public_search_results": None,
    }
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        ("wf-existing", "project-1", "WF-1_PROJECT_INTAKE", existing_status, 0, json.dumps(state), now, now),
    )
    engine = RecoverableWorkflowEngine(
        db, SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace()
    )

    with pytest.raises(ValueError, match="已有未结束"):
        engine.start("project-1", "WF-1_PROJECT_INTAKE")


def test_duplicate_in_process_advance_returns_without_reexecution(tmp_path):
    db = make_executor_db(tmp_path)
    now = utc_now()
    state = {
        "workflow_type": "WF-1_PROJECT_INTAKE",
        "options": {},
        "step_results": {},
        "repair_attempts": {},
        "public_search_results": None,
    }
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        ("wf-1", "project-1", "WF-1_PROJECT_INTAKE", "RUNNING", 3, json.dumps(state), now, now),
    )
    engine = RecoverableWorkflowEngine(
        db, SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace()
    )
    engine._active_workflow_ids.add("wf-1")

    result = asyncio.run(engine.advance("wf-1"))

    assert result["status"] == "RUNNING"
    assert result["current_step"] == 3


class SequencePromptExecutor:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.call_kwargs = []

    async def execute(self, *args, **kwargs):
        self.calls += 1
        self.call_kwargs.append(dict(kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class SimulatedProcessCrash(BaseException):
    pass


def _wrapped_provider_failure(
    kind,
    *,
    http_status=None,
    retry_after=None,
    retryable_hint=True,
):
    from app.executor import PromptExecutionError
    from app.llm import ProviderError

    provider = ProviderError(
        "provider failed",
        kind=kind,
        http_status=http_status,
        retry_after_seconds=retry_after,
        retryable_hint=retryable_hint,
    )
    try:
        raise PromptExecutionError("prompt execution failed") from provider
    except PromptExecutionError as exc:
        return exc


def _workflow_for_retry_test(db, state):
    now = utc_now()
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            "wf-retry",
            "project-1",
            "WF-1_PROJECT_INTAKE",
            "RUNNING",
            2,
            json.dumps(state),
            now,
            now,
        ),
    )
    return {
        "id": "wf-retry",
        "project_id": "project-1",
        "workflow_type": "WF-1_PROJECT_INTAKE",
        "status": "RUNNING",
        "current_step": 2,
        "state": state,
    }


def test_same_node_empty_stream_retries_then_recovers_without_semantic_budget(tmp_path):
    from app.repair_ledger import RepairLedger
    from app.runtime_failures import ProviderFailureKind
    from app.workflows import WorkflowEngine

    db = make_executor_db(tmp_path)
    state = {
        "workflow_type": "WF-1_PROJECT_INTAKE",
        "options": {
            "provider_retry_limit": 2,
            "provider_retry_base_delay_seconds": 0,
        },
        "step_results": {},
    }
    wf = _workflow_for_retry_test(db, state)
    success = {"run_id": "run-ok", "status": "PASS", "output": {"status": "PASS"}}
    executor = SequencePromptExecutor(
        [
            _wrapped_provider_failure(ProviderFailureKind.EMPTY_STREAM),
            _wrapped_provider_failure(ProviderFailureKind.EMPTY_STREAM),
            success,
        ]
    )
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
            prompt_id="P-TEST",
            envelope={"payload": {}},
        )
    )

    assert result is success
    assert executor.calls == 3
    retry_key = "2:P-TEST"
    assert RepairLedger.count(state, "provider_retries", retry_key) == 2
    assert RepairLedger.count(state, "semantic_repairs", retry_key) == 0
    assert [
        item["event"] for item in RepairLedger.events(state, key=retry_key)
    ] == ["PROVIDER_RETRY", "PROVIDER_RETRY", "PROVIDER_RECOVERED"]
    assert "provider_wait" not in state
    assert db.fetchone(
        "SELECT COUNT(*) AS n FROM artifacts WHERE artifact_type='RUNTIME_FAILURE'"
    )["n"] == 2


def test_response_parse_failure_regenerates_with_distinct_attempt_identity(tmp_path):
    from app.repair_ledger import RepairLedger
    from app.runtime_failures import ProviderFailureKind
    from app.workflows import WorkflowEngine

    db = make_executor_db(tmp_path)
    state = {
        "workflow_type": "WF-1_PROJECT_INTAKE",
        "options": {
            "provider_retry_limit": 1,
            "provider_retry_base_delay_seconds": 0,
        },
        "step_results": {},
        "active_section_id": "section-1",
        "section_progress": {"section-1": {"phase": "BLUEPRINT_CRITIC"}},
    }
    wf = _workflow_for_retry_test(db, state)
    success = {"run_id": "run-ok", "status": "PASS", "output": {"status": "PASS"}}
    executor = SequencePromptExecutor(
        [
            _wrapped_provider_failure(
                ProviderFailureKind.RESPONSE_PARSE,
                retryable_hint=False,
            ),
            success,
        ]
    )
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
            prompt_id="P-WRITE-BLUEPRINT-CRITIC",
            envelope={"payload": {}},
            call_key="call-section-review",
        )
    )

    assert result is success
    assert executor.calls == 2
    call_keys = [item["call_key"] for item in executor.call_kwargs]
    assert len(set(call_keys)) == 2
    assert call_keys[0].startswith("call-section-review-cycle-")
    assert call_keys[0].endswith("-attempt-1")
    assert call_keys[1].endswith("-attempt-2")
    cycle = state["provider_call_cycles"][
        "2:P-WRITE-BLUEPRINT-CRITIC:section-1:BLUEPRINT_CRITIC"
    ]
    assert cycle["input_hash"] == sha256_json({"payload": {}})
    retry_key = "2:P-WRITE-BLUEPRINT-CRITIC:section-1:BLUEPRINT_CRITIC"
    assert RepairLedger.count(state, "provider_retries", retry_key) == 1
    assert RepairLedger.count(state, "semantic_repairs", retry_key) == 0


def test_same_node_retry_exhaustion_reports_total_attempts(tmp_path):
    from app.retry_policy import ProviderRetriesExhausted
    from app.runtime_failures import ProviderFailureKind
    from app.workflows import WorkflowEngine

    db = make_executor_db(tmp_path)
    state = {
        "workflow_type": "WF-1_PROJECT_INTAKE",
        "options": {
            "provider_retry_limit": 1,
            "provider_retry_base_delay_seconds": 0,
        },
        "step_results": {},
    }
    wf = _workflow_for_retry_test(db, state)
    executor = SequencePromptExecutor(
        [
            _wrapped_provider_failure(ProviderFailureKind.TRANSPORT),
            _wrapped_provider_failure(ProviderFailureKind.TRANSPORT),
        ]
    )
    engine = WorkflowEngine(
        db,
        SimpleNamespace(),
        SimpleNamespace(),
        executor,
        SimpleNamespace(),
    )

    with pytest.raises(ProviderRetriesExhausted) as captured:
        asyncio.run(
            engine._execute_prompt_with_provider_retry(
                wf,
                state,
                prompt_id="P-TEST",
                envelope={"payload": {}},
            )
        )

    assert executor.calls == 2
    assert captured.value.decision.completed_attempts == 2
    assert captured.value.decision.max_retries == 1
    assert captured.value.decision.exhausted_status == "BLOCKED_PROVIDER"
    assert state["provider_wait"]["completed_attempts"] == 2
    assert state["provider_wait"]["decision"]["should_retry"] is False


def test_provider_retry_resumes_same_inflight_attempt_after_process_crash(tmp_path):
    from app.runtime_failures import ProviderFailureKind
    from app.workflows import WorkflowEngine

    db = make_executor_db(tmp_path)
    state = {
        "workflow_type": "WF-1_PROJECT_INTAKE",
        "options": {
            "provider_retry_limit": 2,
            "provider_retry_base_delay_seconds": 0,
        },
        "step_results": {},
    }
    wf = _workflow_for_retry_test(db, state)
    first_executor = SequencePromptExecutor(
        [
            _wrapped_provider_failure(ProviderFailureKind.TRANSPORT),
            SimulatedProcessCrash("process exited during attempt 2"),
        ]
    )
    first_engine = WorkflowEngine(
        db,
        SimpleNamespace(),
        SimpleNamespace(),
        first_executor,
        SimpleNamespace(),
    )

    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(
            first_engine._execute_prompt_with_provider_retry(
                wf,
                state,
                prompt_id="P-TEST",
                envelope={"payload": {"value": 1}},
            )
        )

    persisted = first_engine.get("wf-retry")
    persisted_state = persisted["state"]
    retry_key = "2:P-TEST"
    assert persisted_state["provider_wait"]["completed_attempts"] == 1
    assert persisted_state["provider_wait"]["attempt_in_flight"] == 2
    assert persisted_state["provider_call_cycles"][retry_key]["attempt_in_flight"] == 2

    success = {"run_id": "run-ok", "status": "PASS", "output": {"status": "PASS"}}
    resumed_executor = SequencePromptExecutor([success])
    resumed_engine = WorkflowEngine(
        db,
        SimpleNamespace(),
        SimpleNamespace(),
        resumed_executor,
        SimpleNamespace(),
    )
    result = asyncio.run(
        resumed_engine._execute_prompt_with_provider_retry(
            persisted,
            persisted_state,
            prompt_id="P-TEST",
            envelope={"payload": {"value": 1}},
        )
    )

    assert result is success
    assert resumed_executor.calls == 1
    assert resumed_executor.call_kwargs[0]["call_key"].endswith("-attempt-2")
    assert "provider_wait" not in persisted_state
    assert persisted_state["provider_call_cycles"][retry_key]["completed_attempts"] == 2
    assert "attempt_in_flight" not in persisted_state["provider_call_cycles"][retry_key]


def test_provider_retry_replays_committed_success_after_outer_crash_without_new_attempt(tmp_path):
    from app.repair_ledger import RepairLedger
    from app.workflows import WorkflowEngine

    db = make_executor_db(tmp_path)
    state = {
        "workflow_type": "WF-1_PROJECT_INTAKE",
        "options": {
            "provider_retry_limit": 2,
            "provider_retry_base_delay_seconds": 0,
        },
        "step_results": {},
    }
    wf = _workflow_for_retry_test(db, state)
    gateway = CountingGateway()
    executor = RuntimePromptExecutor(
        db, ExecutorPack(), ExecutorRouter(), gateway, quality_guard_enabled=False
    )
    engine = WorkflowEngine(
        db, SimpleNamespace(), SimpleNamespace(), executor, SimpleNamespace()
    )
    envelope = {
        "security_context": {"input_max_security_level": "INTERNAL"},
        "payload": {"value": 1},
    }

    first = asyncio.run(
        engine._execute_prompt_with_provider_retry(
            wf, state, prompt_id="P-TEST", envelope=envelope
        )
    )
    # Simulate a process exit after the retry wrapper persisted the successful
    # call checkpoint but before its caller stored the business step result.
    persisted = engine.get("wf-retry")
    second = asyncio.run(
        engine._execute_prompt_with_provider_retry(
            persisted,
            persisted["state"],
            prompt_id="P-TEST",
            envelope=envelope,
        )
    )

    retry_key = "2:P-TEST"
    cycle = persisted["state"]["provider_call_cycles"][retry_key]
    assert first["run_id"] == second["run_id"]
    assert second["reused_committed_result"] is True
    assert gateway.calls == 1
    assert cycle["completed_attempts"] == 1
    assert cycle["successful_attempt"] == 1
    assert RepairLedger.count(persisted["state"], "provider_retries", retry_key) == 0


def test_provider_retry_starts_new_cycle_after_provider_request_spec_change(
    monkeypatch, tmp_path
):
    from app.workflows import WorkflowEngine

    db = make_executor_db(tmp_path)
    state = {
        "workflow_type": "WF-1_PROJECT_INTAKE",
        "options": {
            "provider_retry_limit": 2,
            "provider_retry_base_delay_seconds": 0,
        },
        "step_results": {},
    }
    wf = _workflow_for_retry_test(db, state)
    gateway = CountingGateway()
    pack = ExecutorPack()
    executor = RuntimePromptExecutor(
        db, pack, ExecutorRouter(), gateway, quality_guard_enabled=False
    )
    engine = WorkflowEngine(
        db, SimpleNamespace(), SimpleNamespace(), executor, SimpleNamespace()
    )
    envelope = {
        "security_context": {"input_max_security_level": "INTERNAL"},
        "payload": {"value": 1},
    }

    first = asyncio.run(
        engine._execute_prompt_with_provider_retry(
            wf, state, prompt_id="P-TEST", envelope=envelope
        )
    )
    first_cycle = dict(state["provider_call_cycles"]["2:P-TEST"])
    monkeypatch.setattr(
        executor, "provider_request_spec_hash", lambda _prompt_id: "changed-provider-request-spec"
    )
    persisted = engine.get("wf-retry")
    second = asyncio.run(
        engine._execute_prompt_with_provider_retry(
            persisted,
            persisted["state"],
            prompt_id="P-TEST",
            envelope=envelope,
        )
    )

    second_cycle = persisted["state"]["provider_call_cycles"]["2:P-TEST"]
    assert gateway.calls == 2
    assert first["run_id"] != second["run_id"]
    assert second_cycle["generation"] == first_cycle["generation"] + 1
    assert second_cycle["previous_cycle_id"] == first_cycle["cycle_id"]
    assert second_cycle["provider_request_spec_hash"] != first_cycle["provider_request_spec_hash"]


def test_recoverable_executor_fault_keeps_attempt_inflight_for_exact_commit_reuse(
    monkeypatch, tmp_path
):
    from app.workflows import WorkflowEngine

    db = make_executor_db(tmp_path)
    state = {
        "workflow_type": "WF-1_PROJECT_INTAKE",
        "options": {
            "provider_retry_limit": 2,
            "provider_retry_base_delay_seconds": 0,
        },
        "step_results": {},
    }
    wf = _workflow_for_retry_test(db, state)
    gateway = CountingGateway()
    executor = RuntimePromptExecutor(
        db, ExecutorPack(), ExecutorRouter(), gateway, quality_guard_enabled=False
    )
    engine = WorkflowEngine(
        db, SimpleNamespace(), SimpleNamespace(), executor, SimpleNamespace()
    )
    envelope = {
        "security_context": {"input_max_security_level": "INTERNAL"},
        "payload": {"value": 1},
    }
    monkeypatch.setenv("RUNTIME_FAULT_POINT", "after_db_transaction")

    with pytest.raises(RecoverablePromptExecutionError):
        asyncio.run(
            engine._execute_prompt_with_provider_retry(
                wf, state, prompt_id="P-TEST", envelope=envelope
            )
        )

    persisted = engine.get("wf-retry")
    wait = persisted["state"]["provider_wait"]
    assert wait["phase"] == "CALLING"
    assert wait["completed_attempts"] == 0
    assert wait["attempt_in_flight"] == 1

    monkeypatch.delenv("RUNTIME_FAULT_POINT", raising=False)
    result = asyncio.run(
        engine._execute_prompt_with_provider_retry(
            persisted,
            persisted["state"],
            prompt_id="P-TEST",
            envelope=envelope,
        )
    )

    assert result["reused_committed_result"] is True
    assert gateway.calls == 1
    cycle = persisted["state"]["provider_call_cycles"]["2:P-TEST"]
    assert cycle["completed_attempts"] == 1
    assert cycle["successful_attempt"] == 1


def test_persisted_failed_call_is_replayed_without_duplicate_provider_invocation(
    monkeypatch, tmp_path
):
    from app.llm import ProviderError
    from app.repair_ledger import RepairLedger
    from app.runtime_failures import ProviderFailureKind
    from app.workflows import WorkflowEngine

    db = make_executor_db(tmp_path)
    state = {
        "workflow_type": "WF-1_PROJECT_INTAKE",
        "options": {
            "provider_retry_limit": 2,
            "provider_retry_base_delay_seconds": 0,
        },
        "step_results": {},
    }
    wf = _workflow_for_retry_test(db, state)
    success_output = {
        "status": "PASS",
        "result": {"value": 2},
        "warnings": [],
        "user_questions": [],
    }
    gateway = SequenceGateway(
        [
            ProviderError(
                "provider transport failed",
                kind=ProviderFailureKind.TRANSPORT,
                retryable_hint=True,
            ),
            LLMResult(
                output=success_output,
                raw_text=json.dumps(success_output),
                model_id="model-1",
                endpoint_id="endpoint-1",
            ),
        ]
    )
    executor = RuntimePromptExecutor(
        db, ExecutorPack(), ExecutorRouter(), gateway, quality_guard_enabled=False
    )
    engine = WorkflowEngine(
        db, SimpleNamespace(), SimpleNamespace(), executor, SimpleNamespace()
    )
    envelope = {
        "security_context": {"input_max_security_level": "INTERNAL"},
        "payload": {"value": 1},
    }
    original_commit_error = executor._commit_error

    def commit_error_then_crash(**kwargs):
        original_commit_error(**kwargs)
        raise SimulatedProcessCrash("process exited after MODEL_CALL_FAILED commit")

    monkeypatch.setattr(executor, "_commit_error", commit_error_then_crash)
    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(
            engine._execute_prompt_with_provider_retry(
                wf, state, prompt_id="P-TEST", envelope=envelope
            )
        )
    monkeypatch.setattr(executor, "_commit_error", original_commit_error)

    persisted = engine.get("wf-retry")
    result = asyncio.run(
        engine._execute_prompt_with_provider_retry(
            persisted,
            persisted["state"],
            prompt_id="P-TEST",
            envelope=envelope,
        )
    )

    retry_key = "2:P-TEST"
    cycle = persisted["state"]["provider_call_cycles"][retry_key]
    assert result["status"] == "PASS"
    assert gateway.calls == 2
    assert cycle["completed_attempts"] == 2
    assert cycle["successful_attempt"] == 2
    assert RepairLedger.count(persisted["state"], "provider_retries", retry_key) == 1
    assert db.fetchone("SELECT COUNT(*) AS n FROM prompt_runs")["n"] == 2


def test_provider_retry_reconstructs_missing_ledger_after_failure_checkpoint_crash(
    monkeypatch, tmp_path
):
    from app.repair_ledger import RepairLedger
    from app.runtime_failures import ProviderFailureKind
    from app.workflows import WorkflowEngine

    db = make_executor_db(tmp_path)
    state = {
        "workflow_type": "WF-1_PROJECT_INTAKE",
        "options": {
            "provider_retry_limit": 2,
            "provider_retry_base_delay_seconds": 0,
        },
        "step_results": {},
    }
    wf = _workflow_for_retry_test(db, state)
    first_executor = SequencePromptExecutor(
        [_wrapped_provider_failure(ProviderFailureKind.TRANSPORT)]
    )
    first_engine = WorkflowEngine(
        db, SimpleNamespace(), SimpleNamespace(), first_executor, SimpleNamespace()
    )
    original_update = first_engine._update
    crashed = False

    def crash_after_failed_checkpoint(workflow, **kwargs):
        nonlocal crashed
        original_update(workflow, **kwargs)
        checkpoint = (kwargs.get("state") or {}).get("provider_wait") or {}
        if (
            not crashed
            and checkpoint.get("phase") == "FAILED"
            and (checkpoint.get("decision") or {}).get("should_retry") is True
        ):
            crashed = True
            raise SimulatedProcessCrash(
                "process exited after failed-attempt checkpoint commit"
            )

    monkeypatch.setattr(first_engine, "_update", crash_after_failed_checkpoint)
    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(
            first_engine._execute_prompt_with_provider_retry(
                wf,
                state,
                prompt_id="P-TEST",
                envelope={"payload": {"value": 1}},
            )
        )

    persisted = first_engine.get("wf-retry")
    retry_key = "2:P-TEST"
    assert RepairLedger.count(persisted["state"], "provider_retries", retry_key) == 0

    success = {"run_id": "run-ok", "status": "PASS", "output": {"status": "PASS"}}
    resumed_executor = SequencePromptExecutor([success])
    resumed_engine = WorkflowEngine(
        db, SimpleNamespace(), SimpleNamespace(), resumed_executor, SimpleNamespace()
    )
    asyncio.run(
        resumed_engine._execute_prompt_with_provider_retry(
            persisted,
            persisted["state"],
            prompt_id="P-TEST",
            envelope={"payload": {"value": 1}},
        )
    )

    assert RepairLedger.count(persisted["state"], "provider_retries", retry_key) == 1
    retry_events = [
        item
        for item in RepairLedger.events(persisted["state"], key=retry_key)
        if item["event"] == "PROVIDER_RETRY"
    ]
    assert len(retry_events) == 1
    assert retry_events[0]["details"]["restored_from_checkpoint"] is True
    assert retry_events[0]["details"]["completed_attempts"] == 1


def test_provider_retry_honors_remaining_delay_after_checkpoint_restart(
    monkeypatch, tmp_path
):
    from app.runtime_failures import ProviderFailureKind
    from app.workflows import WorkflowEngine

    db = make_executor_db(tmp_path)
    state = {
        "workflow_type": "WF-1_PROJECT_INTAKE",
        "options": {
            "provider_retry_limit": 2,
            "provider_retry_base_delay_seconds": 30,
            "provider_retry_max_delay_seconds": 30,
        },
        "step_results": {},
    }
    wf = _workflow_for_retry_test(db, state)
    first_executor = SequencePromptExecutor(
        [_wrapped_provider_failure(ProviderFailureKind.TRANSPORT)]
    )
    first_engine = WorkflowEngine(
        db, SimpleNamespace(), SimpleNamespace(), first_executor, SimpleNamespace()
    )
    original_update = first_engine._update
    crashed = False

    def crash_after_failed_checkpoint(workflow, **kwargs):
        nonlocal crashed
        original_update(workflow, **kwargs)
        checkpoint = (kwargs.get("state") or {}).get("provider_wait") or {}
        if not crashed and checkpoint.get("phase") == "FAILED":
            crashed = True
            raise SimulatedProcessCrash("process exited before retry delay")

    monkeypatch.setattr(first_engine, "_update", crash_after_failed_checkpoint)
    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(
            first_engine._execute_prompt_with_provider_retry(
                wf,
                state,
                prompt_id="P-TEST",
                envelope={"payload": {"value": 1}},
            )
        )

    persisted = first_engine.get("wf-retry")
    assert persisted["state"]["provider_wait"]["retry_not_before"]
    observed_delays: list[float] = []

    async def record_sleep(delay: float):
        observed_delays.append(delay)

    monkeypatch.setattr("app.workflows.asyncio.sleep", record_sleep)
    resumed_executor = SequencePromptExecutor(
        [{"run_id": "run-ok", "status": "PASS", "output": {"status": "PASS"}}]
    )
    resumed_engine = WorkflowEngine(
        db, SimpleNamespace(), SimpleNamespace(), resumed_executor, SimpleNamespace()
    )
    asyncio.run(
        resumed_engine._execute_prompt_with_provider_retry(
            persisted,
            persisted["state"],
            prompt_id="P-TEST",
            envelope={"payload": {"value": 1}},
        )
    )

    assert len(observed_delays) == 1
    assert 0 < observed_delays[0] <= 30


def test_provider_retry_reuses_persisted_base_call_key_after_restart(tmp_path):
    from app.runtime_failures import ProviderFailureKind
    from app.workflows import WorkflowEngine

    db = make_executor_db(tmp_path)
    state = {
        "workflow_type": "WF-1_PROJECT_INTAKE",
        "options": {
            "provider_retry_limit": 2,
            "provider_retry_base_delay_seconds": 0,
        },
        "step_results": {},
    }
    wf = _workflow_for_retry_test(db, state)
    first_executor = SequencePromptExecutor(
        [
            _wrapped_provider_failure(ProviderFailureKind.TRANSPORT),
            SimulatedProcessCrash("process exited during attempt 2"),
        ]
    )
    first_engine = WorkflowEngine(
        db, SimpleNamespace(), SimpleNamespace(), first_executor, SimpleNamespace()
    )

    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(
            first_engine._execute_prompt_with_provider_retry(
                wf,
                state,
                prompt_id="P-TEST",
                envelope={"payload": {"value": 1}},
                call_key="call-original",
            )
        )

    persisted = first_engine.get("wf-retry")
    success = {"run_id": "run-ok", "status": "PASS", "output": {"status": "PASS"}}
    resumed_executor = SequencePromptExecutor([success])
    resumed_engine = WorkflowEngine(
        db, SimpleNamespace(), SimpleNamespace(), resumed_executor, SimpleNamespace()
    )
    asyncio.run(
        resumed_engine._execute_prompt_with_provider_retry(
            persisted,
            persisted["state"],
            prompt_id="P-TEST",
            envelope={"payload": {"value": 1}},
            call_key="call-drifted",
        )
    )

    resumed_call_key = resumed_executor.call_kwargs[0]["call_key"]
    assert resumed_call_key.startswith("call-original-cycle-")
    assert not resumed_call_key.startswith("call-drifted-cycle-")





def test_legacy_provider_cycle_migrates_without_resetting_inflight_attempt(tmp_path):
    from app.workflows import WorkflowEngine

    db = make_executor_db(tmp_path)
    envelope = {"payload": {"value": 1}}
    input_hash = sha256_json(envelope)
    retry_key = "2:P-TEST"
    cycle_id = "legacy-cycle"
    state = {
        "workflow_type": "WF-1_PROJECT_INTAKE",
        "options": {
            "provider_retry_limit": 2,
            "provider_retry_base_delay_seconds": 0,
        },
        "step_results": {},
        "provider_call_cycles": {
            retry_key: {
                "cycle_id": cycle_id,
                "generation": 1,
                "input_hash": input_hash,
                "protocol_version": MODEL_RESPONSE_PROTOCOL_VERSION,
                "completed_attempts": 1,
                "attempt_in_flight": 2,
            }
        },
        "provider_wait": {
            "retry_key": retry_key,
            "cycle_id": cycle_id,
            "input_hash": input_hash,
            "protocol_version": MODEL_RESPONSE_PROTOCOL_VERSION,
            "completed_attempts": 1,
            "attempt_in_flight": 2,
            "retry_limit": 2,
            "max_attempts": 3,
            "prompt_id": "P-TEST",
            "phase": "CALLING",
        },
    }
    wf = _workflow_for_retry_test(db, state)
    success = {"run_id": "run-ok", "status": "PASS", "output": {"status": "PASS"}}
    executor = SequencePromptExecutor([success])
    executor.provider_request_spec_hash = lambda _prompt_id: "provider-spec-current"
    engine = WorkflowEngine(
        db, SimpleNamespace(), SimpleNamespace(), executor, SimpleNamespace()
    )

    result = asyncio.run(
        engine._execute_prompt_with_provider_retry(
            wf,
            state,
            prompt_id="P-TEST",
            envelope=envelope,
        )
    )

    legacy_base = "call-provider-" + sha256_json(
        {
            "workflow_id": "wf-retry",
            "retry_key": retry_key,
            "input_hash": input_hash,
            "protocol_version": MODEL_RESPONSE_PROTOCOL_VERSION,
        }
    )[:24]
    cycle = state["provider_call_cycles"][retry_key]
    assert result is success
    assert executor.calls == 1
    assert executor.call_kwargs[0]["call_key"] == (
        f"{legacy_base}-cycle-{cycle_id}-attempt-2"
    )
    assert cycle["generation"] == 1
    assert cycle["completed_attempts"] == 2
    assert cycle["provider_request_spec_hash"] == "provider-spec-current"
    assert cycle["base_call_key"] == legacy_base
    assert cycle["request_spec_identity_migrated_at"]


def test_persisted_retry_delay_is_capped_for_malformed_future_timestamp(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app.workflows import WorkflowEngine

    observed: list[float] = []

    async def record_sleep(delay: float):
        observed.append(delay)

    monkeypatch.setattr("app.workflows.asyncio.sleep", record_sleep)
    asyncio.run(
        WorkflowEngine._honor_persisted_retry_delay(
            {
                "retry_not_before": (
                    datetime.now(timezone.utc) + timedelta(days=365)
                ).isoformat()
            }
        )
    )

    assert observed == [300.0]

def test_concurrent_restart_cannot_invoke_same_provider_attempt_twice(tmp_path):
    from app.workflows import WorkflowEngine

    db = make_executor_db(tmp_path)
    state = {
        "workflow_type": "WF-1_PROJECT_INTAKE",
        "options": {
            "provider_retry_limit": 2,
            "provider_retry_base_delay_seconds": 0,
        },
        "step_results": {},
    }
    wf = _workflow_for_retry_test(db, state)
    crashing_executor = SequencePromptExecutor(
        [SimulatedProcessCrash("process exited during attempt 1")]
    )
    crashing_engine = WorkflowEngine(
        db, SimpleNamespace(), SimpleNamespace(), crashing_executor, SimpleNamespace()
    )
    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(
            crashing_engine._execute_prompt_with_provider_retry(
                wf,
                state,
                prompt_id="P-TEST",
                envelope={"payload": {"value": 1}},
            )
        )

    first_snapshot = crashing_engine.get("wf-retry")
    stale_snapshot = crashing_engine.get("wf-retry")
    first_success = {
        "run_id": "run-first",
        "status": "PASS",
        "output": {"status": "PASS"},
    }
    first_executor = SequencePromptExecutor([first_success])
    first_engine = WorkflowEngine(
        db, SimpleNamespace(), SimpleNamespace(), first_executor, SimpleNamespace()
    )
    result = asyncio.run(
        first_engine._execute_prompt_with_provider_retry(
            first_snapshot,
            first_snapshot["state"],
            prompt_id="P-TEST",
            envelope={"payload": {"value": 1}},
        )
    )
    assert result is first_success
    assert first_executor.calls == 1

    duplicate_executor = SequencePromptExecutor(
        [{"run_id": "run-duplicate", "status": "PASS", "output": {"status": "PASS"}}]
    )
    duplicate_engine = WorkflowEngine(
        db, SimpleNamespace(), SimpleNamespace(), duplicate_executor, SimpleNamespace()
    )
    with pytest.raises(RuntimeError, match="workflow changed during atomic update"):
        asyncio.run(
            duplicate_engine._execute_prompt_with_provider_retry(
                stale_snapshot,
                stale_snapshot["state"],
                prompt_id="P-TEST",
                envelope={"payload": {"value": 1}},
            )
        )
    assert duplicate_executor.calls == 0

def test_configuration_recovery_starts_fresh_provider_generation(tmp_path):
    from app.executor import PromptExecutionError
    from app.llm import ProviderError
    from app.runtime_failures import ProviderFailureKind
    from app.workflows import WorkflowEngine

    db = make_executor_db(tmp_path)
    state = {
        "workflow_type": "WF-1_PROJECT_INTAKE",
        "options": {
            "provider_retry_limit": 2,
            "provider_retry_base_delay_seconds": 0,
        },
        "step_results": {},
    }
    wf = _workflow_for_retry_test(db, state)
    success_output = {
        "status": "PASS",
        "result": {"value": 2},
        "warnings": [],
        "user_questions": [],
    }
    gateway = SequenceGateway(
        [
            ProviderError(
                "credential rejected",
                kind=ProviderFailureKind.HTTP_STATUS,
                http_status=401,
                retryable_hint=False,
            ),
            LLMResult(
                output=success_output,
                raw_text=json.dumps(success_output),
                model_id="model-1",
                endpoint_id="endpoint-1",
            ),
        ]
    )
    executor = RuntimePromptExecutor(
        db, ExecutorPack(), ExecutorRouter(), gateway, quality_guard_enabled=False
    )
    engine = WorkflowEngine(
        db, SimpleNamespace(), SimpleNamespace(), executor, SimpleNamespace()
    )
    envelope = {
        "security_context": {"input_max_security_level": "INTERNAL"},
        "payload": {"value": 1},
    }

    with pytest.raises(PromptExecutionError):
        asyncio.run(
            engine._execute_prompt_with_provider_retry(
                wf, state, prompt_id="P-TEST", envelope=envelope
            )
        )
    assert state["provider_wait"]["exhausted_status"] == "WAITING_CONFIGURATION"
    first_cycle = dict(state["provider_call_cycles"]["2:P-TEST"])
    engine._update(wf, status="WAITING_CONFIGURATION", state=state)
    waiting = engine.get("wf-retry")
    assert engine._seal_persisted_provider_exhaustion(
        waiting, waiting["state"]
    ) is False
    state = waiting["state"]

    archived = engine._invalidate_provider_checkpoint_after_configuration_recovery(
        state
    )
    assert archived is not None
    assert archived["http_status"] == 401
    assert "provider_wait" not in state
    assert state["provider_call_cycles"]["2:P-TEST"]["force_new_generation"] is True
    engine._update(waiting, status="RUNNING", state=state)
    resumed = engine.get("wf-retry")

    result = asyncio.run(
        engine._execute_prompt_with_provider_retry(
            resumed,
            resumed["state"],
            prompt_id="P-TEST",
            envelope=envelope,
        )
    )

    second_cycle = resumed["state"]["provider_call_cycles"]["2:P-TEST"]
    assert result["status"] == "PASS"
    assert gateway.calls == 2
    assert second_cycle["generation"] == first_cycle["generation"] + 1
    assert second_cycle["previous_cycle_id"] == first_cycle["cycle_id"]
    assert second_cycle["cycle_id"] != first_cycle["cycle_id"]
    assert db.fetchone("SELECT COUNT(*) AS n FROM prompt_runs")["n"] == 2


def test_sealed_configuration_exhaustion_reaches_recheck_and_invalidates_checkpoint(
    monkeypatch, tmp_path
):
    from app.dependency_preflight import DependencyReport
    from app.workflow_defs import WORKFLOWS
    from app.workflows import WorkflowEngine

    db = make_executor_db(tmp_path)
    now = utc_now()
    retry_key = "0:P-TEST"
    state = {
        "workflow_type": "WF-1_PROJECT_INTAKE",
        "options": {},
        "step_results": {},
        "configuration_wait": {"issues": [{"code": "MODEL_CREDENTIAL_INVALID"}]},
        "last_error": "credential rejected",
        "provider_call_cycles": {
            retry_key: {
                "cycle_id": "cycle-old",
                "generation": 1,
                "input_hash": "input-old",
                "protocol_version": MODEL_RESPONSE_PROTOCOL_VERSION,
                "completed_attempts": 1,
            }
        },
        "provider_wait": {
            "retry_key": retry_key,
            "cycle_id": "cycle-old",
            "input_hash": "input-old",
            "protocol_version": MODEL_RESPONSE_PROTOCOL_VERSION,
            "completed_attempts": 1,
            "category": "CONFIGURATION_ERROR",
            "workflow_status": "WAITING_CONFIGURATION",
            "exhausted_status": "WAITING_CONFIGURATION",
            "http_status": 401,
            "decision": {
                "should_retry": False,
                "exhausted_status": "WAITING_CONFIGURATION",
            },
            "exhausted": True,
        },
    }
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            "wf-config-provider",
            "project-1",
            "WF-1_PROJECT_INTAKE",
            "WAITING_CONFIGURATION",
            len(WORKFLOWS["WF-1_PROJECT_INTAKE"]),
            json.dumps(state),
            now,
            now,
        ),
    )
    quality = SimpleNamespace(open_blockers=lambda *args, **kwargs: [])
    engine = WorkflowEngine(
        db,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        quality_manager=quality,
        dependency_preflight=SimpleNamespace(),
    )
    monkeypatch.setattr(
        engine,
        "_configuration_recheck_report",
        lambda workflow, workflow_state: DependencyReport(scope="TEST"),
    )

    result = asyncio.run(engine.advance("wf-config-provider"))

    assert result["status"] == "COMPLETED"
    assert "provider_wait" not in result["state"]
    cycle = result["state"]["provider_call_cycles"][retry_key]
    assert cycle["force_new_generation"] is True
    assert cycle["invalidation_reason"] == "CONFIGURATION_RECOVERED"
    assert result["state"]["configuration_recovered"]["provider_checkpoint"][
        "http_status"
    ] == 401
    assert "last_error" not in result["state"]

def test_advance_seals_persisted_provider_exhaustion_without_new_model_call(tmp_path):
    from app.retry_policy import ProviderRetriesExhausted
    from app.runtime_failures import ProviderFailureKind
    from app.workflows import WorkflowEngine

    db = make_executor_db(tmp_path)
    state = {
        "workflow_type": "WF-1_PROJECT_INTAKE",
        "options": {
            "provider_retry_limit": 1,
            "provider_retry_base_delay_seconds": 0,
        },
        "step_results": {},
    }
    wf = _workflow_for_retry_test(db, state)
    failing_executor = SequencePromptExecutor(
        [
            _wrapped_provider_failure(ProviderFailureKind.TRANSPORT),
            _wrapped_provider_failure(ProviderFailureKind.TRANSPORT),
        ]
    )
    first_engine = WorkflowEngine(
        db,
        SimpleNamespace(),
        SimpleNamespace(),
        failing_executor,
        SimpleNamespace(),
    )
    with pytest.raises(ProviderRetriesExhausted):
        asyncio.run(
            first_engine._execute_prompt_with_provider_retry(
                wf,
                state,
                prompt_id="P-TEST",
                envelope={"payload": {}},
            )
        )

    persisted = first_engine.get("wf-retry")
    assert persisted["status"] == "RUNNING"
    assert persisted["state"]["provider_wait"]["decision"]["should_retry"] is False

    no_call_executor = SequencePromptExecutor([])
    recovery_engine = WorkflowEngine(
        db,
        SimpleNamespace(),
        SimpleNamespace(),
        no_call_executor,
        SimpleNamespace(),
    )
    blocked = asyncio.run(recovery_engine.advance("wf-retry"))

    assert blocked["status"] == "BLOCKED_PROVIDER"
    assert no_call_executor.calls == 0
    assert blocked["state"]["provider_wait"]["exhausted"] is True
    assert blocked["state"]["provider_wait"]["boundary"] == "CRASH_RECOVERY"
    audit = db.fetchone(
        "SELECT metadata_json FROM audit_events "
        "WHERE object_id=? AND event_type='PROVIDER_RETRIES_EXHAUSTED_RECOVERED' "
        "ORDER BY id DESC LIMIT 1",
        ("wf-retry",),
    )
    assert json.loads(audit["metadata_json"])["completed_attempts"] == 2


def test_provider_retry_input_change_starts_fresh_cycle_at_attempt_one(tmp_path):
    from app.llm import MODEL_RESPONSE_PROTOCOL_VERSION
    from app.workflows import WorkflowEngine

    db = make_executor_db(tmp_path)
    old_envelope = {"payload": {"value": "old"}}
    new_envelope = {"payload": {"value": "new"}}
    retry_key = "2:P-TEST"
    state = {
        "workflow_type": "WF-1_PROJECT_INTAKE",
        "options": {
            "provider_retry_limit": 2,
            "provider_retry_base_delay_seconds": 0,
        },
        "step_results": {},
        "provider_call_cycles": {
            retry_key: {
                "cycle_id": "old-cycle",
                "generation": 1,
                "input_hash": sha256_json(old_envelope),
                "protocol_version": MODEL_RESPONSE_PROTOCOL_VERSION,
                "completed_attempts": 2,
            }
        },
        "provider_wait": {
            "retry_key": retry_key,
            "cycle_id": "old-cycle",
            "input_hash": sha256_json(old_envelope),
            "protocol_version": MODEL_RESPONSE_PROTOCOL_VERSION,
            "completed_attempts": 2,
            "decision": {"should_retry": True},
        },
    }
    wf = _workflow_for_retry_test(db, state)
    success = {"run_id": "run-new", "status": "PASS", "output": {"status": "PASS"}}
    executor = SequencePromptExecutor([success])
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
            prompt_id="P-TEST",
            envelope=new_envelope,
        )
    )

    assert result is success
    assert executor.call_kwargs[0]["call_key"].endswith("-attempt-1")
    cycle = state["provider_call_cycles"][retry_key]
    assert cycle["generation"] == 2
    assert cycle["cycle_id"] != "old-cycle"
    assert cycle["input_hash"] == sha256_json(new_envelope)
    assert cycle["completed_attempts"] == 1


def test_provider_exhaustion_has_same_typed_owner_at_section_boundary(tmp_path):
    from app.retry_policy import ProviderRetriesExhausted
    from app.runtime_failures import ProviderFailureKind
    from app.workflows import WorkflowEngine

    db = make_executor_db(tmp_path)
    state = {
        "workflow_type": "WF-4_PROPOSAL_AUTHORING",
        "options": {
            "provider_retry_limit": 0,
            "provider_retry_base_delay_seconds": 0,
        },
        "step_results": {},
        "runtime_recoverable": True,
        "runtime_failure_point": "WORKFLOW_ADVANCE",
        "runtime_blocked_at": utc_now(),
    }
    wf = _workflow_for_retry_test(db, state)
    executor = SequencePromptExecutor(
        [_wrapped_provider_failure(ProviderFailureKind.RESPONSE_PARSE)]
    )
    engine = WorkflowEngine(
        db,
        SimpleNamespace(),
        SimpleNamespace(),
        executor,
        SimpleNamespace(),
    )

    with pytest.raises(ProviderRetriesExhausted) as captured:
        asyncio.run(
            engine._execute_prompt_with_provider_retry(
                wf,
                state,
                prompt_id="P-WRITE-CRITIC",
                envelope={"payload": {}},
            )
        )

    blocked = engine._block_provider_retries_exhausted(
        wf,
        state,
        captured.value,
        boundary="WRITE_SECTIONS",
    )

    assert blocked["status"] == captured.value.decision.exhausted_status
    assert blocked["state"]["provider_wait"]["exhausted"] is True
    assert blocked["state"]["provider_wait"]["boundary"] == "WRITE_SECTIONS"
    assert "runtime_failure_point" not in blocked["state"]
    audit = db.fetchone(
        "SELECT metadata_json FROM audit_events "
        "WHERE object_id=? AND event_type='PROVIDER_RETRIES_EXHAUSTED' "
        "ORDER BY id DESC LIMIT 1",
        (wf["id"],),
    )
    assert json.loads(audit["metadata_json"])["prompt_id"] == "P-WRITE-CRITIC"


def test_protocol_upgrade_recovers_only_exhausted_provider_checkpoint(tmp_path):
    from app.llm import MODEL_RESPONSE_PROTOCOL_VERSION
    from app.repair_ledger import RepairLedger
    from app.workflows import WorkflowEngine

    db = make_executor_db(tmp_path)
    retry_key = "5:P-WRITE-CONTENT:new-objective:CONTENT"
    state = {
        "active_section_id": "new-objective",
        "section_progress": {
            "new-objective": {"phase": "CONTENT", "status": "RUNNING"}
        },
        "provider_wait": {
            "retry_key": retry_key,
            "exhausted": True,
            "failure": {"retryable": True, "failure_kind": "RESPONSE_SHAPE"},
        },
        "provider_call_cycles": {
            retry_key: {
                "protocol_version": "older-provider-protocol",
                "cycle_id": "old-cycle",
            }
        },
        "last_error": "old provider response shape failed",
    }
    RepairLedger.provider_retry(state, retry_key)
    now = utc_now()
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            "wf-protocol-recovery",
            "project-1",
            "WF-4_PROPOSAL_AUTHORING",
            "BLOCKED_CONTRACT",
            5,
            json.dumps(state),
            now,
            now,
        ),
    )
    engine = WorkflowEngine(
        db,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
    )
    wf = engine.get("wf-protocol-recovery")

    assert engine._recover_provider_block_after_protocol_upgrade(wf, wf["state"])

    recovered = engine.get("wf-protocol-recovery")
    recovered_state = recovered["state"]
    assert recovered["status"] == "RUNNING"
    assert recovered_state["section_progress"]["new-objective"]["phase"] == "CONTENT"
    assert "provider_wait" not in recovered_state
    assert "last_error" not in recovered_state
    assert RepairLedger.count(recovered_state, "provider_retries", retry_key) == 1
    recovery = recovered_state["checkpoint_recovery_history"][-1]
    assert recovery["from_protocol_version"] == "older-provider-protocol"
    assert recovery["to_protocol_version"] == MODEL_RESPONSE_PROTOCOL_VERSION
    audit = db.fetchone(
        "SELECT metadata_json FROM audit_events "
        "WHERE object_id=? AND event_type='PROVIDER_PROTOCOL_CHECKPOINT_RECOVERED' "
        "ORDER BY id DESC LIMIT 1",
        ("wf-protocol-recovery",),
    )
    assert json.loads(audit["metadata_json"])["retry_key"] == retry_key


def test_current_protocol_does_not_reopen_exhausted_provider_checkpoint(tmp_path):
    from app.llm import MODEL_RESPONSE_PROTOCOL_VERSION
    from app.workflows import WorkflowEngine

    db = make_executor_db(tmp_path)
    retry_key = "5:P-WRITE-CONTENT:new-objective:CONTENT"
    state = {
        "provider_wait": {
            "retry_key": retry_key,
            "exhausted": True,
            "failure": {"retryable": True},
        },
        "provider_call_cycles": {
            retry_key: {"protocol_version": MODEL_RESPONSE_PROTOCOL_VERSION}
        },
    }
    now = utc_now()
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            "wf-current-protocol",
            "project-1",
            "WF-4_PROPOSAL_AUTHORING",
            "BLOCKED_CONTRACT",
            5,
            json.dumps(state),
            now,
            now,
        ),
    )
    engine = WorkflowEngine(
        db,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
    )
    wf = engine.get("wf-current-protocol")

    assert not engine._recover_provider_block_after_protocol_upgrade(wf, wf["state"])
    assert engine.get("wf-current-protocol")["status"] == "BLOCKED_CONTRACT"


def _insert_wf4_failure(db, *, status, classification, legacy_state=None):
    state = {
        "workflow_type": "WF-4_PROPOSAL_AUTHORING",
        "options": {
            "provider_retry_limit": 2,
            "provider_retry_base_delay_seconds": 0,
        },
        "step_results": {"5": {"prompt_id": "P-WRITE-BLUEPRINT-CRITIC"}},
        "last_error": "provider failure",
        **(legacy_state or {}),
    }
    now = utc_now()
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            "wf-wf4",
            "project-1",
            "WF-4_PROPOSAL_AUTHORING",
            status,
            5,
            json.dumps(state),
            now,
            now,
        ),
    )
    db.execute(
        """INSERT INTO artifacts(
               id,project_id,workflow_id,artifact_type,prompt_id,version,status,
               security_level,context_hash,content_json,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "artifact-runtime-failure",
            "project-1",
            "wf-wf4",
            "RUNTIME_FAILURE",
            "P-WRITE-BLUEPRINT-CRITIC",
            1,
            str(classification.get("category") or "ERROR"),
            "INTERNAL",
            "hash",
            json.dumps(classification),
            now,
        ),
    )
    return state


def test_wf4_recovery_uses_persisted_formal_classification(tmp_path):
    from scripts.recover_wf4_runtime_v1 import recover

    db = make_executor_db(tmp_path)
    _insert_wf4_failure(
        db,
        status="BLOCKED_PROVIDER",
        classification={
            "category": "PROVIDER_TRANSIENT_ERROR",
            "retryable": True,
            "failure_kind": "EMPTY_STREAM",
            "workflow_status": "WAITING_PROVIDER",
        },
    )

    result = recover(db.path, "wf-wf4", apply=False)

    assert result["eligible"] is True
    assert result["to_status"] == "WAITING_PROVIDER"
    assert result["classification_source"] == "RUNTIME_FAILURE_ARTIFACT"
    assert result["retry_policy"]["max_attempts"] == 3
    assert result["applied"] is False


def test_wf4_recovery_refuses_incomplete_runtime_semantics_migration(tmp_path):
    from scripts.recover_wf4_runtime_v1 import recover

    db = make_executor_db(tmp_path)
    _insert_wf4_failure(
        db,
        status="BLOCKED_PROVIDER",
        classification={
            "category": "PROVIDER_TRANSIENT_ERROR",
            "retryable": True,
        },
        legacy_state={"repair_overrides": {"section": {"status": "PASS"}}},
    )

    dry_run = recover(db.path, "wf-wf4", apply=False)
    assert dry_run["eligible"] is False
    assert any("legacy state keys remain" in item for item in dry_run["refusals"])
    with pytest.raises(ValueError, match="migration is incomplete"):
        recover(db.path, "wf-wf4", apply=True)


def test_wf4_recovery_refuses_nonprovider_failure(tmp_path):
    from scripts.recover_wf4_runtime_v1 import recover

    db = make_executor_db(tmp_path)
    _insert_wf4_failure(
        db,
        status="BLOCKED_CONTRACT",
        classification={
            "category": "OUTPUT_CONTRACT_ERROR",
            "retryable": False,
        },
    )

    result = recover(db.path, "wf-wf4", apply=False)
    assert result["eligible"] is False
    assert result["to_status"] == "BLOCKED_CONTRACT"
    assert any("not a retryable provider failure" in item for item in result["refusals"])


def test_wf4_recovery_apply_uses_status_cas_and_preserves_step(tmp_path):
    from scripts.recover_wf4_runtime_v1 import recover

    db = make_executor_db(tmp_path)
    _insert_wf4_failure(
        db,
        status="BLOCKED_PROVIDER",
        classification={
            "category": "PROVIDER_TRANSIENT_ERROR",
            "retryable": True,
            "failure_kind": "TRANSPORT",
        },
    )

    result = recover(db.path, "wf-wf4", apply=True)
    row = db.fetchone(
        "SELECT status,current_step,state_json FROM workflows WHERE id=?",
        ("wf-wf4",),
    )
    state = json.loads(row["state_json"])

    assert result["applied"] is True
    assert row["status"] == "WAITING_PROVIDER"
    assert row["current_step"] == 5
    assert state["provider_wait"]["new_retry_cycle"] is True
    assert state["provider_wait"]["max_attempts"] == 3


class _MigrationProbeWorkflowEngine:
    """Mixin target is built lazily in tests to avoid module-level imports."""


def _insert_blocked_wf4(db, *, workflow_id, status, state):
    now = utc_now()
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            workflow_id,
            "project-1",
            "WF-4_PROPOSAL_AUTHORING",
            status,
            5,
            json.dumps(state),
            now,
            now,
        ),
    )


def test_typed_block_does_not_rerun_business_step_without_explicit_recovery(tmp_path):
    from app.workflows import WorkflowEngine

    class ProbeEngine(WorkflowEngine):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.write_calls = 0

        async def _write_sections(self, wf, state):
            self.write_calls += 1
            return self.get(wf["id"])

    db = make_executor_db(tmp_path)
    state = {
        "workflow_type": "WF-4_PROPOSAL_AUTHORING",
        "options": {},
        "prerequisite_workflow_ids": {},
        "step_results": {},
        "active_section_id": "new-objective",
        "section_progress": {
            "new-objective": {"phase": "BLUEPRINT_CRITIC", "status": "BLOCKED"}
        },
        "last_error": "contract remains blocked",
    }
    _insert_blocked_wf4(
        db,
        workflow_id="wf-typed-pause",
        status="BLOCKED_CONTRACT",
        state=state,
    )
    engine = ProbeEngine(
        db,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(output_normalizer_version="normalizer-v2"),
        SimpleNamespace(),
    )

    result = asyncio.run(engine.advance("wf-typed-pause"))

    assert result["status"] == "BLOCKED_CONTRACT"
    assert result["state"]["last_error"] == "contract remains blocked"
    assert engine.write_calls == 0


@pytest.mark.parametrize("blocked_status", ["BLOCKED", "BLOCKED_CONTRACT"])
def test_targeted_repair_contract_block_selects_saved_repair_prompt_for_migration(
    tmp_path,
    blocked_status,
):
    from app.workflows import WorkflowEngine

    class ProbeEngine(WorkflowEngine):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.write_calls = 0

        async def _write_sections(self, wf, state):
            self.write_calls += 1
            return self.get(wf["id"])

    db = make_executor_db(tmp_path)
    state = {
        "workflow_type": "WF-4_PROPOSAL_AUTHORING",
        "options": {},
        "prerequisite_workflow_ids": {},
        "step_results": {},
        "active_section_id": "new-objective",
        "section_progress": {
            "new-objective": {"phase": "BLUEPRINT_CRITIC", "status": "BLOCKED_CONTRACT"}
        },
        "last_targeted_repair_failure": {
            "critic_prompt": "P-WRITE-BLUEPRINT-CRITIC",
            "category": "OUTPUT_CONTRACT_ERROR",
            "error": "missing unresolved_finding_ids",
            "run_id": "run-repair-error",
            "repair_id": "repair-legacy-error",
            "repair_attempt_key": "section:new-objective:P-WRITE-BLUEPRINT-CRITIC",
        },
        "last_error": "targeted repair output contract failed",
    }
    workflow_id = f"wf-repair-migration-{blocked_status.lower()}"
    _insert_blocked_wf4(
        db,
        workflow_id=workflow_id,
        status=blocked_status,
        state=state,
    )
    now = utc_now()
    db.execute(
        """INSERT INTO prompt_runs(
               id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,
               input_hash,output_hash,input_json,output_json,error,duration_ms,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "run-repair-error",
            "project-1",
            workflow_id,
            "P-TARGETED-REPAIR",
            "ERROR",
            "model",
            "endpoint",
            "input-hash",
            None,
            "{}",
            "{}",
            "strict schema validation failed",
            1,
            now,
        ),
    )
    db.audit(
        "MODEL_CALL_FAILED",
        project_id="project-1",
        object_id="call-repair-error",
        metadata={
            "run_id": "run-repair-error",
            "output_normalizer_version": "normalizer-v1",
        },
    )
    engine = ProbeEngine(
        db,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(output_normalizer_version="normalizer-v2"),
        SimpleNamespace(),
    )

    result = asyncio.run(engine.advance(workflow_id))
    if blocked_status == "BLOCKED":
        assert result["status"] == "BLOCKED_CONTRACT"
        assert "contract_migration_recovery" not in result["state"]
        result = asyncio.run(engine.advance(workflow_id))

    recovery = result["state"]["contract_migration_recovery"]
    assert recovery["checkpoint_identity_version"] == 1
    assert recovery["step"] == 5
    assert recovery["section_id"] == "new-objective"
    assert recovery["section_phase"] == "BLUEPRINT_CRITIC"
    assert recovery["prompt_id"] == "P-TARGETED-REPAIR"
    assert recovery["output_normalizer_version"] == "normalizer-v2"
    assert result["status"] == "RUNNING"
    assert engine.write_calls == 1


def test_current_normalizer_contract_failure_is_not_automatically_reopened(tmp_path):
    from app.workflows import WorkflowEngine

    class ProbeEngine(WorkflowEngine):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.write_calls = 0

        async def _write_sections(self, wf, state):
            self.write_calls += 1
            return self.get(wf["id"])

    db = make_executor_db(tmp_path)
    state = {
        "workflow_type": "WF-4_PROPOSAL_AUTHORING",
        "options": {},
        "prerequisite_workflow_ids": {},
        "step_results": {},
        "active_section_id": "new-objective",
        "section_progress": {
            "new-objective": {"phase": "BLUEPRINT_CRITIC", "status": "BLOCKED_CONTRACT"}
        },
        "last_targeted_repair_failure": {
            "category": "OUTPUT_CONTRACT_ERROR",
            "error": "current contract rejected the output",
            "run_id": "run-current-contract",
        },
        "last_error": "current contract rejected the output",
    }
    _insert_blocked_wf4(
        db,
        workflow_id="wf-current-contract",
        status="BLOCKED_CONTRACT",
        state=state,
    )
    now = utc_now()
    db.execute(
        """INSERT INTO prompt_runs(
               id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,
               input_hash,output_hash,input_json,output_json,error,duration_ms,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "run-current-contract", "project-1", "wf-current-contract",
            "P-TARGETED-REPAIR", "ERROR", "model", "endpoint", "input-hash",
            None, "{}", "{}", "strict schema validation failed", 1, now,
        ),
    )
    db.audit(
        "MODEL_CALL_FAILED",
        project_id="project-1",
        object_id="call-current-contract",
        metadata={
            "run_id": "run-current-contract",
            "output_normalizer_version": "normalizer-v2",
        },
    )
    engine = ProbeEngine(
        db,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(output_normalizer_version="normalizer-v2"),
        SimpleNamespace(),
    )

    result = asyncio.run(engine.advance("wf-current-contract"))

    assert result["status"] == "BLOCKED_CONTRACT"
    assert "contract_migration_recovery" not in result["state"]
    assert engine.write_calls == 0


def test_contract_recovery_uses_run_bound_to_current_targeted_repair_checkpoint(tmp_path):
    from app.workflows import WorkflowEngine

    class ProbeEngine(WorkflowEngine):
        async def _write_sections(self, wf, state):
            return self.get(wf["id"])

    db = make_executor_db(tmp_path)
    state = {
        "workflow_type": "WF-4_PROPOSAL_AUTHORING",
        "options": {},
        "prerequisite_workflow_ids": {},
        "step_results": {},
        "active_section_id": "new-objective",
        "section_progress": {
            "new-objective": {
                "phase": "BLUEPRINT_CRITIC",
                "status": "BLOCKED_CONTRACT",
            }
        },
        "last_targeted_repair_failure": {
            "critic_prompt": "P-WRITE-BLUEPRINT-CRITIC",
            "category": "OUTPUT_CONTRACT_ERROR",
            "error": "repair output contract failed",
            "run_id": "run-current-repair",
            "repair_id": "repair-current",
            "repair_attempt_key": "section:new-objective:P-WRITE-BLUEPRINT-CRITIC",
        },
        "last_error": "repair output contract failed",
    }
    _insert_blocked_wf4(
        db,
        workflow_id="wf-exact-repair-run",
        status="BLOCKED_CONTRACT",
        state=state,
    )
    for run_id, created_at in (
        ("run-current-repair", "2026-08-03T10:00:00+00:00"),
        ("run-other-section", "2026-08-03T10:01:00+00:00"),
    ):
        db.execute(
            """INSERT INTO prompt_runs(
                   id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,
                   input_hash,output_hash,input_json,output_json,error,duration_ms,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                "project-1",
                "wf-exact-repair-run",
                "P-TARGETED-REPAIR",
                "ERROR",
                "model",
                "endpoint",
                f"input-{run_id}",
                None,
                "{}",
                "{}",
                "strict schema validation failed",
                1,
                created_at,
            ),
        )
        db.audit(
            "MODEL_CALL_FAILED",
            project_id="project-1",
            object_id=f"call-{run_id}",
            metadata={
                "run_id": run_id,
                "output_normalizer_version": "normalizer-v1",
            },
        )
    engine = ProbeEngine(
        db,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(output_normalizer_version="normalizer-v2"),
        SimpleNamespace(),
    )

    result = asyncio.run(engine.advance("wf-exact-repair-run"))

    recovery = result["state"]["contract_migration_recovery"]
    assert recovery["checkpoint_identity_version"] == 1
    assert recovery["step"] == 5
    assert recovery["section_id"] == "new-objective"
    assert recovery["section_phase"] == "BLUEPRINT_CRITIC"
    assert recovery["failed_run_id"] == "run-current-repair"
    assert recovery["prompt_id"] == "P-TARGETED-REPAIR"


def test_section_chain_block_preserves_targeted_repair_failure_category():
    from app.workflow_authoring_base import WorkflowAuthoringMixin

    assert WorkflowAuthoringMixin._section_block_status({
        "last_targeted_repair_failure": {"category": "OUTPUT_CONTRACT_ERROR"}
    }) == "BLOCKED_CONTRACT"
    assert WorkflowAuthoringMixin._section_block_status({
        "last_targeted_repair_failure": {"category": "PROVIDER_TRANSIENT_ERROR"}
    }) == "BLOCKED_PROVIDER"
    assert WorkflowAuthoringMixin._section_block_status({
        "last_targeted_repair_failure": {"category": "TECHNICAL_ERROR"}
    }) == "BLOCKED_TECHNICAL"
    assert WorkflowAuthoringMixin._section_block_status({
        "last_targeted_repair_failure": {"category": "SEMANTIC_REPAIR_REJECTED"}
    }) == "BLOCKED_CONTENT"


def test_waiting_configuration_does_not_resume_without_dependency_checker(tmp_path):
    from app.workflows import WorkflowEngine

    db = make_executor_db(tmp_path)
    now = utc_now()
    state = {
        "workflow_type": "WF-1_PROJECT_INTAKE",
        "options": {},
        "step_results": {},
        "last_error": "missing model configuration",
        "configuration_wait": {"issues": [{"code": "MODEL_MISSING"}]},
    }
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            "wf-config-wait",
            "project-1",
            "WF-1_PROJECT_INTAKE",
            "WAITING_CONFIGURATION",
            0,
            json.dumps(state),
            now,
            now,
        ),
    )
    engine = WorkflowEngine(
        db,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        dependency_preflight=None,
    )

    result = asyncio.run(engine.advance("wf-config-wait"))

    assert result["status"] == "WAITING_CONFIGURATION"
    assert result["state"]["configuration_wait"]["issues"][0]["code"] == "MODEL_MISSING"


def test_contract_migration_finds_exact_failure_metadata_beyond_recent_audit_window(
    tmp_path,
):
    from app.workflows import WorkflowEngine

    class ProbeEngine(WorkflowEngine):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.write_calls = 0

        async def _write_sections(self, wf, state):
            self.write_calls += 1
            return self.get(wf["id"])

    db = make_executor_db(tmp_path)
    state = {
        "workflow_type": "WF-4_PROPOSAL_AUTHORING",
        "options": {},
        "prerequisite_workflow_ids": {},
        "step_results": {},
        "active_section_id": "new-objective",
        "section_progress": {
            "new-objective": {
                "phase": "BLUEPRINT_CRITIC",
                "status": "BLOCKED_CONTRACT",
            }
        },
        "last_targeted_repair_failure": {
            "critic_prompt": "P-WRITE-BLUEPRINT-CRITIC",
            "category": "OUTPUT_CONTRACT_ERROR",
            "error": "repair output contract failed",
            "run_id": "run-old-exact-repair",
            "repair_id": "repair-old-exact",
            "repair_attempt_key": "section:new-objective:P-WRITE-BLUEPRINT-CRITIC",
        },
        "last_error": "repair output contract failed",
    }
    _insert_blocked_wf4(
        db,
        workflow_id="wf-old-audit-window",
        status="BLOCKED_CONTRACT",
        state=state,
    )
    db.execute(
        """INSERT INTO prompt_runs(
               id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,
               input_hash,output_hash,input_json,output_json,error,duration_ms,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "run-old-exact-repair", "project-1", "wf-old-audit-window",
            "P-TARGETED-REPAIR", "ERROR", "model", "endpoint", "input-hash",
            "output-hash", "{}", "{}", "strict schema validation failed", 1,
            "2026-01-01T00:00:00+00:00",
        ),
    )
    db.audit(
        "MODEL_CALL_FAILED",
        project_id="project-1",
        object_id="call-old-exact-repair",
        metadata={
            "run_id": "run-old-exact-repair",
            "output_normalizer_version": "normalizer-v1",
        },
    )
    for index in range(205):
        db.audit(
            "MODEL_CALL_FAILED",
            project_id="project-1",
            object_id=f"call-unrelated-{index}",
            metadata={"run_id": f"run-unrelated-{index}"},
        )
    engine = ProbeEngine(
        db,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(output_normalizer_version="normalizer-v2"),
        SimpleNamespace(),
    )

    result = asyncio.run(engine.advance("wf-old-audit-window"))

    assert result["status"] == "RUNNING"
    assert result["state"]["contract_migration_recovery"]["failed_run_id"] == (
        "run-old-exact-repair"
    )
    assert engine.write_calls == 1


def test_contract_migration_replays_same_provider_attempt_without_new_retry_slot(
    tmp_path,
):
    from app.llm import MODEL_RESPONSE_PROTOCOL_VERSION
    from app.workflows import WorkflowEngine

    db = make_executor_db(tmp_path)
    envelope = {"payload": {"value": 1}}
    input_hash = sha256_json(envelope)
    retry_key = "2:P-TARGETED-REPAIR"
    cycle_id = "cycle-contract-migration"
    base_call_key = "call-repair-exact-base"
    actual_call_key = f"{base_call_key}-cycle-{cycle_id}-attempt-1"
    state = {
        "workflow_type": "WF-1_PROJECT_INTAKE",
        "options": {
            "provider_retry_limit": 2,
            "provider_retry_base_delay_seconds": 0,
        },
        "step_results": {},
        "provider_call_cycles": {
            retry_key: {
                "cycle_id": cycle_id,
                "generation": 1,
                "input_hash": input_hash,
                "protocol_version": MODEL_RESPONSE_PROTOCOL_VERSION,
                "provider_request_spec_hash": "",
                "base_call_key": base_call_key,
                "completed_attempts": 1,
            }
        },
        "provider_wait": {
            "retry_key": retry_key,
            "cycle_id": cycle_id,
            "input_hash": input_hash,
            "protocol_version": MODEL_RESPONSE_PROTOCOL_VERSION,
            "provider_request_spec_hash": "",
            "completed_attempts": 1,
            "base_call_key": base_call_key,
            "attempt_call_key": actual_call_key,
            "prompt_id": "P-TARGETED-REPAIR",
            "failure_run_id": "run-contract-failed",
            "phase": "FAILED",
            "decision": {"should_retry": False},
        },
        "contract_migration_recovery": {
            "checkpoint_identity_version": 1,
            "step": 2,
            "section_id": None,
            "section_phase": None,
            "prompt_id": "P-TARGETED-REPAIR",
            "failed_run_id": "run-contract-failed",
        },
    }
    wf = _workflow_for_retry_test(db, state)
    success = {
        "run_id": "run-contract-recovered",
        "status": "PASS",
        "output": {"status": "PASS"},
        "call_key": actual_call_key,
        "contract_recovered_from_run_id": "run-contract-failed",
    }
    executor = SequencePromptExecutor([success])
    engine = WorkflowEngine(
        db, SimpleNamespace(), SimpleNamespace(), executor, SimpleNamespace()
    )

    result = asyncio.run(
        engine._execute_prompt_with_provider_retry(
            wf,
            state,
            prompt_id="P-TARGETED-REPAIR",
            envelope=envelope,
            call_key=base_call_key,
        )
    )

    assert result is success
    assert executor.calls == 1
    assert executor.call_kwargs[0]["call_key"] == actual_call_key
    assert executor.call_kwargs[0]["recovery_run_id"] == "run-contract-failed"
    assert state["provider_call_cycles"][retry_key]["completed_attempts"] == 1
    assert "provider_wait" not in state
    assert "contract_migration_recovery" not in state


def test_stale_targeted_repair_failure_does_not_override_current_section_prompt(
    tmp_path,
):
    from app.workflows import WORKFLOWS, WorkflowEngine

    db = make_executor_db(tmp_path)
    engine = WorkflowEngine(
        db, SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace()
    )
    wf = {
        "id": "wf-stale-repair",
        "project_id": "project-1",
        "workflow_type": "WF-4_PROPOSAL_AUTHORING",
        "status": "BLOCKED_CONTRACT",
        "current_step": 5,
    }
    state = {
        "active_section_id": "section-b",
        "section_progress": {
            "section-b": {"phase": "CONTENT_CRITIC"},
        },
        "last_targeted_repair_failure": {
            "critic_prompt": "P-WRITE-BLUEPRINT-CRITIC",
            "category": "OUTPUT_CONTRACT_ERROR",
            "run_id": "run-old-section-a-repair",
            "section_id": "section-a",
            "workflow_step": 5,
        },
        "provider_wait": {
            "prompt_id": "P-WRITE-CRITIC",
            "failure_run_id": "run-current-section-b-critic",
            "section_id": "section-b",
            "section_phase": "CONTENT_CRITIC",
        },
    }

    prompt_id = engine._blocked_failure_prompt_id(
        wf,
        state,
        WORKFLOWS[wf["workflow_type"]],
        is_section_step=True,
    )
    run_id = engine._blocked_failure_run_id(
        wf,
        state,
        prompt_id=prompt_id,
    )

    assert prompt_id == "P-WRITE-CRITIC"
    assert run_id == "run-current-section-b-critic"


def test_legacy_targeted_repair_failure_without_section_identity_is_not_current(
    tmp_path,
):
    from app.workflows import WORKFLOWS, WorkflowEngine

    db = make_executor_db(tmp_path)
    engine = WorkflowEngine(
        db, SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace()
    )
    wf = {
        "id": "wf-legacy-repair",
        "project_id": "project-1",
        "workflow_type": "WF-4_PROPOSAL_AUTHORING",
        "status": "BLOCKED_CONTRACT",
        "current_step": 5,
    }
    state = {
        "active_section_id": "section-b",
        "section_progress": {"section-b": {"phase": "CONTENT_CRITIC"}},
        "last_targeted_repair_failure": {
            "critic_prompt": "P-WRITE-CRITIC",
            "category": "OUTPUT_CONTRACT_ERROR",
            "run_id": "run-legacy-ambiguous-repair",
        },
        "provider_wait": {
            "prompt_id": "P-WRITE-CRITIC",
            "failure_run_id": "run-current-section-b-critic",
            "section_id": "section-b",
            "section_phase": "CONTENT_CRITIC",
        },
    }

    prompt_id = engine._blocked_failure_prompt_id(
        wf,
        state,
        WORKFLOWS[wf["workflow_type"]],
        is_section_step=True,
    )
    run_id = engine._blocked_failure_run_id(wf, state, prompt_id=prompt_id)

    assert prompt_id == "P-WRITE-CRITIC"
    assert run_id == "run-current-section-b-critic"


def test_legacy_section_contract_block_without_exact_run_binding_stays_closed(
    tmp_path,
):
    from app.workflows import WorkflowEngine

    class ProbeEngine(WorkflowEngine):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.write_calls = 0

        async def _write_sections(self, wf, state):
            self.write_calls += 1
            return self.get(wf["id"])

    db = make_executor_db(tmp_path)
    state = {
        "workflow_type": "WF-4_PROPOSAL_AUTHORING",
        "options": {},
        "prerequisite_workflow_ids": {},
        "step_results": {},
        "active_section_id": "section-b",
        "section_progress": {
            "section-b": {
                "phase": "CONTENT_CRITIC",
                "status": "BLOCKED_CONTRACT",
            }
        },
        "last_error": "legacy section contract block",
    }
    _insert_blocked_wf4(
        db,
        workflow_id="wf-ambiguous-section-contract",
        status="BLOCKED_CONTRACT",
        state=state,
    )
    db.execute(
        """INSERT INTO prompt_runs(
               id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,
               input_hash,output_hash,input_json,output_json,error,duration_ms,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "run-ambiguous-old-section", "project-1",
            "wf-ambiguous-section-contract", "P-WRITE-CRITIC", "ERROR",
            "model", "endpoint", "input-hash", "output-hash", "{}", "{}",
            "strict schema validation failed", 1, utc_now(),
        ),
    )
    db.audit(
        "MODEL_CALL_FAILED",
        project_id="project-1",
        object_id="call-ambiguous-old-section",
        metadata={
            "run_id": "run-ambiguous-old-section",
            "output_normalizer_version": "normalizer-v1",
        },
    )
    engine = ProbeEngine(
        db,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(output_normalizer_version="normalizer-v2"),
        SimpleNamespace(),
    )

    result = asyncio.run(engine.advance("wf-ambiguous-section-contract"))

    assert result["status"] == "BLOCKED_CONTRACT"
    assert "contract_migration_recovery" not in result["state"]
    assert engine.write_calls == 0


def test_runtime_failure_history_requires_exact_section_and_phase_for_recovery(
    tmp_path,
):
    from app.workflows import WorkflowEngine

    db = make_executor_db(tmp_path)
    engine = WorkflowEngine(
        db, SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace()
    )
    wf = {
        "id": "wf-section-history",
        "project_id": "project-1",
        "workflow_type": "WF-4_PROPOSAL_AUTHORING",
        "status": "BLOCKED_CONTRACT",
        "current_step": 5,
    }
    state = {
        "active_section_id": "section-b",
        "section_progress": {"section-b": {"phase": "CONTENT_CRITIC"}},
        "runtime_failure_history": [
            {
                "prompt_id": "P-WRITE-CRITIC",
                "step": 5,
                "section_id": "section-a",
                "section_phase": "CONTENT_CRITIC",
                "run_id": "run-section-a",
            },
            {
                "prompt_id": "P-WRITE-CRITIC",
                "step": 5,
                "section_id": "section-b",
                "section_phase": "BLUEPRINT_CRITIC",
                "run_id": "run-section-b-wrong-phase",
            },
            {
                "prompt_id": "P-WRITE-CRITIC",
                "step": 5,
                "section_id": "section-b",
                "section_phase": "CONTENT_CRITIC",
                "run_id": "run-section-b-current",
            },
        ],
    }

    assert engine._blocked_failure_run_id(
        wf, state, prompt_id="P-WRITE-CRITIC"
    ) == "run-section-b-current"
    state["runtime_failure_history"].pop()
    assert engine._blocked_failure_run_id(
        wf, state, prompt_id="P-WRITE-CRITIC"
    ) == ""
