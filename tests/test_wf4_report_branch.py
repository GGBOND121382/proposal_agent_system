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
import asyncio
from types import SimpleNamespace

from app.context_base import (
    REPORT_CONTENT_CRITIC_PROMPT,
    REPORT_SECTION_WRITE_PROMPT,
)
from app.executor import PromptExecutionError


def _outline_artifact(db: Database, project_id: str, workflow_id: str, sections: list[dict]) -> str:
    artifact_id = new_id("artifact")
    content = {
        "status": "PASS",
        "result": {
            "report_title": "某公开系统调研报告",
            "audience": "技术管理人员",
            "report_sections": sections,
            "overall_gaps": ["局限维度缺少权威公开证据。"],
        },
    }
    db.execute(
        """INSERT INTO artifacts(id,project_id,workflow_id,artifact_type,prompt_id,version,status,security_level,context_hash,content_json,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            artifact_id,
            project_id,
            workflow_id,
            "PROMPT_OUTPUT",
            "P-REPORT-OUTLINE",
            1,
            "PASS",
            "INTERNAL",
            "a" * 64,
            json.dumps(content, ensure_ascii=False),
            utc_now(),
        ),
    )
    return artifact_id


def _sample_sections() -> list[dict]:
    return [
        {
            "section_key": "object-and-evolution",
            "title": "对象与演进",
            "goal": "陈述对象演进事实",
            "must_answer_questions": ["该系统是什么"],
            "evidence_card_ids": ["bgcard-001"],
            "known_gaps": [],
            "estimated_share_percent": 40,
        },
        {
            "section_key": "references",
            "title": "参考资料与证据对照表",
            "goal": "列出引用对照",
            "must_answer_questions": [],
            "evidence_card_ids": [],
            "known_gaps": [],
            "estimated_share_percent": 10,
        },
        {
            "section_key": "conclusion",
            "title": "结论",
            "goal": "总结可证实结论",
            "must_answer_questions": ["哪些结论有公开证据"],
            "evidence_card_ids": ["bgcard-002"],
            "known_gaps": ["局限维度缺少证据。"],
            "estimated_share_percent": 20,
        },
    ]


class _FakeReportExecutor:
    def __init__(self, exports_dir: Path, fail_keys: set[str] | None = None):
        self.gateway = SimpleNamespace(
            settings=SimpleNamespace(exports_dir=exports_dir)
        )
        self.fail_keys = set(fail_keys or set())
        self.calls: list[str] = []

    async def execute(self, prompt_id, envelope, **kwargs):
        section = (envelope.get("payload") or {}).get("section") or {}
        section_key = str(section.get("section_key") or "")
        self.calls.append(section_key)
        if section_key in self.fail_keys:
            raise PromptExecutionError("模拟章节写作契约失败")
        return {
            "status": "PASS",
            "run_id": new_id("run"),
            "output": {
                "status": "PASS",
                "result": {
                    "section_key": section_key,
                    "markdown_body": f"{section_key} 的正文内容 [bgcard-001]",
                    "cited_card_ids": ["bgcard-001"],
                    "unresolved_questions": [],
                },
            },
            "route": {"environment": "OFFLINE_LOCAL"},
        }


def _writing_engine(db: Database, exports_dir: Path, fail_keys: set[str] | None = None):
    pack = PromptPack(ROOT / "prompt_pack")
    builder = ContextBuilder(db, pack)
    executor = _FakeReportExecutor(exports_dir, fail_keys)
    engine = WorkflowEngine(db, pack, builder, executor, SimpleNamespace())

    async def _direct(wf, state, *, prompt_id, envelope, call_key=None):
        return await executor.execute(
            prompt_id,
            envelope,
            project_id=wf["project_id"],
            workflow_id=wf["id"],
        )

    engine._execute_prompt_with_provider_retry = _direct
    return engine


def _started_report_workflow(db: Database, project_id: str):
    add_workflow(db, project_id, "WF-1_PROJECT_INTAKE")
    _completed_wf3b(db, project_id)
    engine = _writing_engine(db, Path("unused"))
    wf = engine.start(project_id, WF4)
    assert wf["status"] == "RUNNING"
    _outline_artifact(db, project_id, wf["id"], _sample_sections())
    return wf


def test_report_write_sections_persists_progress_and_skips_references(tmp_path):
    db = Database(tmp_path / "state.db")
    project_id = _survey_project(db)
    wf = _started_report_workflow(db, project_id)
    engine = _writing_engine(db, tmp_path / "exports")
    state = wf["state"]
    step_before = wf["current_step"]

    result = asyncio.run(engine._write_report_sections(wf, state))

    assert result is None
    progress = state["report_section_progress"]
    assert progress["object-and-evolution"]["status"] == "COMPLETED"
    assert progress["conclusion"]["status"] == "COMPLETED"
    assert "references" not in progress
    assert wf["current_step"] == step_before + 1  # -> P-REPORT-CONTENT-CRITIC
    rows = db.fetchall(
        "SELECT content_json FROM artifacts WHERE workflow_id=? AND artifact_type='REPORT_SECTION'",
        (wf["id"],),
    )
    written = {json.loads(row["content_json"])["section_key"] for row in rows}
    assert written == {"object-and-evolution", "conclusion"}


def test_report_write_sections_failure_is_per_section(tmp_path):
    db = Database(tmp_path / "state.db")
    project_id = _survey_project(db)
    wf = _started_report_workflow(db, project_id)
    engine = _writing_engine(db, tmp_path / "exports", fail_keys={"conclusion"})
    state = wf["state"]

    result = asyncio.run(engine._write_report_sections(wf, state))

    assert result is None  # one completed section is enough to continue
    progress = state["report_section_progress"]
    assert progress["object-and-evolution"]["status"] == "COMPLETED"
    assert progress["conclusion"]["status"] == "FAILED"
    assert progress["conclusion"]["attempts"] == 2
    assert "契约失败" in progress["conclusion"]["last_error"]


def test_report_assemble_generates_markdown_with_honest_status(tmp_path):
    db = Database(tmp_path / "state.db")
    project_id = _survey_project(db)
    wf = _started_report_workflow(db, project_id)
    exports = tmp_path / "exports"
    engine = _writing_engine(db, exports, fail_keys={"conclusion"})
    state = wf["state"]
    asyncio.run(engine._write_report_sections(wf, state))

    delivery = engine._assemble_report(wf, state)

    assert delivery["content_status"] == "PARTIAL"
    assert delivery["missing_sections"] == ["结论"]
    markdown_path = exports / f"report_{wf['id']}.md"
    markdown = markdown_path.read_text(encoding="utf-8")
    assert markdown.startswith("# 某公开系统调研报告")
    assert "object-and-evolution 的正文内容 [bgcard-001]" in markdown
    assert "【本章未能生成" in markdown
    assert "bgcard-001" in markdown and "官方首报" in markdown  # references table
    assert "局限维度缺少权威公开证据。" in markdown  # overall gap appendix
    assert "附录：检查状态" in markdown
    assert state["report_markdown_artifact_id"]
    row = db.fetchone(
        "SELECT artifact_type, status FROM artifacts WHERE id=?",
        (state["report_markdown_artifact_id"],),
    )
    assert row["artifact_type"] == "REPORT_MARKDOWN"


def test_section_write_payload_projects_section_context(tmp_path):
    db = Database(tmp_path / "state.db")
    project_id = _survey_project(db)
    wf3b_id = _completed_wf3b(db, project_id)
    # Add retrievable passages to the frozen WF-3B state.
    row = db.fetchone("SELECT state_json FROM workflows WHERE id=?", (wf3b_id,))
    wf3b_state = json.loads(row["state_json"])
    wf3b_state["background_search_results"] = {
        "passages": [
            {"passage_id": "p1", "source_ref": {"source_id": "src-1"}, "text": "首报原文片段。"},
            {"passage_id": "p2", "source_ref": {"source_id": "src-unreferenced"}, "text": "无关来源片段。"},
        ]
    }
    db.execute(
        "UPDATE workflows SET state_json=? WHERE id=?",
        (json.dumps(wf3b_state, ensure_ascii=False), wf3b_id),
    )
    wf = _started_report_workflow(db, project_id)
    state = wf["state"]
    state["active_report_section"] = {
        "section_key": "object-and-evolution",
        "title": "对象与演进",
        "goal": "陈述对象演进事实",
        "evidence_card_ids": ["bgcard-001"],
        "must_answer_questions": ["该系统是什么"],
        "known_gaps": [],
    }
    state["report_section_progress"] = {
        "conclusion": {"status": "COMPLETED", "title": "结论", "summary": "结论摘要。"}
    }
    state["report_revision_guidance"] = {"object-and-evolution": ["补充时间信息。"]}
    builder = ContextBuilder(db, PromptPack(ROOT / "prompt_pack"))
    envelope = builder.build(
        REPORT_SECTION_WRITE_PROMPT,
        project_id,
        workflow_id=wf["id"],
        workflow_state=state,
    )
    payload = envelope["payload"]
    assert payload["section"]["section_key"] == "object-and-evolution"
    assert [card["card_id"] for card in payload["background_cards"]] == ["bgcard-001"]
    assert [p["source_id"] for p in payload["evidence_passages"]] == ["src-1"]
    assert payload["previous_section_summaries"][0]["section_key"] == "conclusion"
    assert payload["revision_guidance"] == ["补充时间信息。"]


def test_content_critic_payload_uses_completed_drafts_only(tmp_path):
    db = Database(tmp_path / "state.db")
    project_id = _survey_project(db)
    wf = _started_report_workflow(db, project_id)
    for section_key, body in (("a-done", "已完成正文"), ("b-revising", "旧版正文")):
        db.execute(
            """INSERT INTO artifacts(id,project_id,workflow_id,artifact_type,prompt_id,version,status,security_level,context_hash,content_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                new_id("artifact"),
                project_id,
                wf["id"],
                "REPORT_SECTION",
                "P-REPORT-SECTION-WRITE",
                1,
                "PASS",
                "INTERNAL",
                "a" * 64,
                json.dumps({"section_key": section_key, "title": section_key, "markdown_body": body}, ensure_ascii=False),
                utc_now(),
            ),
        )
    state = wf["state"]
    state["report_section_progress"] = {
        "a-done": {"status": "COMPLETED"},
        "b-revising": {"status": "PENDING_REVISION"},
    }
    builder = ContextBuilder(db, PromptPack(ROOT / "prompt_pack"))
    envelope = builder.build(
        REPORT_CONTENT_CRITIC_PROMPT,
        project_id,
        workflow_id=wf["id"],
        workflow_state=state,
    )
    payload = envelope["payload"]
    assert [draft["section_key"] for draft in payload["section_drafts"]] == ["a-done"]
    assert [s["section_key"] for s in payload["outline_sections"]] == [
        "object-and-evolution",
        "references",
        "conclusion",
    ]
    assert payload["background_cards"]


def test_report_content_critic_runs_accepted_after_revision_policy(tmp_path):
    db = Database(tmp_path / "state.db")
    project_id = _survey_project(db)
    wf = _started_report_workflow(db, project_id)
    engine = _writing_engine(db, tmp_path / "exports")
    run_ids = []
    for _ in range(2):
        run_id = new_id("run")
        db.execute(
            """INSERT INTO prompt_runs(id,project_id,workflow_id,prompt_id,status,input_hash,input_json,duration_ms,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                project_id,
                wf["id"],
                "P-REPORT-CONTENT-CRITIC",
                "REVISE",
                "b" * 64,
                "{}",
                10,
                utc_now(),
            ),
        )
        run_ids.append(run_id)

    engine._accept_report_content_critic_runs(wf, wf["state"], reason="REPORT_REVISION_POLICY_CONTINUE")

    blockers = [
        {
            "finding": {"code": code},
            "lifecycle": {"opened_by": {"run_id": run_id}},
        }
        for code, run_id in zip(
            ["RC_UNSUPPORTED_CLAIM", "RC_MUST_ANSWER_MISSING"], run_ids
        )
    ]
    remaining, accepted = WorkflowEngine._unaccepted_completion_blockers(
        blockers, wf["state"]
    )
    assert remaining == []
    assert len(accepted) == 2
    # QG deterministic findings are never stage-accepted.
    qg = [
        {
            "finding": {"code": "QG_DOCUMENT_TEMPLATE_REPETITION"},
            "lifecycle": {"opened_by": {"run_id": run_ids[0]}},
        }
    ]
    remaining_qg, _ = WorkflowEngine._unaccepted_completion_blockers(qg, wf["state"])
    assert len(remaining_qg) == 1


def test_report_body_normalization():
    from app.workflow_authoring_base import WorkflowAuthoringMixin

    body = (
        "## 对象与演进\n"
        "\n"
        "首段。\n"
        "\n"
        "### 已有小节\n"
        "\n"
        "[[MERMAID]]演进关系示意|14\n"
        "flowchart LR\n"
        "A --> B\n"
        "\n"
        "收尾。\n"
    )
    normalized = WorkflowAuthoringMixin._normalize_report_body(body, "对象与演进")
    # Duplicate chapter heading removed.
    assert not normalized.startswith("## 对象与演进")
    # In-body headings demoted one level so chapters own ##.
    assert "### 已有小节" in normalized
    assert "\n## 已有小节" not in normalized
    # Mermaid placeholder becomes a captioned fenced block.
    assert "**图：演进关系示意**" in normalized
    assert "```mermaid\nflowchart LR\nA --> B\n```" in normalized
    assert "[[MERMAID]]" not in normalized
