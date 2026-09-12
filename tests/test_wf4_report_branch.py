from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.background_research import WF3B_WORKFLOW_TYPE
from app.context_base import ContextBuilder
from app.db import Database
from app.pack import PromptPack
from app.util import new_id, utc_now
from app.workflow_defs import WF4_REPORT_BRANCH_STEPS, WORKFLOWS
from app.workflows import WorkflowEngine
from tests.test_wf3b_background_research import StubContext
from tests.test_workflow_lifecycle_rebuild import add_project, add_workflow

ROOT = Path(__file__).resolve().parents[1]
WF4 = "WF-4_PROPOSAL_AUTHORING"


def _engine(db: Database) -> WorkflowEngine:
    return WorkflowEngine(
        db,
        SimpleNamespace(),
        StubContext(),
        SimpleNamespace(),
        SimpleNamespace(),
    )


def _survey_project(db: Database) -> str:
    project_id = add_project(db, "survey")
    db.execute(
        "UPDATE projects SET config_json=? WHERE id=?",
        (
            json.dumps({"require_public_research": False, "document_type": "SURVEY_REPORT"}),
            project_id,
        ),
    )
    return project_id


def _add_background_artifact(db: Database, project_id: str, workflow_id: str) -> str:
    artifact_id = new_id("artifact")
    now = utc_now()
    content = {
        "schema_version": "1.0",
        "project_id": project_id,
        "workflow_id": workflow_id,
        "topic_id": "topic-abc123",
        "topic": "某公开系统调研",
        "completion_semantics": "COMPLETED_WITH_BACKGROUND_GAPS",
        "background_cards": [
            {
                "card_id": "bgcard-001",
                "claim_id": "claim-001",
                "dimension": "OBJECT_AND_EVOLUTION",
                "claim_text": "该对象于2024年首次公开。",
                "source_ids": ["src-1"],
                "evidence_mode": "FULL_TEXT",
                "scope_qualifiers": [],
                "target_section_profiles": [],
                "conflicts": [],
                "limitations": [],
            },
            {
                "card_id": "bgcard-002",
                "dimension": "EVALUATION_AND_EFFECT",
                "claim_text": "公开报道显示响应时间缩短。",
            },
        ],
        "background_gaps": [
            {
                "gap_id": "background-gap-001",
                "scope": "DIMENSION",
                "dimension": "LIMITATIONS_AND_GAPS",
                "description": "局限与缺口维度没有通过校验的证据卡。",
            },
            {
                "gap_id": "gap-query-1",
                "scope": "QUERY",
                "query_id": "q-1",
                "query": "示例查询",
                "description": "查询缺少权威来源。",
            },
        ],
        "source_catalog": [
            {
                "source_id": "src-1",
                "title": "官方首报",
                "url": "https://example.gov/1",
                "source_type": "GOVERNMENT",
                "published_at": "2024-06-01",
                "excerpt": "不应进入模型输入的长摘要",
            },
            {
                "source_id": "src-unreferenced",
                "title": "未被任何卡片引用的来源",
                "url": "https://example.org/2",
            },
        ],
    }
    db.execute(
        """INSERT INTO artifacts(id,project_id,workflow_id,artifact_type,prompt_id,version,status,security_level,context_hash,content_json,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            artifact_id,
            project_id,
            workflow_id,
            "TOPIC_BACKGROUND_RESULT",
            "P-ONLINE-RESULT-IMPORT-CRITIC",
            1,
            "SUFFICIENT",
            "INTERNAL",
            "a" * 64,
            json.dumps(content, ensure_ascii=False),
            now,
        ),
    )
    return artifact_id


def _completed_wf3b(db: Database, project_id: str) -> str:
    workflow_id = add_workflow(db, project_id, WF3B_WORKFLOW_TYPE)
    artifact_id = _add_background_artifact(db, project_id, workflow_id)
    state = {
        "workflow_type": WF3B_WORKFLOW_TYPE,
        "options": {
            "topic": "某公开系统调研",
            "topic_id": "topic-abc123",
            "survey_research_brief": {
                "must_answer_questions": ["该系统是什么"],
                "evidence_requirements": ["数字必须绑定官方来源"],
                "deliverable_notes": ["背景调研为主体"],
                "scope_exclusions": [],
            },
        },
        "step_results": {},
        "prerequisite_workflow_ids": {},
        "wf3b_background_result_artifact_id": artifact_id,
    }
    db.execute(
        "UPDATE workflows SET state_json=? WHERE id=?",
        (json.dumps(state, ensure_ascii=False), workflow_id),
    )
    return workflow_id


def test_survey_report_wf4_requires_wf1_and_wf3b(tmp_path):
    db = Database(tmp_path / "state.db")
    project_id = _survey_project(db)
    engine = _engine(db)
    required = engine._required_workflow_types(project_id, WF4, {})
    assert required == ["WF-1_PROJECT_INTAKE", WF3B_WORKFLOW_TYPE]


def test_proposal_mode_wf4_prerequisites_unchanged(tmp_path):
    db = Database(tmp_path / "state.db")
    project_id = add_project(db)
    engine = _engine(db)
    assert engine._required_workflow_types(project_id, WF4, {}) == [
        "WF-1_PROJECT_INTAKE",
        "WF-2_TEMPLATE_EXTRACTION",
    ]
    with_research = engine._required_workflow_types(project_id, WF4, {"require_public_research": True})
    assert with_research == [
        "WF-1_PROJECT_INTAKE",
        "WF-2_TEMPLATE_EXTRACTION",
        "WF-3_HYBRID_ONLINE_ASSIST",
    ]


def test_report_branch_steps_frozen_at_start(tmp_path):
    db = Database(tmp_path / "state.db")
    project_id = _survey_project(db)
    add_workflow(db, project_id, "WF-1_PROJECT_INTAKE")
    _completed_wf3b(db, project_id)
    engine = _engine(db)
    wf = engine.start(project_id, WF4)
    state = wf["state"]
    assert state["frozen_steps"] == WF4_REPORT_BRANCH_STEPS
    assert state["report_branch"] == "SURVEY_REPORT"
    assert wf["steps"] == WF4_REPORT_BRANCH_STEPS
    assert wf["status"] == "RUNNING"


def test_proposal_mode_wf4_has_no_frozen_steps(tmp_path):
    db = Database(tmp_path / "state.db")
    project_id = add_project(db)
    add_workflow(db, project_id, "WF-1_PROJECT_INTAKE")
    add_workflow(db, project_id, "WF-2_TEMPLATE_EXTRACTION")
    engine = _engine(db)
    wf = engine.start(project_id, WF4)
    assert "frozen_steps" not in wf["state"]
    assert wf["steps"] == WORKFLOWS[WF4]


def test_report_branch_waits_for_missing_wf3b(tmp_path):
    db = Database(tmp_path / "state.db")
    project_id = _survey_project(db)
    add_workflow(db, project_id, "WF-1_PROJECT_INTAKE")
    engine = _engine(db)
    wf = engine.start(project_id, WF4)
    assert wf["status"] == "WAITING_PREREQUISITE"
    assert WF3B_WORKFLOW_TYPE in str(wf["state"].get("last_error") or "")
    # Steps are frozen at start even while waiting, so a late prerequisite
    # recovery can never fall back to the proposal step list.
    assert wf["state"]["frozen_steps"] == WF4_REPORT_BRANCH_STEPS


def test_explicit_prerequisite_binding_accepts_wf3b(tmp_path):
    db = Database(tmp_path / "state.db")
    project_id = _survey_project(db)
    wf1_id = add_workflow(db, project_id, "WF-1_PROJECT_INTAKE")
    wf3b_id = _completed_wf3b(db, project_id)
    engine = _engine(db)
    wf = engine.start(
        project_id,
        WF4,
        prerequisite_workflow_ids={
            "WF-1_PROJECT_INTAKE": wf1_id,
            WF3B_WORKFLOW_TYPE: wf3b_id,
        },
    )
    assert wf["status"] == "RUNNING"
    assert wf["state"]["prerequisite_workflow_ids"][WF3B_WORKFLOW_TYPE] == wf3b_id


def test_steps_for_falls_back_to_static_definition():
    wf = {"workflow_type": WF4, "state": {}}
    assert WorkflowEngine._steps_for(wf) == WORKFLOWS[WF4]
    frozen = [{"prompt_id": "P-REPORT-OUTLINE"}]
    wf_frozen = {"workflow_type": WF4, "state": {"frozen_steps": frozen}}
    assert WorkflowEngine._steps_for(wf_frozen) == frozen
    other = {"workflow_type": "WF-2_TEMPLATE_EXTRACTION", "state": {"frozen_steps": frozen}}
    assert WorkflowEngine._steps_for(other) == WORKFLOWS["WF-2_TEMPLATE_EXTRACTION"]


def _outline_context(db: Database, project_id: str, wf3b_id: str, prompt_id: str) -> dict:
    builder = ContextBuilder(db, PromptPack(ROOT / "prompt_pack"))
    state = {
        "workflow_type": WF4,
        "options": {},
        "step_results": {},
        "prerequisite_workflow_ids": {
            "WF-1_PROJECT_INTAKE": "wf-1-placeholder",
            WF3B_WORKFLOW_TYPE: wf3b_id,
        },
    }
    return builder.build(prompt_id, project_id, workflow_id="wf-report-1", workflow_state=state)


def test_outline_payload_binds_real_wf3b_cards_and_gaps(tmp_path):
    db = Database(tmp_path / "state.db")
    project_id = _survey_project(db)
    wf3b_id = _completed_wf3b(db, project_id)
    envelope = _outline_context(db, project_id, wf3b_id, "P-REPORT-OUTLINE")
    payload = envelope["payload"]
    assert payload["task_type"] == "REPORT_OUTLINE"
    assert payload["document_type"] == "SURVEY_REPORT"
    assert payload["topic"] == {"topic_id": "topic-abc123", "topic_description": "某公开系统调研"}
    assert payload["research_dimension_mode"] == "SURVEY_TECHNICAL"
    assert payload["report_title_hint"] == "survey"
    cards = payload["background_cards"]
    assert [card["card_id"] for card in cards] == ["bgcard-001", "bgcard-002"]
    # Runtime-only card fields must not leak into the model contract.
    assert all("claim_id" not in card for card in cards)
    assert all("conflicts" not in card for card in cards)
    assert all("target_section_profiles" not in card for card in cards)
    gaps = payload["background_gaps"]
    assert [gap["gap_id"] for gap in gaps] == ["background-gap-001", "gap-query-1"]
    assert all(set(gap) <= {"gap_id", "scope", "dimension", "description"} for gap in gaps)
    brief = payload["survey_research_brief"]
    assert brief["must_answer_questions"] == ["该系统是什么"]
    sources = payload["source_catalog"]
    # Only card-referenced sources are exposed, projected to the whitelist.
    assert [source["source_id"] for source in sources] == ["src-1"]
    assert set(sources[0]) <= {"source_id", "title", "url", "source_type", "published_at"}
    assert "excerpt" not in sources[0]


def test_outline_payload_requires_bound_wf3b_artifact(tmp_path):
    db = Database(tmp_path / "state.db")
    project_id = _survey_project(db)
    builder = ContextBuilder(db, PromptPack(ROOT / "prompt_pack"))
    state = {
        "workflow_type": WF4,
        "options": {},
        "step_results": {},
        "prerequisite_workflow_ids": {},
    }
    with pytest.raises(ValueError, match="WF-3B"):
        builder.build("P-REPORT-OUTLINE", project_id, workflow_id="wf-report-1", workflow_state=state)
