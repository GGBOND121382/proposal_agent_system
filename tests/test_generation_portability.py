from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.db import Database
from app.executor import PromptExecutionError
from app.generation_mode import COMMITTED_RESULT_REUSE
from app.llm import LLMResult
from app.exporter import DocxExporter
from app.post_export_acceptance import PostExportAcceptanceManager
from app.runtime_executor import RuntimePromptExecutor
from app.security import Route
from app.util import utc_now
from app.workflows import WorkflowEngine


class PortablePack:
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
        return {"version": "3.0.0", "next_human_gate": None}




class ContractAwarePortablePack(PortablePack):
    def __init__(self, marker: str):
        self.marker = marker

    def inlined_schema(self, prompt_id, kind):
        return {"type": "object", "x-contract-marker": self.marker, "x-kind": kind}


class PortableRouter:
    def __init__(self, model_id: str, endpoint_id: str, provider_model_name: str):
        self.model_id = model_id
        self.endpoint_id = endpoint_id
        self.provider_model_name = provider_model_name

    def route(self, prompt_id, envelope, original_environment=None):
        return Route(
            prompt_id=prompt_id,
            environment="OFFLINE_LOCAL",
            model_id=self.model_id,
            endpoint_id=self.endpoint_id,
            provider_model_name=self.provider_model_name,
            endpoint={},
            profile={},
        )


class PortableGateway:
    supports_runtime_evidence = False

    def __init__(self):
        self.calls = 0
        self.settings = SimpleNamespace(runtime_mode="REPLAY")

    async def invoke(self, route, prompt_id, system_prompt, envelope, output_schema):
        self.calls += 1
        output = {
            "status": "PASS",
            "result": {"provider": route.provider_model_name, "call": self.calls},
            "warnings": [],
            "user_questions": [],
        }
        return LLMResult(
            output=output,
            raw_text=json.dumps(output),
            model_id=route.model_id,
            endpoint_id=route.endpoint_id,
        )


def make_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "portability.sqlite3")
    now = utc_now()
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        ("project-1", "Portable project", "Raw-input portability test", "INTERNAL", "{}", now, now),
    )
    return db


@pytest.fixture(autouse=True)
def portable_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("CAPABILITY_ACCEPTANCE_MODE", "false")
    monkeypatch.setenv("MODEL_RUNTIME_MODE", "REPLAY")
    monkeypatch.setenv("MODEL_CALL_EVIDENCE_DIR", str(tmp_path / "model_calls"))
    monkeypatch.setenv("PROPOSAL_GENERATION_MODE", "RESUME_FROM_CHECKPOINT")
    monkeypatch.delenv("RUNTIME_FAULT_POINT", raising=False)
    monkeypatch.delenv("RUNTIME_FAULT_ACTION", raising=False)


def execute(executor: RuntimePromptExecutor):
    envelope = {
        "prompt_version": "3.0.0",
        "security_context": {"input_max_security_level": "INTERNAL"},
        "payload": {"raw_material": "same input"},
    }
    return asyncio.run(
        executor.execute(
            "P-PORTABLE",
            envelope,
            project_id="project-1",
            workflow_id="wf-1",
        )
    )


def test_model_route_is_part_of_reuse_identity(tmp_path):
    db = make_db(tmp_path)
    first_gateway = PortableGateway()
    first = RuntimePromptExecutor(
        db,
        PortablePack(),
        PortableRouter("model-a", "endpoint-a", "provider/a"),
        first_gateway,
        quality_guard_enabled=False,
    )
    first_result = execute(first)

    second_gateway = PortableGateway()
    second = RuntimePromptExecutor(
        db,
        PortablePack(),
        PortableRouter("model-b", "endpoint-b", "provider/b"),
        second_gateway,
        quality_guard_enabled=False,
    )
    second_result = execute(second)

    assert first_result["call_key"] != second_result["call_key"]
    assert first_gateway.calls == 1
    assert second_gateway.calls == 1
    assert db.fetchone("SELECT COUNT(*) AS n FROM prompt_runs WHERE status='PASS'")["n"] == 2


def test_prompt_and_schema_contract_is_part_of_reuse_identity(tmp_path):
    db = make_db(tmp_path)
    first_gateway = PortableGateway()
    first = RuntimePromptExecutor(
        db,
        ContractAwarePortablePack("schema-v1"),
        PortableRouter("model-a", "endpoint-a", "provider/a"),
        first_gateway,
        quality_guard_enabled=False,
    )
    first_result = execute(first)

    second_gateway = PortableGateway()
    second = RuntimePromptExecutor(
        db,
        ContractAwarePortablePack("schema-v2"),
        PortableRouter("model-a", "endpoint-a", "provider/a"),
        second_gateway,
        quality_guard_enabled=False,
    )
    second_result = execute(second)

    assert first_result["call_key"] != second_result["call_key"]
    assert first_gateway.calls == 1
    assert second_gateway.calls == 1
    events = db.fetchall(
        "SELECT metadata_json FROM audit_events WHERE event_type='MODEL_CALL_COMMITTED' ORDER BY id"
    )
    hashes = [json.loads(row["metadata_json"])["prompt_contract_hash"] for row in events]
    assert len(set(hashes)) == 2


def test_fresh_generation_rejects_checkpoint_contamination(monkeypatch, tmp_path):
    db = make_db(tmp_path)
    first_gateway = PortableGateway()
    first = RuntimePromptExecutor(
        db,
        PortablePack(),
        PortableRouter("model-a", "endpoint-a", "provider/a"),
        first_gateway,
        quality_guard_enabled=False,
    )
    execute(first)

    monkeypatch.setenv("PROPOSAL_GENERATION_MODE", "FRESH_GENERATION")
    fresh_gateway = PortableGateway()
    fresh = RuntimePromptExecutor(
        db,
        PortablePack(),
        PortableRouter("model-a", "endpoint-a", "provider/a"),
        fresh_gateway,
        quality_guard_enabled=False,
    )
    with pytest.raises(PromptExecutionError, match="Fresh generation refused committed result reuse"):
        execute(fresh)
    assert fresh_gateway.calls == 0


def test_resume_reuses_exact_identity_and_records_lineage(tmp_path):
    db = make_db(tmp_path)
    gateway = PortableGateway()
    executor = RuntimePromptExecutor(
        db,
        PortablePack(),
        PortableRouter("model-a", "endpoint-a", "provider/a"),
        gateway,
        quality_guard_enabled=False,
    )
    first = execute(executor)
    second = execute(executor)

    assert second["reused_committed_result"] is True
    assert second["generation_origin"] == COMMITTED_RESULT_REUSE
    assert second["source_run_id"] == first["run_id"]
    assert gateway.calls == 1
    event = db.fetchone(
        "SELECT metadata_json FROM audit_events WHERE event_type='MODEL_CALL_REUSED_FROM_CHECKPOINT' ORDER BY id DESC LIMIT 1"
    )
    metadata = json.loads(event["metadata_json"])
    assert metadata["source_run_id"] == first["run_id"]
    assert metadata["model_id"] == "model-a"
    assert metadata["endpoint_id"] == "endpoint-a"



class ProviderAliasGateway(PortableGateway):
    async def invoke(self, route, prompt_id, system_prompt, envelope, output_schema):
        self.calls += 1
        output = {
            "status": "PASS",
            "result": {"provider": route.provider_model_name, "call": self.calls},
            "warnings": [],
            "user_questions": [],
        }
        return LLMResult(
            output=output,
            raw_text=json.dumps(output),
            model_id="provider-reported-model-revision",
            endpoint_id="provider-reported-endpoint",
        )


def test_provider_reported_alias_does_not_break_same_route_resume(tmp_path):
    db = make_db(tmp_path)
    gateway = ProviderAliasGateway()
    executor = RuntimePromptExecutor(
        db,
        PortablePack(),
        PortableRouter("configured-model", "configured-endpoint", "provider/model-v2"),
        gateway,
        quality_guard_enabled=False,
    )
    first = execute(executor)
    second = execute(executor)
    assert first["route"]["model_id"] == "provider-reported-model-revision"
    assert first["route"]["configured_model_id"] == "configured-model"
    assert first["route"]["provider_model_name"] == "provider/model-v2"
    assert second["reused_committed_result"] is True
    assert gateway.calls == 1

def _wf4_state(candidate_hash: str, candidate_id: str) -> str:
    return json.dumps(
        {
            "workflow_type": "WF-4_PROPOSAL_AUTHORING",
            "full_proposal_review_history": [
                {
                    "status": "PASS",
                    "candidate_set_hash": candidate_hash,
                    "section_manifest": [
                        {
                            "section_id": "sec-01",
                            "candidate_id": candidate_id,
                            "polish_run_id": f"polish-{candidate_id}",
                            "expression_critic_run_id": f"critic-{candidate_id}",
                        }
                    ],
                }
            ],
        }
    )


def test_wf5_binds_requested_frozen_wf4_not_latest(tmp_path):
    db = make_db(tmp_path)
    now = utc_now()
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        ("wf4-old", "project-1", "WF-4_PROPOSAL_AUTHORING", "COMPLETED", 99, _wf4_state("hash-old", "candidate-old"), now, now),
    )
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        ("wf4-new", "project-1", "WF-4_PROPOSAL_AUTHORING", "COMPLETED", 99, _wf4_state("hash-new", "candidate-new"), now, now),
    )
    engine = WorkflowEngine(
        db,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
    )
    workflow = engine.start(
        "project-1",
        "WF-5_SECURITY_REVIEW_AND_EXPORT",
        {"source_workflow_id": "wf4-old"},
    )
    assert workflow["status"] == "RUNNING"
    assert workflow["state"]["source_workflow_id"] == "wf4-old"
    assert workflow["state"]["source_candidate_set_hash"] == "hash-old"


def test_export_approval_lookup_is_scoped_to_frozen_wf4(tmp_path):
    db = make_db(tmp_path)
    now = utc_now()
    for wf5_id, source_id, source_hash in (
        ("wf5-old", "wf4-old", "hash-old"),
        ("wf5-new", "wf4-new", "hash-new"),
    ):
        state = json.dumps(
            {
                "workflow_type": "WF-5_SECURITY_REVIEW_AND_EXPORT",
                "source_workflow_id": source_id,
                "source_candidate_set_hash": source_hash,
            }
        )
        db.execute(
            "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (wf5_id, "project-1", "WF-5_SECURITY_REVIEW_AND_EXPORT", "COMPLETED", 99, state, now, now),
        )
    manager = PostExportAcceptanceManager(
        db,
        SimpleNamespace(),
        exporter=SimpleNamespace(),
    )
    assert manager._approval_workflow_for_source("project-1", "wf4-old") == "wf5-old"
    assert manager._approval_workflow_for_source("project-1", "wf4-new") == "wf5-new"


def test_production_code_has_no_loose_prior_section_injection():
    root = Path(__file__).resolve().parents[1]
    forbidden = (
        "prior_section_content",
        "respond_clean_wf",
        "run_clean_wf",
        "build_chat_bridge_materials",
        "面向中小型软件项目的需求变更影响分析",
    )
    hits: list[str] = []
    for folder in (root / "app", root / "scripts"):
        for path in folder.rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            for token in forbidden:
                if token in text:
                    hits.append(f"{path.relative_to(root)}:{token}")
    assert hits == []



def _insert_section_runs(
    db: Database,
    *,
    workflow_id: str,
    section_id: str,
    candidate_id: str,
    text: str,
    suffix: str,
) -> tuple[str, str]:
    now = utc_now()
    polish_id = f"polish-{suffix}"
    critic_id = f"critic-{suffix}"
    source_section = {
        "section_id": section_id,
        "section_key": section_id,
        "title": section_id,
        "level": 1,
        "text_hash": f"source-{suffix}",
    }
    candidate = {
        "candidate_id": candidate_id,
        "candidate_text": text,
        "paragraphs": [{"paragraph_id": f"p-{suffix}", "sequence": 1, "text": text}],
    }
    db.execute(
        "INSERT INTO prompt_runs(id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,input_hash,output_hash,input_json,output_json,error,duration_ms,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            polish_id,
            "project-1",
            workflow_id,
            "P-EXPRESSION-POLISH",
            "PASS",
            "model-a",
            "endpoint-a",
            f"input-{suffix}",
            f"output-{suffix}",
            json.dumps({"payload": {"source_section": source_section}}, ensure_ascii=False),
            json.dumps({"result": candidate}, ensure_ascii=False),
            None,
            1,
            now,
        ),
    )
    db.execute(
        "INSERT INTO prompt_runs(id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,input_hash,output_hash,input_json,output_json,error,duration_ms,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            critic_id,
            "project-1",
            workflow_id,
            "P-EXPRESSION-CRITIC",
            "PASS",
            "model-a",
            "endpoint-a",
            f"critic-input-{suffix}",
            f"critic-output-{suffix}",
            json.dumps({"payload": {"polished_candidate": candidate}}, ensure_ascii=False),
            json.dumps({"result": {"verdict": "ACCEPT"}}, ensure_ascii=False),
            None,
            1,
            now,
        ),
    )
    return polish_id, critic_id


def test_legacy_checkpoint_is_migrated_from_exact_run_ids(tmp_path):
    db = make_db(tmp_path)
    polish_id, critic_id = _insert_section_runs(
        db,
        workflow_id="wf4-legacy",
        section_id="sec-legacy",
        candidate_id="candidate-legacy",
        text="legacy model output",
        suffix="legacy",
    )
    state = {
        "workflow_type": "WF-4_PROPOSAL_AUTHORING",
        "step_results": {
            "6": {"prompt_id": "P-INTEGRATION-CRITIC", "run_id": "integration-legacy", "status": "PASS"}
        },
        "section_results": [
            {
                "section_id": "sec-legacy",
                "status": "COMPLETED",
                "runs": [
                    {"prompt_id": "P-EXPRESSION-POLISH", "run_id": polish_id, "status": "PASS"},
                    {"prompt_id": "P-EXPRESSION-CRITIC", "run_id": critic_id, "status": "PASS"},
                ],
            }
        ],
    }
    now = utc_now()
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        ("wf4-legacy", "project-1", "WF-4_PROPOSAL_AUTHORING", "COMPLETED", 99, json.dumps(state), now, now),
    )
    engine = WorkflowEngine(db, SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
    resolved = engine._resolve_source_wf4("project-1", "wf4-legacy")
    assert resolved["binding_mode"] == "MIGRATED_LEGACY_CHECKPOINT"
    assert resolved["section_manifest"] == [
        {
            "section_id": "sec-legacy",
            "candidate_id": "candidate-legacy",
            "polish_run_id": polish_id,
            "expression_critic_run_id": critic_id,
        }
    ]


def test_exporter_fallback_is_scoped_to_selected_legacy_workflow(tmp_path):
    db = make_db(tmp_path)
    _insert_section_runs(
        db,
        workflow_id="wf4-selected",
        section_id="sec-shared",
        candidate_id="candidate-selected",
        text="selected workflow text",
        suffix="selected",
    )
    _insert_section_runs(
        db,
        workflow_id="wf4-other",
        section_id="sec-shared",
        candidate_id="candidate-other",
        text="newer unrelated text",
        suffix="other",
    )
    exporter = object.__new__(DocxExporter)
    exporter.db = db
    exporter.review_workflow_id = "wf4-selected"
    candidates = exporter._candidate_runs("project-1")
    assert len(candidates) == 1
    assert candidates[0]["candidate_id"] == "candidate-selected"
    assert candidates[0]["paragraphs"] == ["selected workflow text"]
