from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.config import Settings
from app.db import Database
from app.pack import PromptPack
from app.research import PublicResearchService
from app.runtime_context import LiveContextBuilder
from app.runtime_workflows import RecoverableWorkflowEngine
from app.util import new_id, sha256_json, utc_now
from app.wf3_input import WF3_INPUT_GATE_TYPE


class NeverExecutor:
    output_normalizer_version = "test"

    async def execute(self, *args, **kwargs):  # pragma: no cover - failure guard
        raise AssertionError("model executor must not run before WF-3 input is resolved")


@pytest.fixture()
def wf3_runtime(tmp_path: Path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("MODEL_RUNTIME_MODE", "SIMULATED")
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PROMPT_PACK_DIR", str(root / "prompt_pack"))
    settings = Settings.load()
    pack = PromptPack(settings.prompt_pack_dir)
    db = Database(settings.db_path)
    builder = LiveContextBuilder(db, pack)
    engine = RecoverableWorkflowEngine(
        db,
        pack,
        builder,
        NeverExecutor(),
        PublicResearchService(settings),
    )
    return settings, pack, db, builder, engine


def create_project(db: Database, *, description: str = "") -> str:
    project_id = new_id("project")
    config = {
        "internet_access_allowed": True,
        "anonymized_external_processing_allowed": True,
        "allowed_public_topics": ["公开学术资料"],
        "prohibited_external_fields": ["真实项目名称"],
        "recipient_scope": ["内部用户"],
        "allowed_model_endpoint_ids": ["offline-primary", "online-public-primary"],
        "retention_days": 365,
    }
    now = utc_now()
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (
            project_id,
            "测试项目",
            description,
            "INTERNAL",
            json.dumps(config, ensure_ascii=False),
            now,
            now,
        ),
    )
    return project_id


def mark_wf1_completed(db: Database, project_id: str) -> None:
    now = utc_now()
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            new_id("wf"),
            project_id,
            "WF-1_PROJECT_INTAKE",
            "COMPLETED",
            9,
            json.dumps({"workflow_type": "WF-1_PROJECT_INTAKE", "options": {}}, ensure_ascii=False),
            now,
            now,
        ),
    )


def add_project_definition_artifact(db: Database, pack: PromptPack, project_id: str) -> None:
    output = pack.replay_output("P-PROJECT-DEFINITION-EXTRACT", "normal")
    output["result"]["project_definition"]["project_id"] = project_id
    artifact_id = new_id("artifact")
    db.execute(
        """INSERT INTO artifacts(
             id,project_id,workflow_id,artifact_type,prompt_id,version,status,
             security_level,context_hash,content_json,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            artifact_id,
            project_id,
            None,
            "PROMPT_OUTPUT",
            "P-PROJECT-DEFINITION-EXTRACT",
            1,
            "PASS",
            "INTERNAL",
            sha256_json(output),
            json.dumps(output, ensure_ascii=False),
            utc_now(),
        ),
    )


def test_wf3_explicit_options_are_mapped_to_safe_package_context(wf3_runtime):
    _, pack, db, builder, _ = wf3_runtime
    project_id = create_project(db)
    state = {
        "workflow_type": "WF-3_HYBRID_ONLINE_ASSIST",
        "options": {
            "research_need": {
                "question": "公开领域中有哪些可核验的代表性比较基线？",
                "reason_online_needed": "需要核验公开来源。",
                "desired_output": "带来源的基线比较表。",
            },
            "target_task_type": "PUBLIC_RESEARCH",
        },
    }

    envelope = builder.build(
        "P-SAFE-ONLINE-PACKAGE",
        project_id,
        workflow_id="wf-explicit",
        workflow_state=state,
    )

    assert envelope["payload"]["research_need"]["question"].startswith("公开领域")
    assert envelope["payload"]["research_need"]["need_id"].startswith("need-")
    assert envelope["payload"]["source_items"] == []
    assert envelope["payload"]["target_task_type"] == "PUBLIC_RESEARCH"
    assert pack.validate("P-SAFE-ONLINE-PACKAGE", "input", envelope) == []


def test_wf3_derives_research_need_from_confirmed_argument_graph(wf3_runtime):
    _, pack, db, builder, _ = wf3_runtime
    project_id = create_project(db)
    add_project_definition_artifact(db, pack, project_id)
    state = {"workflow_type": "WF-3_HYBRID_ONLINE_ASSIST", "options": {}}

    envelope = builder.build(
        "P-SAFE-ONLINE-PACKAGE",
        project_id,
        workflow_id="wf-derived",
        workflow_state=state,
    )

    need = envelope["payload"]["research_need"]
    assert "代表性方法" in need["question"]
    assert "如何把不完备业务语义" in need["question"]
    # Context construction is pure with respect to caller-owned workflow state.
    # Derived provenance is used inside the build but is not persisted as a
    # hidden workflow-state side effect.
    assert "wf3_input_resolution" not in state
    assert any(item["object_type"].startswith("PROMPT_ARTIFACT:") for item in envelope["payload"]["source_items"])
    assert pack.validate("P-SAFE-ONLINE-PACKAGE", "input", envelope) == []


def test_wf3_missing_question_creates_user_input_gate_without_model_run(wf3_runtime):
    _, pack, db, builder, engine = wf3_runtime
    project_id = create_project(db, description="仅有行政性项目说明")
    mark_wf1_completed(db, project_id)
    workflow = engine.start(project_id, "WF-3_HYBRID_ONLINE_ASSIST", {})

    workflow = asyncio.run(engine.advance(workflow["id"]))

    assert workflow["status"] == "WAITING_GATE"
    assert workflow["current_step"] == 0
    gates = engine.list_gates(workflow_id=workflow["id"])
    gate = next(item for item in gates if item["status"] == "OPEN")
    assert gate["gate_type"] == WF3_INPUT_GATE_TYPE
    assert gate["required_role"] == "PROJECT_OWNER"
    assert "PROVIDE_INFORMATION" in gate["allowed_actions"]
    assert db.fetchone("SELECT COUNT(*) AS n FROM prompt_runs WHERE workflow_id=?", (workflow["id"],))["n"] == 0

    answers = [
        {"question_id": "wf3-research-question", "value": "公开研究中常用的比较基线和评价指标有哪些？"},
        {"question_id": "wf3-target-task-type", "value": "PUBLIC_RESEARCH"},
    ]
    engine.decide_gate(
        gate["id"],
        action="PROVIDE_INFORMATION",
        decided_by="pytest",
        decided_role="PROJECT_OWNER",
        answers=answers,
    )
    updated = engine.get(workflow["id"])
    assert updated["status"] == "RUNNING"
    assert updated["current_step"] == 0
    assert updated["state"]["options"]["research_need"]["question"].startswith("公开研究")
    artifact_ids = updated["state"]["human_resolution_artifact_ids"]["P-SAFE-ONLINE-PACKAGE"]
    assert artifact_ids
    stored_artifact = db.fetchone(
        "SELECT content_json FROM artifacts WHERE id=?", (artifact_ids[0],)
    )
    stored_resolution = json.loads(stored_artifact["content_json"])["resolution"]
    assert stored_resolution["question_id"] == "wf3-research-question"
    assert stored_resolution["target_paths"] == ["/payload/research_need/question"]
    assert "human_resolutions" not in updated["state"]

    envelope = builder.build(
        "P-SAFE-ONLINE-PACKAGE",
        project_id,
        workflow_id=workflow["id"],
        workflow_state=updated["state"],
    )
    assert pack.validate("P-SAFE-ONLINE-PACKAGE", "input", envelope) == []
    assert any(
        item["question_id"] == "wf3-research-question"
        for item in envelope["payload"]["human_resolutions"]
    )

    # Approved Gate evidence is reconstructed from immutable artifacts without
    # reopening the Gate or repopulating hidden workflow-state overrides.
    legacy_state = json.loads(json.dumps(updated["state"], ensure_ascii=False))
    legacy_envelope = builder.build(
        "P-SAFE-ONLINE-PACKAGE",
        project_id,
        workflow_id=workflow["id"],
        workflow_state=legacy_state,
    )
    legacy_resolution = legacy_envelope["payload"]["human_resolutions"][0]
    assert legacy_resolution["gate_id"] == gate["id"]
    assert legacy_resolution["decided_by"] == "pytest"
    assert legacy_resolution["decided_role"] == "PROJECT_OWNER"


def test_legacy_live_context_block_recovers_to_input_gate_even_after_retry_limit(wf3_runtime):
    _, _, db, _, engine = wf3_runtime
    project_id = create_project(db)
    mark_wf1_completed(db, project_id)
    workflow = engine.start(project_id, "WF-3_HYBRID_ONLINE_ASSIST", {})
    state = workflow["state"]
    state["last_error"] = (
        "LIVE context for P-SAFE-ONLINE-PACKAGE contains unresolved schema scaffold fields: "
        "payload.research_need.need_id, payload.research_need.question, "
        "payload.research_need.reason_online_needed, payload.research_need.desired_output, "
        "payload.source_items, payload.target_task_type"
    )
    state["technical_retry_attempts"] = {"0": 2}
    engine._update(workflow, status="BLOCKED", state=state)

    recovered = asyncio.run(engine.advance(workflow["id"]))

    assert recovered["status"] == "WAITING_GATE"
    assert recovered["current_step"] == 0
    gate = next(item for item in engine.list_gates(workflow_id=workflow["id"]) if item["status"] == "OPEN")
    assert gate["gate_type"] == WF3_INPUT_GATE_TYPE

def test_wf3_input_gate_rejects_empty_answer_and_remains_open(wf3_runtime):
    _, _, db, _, engine = wf3_runtime
    project_id = create_project(db)
    mark_wf1_completed(db, project_id)
    workflow = engine.start(project_id, "WF-3_HYBRID_ONLINE_ASSIST", {})
    workflow = asyncio.run(engine.advance(workflow["id"]))
    gate = next(item for item in engine.list_gates(workflow_id=workflow["id"]) if item["status"] == "OPEN")

    with pytest.raises(ValueError, match="必须填写"):
        engine.decide_gate(
            gate["id"],
            action="PROVIDE_INFORMATION",
            decided_by="pytest",
            decided_role="PROJECT_OWNER",
            answers=[],
        )

    assert engine._gate(gate["id"])["status"] == "OPEN"
    assert engine.get(workflow["id"])["status"] == "WAITING_GATE"
