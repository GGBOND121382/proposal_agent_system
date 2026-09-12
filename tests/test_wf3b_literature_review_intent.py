from __future__ import annotations

import asyncio
import json

from app.background_research import (
    WF3B_WORKFLOW_TYPE,
    detect_literature_review_intent,
    normalize_wf3b_options,
)
from app.simulated_llm import SimulatedLLM
from app.workflow_status import should_pause_automatic_advancement
from tests.test_v04_complex_runtime import _finish, _runtime
from app.db import Database
from app.util import new_id, utc_now

LITERATURE_FOCUS = (
    "以2021—2026年近五年科技文献为主体，学术论文和正式技术报告优先，"
    "普通网页只补充政策、标准和工程案例。目标先收集80—100条候选。"
)

MULTIMODAL_TOPIC = (
    "多式联运协同优化与智能调度技术研究进展，重点关注货运海铁公联运、"
    "网络与服务规划、跨方式衔接、动态调度、不确定性与智能优化方法"
)


def test_default_survey_has_no_literature_intent():
    options = normalize_wf3b_options(
        {"topic": "DASH 系统调研", "focus": "调研对象的公开背景与部署情况"},
        project_id="project-1",
        document_type="SURVEY_REPORT",
    )
    assert options["literature_review_intent"] is False


def test_non_survey_document_type_never_flags_intent():
    assert detect_literature_review_intent(
        "APPLICATION",
        {"focus": LITERATURE_FOCUS},
    ) is False
    assert detect_literature_review_intent(
        None,
        {"focus": LITERATURE_FOCUS},
    ) is False


def test_literature_intent_recognized_from_focus():
    options = normalize_wf3b_options(
        {"topic": MULTIMODAL_TOPIC, "focus": LITERATURE_FOCUS},
        project_id="project-1",
        document_type="SURVEY_REPORT",
    )
    assert options["literature_review_intent"] is True


def test_literature_intent_recognized_from_survey_brief():
    brief = {
        "evidence_requirements": ["近五年科技文献不少于50篇，其中外文文献不少于25篇"],
        "deliverable_notes": ["形成文献综述调研资料包"],
    }
    assert detect_literature_review_intent(
        "SURVEY_REPORT",
        {"survey_research_brief": brief},
    ) is True


def test_literature_intent_recognized_from_background_research_envelope():
    options = normalize_wf3b_options(
        {
            "background_research": {
                "topic_override": MULTIMODAL_TOPIC,
                "focus": LITERATURE_FOCUS,
            }
        },
        project_id="project-1",
        document_type="SURVEY_REPORT",
    )
    assert options["literature_review_intent"] is True
    assert options["topic"] == MULTIMODAL_TOPIC


def test_generic_brief_without_literature_terms_not_flagged():
    brief = {
        "evidence_requirements": ["每个关键事实绑定公开来源证据"],
        "deliverable_notes": ["输出结构化调研报告"],
    }
    assert detect_literature_review_intent(
        "SURVEY_REPORT",
        {"survey_research_brief": brief},
    ) is False


def _plan_envelope(*, literature_intent: bool) -> dict:
    return {
        "payload": {
            "topic": MULTIMODAL_TOPIC,
            "required_dimensions": ["TECHNOLOGY_AND_IMPLEMENTATION"],
            "literature_review_intent": literature_intent,
        }
    }


def test_simulated_plan_stays_web_background_without_intent():
    base = {"result": {}}
    output = SimulatedLLM._handle_background_research_plan(None, base, _plan_envelope(literature_intent=False))
    queries = output["result"]["queries"]
    assert queries
    assert all("公开统计" in query["query"] for query in queries)
    assert all("学术论文" not in query["query"] for query in queries)


def test_simulated_plan_academic_first_with_literature_intent():
    base = {"result": {}}
    output = SimulatedLLM._handle_background_research_plan(None, base, _plan_envelope(literature_intent=True))
    queries = output["result"]["queries"]
    assert queries
    assert all("学术论文" in query["query"] for query in queries)
    assert all("学术" in query["purpose"] for query in queries)


def _survey_project(db: Database) -> str:
    project_id = new_id("project")
    now = utc_now()
    config = {
        "internet_access_allowed": True,
        "anonymized_external_processing_allowed": True,
        "allowed_public_topics": ["multimodal freight transport", "多式联运"],
        "prohibited_external_fields": ["人员姓名"],
        "recipient_scope": ["内部测试"],
        "allowed_model_endpoint_ids": ["offline-primary", "online-public-primary"],
        "retention_days": 365,
        "task_instruction": None,
        "document_type": "SURVEY_REPORT",
    }
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (project_id, "多式联运技术综述测试", "", "INTERNAL", json.dumps(config, ensure_ascii=False), now, now),
    )
    return project_id


def test_wf3b_literature_intent_reaches_plan_payload_end_to_end(tmp_path, monkeypatch):
    settings, pack, db, builder, executor, engine = _runtime(tmp_path, monkeypatch)
    project_id = _survey_project(db)
    captured_payloads = []
    original_plan = SimulatedLLM._handle_background_research_plan

    def capturing_plan(self, base, envelope):
        captured_payloads.append(json.loads(json.dumps(envelope.get("payload") or {})))
        return original_plan(self, base, envelope)

    monkeypatch.setattr(SimulatedLLM, "_handle_background_research_plan", capturing_plan)

    async def finish():
        intake = await _finish(engine, project_id, "WF-1_PROJECT_INTAKE")
        assert intake["status"] == "COMPLETED", intake["state"].get("last_error")
        # The simulated retrieval path embeds the topic into every synthesized
        # source; transport-marker topics select the larger transport catalog
        # and would blow the deterministic provider request budget, so the e2e
        # uses a generic survey topic while the intent comes from the focus.
        wf = engine.start(
            project_id,
            WF3B_WORKFLOW_TYPE,
            {"topic": "某技术领域研究进展综述", "focus": LITERATURE_FOCUS},
        )
        assert wf["status"] == "RUNNING", wf["state"].get("last_error")
        for _ in range(500):
            wf = await engine.advance(wf["id"])
            if wf["status"] == "WAITING_GATE":
                gate = next(
                    gate
                    for gate in engine.list_gates(workflow_id=wf["id"])
                    if gate["status"] == "OPEN"
                )
                action = "APPROVE" if "APPROVE" in gate["allowed_actions"] else "CONFIRM"
                engine.decide_gate(
                    gate["id"],
                    action=action,
                    decided_by="pytest",
                    decided_role=gate["required_role"],
                )
                continue
            if should_pause_automatic_advancement(wf["status"]):
                break
        assert wf["status"] == "COMPLETED", wf["state"].get("last_error")
        return wf

    wf = asyncio.run(finish())
    assert wf["state"]["options"]["literature_review_intent"] is True
    assert captured_payloads, "plan prompt was never invoked"
    assert all(payload.get("literature_review_intent") is True for payload in captured_payloads)
