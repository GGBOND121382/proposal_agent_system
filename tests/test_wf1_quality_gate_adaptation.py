"""WF-1 quality-gate document-kind adaptation and model-repairable QG retry routing."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.config import Settings
from app.db import Database
from app.documents import parse_document
from app.model_semantic_contracts import build_project_definition_extract_model_input
from app.pack import PromptPack
from app.proposal_quality import ProposalQualityGuard, _document_kind_hint
from app.research import PublicResearchService
from app.runtime_api import ContextBuilder, DocxExporter, ModelGateway, PromptExecutor, WorkflowEngine
from app.security import SecurityRouter
from app.util import new_id, utc_now
from app.workflow_status import should_pause_automatic_advancement


ROOT = Path(__file__).resolve().parents[1]
PACK = PromptPack(ROOT / "prompt_pack")


@pytest.fixture()
def runtime(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MODEL_RUNTIME_MODE", "SIMULATED")
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PROMPT_PACK_DIR", str(ROOT / "prompt_pack"))
    settings = Settings.load()
    pack = PromptPack(settings.prompt_pack_dir)
    db = Database(settings.db_path)
    router = SecurityRouter(pack)
    gateway = ModelGateway(settings, pack)
    builder = ContextBuilder(db, pack)
    executor = PromptExecutor(db, pack, router, gateway)
    research = PublicResearchService(settings)
    engine = WorkflowEngine(db, pack, builder, executor, research)
    exporter = DocxExporter(db, settings)
    return settings, pack, db, router, builder, executor, engine, exporter


def create_project(db: Database) -> str:
    project_id = new_id("project")
    now = utc_now()
    config = {
        "internet_access_allowed": False,
        "anonymized_external_processing_allowed": False,
        "allowed_public_topics": ["公开政策"],
        "prohibited_external_fields": ["真实项目名称"],
        "recipient_scope": ["内部用户"],
        "allowed_model_endpoint_ids": ["offline-primary", "online-public-primary"],
        "retention_days": 365,
        "task_instruction": None,
    }
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (project_id, "测试项目", "", "INTERNAL", json.dumps(config, ensure_ascii=False), now, now),
    )
    return project_id


def add_standard_materials(settings: Settings, db: Database, project_id: str) -> None:
    materials = [
        ("guide.md", "APPLICATION_GUIDE", "# 申报指南\n本项目按科研项目申请书评审，主文不超过35页，突出研究问题、方法、创新、验证和研究基础。"),
        ("brief.md", "PROJECT_BRIEF", "# 项目任务\n研究动态运输优化中的约束映射与低扰动增量重规划，原型仅作为验证载体。"),
        ("reference.md", "REFERENCE_PROPOSAL", "# 立项依据\n从代表工作能力边界推出具体差距。\n# 研究方案\n每个问题分别绑定方法、基线和实验。"),
        ("evidence.md", "EVIDENCE_MATERIAL", "# 前期成果\n团队已完成组合优化原型和动态调度实验代码，形成可复现实验记录与初步对照结果，可支撑本项目模型和实验。"),
        ("draft.md", "CURRENT_PROPOSAL", "# 全文\n待完善。\n# 立项依据\n待编写。\n# 研究目标\n待编写。"),
    ]
    for filename, role, text in materials:
        raw = text.encode("utf-8")
        parsed = parse_document(filename, raw, role, "INTERNAL")
        path = settings.uploads_dir / filename
        path.write_bytes(raw)
        db.execute(
            "INSERT INTO documents(id,project_id,filename,role,security_level,document_hash,file_path,parsed_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (parsed["document_id"], project_id, filename, role, "INTERNAL", parsed["document_hash"], str(path), json.dumps(parsed, ensure_ascii=False), utc_now()),
        )


async def finish_workflow(engine: WorkflowEngine, project_id: str, workflow_type: str, *, max_steps: int = 500):
    wf = engine.start(project_id, workflow_type)
    for _ in range(max_steps):
        wf = await engine.advance(wf["id"])
        if wf["status"] == "WAITING_GATE":
            gate = next(g for g in engine.list_gates(workflow_id=wf["id"]) if g["status"] == "OPEN")
            action = "APPROVE" if "APPROVE" in gate["allowed_actions"] else "CONFIRM"
            engine.decide_gate(gate["id"], action=action, decided_by="pytest", decided_role=gate["required_role"])
            continue
        if should_pause_automatic_advancement(wf["status"]):
            break
    return wf


def _pd_with_types(item_types: list[str]) -> dict:
    return {
        "items": [
            {
                "item_id": f"item-{index:03d}",
                "item_type": item_type,
                "content": {"summary": f"条目{index}"},
                "knowledge_status": "ESTIMATED",
                "source_refs": [],
            }
            for index, item_type in enumerate(item_types, 1)
        ],
        "relations": [],
    }


_CRITICAL_WITHOUT_EXPERIMENT_INNOVATION = [
    "GAP",
    "PROBLEM",
    "OBJECTIVE",
    "WORK_PACKAGE",
    "METHOD",
    "DELIVERABLE",
    "METRIC",
]


def test_pd_extract_prompt_lists_relation_direction_constraints() -> None:
    text = PACK.prompt_text("P-PROJECT-DEFINITION-EXTRACT")
    assert "关系方向约束" in text
    assert "DECOMPOSES_TO" in text and "OBJECTIVE" in text and "WORK_PACKAGE" in text
    assert "VALIDATED_BY" in text and "EXPERIMENT / METRIC" in text
    assert "MEASURED_BY" in text and "OCCURS_IN" in text


def test_document_kind_hint_defaults_to_application() -> None:
    assert _document_kind_hint({}) == "APPLICATION"
    assert (
        _document_kind_hint(
            {"scheme_profile": {"scheme_type": "APPLICATION_GUIDE", "research_attribute": "应用研究"}}
        )
        == "APPLICATION"
    )


def test_document_kind_hint_detects_research_report() -> None:
    assert (
        _document_kind_hint(
            {"scheme_profile": {"scheme_type": "调研分析报告（PROJECT_BRIEF）"}}
        )
        == "RESEARCH_REPORT"
    )
    # Application markers win over report markers.
    assert (
        _document_kind_hint(
            {"scheme_profile": {"scheme_type": "申报指南调研专题 APPLICATION_GUIDE"}}
        )
        == "APPLICATION"
    )
    # Confirmed human answer on research_attribute is also a signal.
    assert (
        _document_kind_hint(
            {
                "human_resolutions": [
                    {"target": "scheme_profile.research_attribute", "answer": "调研/情报研究"}
                ]
            }
        )
        == "RESEARCH_REPORT"
    )


def test_document_kind_hint_ignores_negated_application_markers() -> None:
    # Real rerun case: research_attribute "非指南类调研分析" mentions 指南 only
    # to negate it; the report markers must win.
    assert (
        _document_kind_hint(
            {
                "scheme_profile": {
                    "scheme_type": "内部调研分析报告",
                    "research_attribute": "非指南类调研分析",
                    "scheme_name": "美空军DASH系统调研分析报告",
                }
            }
        )
        == "RESEARCH_REPORT"
    )
    # A genuinely application-scoped profile still wins even with report words.
    assert (
        _document_kind_hint(
            {
                "scheme_profile": {
                    "scheme_type": "重点研发申报",
                    "research_attribute": "应用研究",
                    "scheme_name": "某专项调研申报指南",
                }
            }
        )
        == "APPLICATION"
    )


def test_research_report_kind_exempts_experiment_and_innovation() -> None:
    guard = ProposalQualityGuard()
    pd = _pd_with_types(_CRITICAL_WITHOUT_EXPERIMENT_INNOVATION)
    findings = guard._audit_project_definition(pd, document_kind="RESEARCH_REPORT")
    codes = {finding.code for finding in findings}
    assert "QG_PROJECT_GRAPH_INCOMPLETE" not in codes


def test_application_kind_still_requires_experiment_and_innovation() -> None:
    guard = ProposalQualityGuard()
    pd = _pd_with_types(_CRITICAL_WITHOUT_EXPERIMENT_INNOVATION)
    findings = guard._audit_project_definition(pd, document_kind="APPLICATION")
    incomplete = [f for f in findings if f.code == "QG_PROJECT_GRAPH_INCOMPLETE"]
    assert incomplete
    assert "EXPERIMENT" in incomplete[0].description
    assert "INNOVATION" in incomplete[0].description
    assert incomplete[0].blocking is True
    shallow = [f for f in findings if f.code == "QG_PROJECT_GRAPH_TOO_SHALLOW"]
    assert shallow and shallow[0].blocking is True


def test_research_report_kind_still_reports_but_does_not_block_on_shallow_graph() -> None:
    guard = ProposalQualityGuard()
    pd = _pd_with_types(
        [t for t in _CRITICAL_WITHOUT_EXPERIMENT_INNOVATION if t != "OBJECTIVE"]
    )
    findings = guard._audit_project_definition(pd, document_kind="RESEARCH_REPORT")
    incomplete = [f for f in findings if f.code == "QG_PROJECT_GRAPH_INCOMPLETE"]
    # Still observed (OBJECTIVE missing) but advisory: the producer's own
    # NEED_USER_INPUT gate and the semantic critic own completeness here.
    assert incomplete
    assert "OBJECTIVE" in incomplete[0].description
    assert incomplete[0].blocking is False
    shallow = [f for f in findings if f.code == "QG_PROJECT_GRAPH_TOO_SHALLOW"]
    assert shallow and shallow[0].blocking is False


def _pd_with_objective_texts(texts: list[str]) -> dict:
    items = [
        {
            "item_id": "item-basic",
            "item_type": "PROJECT_BASIC",
            "content": {"summary": "调研任务"},
            "knowledge_status": "ESTIMATED",
            "source_refs": [],
        }
    ]
    for index, text in enumerate(texts, 1):
        items.append(
            {
                "item_id": f"item-obj-{index}",
                "item_type": "OBJECTIVE",
                "content": {"statement": text},
                "knowledge_status": "ESTIMATED",
                "source_refs": [],
            }
        )
    return {"items": items, "relations": []}


def test_engineering_objective_check_matches_within_single_objective() -> None:
    guard = ProposalQualityGuard()
    # "形成" ends one objective and "原型" begins another; the joined-text
    # regex would misfire, per-objective checking must not.
    pd = _pd_with_objective_texts(
        ["梳理流程，并形成流程图。", "调研原型迭代效率相关的公开证据。"]
    )
    findings = guard._audit_project_definition(pd, document_kind="APPLICATION")
    assert not any(
        f.code == "QG_ENGINEERING_OBJECTIVE_MASQUERADES_AS_RESEARCH"
        for f in findings
    )
    # A single objective that really promises to build a system still fires.
    pd = _pd_with_objective_texts(["构建一套智能决策支持系统。"])
    findings = guard._audit_project_definition(pd, document_kind="APPLICATION")
    masquerade = [
        f
        for f in findings
        if f.code == "QG_ENGINEERING_OBJECTIVE_MASQUERADES_AS_RESEARCH"
    ]
    assert masquerade and masquerade[0].blocking is True


def test_engineering_objective_check_is_advisory_for_research_report() -> None:
    guard = ProposalQualityGuard()
    # Survey-report objectives are investigation targets ("核查/梳理"), not
    # engineering promises; even a literal match must not block intake.
    pd = _pd_with_objective_texts(["构建DASH系统组成的完整认知框架。"])
    findings = guard._audit_project_definition(pd, document_kind="RESEARCH_REPORT")
    masquerade = [
        f
        for f in findings
        if f.code == "QG_ENGINEERING_OBJECTIVE_MASQUERADES_AS_RESEARCH"
    ]
    assert masquerade and masquerade[0].blocking is False


def _qg_revise_output() -> dict:
    return {
        "result": {},
        "findings": [
            {
                "code": "QG_PROJECT_RELATION_DIRECTION_INVALID",
                "severity": "P1",
                "category": "PROJECT_DEFINITION",
                "target_type": "PROJECT_RELATION",
                "target_path_or_span": "/result/project_definition/relations/3",
                "description": "关系rel-x的DECOMPOSES_TO方向不合法：WORK_PACKAGE → OBJECTIVE。",
                "repair_instruction": "按关系语义调整方向或选择合法关系类型；不得保留反向边。",
                "blocking": True,
            },
            {
                "code": "QG_PROJECT_GRAPH_INCOMPLETE",
                "severity": "P1",
                "category": "PROJECT_DEFINITION",
                "target_type": "PROJECT_DEFINITION",
                "target_path_or_span": "items",
                "description": "非模型可修复项不应进入反馈通道。",
                "repair_instruction": "ignored",
                "blocking": True,
            },
        ],
        "user_questions": [],
    }


def _prepare_pd_extract_step(engine, project_id: str):
    asyncio.run(_finish_wf1(engine, project_id))
    workflow = engine.start(project_id, "WF-1_PROJECT_INTAKE")
    workflow = engine.get(workflow["id"])
    pd_step = next(
        index
        for index, step in enumerate(workflow["steps"])
        if step.get("prompt_id") == "P-PROJECT-DEFINITION-EXTRACT"
    )
    state = workflow["state"]
    engine._update(workflow, status="RUNNING", current_step=pd_step, state=state)
    workflow = engine.get(workflow["id"])
    state = workflow["state"]
    state.setdefault("step_results", {})[str(pd_step)] = {
        "prompt_id": "P-PROJECT-DEFINITION-EXTRACT",
        "run_id": "run-pd-extract-baseline",
        "status": "REVISE",
    }
    # Pin the regeneration budget so the EXHAUSTED assertion is deterministic.
    state.setdefault("options", {})["semantic_producer_regeneration_limit"] = 1
    engine._update(workflow, state=state)
    return engine.get(workflow["id"]), pd_step


async def _finish_wf1(engine, project_id: str):
    workflow = await finish_workflow(engine, project_id, "WF-1_PROJECT_INTAKE")
    assert workflow["status"] == "COMPLETED"


def test_revision_findings_reach_semantic_model_input() -> None:
    envelope = PACK.replay_input("P-PROJECT-DEFINITION-EXTRACT")
    assert "revision_findings" in envelope["payload"]
    assert PACK.validate("P-PROJECT-DEFINITION-EXTRACT", "input", envelope) == []
    baseline = build_project_definition_extract_model_input(envelope)
    assert "revision_issues" not in baseline

    envelope["payload"]["revision_findings"] = [
        {
            "finding_instance_id": "runtime-quality-gate-P-PROJECT-DEFINITION-EXTRACT-1-1",
            "defect_key": None,
            "code": "QG_PROJECT_RELATION_DIRECTION_INVALID",
            "severity": "P1",
            "category": "PROJECT_DEFINITION",
            "target_type": "PROJECT_RELATION",
            "target_path_or_span": "/result/project_definition/relations/3",
            "description": "关系rel-x的DECOMPOSES_TO方向不合法：WORK_PACKAGE → OBJECTIVE。",
            "evidence_refs": [],
            "repairable": False,
            "repair_instruction": "按关系语义调整方向或选择合法关系类型；不得保留反向边。",
            "suggested_route": "ORIGINAL_PRODUCER",
            "blocking": True,
        }
    ]
    assert PACK.validate("P-PROJECT-DEFINITION-EXTRACT", "input", envelope) == []
    projected = build_project_definition_extract_model_input(envelope)
    issues = projected.get("revision_issues")
    assert issues and len(issues) == 1
    assert "QG_PROJECT_RELATION_DIRECTION_INVALID" in issues[0]["problem"]
    assert issues[0]["required_action"]
    assert PACK.validate_model("P-PROJECT-DEFINITION-EXTRACT", "input", projected) == []


def test_model_repairable_quality_failure_schedules_producer_regeneration(runtime) -> None:
    settings, pack, db, _, _, _, engine, _ = runtime
    project_id = create_project(db)
    add_standard_materials(settings, db, project_id)
    workflow, pd_step = _prepare_pd_extract_step(engine, project_id)

    first = engine._prepare_semantic_producer_regeneration(
        workflow,
        workflow["state"],
        producer_prompt="P-PROJECT-DEFINITION-EXTRACT",
        output=_qg_revise_output(),
    )
    assert first == "SCHEDULED"

    scheduled = engine.get(workflow["id"])
    assert scheduled["status"] == "RUNNING"
    assert scheduled["current_step"] == pd_step
    feedback = scheduled["state"]["producer_revision_findings"][
        "P-PROJECT-DEFINITION-EXTRACT"
    ]
    # Only the model-repairable finding is routed back; GRAPH_INCOMPLETE is not.
    assert len(feedback) == 1
    assert feedback[0]["code"] == "QG_PROJECT_RELATION_DIRECTION_INVALID"
    assert feedback[0]["suggested_route"] == "ORIGINAL_PRODUCER"
    assert feedback[0]["blocking"] is True
    assert pack.validate_common("finding.schema.json", feedback[0]) == []

    next_envelope = engine.context_builder.build(
        "P-PROJECT-DEFINITION-EXTRACT",
        project_id,
        workflow_id=workflow["id"],
        workflow_state=scheduled["state"],
    )
    assert pack.validate("P-PROJECT-DEFINITION-EXTRACT", "input", next_envelope) == []
    assert next_envelope["payload"]["revision_findings"] == feedback
    projected = build_project_definition_extract_model_input(next_envelope)
    assert projected.get("revision_issues")
    assert pack.validate_model("P-PROJECT-DEFINITION-EXTRACT", "input", projected) == []

    second = engine._prepare_semantic_producer_regeneration(
        scheduled,
        scheduled["state"],
        producer_prompt="P-PROJECT-DEFINITION-EXTRACT",
        output=_qg_revise_output(),
    )
    assert second == "EXHAUSTED"
    assert engine.get(workflow["id"])["status"] == "BLOCKED_CONTENT"


def test_non_repairable_quality_failure_is_not_routed(runtime) -> None:
    settings, pack, db, _, _, _, engine, _ = runtime
    project_id = create_project(db)
    add_standard_materials(settings, db, project_id)
    workflow, _ = _prepare_pd_extract_step(engine, project_id)

    revise_output = {
        "result": {},
        "findings": [
            {
                "code": "QG_PROJECT_GRAPH_INCOMPLETE",
                "severity": "P1",
                "category": "PROJECT_DEFINITION",
                "target_type": "PROJECT_DEFINITION",
                "target_path_or_span": "items",
                "description": "缺少关键对象类型。",
                "repair_instruction": "补全对象。",
                "blocking": True,
            }
        ],
        "user_questions": [],
    }
    assert (
        engine._prepare_semantic_producer_regeneration(
            workflow,
            workflow["state"],
            producer_prompt="P-PROJECT-DEFINITION-EXTRACT",
            output=revise_output,
        )
        == "NOT_APPLICABLE"
    )
