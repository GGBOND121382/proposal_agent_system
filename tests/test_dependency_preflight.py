from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.db import Database
from app.dependency_preflight import RuntimeDependencyPreflight
from app.executor import PromptExecutionError
from app.pack import PromptPack
from app.research import PublicResearchService
from app.runtime_workflows import RecoverableWorkflowEngine
from app.staged_workflows import StagedWorkflowCoordinator
from app.util import new_id, utc_now


class ReplayContext:
    def __init__(self, pack: PromptPack):
        self.pack = pack

    def build(self, prompt_id: str, project_id: str, **_: Any) -> dict[str, Any]:
        value = self.pack.replay_input(prompt_id)
        value.setdefault("scope", {})["project_id"] = project_id
        return value


class FailingExecutor:
    output_normalizer_version = "preflight-test"

    def __init__(self, message: str = "LLM endpoint returned 401") -> None:
        self.calls = 0
        self.message = message

    async def execute(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        raise PromptExecutionError(self.message)


class NeverExecutor(FailingExecutor):
    async def execute(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        raise AssertionError("model must not be called while configuration is incomplete")


@pytest.fixture()
def live_env(tmp_path: Path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("MODEL_RUNTIME_MODE", "LIVE")
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PROMPT_PACK_DIR", str(root / "prompt_pack"))
    monkeypatch.setenv("OFFLINE_LLM_ENABLED", "true")
    monkeypatch.setenv("OFFLINE_LLM_BASE_URL", "http://offline.invalid/v1")
    monkeypatch.setenv("OFFLINE_LLM_API_KEY", "test")
    monkeypatch.setenv("OFFLINE_GENERAL_MODEL", "offline-general")
    monkeypatch.setenv("OFFLINE_CRITIC_MODEL", "offline-critic")
    monkeypatch.setenv("ONLINE_LLM_ENABLED", "true")
    monkeypatch.setenv("ONLINE_LLM_BASE_URL", "http://online.invalid/v1")
    monkeypatch.setenv("ONLINE_LLM_API_KEY", "test")
    monkeypatch.setenv("ONLINE_PUBLIC_MODEL", "online-public")
    monkeypatch.setenv("PUBLIC_SEARCH_PROVIDER", "disabled")
    monkeypatch.delenv("RUNTIME_FAULT_POINT", raising=False)
    settings = Settings.load()
    pack = PromptPack(settings.prompt_pack_dir)
    db = Database(settings.db_path)
    return settings, pack, db


def create_project(db: Database, *, online: bool = True) -> str:
    project_id = new_id("project")
    config = {
        "internet_access_allowed": online,
        "anonymized_external_processing_allowed": online,
        "allowed_public_topics": ["公开资料"],
        "prohibited_external_fields": [],
        "recipient_scope": ["内部用户"],
        "allowed_model_endpoint_ids": ["offline-primary"] + (["online-public-primary"] if online else []),
        "retention_days": 365,
    }
    now = utc_now()
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (project_id, "预检测试项目", "", "INTERNAL", json.dumps(config), now, now),
    )
    return project_id


def add_completed_workflow(db: Database, project_id: str, workflow_type: str) -> str:
    workflow_id = new_id("wf")
    now = utc_now()
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            workflow_id,
            project_id,
            workflow_type,
            "COMPLETED",
            999,
            json.dumps({"workflow_type": workflow_type, "step_results": {}, "options": {}}),
            now,
            now,
        ),
    )
    return workflow_id


def build_engine(settings: Settings, pack: PromptPack, db: Database, executor) -> RecoverableWorkflowEngine:
    preflight = RuntimeDependencyPreflight(settings, pack, db)
    return RecoverableWorkflowEngine(
        db,
        pack,
        ReplayContext(pack),
        executor,
        PublicResearchService(settings),
        dependency_preflight=preflight,
    )


def test_wf3_disabled_search_starts_waiting_configuration_without_model_call(live_env) -> None:
    settings, pack, db = live_env
    project_id = create_project(db)
    add_completed_workflow(db, project_id, "WF-1_PROJECT_INTAKE")
    executor = NeverExecutor()
    engine = build_engine(settings, pack, db, executor)

    workflow = engine.start(project_id, "WF-3_HYBRID_ONLINE_ASSIST")

    assert workflow["status"] == "WAITING_CONFIGURATION"
    assert executor.calls == 0
    issues = workflow["state"]["configuration_wait"]["issues"]
    assert any(item["code"] == "PUBLIC_SEARCH_DISABLED" for item in issues)
    assert workflow["state"]["configuration_wait"]["resume_step"] == 0


def test_legacy_public_search_block_migrates_without_consuming_retry(live_env) -> None:
    settings, pack, db = live_env
    project_id = create_project(db)
    prerequisite = add_completed_workflow(db, project_id, "WF-1_PROJECT_INTAKE")
    workflow_id = new_id("wf")
    now = utc_now()
    state = {
        "workflow_type": "WF-3_HYBRID_ONLINE_ASSIST",
        "options": {},
        "step_results": {"0": {"status": "PASS"}, "1": {"status": "PASS"}, "2": {"status": "PASS"}},
        "repair_attempts": {},
        "repair_overrides": {},
        "prerequisite_workflow_ids": {"WF-1_PROJECT_INTAKE": prerequisite},
        "technical_retry_attempts": {"3": 2},
        "last_error": "PUBLIC_SEARCH_PROVIDER is disabled",
    }
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (workflow_id, project_id, "WF-3_HYBRID_ONLINE_ASSIST", "BLOCKED", 3, json.dumps(state), now, now),
    )
    executor = NeverExecutor()
    engine = build_engine(settings, pack, db, executor)

    result = asyncio.run(engine.advance(workflow_id))

    assert result["status"] == "WAITING_CONFIGURATION"
    assert result["current_step"] == 3
    assert result["state"]["technical_retry_attempts"]["3"] == 2
    assert executor.calls == 0
    assert result["state"]["configuration_wait"]["source"] == "LEGACY_BLOCKED_CONFIGURATION_MIGRATION"


def test_prompt_endpoint_error_becomes_waiting_configuration(live_env) -> None:
    settings, pack, db = live_env
    project_id = create_project(db)
    executor = FailingExecutor("LLM endpoint returned 401: invalid key")
    engine = build_engine(settings, pack, db, executor)

    workflow = engine.start(project_id, "WF-1_PROJECT_INTAKE")
    assert workflow["status"] == "RUNNING"
    result = asyncio.run(engine.advance(workflow["id"]))

    assert result["status"] == "WAITING_CONFIGURATION"
    assert result["current_step"] == 0
    assert executor.calls == 1
    assert result["state"]["configuration_wait"]["issues"][0]["code"] == "MODEL_ENDPOINT_RUNTIME_UNAVAILABLE"
    assert not result["state"].get("technical_retry_attempts")


def test_fault_injection_is_reported_before_live_workflow_runs(live_env, monkeypatch) -> None:
    settings, pack, db = live_env
    monkeypatch.setenv("RUNTIME_FAULT_POINT", "before_model_request")
    preflight = RuntimeDependencyPreflight(settings, pack, db)

    report = preflight.application_report()

    assert any(item.code == "RUNTIME_FAULT_INJECTION_ENABLED" for item in report.blocking_issues)


def test_staged_transition_missing_evidence_waits_for_configuration(tmp_path: Path, monkeypatch) -> None:
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("MODEL_RUNTIME_MODE", "REPLAY")
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PROMPT_PACK_DIR", str(root / "prompt_pack"))
    settings = Settings.load()
    pack = PromptPack(settings.prompt_pack_dir)
    db = Database(settings.db_path)
    project_id = create_project(db)
    preflight = RuntimeDependencyPreflight(settings, pack, db)
    coordinator = StagedWorkflowCoordinator(db, settings, dependency_preflight=preflight)
    workflow = coordinator.start(project_id, {"project_title": "测试课题"})

    state = workflow["state"]
    stage4_dir = Path(state["run_root"]) / "stage4"
    stage4_dir.mkdir(parents=True, exist_ok=True)
    (stage4_dir / "LATEST_STATE.json").write_text(
        json.dumps({"status": "COMPLETED", "next_stage": "STAGE_4A_EVIDENCE_COMPLETION"}),
        encoding="utf-8",
    )
    state["current_stage"] = "stage4"
    state["stage_runs"]["stage4"] = str(stage4_dir)
    row = coordinator._row(workflow["id"])
    coordinator._save(row, status="RUNNING", current_step=3, state=state)

    result = asyncio.run(coordinator.advance(workflow["id"]))

    assert result["status"] == "WAITING_CONFIGURATION"
    assert result["state"]["configuration_wait"]["issues"][0]["code"] == "STAGE4A_EVIDENCE_INPUTS_MISSING"
    assert result["state"]["configuration_wait"]["resume_stage"] == "stage4"


def test_staged_configuration_wait_resumes_pending_transition(tmp_path: Path, monkeypatch) -> None:
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("MODEL_RUNTIME_MODE", "REPLAY")
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PROMPT_PACK_DIR", str(root / "prompt_pack"))
    settings = Settings.load()
    pack = PromptPack(settings.prompt_pack_dir)
    db = Database(settings.db_path)
    project_id = create_project(db)
    preflight = RuntimeDependencyPreflight(settings, pack, db)
    coordinator = StagedWorkflowCoordinator(db, settings, dependency_preflight=preflight)
    workflow = coordinator.start(project_id, {"project_title": "测试课题"})

    state = workflow["state"]
    stage4_dir = Path(state["run_root"]) / "stage4"
    stage4_dir.mkdir(parents=True, exist_ok=True)
    (stage4_dir / "LATEST_STATE.json").write_text(
        json.dumps({"status": "COMPLETED", "next_stage": "STAGE_4A_EVIDENCE_COMPLETION"}),
        encoding="utf-8",
    )
    state["current_stage"] = "stage4"
    state["current_stage_state"] = {
        "status": "COMPLETED",
        "next_stage": "STAGE_4A_EVIDENCE_COMPLETION",
    }
    state["stage_runs"]["stage4"] = str(stage4_dir)
    row = coordinator._row(workflow["id"])
    coordinator._save(row, status="RUNNING", current_step=3, state=state)
    waiting = asyncio.run(coordinator.advance(workflow["id"]))
    assert waiting["status"] == "WAITING_CONFIGURATION"

    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    waiting_state = waiting["state"]
    waiting_state["options"]["evidence_inputs"] = str(evidence)
    waiting_row = coordinator._row(workflow["id"])
    coordinator._save(waiting_row, status="WAITING_CONFIGURATION", state=waiting_state)

    initialized: list[str] = []

    def fake_initialize(workflow_id: str, next_state: dict[str, Any], stage: str, **_: Any) -> None:
        initialized.append(stage)
        next_state["current_stage"] = stage
        next_state.setdefault("stage_runs", {})[stage] = str(Path(next_state["run_root"]) / stage)
        current = coordinator._row(workflow_id)
        current["state"] = next_state
        coordinator._save(
            current,
            status="RUNNING",
            current_step=STAGED_STEPS.index(stage),
            state=next_state,
        )

    from app.staged_workflows import STAGED_STEPS

    monkeypatch.setattr(coordinator, "_initialize_stage", fake_initialize)
    resumed = asyncio.run(coordinator.advance(workflow["id"]))

    assert initialized == ["stage4a"]
    assert resumed["status"] == "RUNNING"
    assert resumed["state"]["current_stage"] == "stage4a"
    assert "configuration_wait" not in resumed["state"]


def test_missing_mermaid_runtime_is_reported_without_breaking_skill_registration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from app.skills.base import SkillContext
    from app.skills.mermaid import MermaidRenderError, MermaidRenderSkill

    root = Path(__file__).resolve().parents[1]
    missing_runtime = tmp_path / "missing-mermaid.min.js"
    monkeypatch.setenv("MODEL_RUNTIME_MODE", "REPLAY")
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PROMPT_PACK_DIR", str(root / "prompt_pack"))
    monkeypatch.setenv("MERMAID_JS_PATH", str(missing_runtime))
    settings = Settings.load()
    pack = PromptPack(settings.prompt_pack_dir)

    # Construction must remain available so the API can expose the preflight
    # diagnosis instead of crashing during module import.
    skill = MermaidRenderSkill(settings)
    report = RuntimeDependencyPreflight(settings, pack).application_report()

    assert any(item.code == "MERMAID_RUNTIME_NOT_FOUND" for item in report.issues)
    with pytest.raises(MermaidRenderError, match="MERMAID_JS_PATH is unavailable"):
        skill.run(
            {"mermaid_source": "flowchart LR\nA --> B"},
            SkillContext(
                project_id="project-test",
                workflow_id="wf-test",
                security_level="INTERNAL",
                data_dir=str(tmp_path / "data"),
            ),
        )
