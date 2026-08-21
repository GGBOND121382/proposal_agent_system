from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.db import Database
from app.documents import parse_document
from app.pack import PromptPack
from app.research import PublicResearchService
from app.runtime_context import LiveContextBuilder
from app.runtime_workflows import RecoverableWorkflowEngine
from app.util import new_id, sha256_json, utc_now
from app.workflow_input import (
    APPLICATION_GUIDE_INPUT,
    REFERENCE_TEMPLATE_INPUT,
    WorkflowInputRequired,
    build_human_resolutions,
    validate_gate_questions,
)
from app.workflow_status import should_pause_automatic_advancement


class NeverExecutor:
    output_normalizer_version = "test"

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, *args, **kwargs):  # pragma: no cover - failure guard
        self.calls += 1
        raise AssertionError("model executor must not run before inputs/prerequisites are resolved")


@pytest.fixture()
def live_runtime(tmp_path: Path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("MODEL_RUNTIME_MODE", "LIVE")
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PROMPT_PACK_DIR", str(root / "prompt_pack"))
    settings = Settings.load()
    pack = PromptPack(settings.prompt_pack_dir)
    db = Database(settings.db_path)
    builder = LiveContextBuilder(db, pack)
    executor = NeverExecutor()
    engine = RecoverableWorkflowEngine(
        db,
        pack,
        builder,
        executor,
        PublicResearchService(settings),
    )
    return settings, pack, db, builder, executor, engine


def create_project(db: Database) -> str:
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
            "公开比较方法与评价指标研究",
            "INTERNAL",
            json.dumps(config, ensure_ascii=False),
            now,
            now,
        ),
    )
    return project_id


def add_document(settings: Settings, db: Database, project_id: str, role: str, text: str) -> str:
    filename = f"{role.lower()}-{new_id('doc')}.md"
    raw = text.encode("utf-8")
    parsed = parse_document(filename, raw, role, "INTERNAL")
    path = settings.uploads_dir / filename
    path.write_bytes(raw)
    db.execute(
        "INSERT INTO documents(id,project_id,filename,role,security_level,document_hash,file_path,parsed_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            parsed["document_id"],
            project_id,
            filename,
            role,
            "INTERNAL",
            parsed["document_hash"],
            str(path),
            json.dumps(parsed, ensure_ascii=False),
            utc_now(),
        ),
    )
    return parsed["document_id"]


def add_workflow(
    db: Database,
    project_id: str,
    workflow_type: str,
    status: str,
    *,
    workflow_id: str | None = None,
    current_step: int = 0,
    state: dict[str, Any] | None = None,
) -> str:
    workflow_id = workflow_id or new_id("wf")
    now = utc_now()
    state = state or {
        "workflow_type": workflow_type,
        "options": {},
        "step_results": {},
        "repair_attempts": {},
        "repair_overrides": {},
    }
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            workflow_id,
            project_id,
            workflow_type,
            status,
            current_step,
            json.dumps(state, ensure_ascii=False),
            now,
            now,
        ),
    )
    return workflow_id


def add_artifact(
    db: Database,
    project_id: str,
    prompt_id: str,
    output: dict[str, Any],
    *,
    workflow_id: str | None,
    status: str = "PASS",
    version: int = 1,
) -> str:
    artifact_id = new_id("artifact")
    db.execute(
        """INSERT INTO artifacts(
             id,project_id,workflow_id,artifact_type,prompt_id,version,status,
             security_level,context_hash,content_json,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            artifact_id,
            project_id,
            workflow_id,
            "PROMPT_OUTPUT",
            prompt_id,
            version,
            status,
            "PUBLIC" if prompt_id.startswith("P-PUBLIC") else "INTERNAL",
            sha256_json(output),
            json.dumps(output, ensure_ascii=False),
            utc_now(),
        ),
    )
    return artifact_id


def add_prompt_run(
    db: Database,
    project_id: str,
    workflow_id: str,
    prompt_id: str,
    output: dict[str, Any],
    *,
    status: str,
) -> str:
    run_id = new_id("run")
    now = utc_now()
    db.execute(
        """INSERT INTO prompt_runs(
             id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,
             input_hash,output_hash,input_json,output_json,error,duration_ms,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            run_id,
            project_id,
            workflow_id,
            prompt_id,
            status,
            "test-model",
            "online-public-primary",
            sha256_json({}),
            sha256_json(output),
            "{}",
            json.dumps(output, ensure_ascii=False),
            None,
            1,
            now,
        ),
    )
    return run_id


def wf3_state() -> dict[str, Any]:
    return {
        "workflow_type": "WF-3_HYBRID_ONLINE_ASSIST",
        "options": {
            "research_need": {
                "question": "公开研究中有哪些可核验的比较基线与评价指标？",
                "reason_online_needed": "需要核验最新公开来源。",
                "desired_output": "带来源、边界和时间范围的比较表。",
            },
            "target_task_type": "PUBLIC_RESEARCH",
        },
        "step_results": {},
        "repair_attempts": {},
        "repair_overrides": {},
    }


def test_gate_accepted_revise_output_is_available_to_next_prompt(live_runtime):
    _, pack, db, builder, _, engine = live_runtime
    project_id = create_project(db)
    state = wf3_state()
    workflow_id = add_workflow(
        db,
        project_id,
        "WF-3_HYBRID_ONLINE_ASSIST",
        "RUNNING",
        current_step=4,
        state=state,
    )
    output = pack.replay_output("P-PUBLIC-RESEARCH-SYNTHESIS", "normal")
    output["status"] = "REVISE"
    output["result"]["limitations"] = ["explicitly accepted limitation"]
    add_artifact(
        db,
        project_id,
        "P-PUBLIC-RESEARCH-SYNTHESIS",
        output,
        workflow_id=workflow_id,
        status="REVISE",
    )
    run_id = add_prompt_run(
        db,
        project_id,
        workflow_id,
        "P-PUBLIC-RESEARCH-SYNTHESIS",
        output,
        status="REVISE",
    )
    workflow = engine.get(workflow_id)
    workflow["state"]["step_results"]["4"] = {
        "prompt_id": "P-PUBLIC-RESEARCH-SYNTHESIS",
        "run_id": run_id,
        "status": "REVISE",
    }
    engine._update(workflow, state=workflow["state"])
    gate_id = engine._create_gate(
        engine.get(workflow_id),
        "PROJECT_GAP_RESOLUTION",
        target_id=run_id,
        questions=[],
    )
    engine.decide_gate(
        gate_id,
        action="CONFIRM",
        decided_by="pytest",
        decided_role="PROJECT_OWNER",
    )

    accepted = builder._latest_output(
        project_id,
        "P-PUBLIC-RESEARCH-SYNTHESIS",
        workflow_id=workflow_id,
    )

    assert accepted == output
    assert accepted["result"]["limitations"] == ["explicitly accepted limitation"]


def test_unapproved_revise_output_is_not_available_to_next_prompt(live_runtime):
    _, pack, db, builder, _, engine = live_runtime
    project_id = create_project(db)
    workflow_id = add_workflow(
        db,
        project_id,
        "WF-3_HYBRID_ONLINE_ASSIST",
        "RUNNING",
        current_step=4,
        state=wf3_state(),
    )
    output = pack.replay_output("P-PUBLIC-RESEARCH-SYNTHESIS", "normal")
    output["status"] = "REVISE"
    add_artifact(
        db,
        project_id,
        "P-PUBLIC-RESEARCH-SYNTHESIS",
        output,
        workflow_id=workflow_id,
        status="REVISE",
    )
    run_id = add_prompt_run(
        db,
        project_id,
        workflow_id,
        "P-PUBLIC-RESEARCH-SYNTHESIS",
        output,
        status="REVISE",
    )
    workflow = engine.get(workflow_id)
    workflow["state"]["accepted_step_results"] = {
        "4": {
            "run_id": run_id,
            "status": "REVISE",
            "gate_id": "gate-not-approved",
            "action": "CONFIRM",
        }
    }
    engine._update(workflow, state=workflow["state"])

    assert builder._latest_output(
        project_id,
        "P-PUBLIC-RESEARCH-SYNTHESIS",
        workflow_id=workflow_id,
    ) is None


def test_wf3_downstream_live_inputs_are_complete_and_schema_valid(live_runtime):
    _, pack, db, builder, _, _ = live_runtime
    project_id = create_project(db)
    state = wf3_state()
    workflow_id = add_workflow(
        db,
        project_id,
        "WF-3_HYBRID_ONLINE_ASSIST",
        "RUNNING",
        workflow_id="wf-live-wf3",
        state=state,
    )
    safe_output = pack.replay_output("P-SAFE-ONLINE-PACKAGE", "normal")
    add_artifact(db, project_id, "P-SAFE-ONLINE-PACKAGE", safe_output, workflow_id=workflow_id)

    critic = builder.build(
        "P-SAFE-ONLINE-PACKAGE-CRITIC",
        project_id,
        workflow_id=workflow_id,
        workflow_state=state,
    )
    assert pack.validate("P-SAFE-ONLINE-PACKAGE-CRITIC", "input", critic) == []
    assert critic["payload"]["deterministic_scan"]["passed"] is True
    assert "source_summary" in critic["payload"]

    plan = builder.build(
        "P-PUBLIC-RESEARCH-PLAN",
        project_id,
        workflow_id=workflow_id,
        workflow_state=state,
    )
    assert pack.validate("P-PUBLIC-RESEARCH-PLAN", "input", plan) == []
    assert plan["payload"]["task_type"] == "PUBLIC_RESEARCH"
    assert plan["payload"]["evidence_requirements"]
    assert "safe_online_package_content" in plan["payload"]

    plan_output = pack.replay_output("P-PUBLIC-RESEARCH-PLAN", "normal")
    add_artifact(db, project_id, "P-PUBLIC-RESEARCH-PLAN", plan_output, workflow_id=workflow_id)
    search_input = pack.replay_case("P-PUBLIC-RESEARCH-SYNTHESIS", "normal")["input"]["payload"]
    state["public_search_results"] = {
        "sources": search_input["retrieved_sources"],
        "passages": search_input["extracted_passages"],
        "queries": ["公开评价方法"],
    }
    synthesis = builder.build(
        "P-PUBLIC-RESEARCH-SYNTHESIS",
        project_id,
        workflow_id=workflow_id,
        workflow_state=state,
    )
    assert pack.validate("P-PUBLIC-RESEARCH-SYNTHESIS", "input", synthesis) == []
    assert "safe_online_package_content" not in synthesis["payload"]

    synthesis_output = pack.replay_output("P-PUBLIC-RESEARCH-SYNTHESIS", "normal")
    add_artifact(db, project_id, "P-PUBLIC-RESEARCH-SYNTHESIS", synthesis_output, workflow_id=workflow_id)
    critic2 = builder.build(
        "P-PUBLIC-RESEARCH-CRITIC",
        project_id,
        workflow_id=workflow_id,
        workflow_state=state,
    )
    assert pack.validate("P-PUBLIC-RESEARCH-CRITIC", "input", critic2) == []
    assert "safe_online_package_content" not in critic2["payload"]


def test_prerequisite_wait_cannot_be_bypassed_by_advance(live_runtime):
    _, _, db, _, executor, engine = live_runtime
    project_id = create_project(db)
    workflow = engine.start(project_id, "WF-3_HYBRID_ONLINE_ASSIST", {})
    assert workflow["status"] == "WAITING_PREREQUISITE"

    resumed = asyncio.run(engine.advance(workflow["id"]))
    assert resumed["status"] == "WAITING_PREREQUISITE"
    assert resumed["current_step"] == 0
    assert executor.calls == 0
    assert db.fetchone("SELECT COUNT(*) AS n FROM prompt_runs WHERE workflow_id=?", (workflow["id"],))["n"] == 0


def test_duplicate_active_parent_workflow_is_rejected(live_runtime):
    _, _, db, _, _, engine = live_runtime
    project_id = create_project(db)
    first = engine.start(project_id, "WF-1_PROJECT_INTAKE", {})
    with pytest.raises(ValueError, match=first["id"]):
        engine.start(project_id, "WF-1_PROJECT_INTAKE", {})


def test_duplicate_blocked_parent_workflow_is_rejected(live_runtime):
    _, _, db, _, _, engine = live_runtime
    project_id = create_project(db)
    blocked_id = add_workflow(
        db,
        project_id,
        "WF-1_PROJECT_INTAKE",
        "BLOCKED",
        state={
            "workflow_type": "WF-1_PROJECT_INTAKE",
            "options": {},
            "step_results": {},
            "repair_attempts": {},
            "repair_overrides": {},
            "last_error": "等待人工修复",
        },
    )
    with pytest.raises(ValueError, match=blocked_id):
        engine.start(project_id, "WF-1_PROJECT_INTAKE", {})


def test_artifact_selection_ignores_failed_and_unrelated_running_outputs(live_runtime):
    _, pack, db, builder, _, _ = live_runtime
    project_id = create_project(db)
    completed_id = add_workflow(db, project_id, "WF-1_PROJECT_INTAKE", "COMPLETED")
    unrelated_id = add_workflow(db, project_id, "WF-1_PROJECT_INTAKE", "RUNNING")
    consumer_state = {
        "workflow_type": "WF-4_PROPOSAL_AUTHORING",
        "options": {},
        "step_results": {},
        "repair_attempts": {},
        "repair_overrides": {},
        "prerequisite_workflow_ids": {"WF-1_PROJECT_INTAKE": completed_id},
    }
    consumer_id = add_workflow(
        db,
        project_id,
        "WF-4_PROPOSAL_AUTHORING",
        "RUNNING",
        state=consumer_state,
    )

    good = pack.replay_output("P-PROJECT-DEFINITION-EXTRACT", "normal")
    good["result"]["project_definition"]["project_name"] = "已确认项目定义"
    add_artifact(db, project_id, "P-PROJECT-DEFINITION-EXTRACT", good, workflow_id=completed_id, version=1)

    unrelated = pack.replay_output("P-PROJECT-DEFINITION-EXTRACT", "normal")
    unrelated["result"]["project_definition"]["project_name"] = "并发未确认结果"
    add_artifact(db, project_id, "P-PROJECT-DEFINITION-EXTRACT", unrelated, workflow_id=unrelated_id, version=99)

    rejected = pack.replay_output("P-PROJECT-DEFINITION-EXTRACT", "normal")
    rejected["result"]["project_definition"]["project_name"] = "被拒绝结果"
    add_artifact(db, project_id, "P-PROJECT-DEFINITION-EXTRACT", rejected, workflow_id=completed_id, status="BLOCK", version=100)

    result = builder._result(
        project_id,
        "P-PROJECT-DEFINITION-EXTRACT",
        "project_definition",
        workflow_id=consumer_id,
    )
    assert result["project_name"] == "已确认项目定义"


def test_artifact_binding_is_frozen_when_a_new_prerequisite_completes(live_runtime):
    _, pack, db, builder, _, _ = live_runtime
    project_id = create_project(db)
    bound_id = add_workflow(db, project_id, "WF-1_PROJECT_INTAKE", "COMPLETED")
    state = {
        "workflow_type": "WF-3_HYBRID_ONLINE_ASSIST",
        "options": {},
        "step_results": {},
        "repair_attempts": {},
        "repair_overrides": {},
        "prerequisite_workflow_ids": {"WF-1_PROJECT_INTAKE": bound_id},
    }
    consumer_id = add_workflow(
        db,
        project_id,
        "WF-3_HYBRID_ONLINE_ASSIST",
        "RUNNING",
        state=state,
    )

    bound = pack.replay_output("P-PROJECT-DEFINITION-EXTRACT", "normal")
    bound["result"]["project_definition"]["project_name"] = "启动时冻结的项目定义"
    add_artifact(db, project_id, "P-PROJECT-DEFINITION-EXTRACT", bound, workflow_id=bound_id, version=1)

    newer_id = add_workflow(db, project_id, "WF-1_PROJECT_INTAKE", "COMPLETED")
    newer = pack.replay_output("P-PROJECT-DEFINITION-EXTRACT", "normal")
    newer["result"]["project_definition"]["project_name"] = "后来完成但未绑定的项目定义"
    add_artifact(db, project_id, "P-PROJECT-DEFINITION-EXTRACT", newer, workflow_id=newer_id, version=99)

    result = builder._result(
        project_id,
        "P-PROJECT-DEFINITION-EXTRACT",
        "project_definition",
        workflow_id=consumer_id,
    )
    assert result["project_name"] == "启动时冻结的项目定义"


def test_public_claims_require_explicit_completed_import_approval(live_runtime):
    _, pack, db, builder, _, _ = live_runtime
    project_id = create_project(db)
    workflow_id = add_workflow(db, project_id, "WF-3_HYBRID_ONLINE_ASSIST", "COMPLETED")

    claim = {
        "claim_id": "public-claim-1",
        "claim_text": "公开资料支持该评价指标。",
        "claim_type": "PUBLIC_CLAIM",
        "subject_id": None,
        "temporal_status": "CURRENT",
        "qualifiers": [],
        "numeric_values": [],
        "source_refs": [],
        "knowledge_status": "DOCUMENT_EXTRACTED",
        "security_level": "PUBLIC",
    }
    synthesis = pack.replay_output("P-PUBLIC-RESEARCH-SYNTHESIS", "normal")
    synthesis["result"]["claims"] = [claim]
    add_artifact(db, project_id, "P-PUBLIC-RESEARCH-SYNTHESIS", synthesis, workflow_id=workflow_id)

    review = pack.replay_output("P-ONLINE-RESULT-IMPORT-CRITIC", "normal")
    review["result"]["accepted_claim_ids"] = []
    add_artifact(db, project_id, "P-ONLINE-RESULT-IMPORT-CRITIC", review, workflow_id=workflow_id)
    gate_id = new_id("gate")
    now = utc_now()
    db.execute(
        """INSERT INTO gates(id,project_id,workflow_id,gate_type,target_id,target_version,context_hash,
           question_version,required_role,allowed_actions_json,questions_json,security_level,status,decision_json,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            gate_id,
            project_id,
            workflow_id,
            "ONLINE_RESULT_IMPORT_APPROVAL",
            "run-import",
            1,
            "0" * 64,
            1,
            "SECURITY_REVIEWER",
            json.dumps(["APPROVE"]),
            "[]",
            "INTERNAL",
            "APPROVED",
            json.dumps({"action": "APPROVE"}),
            now,
            now,
        ),
    )
    assert builder._approved_public_claims(project_id) == []

    review["result"]["accepted_claim_ids"] = [claim["claim_id"]]
    add_artifact(
        db,
        project_id,
        "P-ONLINE-RESULT-IMPORT-CRITIC",
        review,
        workflow_id=workflow_id,
        version=2,
    )
    assert builder._approved_public_claims(project_id) == [claim]


def test_generic_information_gate_reruns_same_prompt_and_injects_answer(live_runtime):
    _, pack, db, builder, _, engine = live_runtime
    project_id = create_project(db)
    state = wf3_state()
    state["step_results"] = {
        "0": {
            "prompt_id": "P-SAFE-ONLINE-PACKAGE",
            "run_id": "run-needs-input",
            "status": "NEED_USER_INPUT",
        }
    }
    workflow_id = add_workflow(
        db,
        project_id,
        "WF-3_HYBRID_ONLINE_ASSIST",
        "RUNNING",
        state=state,
    )
    workflow = engine.get(workflow_id)
    gate_id = engine._create_gate(
        workflow,
        "PROJECT_GAP_RESOLUTION",
        target_id="run-needs-input",
        questions=[
            {
                "question_id": "research-question",
                "question": "请确认公开研究问题",
                "target_paths": ["payload.research_need.question"],
                "answer_schema": {"type": "STRING"},
                "required": True,
            }
        ],
    )
    engine._update(workflow, status="WAITING_GATE", state=state)

    engine.decide_gate(
        gate_id,
        action="CONFIRM",
        decided_by="pytest",
        decided_role="PROJECT_OWNER",
        answers=[{"question_id": "research-question", "value": "公开可核验基线有哪些？"}],
    )
    updated = engine.get(workflow_id)
    assert updated["status"] == "RUNNING"
    assert updated["current_step"] == 0
    assert "0" not in updated["state"]["step_results"]
    assert updated["state"].get("repair_attempts", {}) == {}
    assert updated["state"]["human_input_reruns"][
        "step:0:P-SAFE-ONLINE-PACKAGE"
    ] == 1

    envelope = builder.build(
        "P-SAFE-ONLINE-PACKAGE",
        project_id,
        workflow_id=workflow_id,
        workflow_state=updated["state"],
    )
    assert envelope["payload"]["research_need"]["question"] == "公开可核验基线有哪些？"
    assert envelope["payload"]["human_resolutions"][0]["answer"] == "公开可核验基线有哪些？"
    assert pack.validate("P-SAFE-ONLINE-PACKAGE", "input", envelope) == []


def test_live_material_role_mismatch_creates_specific_input_requirement(live_runtime):
    settings, _, db, builder, _, _ = live_runtime
    project_id = create_project(db)
    add_document(settings, db, project_id, "PROJECT_BRIEF", "# 项目简介\n研究目标与验证方案。")

    with pytest.raises(WorkflowInputRequired) as scheme_exc:
        builder.build("P-SCHEME-EXTRACT", project_id)
    assert scheme_exc.value.gate_type == APPLICATION_GUIDE_INPUT

    add_document(settings, db, project_id, "CURRENT_PROPOSAL", "# 当前申请书\n待修改。")
    with pytest.raises(WorkflowInputRequired) as template_exc:
        builder.build("P-TEMPLATE-EXTRACT", project_id)
    assert template_exc.value.gate_type == REFERENCE_TEMPLATE_INPUT


def test_blocking_gate_answer_is_required_and_enum_is_enforced():
    from app.workflow_input import build_human_resolutions

    questions = [
        {
            "question_id": "choice",
            "question": "请选择处理方式",
            "target_paths": ["payload.target_task_type"],
            "answer_schema": {
                "type": "ENUM",
                "allowed_values": ["PUBLIC_RESEARCH", "PUBLIC_TEMPLATE_ANALYSIS"],
            },
            "blocking": True,
        }
    ]
    with pytest.raises(ValueError, match="必须回答"):
        build_human_resolutions(
            gate_id="gate-test",
            prompt_id="P-SAFE-ONLINE-PACKAGE",
            questions=questions,
            answers=[],
            decided_by="pytest",
            decided_role="PROJECT_OWNER",
        )
    with pytest.raises(ValueError, match="允许范围"):
        build_human_resolutions(
            gate_id="gate-test",
            prompt_id="P-SAFE-ONLINE-PACKAGE",
            questions=questions,
            answers=[{"question_id": "choice", "value": "UNSUPPORTED"}],
            decided_by="pytest",
            decided_role="PROJECT_OWNER",
        )


def test_strict_live_context_completes_full_workflow_with_simulated_provider(tmp_path: Path, monkeypatch):
    from app.runtime_api import ContextBuilder, ModelGateway, PromptExecutor, WorkflowEngine
    from app.security import SecurityRouter

    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("MODEL_RUNTIME_MODE", "SIMULATED")
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "strict-live-data"))
    monkeypatch.setenv("PROMPT_PACK_DIR", str(root / "prompt_pack"))
    settings = Settings.load()
    pack = PromptPack(settings.prompt_pack_dir)
    db = Database(settings.db_path)
    builder = ContextBuilder(db, pack)
    # Exercise LIVE input assembly and unresolved-scaffold checks while the
    # provider remains deterministic and offline for regression testing.
    builder.runtime_mode = "LIVE"
    engine = WorkflowEngine(
        db,
        pack,
        builder,
        PromptExecutor(db, pack, SecurityRouter(pack), ModelGateway(settings, pack)),
        PublicResearchService(settings),
    )
    project_id = create_project(db)
    add_document(settings, db, project_id, "APPLICATION_GUIDE", "# 申报指南\n要求问题、方法、创新和验证闭环。")
    add_document(settings, db, project_id, "PROJECT_BRIEF", "# 项目任务\n研究动态优化与低扰动重规划。")
    add_document(settings, db, project_id, "REFERENCE_PROPOSAL", "# 立项依据\n从差距导出问题。\n# 研究方案\n方法绑定验证。")
    add_document(settings, db, project_id, "EVIDENCE_MATERIAL", "# 前期成果\n已有原型和实验记录。")
    add_document(
        settings,
        db,
        project_id,
        "CURRENT_PROPOSAL",
        "# 全文\n待完善。\n# 立项依据\n待写。\n# 研究目标\n待写。\n# 研究内容\n待写。\n# 研究方案\n待写。\n# 创新点\n待写。\n# 研究基础\n待写。\n# 参考文献\n待写。",
    )

    async def finish(workflow_type: str) -> dict[str, Any]:
        workflow = engine.start(project_id, workflow_type, {})
        for _ in range(800):
            workflow = await engine.advance(workflow["id"])
            if workflow["status"] == "WAITING_GATE":
                gate = next(
                    item
                    for item in engine.list_gates(workflow_id=workflow["id"])
                    if item["status"] == "OPEN"
                )
                action = "APPROVE" if "APPROVE" in gate["allowed_actions"] else "CONFIRM"
                engine.decide_gate(
                    gate["id"],
                    action=action,
                    decided_by="pytest",
                    decided_role=gate["required_role"],
                )
                continue
            if should_pause_automatic_advancement(workflow["status"]):
                return workflow
        return workflow

    async def run_all() -> None:
        for workflow_type in (
            "WF-1_PROJECT_INTAKE",
            "WF-2_TEMPLATE_EXTRACTION",
            "WF-3_HYBRID_ONLINE_ASSIST",
            "WF-4_PROPOSAL_AUTHORING",
            "WF-5_SECURITY_REVIEW_AND_EXPORT",
        ):
            workflow = await finish(workflow_type)
            assert workflow["status"] == "COMPLETED", workflow["state"].get("last_error")

    asyncio.run(run_all())


def test_human_answer_bundle_rejects_duplicate_and_stale_question_ids() -> None:
    from app.workflow_input import build_human_resolutions

    questions = [
        {
            "question_id": "current-question",
            "question": "请提供当前值",
            "target_paths": ["payload.target"],
            "answer_schema": {"type": "STRING"},
            "blocking": True,
        }
    ]
    with pytest.raises(ValueError, match="多个回答"):
        build_human_resolutions(
            gate_id="gate-test",
            prompt_id="P-TEST",
            questions=questions,
            answers=[
                {"question_id": "current-question", "value": "first"},
                {"question_id": "current-question", "value": "second"},
            ],
            decided_by="pytest",
            decided_role="PROJECT_OWNER",
        )
    with pytest.raises(ValueError, match="不属于当前 Gate"):
        build_human_resolutions(
            gate_id="gate-test",
            prompt_id="P-TEST",
            questions=questions,
            answers=[
                {"question_id": "current-question", "value": "current"},
                {"question_id": "stale-question", "value": "stale"},
            ],
            decided_by="pytest",
            decided_role="PROJECT_OWNER",
        )


def test_human_gate_rejects_duplicate_question_identity() -> None:
    from app.workflow_input import build_human_resolutions

    questions = [
        {
            "question_id": "duplicate",
            "question": "第一个问题",
            "target_paths": ["payload.first"],
            "answer_schema": {"type": "STRING"},
            "blocking": True,
        },
        {
            "question_id": "duplicate",
            "question": "第二个问题",
            "target_paths": ["payload.second"],
            "answer_schema": {"type": "STRING"},
            "blocking": True,
        },
    ]
    with pytest.raises(ValueError, match="question_id 重复"):
        build_human_resolutions(
            gate_id="gate-test",
            prompt_id="P-TEST",
            questions=questions,
            answers=[{"question_id": "duplicate", "value": "answer"}],
            decided_by="pytest",
            decided_role="PROJECT_OWNER",
        )


def test_enum_human_answer_preserves_boolean_and_numeric_allowed_values() -> None:
    for answer, allowed in ((False, [True, False]), (0, [0, 1])):
        resolutions = build_human_resolutions(
            gate_id=f"gate-{answer!r}",
            prompt_id="P-TEST",
            questions=[
                {
                    "question_id": "choice",
                    "question": "请选择",
                    "target_paths": ["payload.choice"],
                    "answer_schema": {
                        "type": "ENUM",
                        "allowed_values": allowed,
                    },
                    "blocking": True,
                }
            ],
            answers=[{"question_id": "choice", "value": answer}],
            decided_by="pytest",
            decided_role="PROJECT_OWNER",
        )
        assert resolutions[0]["answer"] is answer or resolutions[0]["answer"] == answer
        assert type(resolutions[0]["answer"]) is type(answer)


@pytest.mark.parametrize(
    ("submitted", "allowed", "expected", "expected_type"),
    [
        ("true", [True, False], True, bool),
        ("false", [True, False], False, bool),
        ("1", [0, 1], 1, int),
        ("2.5", [1.0, 2.5], 2.5, float),
    ],
)
def test_enum_human_answer_recovers_browser_string_types(
    submitted: str,
    allowed: list[Any],
    expected: Any,
    expected_type: type,
) -> None:
    resolutions = build_human_resolutions(
        gate_id="gate-browser-enum",
        prompt_id="P-TEST",
        questions=[
            {
                "question_id": "choice",
                "question": "请选择",
                "answer_schema": {"type": "ENUM", "allowed_values": allowed},
                "blocking": True,
            }
        ],
        answers=[{"question_id": "choice", "value": submitted}],
        decided_by="pytest",
        decided_role="PROJECT_OWNER",
    )

    assert resolutions[0]["answer"] == expected
    assert type(resolutions[0]["answer"]) is expected_type


def test_enum_browser_string_is_rejected_when_scalar_recovery_is_ambiguous() -> None:
    with pytest.raises(ValueError, match="歧义"):
        build_human_resolutions(
            gate_id="gate-ambiguous-enum",
            prompt_id="P-TEST",
            questions=[
                {
                    "question_id": "choice",
                    "question": "请选择",
                    "answer_schema": {
                        "type": "ENUM",
                        "allowed_values": [True, 1],
                    },
                    "blocking": True,
                }
            ],
            answers=[{"question_id": "choice", "value": "1"}],
            decided_by="pytest",
            decided_role="PROJECT_OWNER",
        )


def test_gate_number_accepts_scientific_notation_and_rejects_boolean() -> None:
    question = {
        "question_id": "number",
        "question": "请输入数值",
        "answer_schema": {"type": "NUMBER"},
        "blocking": True,
    }
    result = build_human_resolutions(
        gate_id="gate-number",
        prompt_id="P-TEST",
        questions=[question],
        answers=[{"question_id": "number", "value": "1e3"}],
        decided_by="pytest",
        decided_role="PROJECT_OWNER",
    )
    assert result[0]["answer"] == 1000.0

    with pytest.raises(ValueError, match="数值"):
        build_human_resolutions(
            gate_id="gate-number",
            prompt_id="P-TEST",
            questions=[question],
            answers=[{"question_id": "number", "value": True}],
            decided_by="pytest",
            decided_role="PROJECT_OWNER",
        )


def test_gate_optional_blank_answer_is_omitted_but_required_blank_is_rejected() -> None:
    optional = {
        "question_id": "optional",
        "question": "可选补充",
        "answer_schema": {"type": "STRING"},
        "blocking": False,
    }
    assert build_human_resolutions(
        gate_id="gate-optional",
        prompt_id="P-TEST",
        questions=[optional],
        answers=[{"question_id": "optional", "value": "   "}],
        decided_by="pytest",
        decided_role="PROJECT_OWNER",
    ) == []

    required = {**optional, "blocking": True}
    with pytest.raises(ValueError, match="必须回答"):
        build_human_resolutions(
            gate_id="gate-required",
            prompt_id="P-TEST",
            questions=[required],
            answers=[{"question_id": "optional", "value": ""}],
            decided_by="pytest",
            decided_role="PROJECT_OWNER",
        )


def test_composite_gate_answer_is_validated_against_nested_schema() -> None:
    question = {
        "question_id": "object",
        "question": "请输入对象",
        "answer_schema": {
            "type": "OBJECT",
            "properties": {"count": {"type": "INTEGER", "minimum": 1}},
            "required": ["count"],
            "additionalProperties": False,
        },
        "blocking": True,
    }
    valid = build_human_resolutions(
        gate_id="gate-object",
        prompt_id="P-TEST",
        questions=[question],
        answers=[{"question_id": "object", "value": '{"count": 2}'}],
        decided_by="pytest",
        decided_role="PROJECT_OWNER",
    )
    assert valid[0]["answer"] == {"count": 2}

    with pytest.raises(ValueError, match="answer_schema"):
        build_human_resolutions(
            gate_id="gate-object",
            prompt_id="P-TEST",
            questions=[question],
            answers=[{"question_id": "object", "value": '{"count": 0}'}],
            decided_by="pytest",
            decided_role="PROJECT_OWNER",
        )


@pytest.mark.parametrize(
    "questions",
    [
        [{"question_id": "bad", "answer_schema": {"type": "UNKNOWN"}}],
        [
            {
                "question_id": "bad",
                "answer_schema": {"type": "ENUM", "allowed_values": [1, 1.0]},
            }
        ],
        [{"question_id": "bad", "answer_schema": {"type": "OBJECT"}}],
    ],
)
def test_invalid_gate_question_contract_is_rejected_before_persistence(
    questions: list[dict[str, Any]],
) -> None:
    with pytest.raises(ValueError):
        validate_gate_questions(questions)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_frontend_gate_decoder_preserves_json_scalar_types() -> None:
    app_js = Path(__file__).resolve().parents[1] / "app" / "static" / "app.js"
    script = r"""
const fs=require('fs');
const source=fs.readFileSync(process.argv[1],'utf8');
const start=source.indexOf('function decodeGateAnswer');
const end=source.indexOf('function collectGateAnswers',start);
if(start<0||end<0)throw new Error('decodeGateAnswer not found');
eval(source.slice(start,end));
const decode=(value,type,required=true)=>decodeGateAnswer({value,dataset:{answerType:type,required:String(required),questionId:'q'}});
const result={
  enumTrue:decode('true','ENUM'),
  enumNumber:decode('2.5','ENUM'),
  booleanFalse:decode('false','BOOLEAN'),
  number:decode('1e3','NUMBER'),
  object:decode('{"count":2}','OBJECT'),
  optionalBlank:decode('','STRING',false)
};
process.stdout.write(JSON.stringify(result));
"""
    completed = subprocess.run(
        [shutil.which("node") or "node", "-e", script, str(app_js)],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)
    assert result == {
        "enumTrue": True,
        "enumNumber": 2.5,
        "booleanFalse": False,
        "number": 1000,
        "object": {"count": 2},
    }


def test_human_answer_cannot_use_both_question_id_and_field_path_aliases() -> None:
    from app.workflow_input import build_human_resolutions

    with pytest.raises(ValueError, match="同时通过 question_id 和 field_path"):
        build_human_resolutions(
            gate_id="gate-test",
            prompt_id="P-TEST",
            questions=[
                {
                    "question_id": "question-1",
                    "field_path": "target",
                    "question": "请提供值",
                    "target_paths": ["payload.target"],
                    "answer_schema": {"type": "STRING"},
                    "blocking": True,
                }
            ],
            answers=[
                {"question_id": "question-1", "value": "first"},
                {"field_path": "target", "value": "second"},
            ],
            decided_by="pytest",
            decided_role="PROJECT_OWNER",
        )
