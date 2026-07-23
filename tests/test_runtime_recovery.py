from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.db import Database
from app.llm import LLMResult
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


def test_recoverable_block_resumes_same_step(tmp_path):
    db = make_executor_db(tmp_path)
    now = utc_now()
    state = {
        "workflow_type": "WF-1_PROJECT_INTAKE",
        "options": {},
        "step_results": {},
        "repair_attempts": {},
        "repair_overrides": {},
        "public_search_results": None,
        "runtime_recoverable": True,
        "runtime_failure_point": "after_db_transaction",
        "last_error": "INJECTED_FAILURE:after_db_transaction:call-1",
    }
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        ("wf-1", "project-1", "WF-1_PROJECT_INTAKE", "BLOCKED", 3, json.dumps(state), now, now),
    )
    engine = RecoverableWorkflowEngine(db, SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
    recovered = engine._recover_status(engine.get("wf-1"))
    assert recovered["status"] == "RUNNING"
    assert recovered["current_step"] == 3
    assert recovered["state"]["recovered_from"] == "after_db_transaction"
