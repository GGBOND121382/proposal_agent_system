from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.config import Settings
from app.context import ContextBuilder
from app.db import Database
from app.documents import parse_document
from app.executor import PromptExecutor
from app.exporter import DocxExporter
from app.llm import ModelGateway
from app.pack import PromptPack
from app.research import PublicResearchService
from app.security import RoutingDenied, SecurityRouter
from app.util import new_id, utc_now
from app.workflows import WorkflowEngine


@pytest.fixture()
def runtime(tmp_path: Path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("MODEL_RUNTIME_MODE", "SIMULATED")
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PROMPT_PACK_DIR", str(root / "prompt_pack"))
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


def create_project(db: Database, *, internet: bool = True) -> str:
    project_id = new_id("project")
    now = utc_now()
    config = {
        "internet_access_allowed": internet,
        "anonymized_external_processing_allowed": internet,
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


def add_standard_materials(settings: Settings, db: Database, project_id: str, *, current_sections: list[str] | None = None) -> None:
    materials = [
        ("guide.md", "APPLICATION_GUIDE", "# 申报指南\n本项目按科研项目申请书评审，主文不超过35页，突出研究问题、方法、创新、验证和研究基础。"),
        ("brief.md", "PROJECT_BRIEF", "# 项目任务\n研究动态运输优化中的约束映射与低扰动增量重规划，原型仅作为验证载体。"),
        ("reference.md", "REFERENCE_PROPOSAL", "# 立项依据\n从代表工作能力边界推出具体差距。\n# 研究方案\n每个问题分别绑定方法、基线和实验。"),
        ("evidence.md", "EVIDENCE_MATERIAL", "# 前期成果\n团队已完成组合优化原型和动态调度实验代码，形成可复现实验记录与初步对照结果，可支撑本项目模型和实验。"),
    ]
    titles = current_sections or ["立项依据", "研究目标", "研究内容", "研究方案", "创新点", "研究基础", "参考文献"]
    draft = "# 全文\n待完善。\n" + "\n".join(f"# {title}\n待编写。" for title in titles)
    materials.append(("draft.md", "CURRENT_PROPOSAL", draft))
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
        if wf["status"] in {"COMPLETED", "BLOCKED", "CANCELLED"}:
            break
    return wf


def test_prompt_pack_and_all_normal_replays(runtime):
    _, pack, *_ = runtime
    assert len(pack.prompt_ids()) == 30
    for prompt_id in pack.prompt_ids():
        case = pack.replay_case(prompt_id, "normal")
        assert pack.validate(prompt_id, "input", case["input"]) == []
        assert pack.validate(prompt_id, "output", case["expected_output"]) == []
        inlined = pack.inlined_schema(prompt_id, "output")
        assert "$ref" not in json.dumps(inlined)


def test_document_context_builder_uses_uploaded_material(runtime):
    settings, pack, db, _, builder, *_ = runtime
    project_id = create_project(db)
    parsed = parse_document("guide.md", b"# Guide\nMust include technical route.", "APPLICATION_GUIDE", "INTERNAL")
    path = settings.uploads_dir / "guide.md"
    path.write_bytes(b"x")
    db.execute(
        "INSERT INTO documents(id,project_id,filename,role,security_level,document_hash,file_path,parsed_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (parsed["document_id"], project_id, "guide.md", "APPLICATION_GUIDE", "INTERNAL", parsed["document_hash"], str(path), json.dumps(parsed), utc_now()),
    )
    envelope = builder.build("P-SCHEME-EXTRACT", project_id)
    assert pack.validate("P-SCHEME-EXTRACT", "input", envelope) == []
    assert envelope["payload"]["guide_documents"][0]["title"] == "guide"


def test_security_router_blocks_unapproved_online(runtime):
    _, pack, _, router, *_ = runtime
    envelope = pack.replay_input("P-PUBLIC-RESEARCH-PLAN")
    envelope["security_context"]["input_max_security_level"] = "INTERNAL"
    with pytest.raises(RoutingDenied):
        router.route("P-PUBLIC-RESEARCH-PLAN", envelope)


def test_project_intake_pauses_at_expected_gate(runtime):
    _, _, db, _, _, _, engine, _ = runtime
    project_id = create_project(db)

    async def run():
        wf = engine.start(project_id, "WF-1_PROJECT_INTAKE")
        wf = await engine.advance(wf["id"])
        assert wf["status"] == "WAITING_GATE"
        assert wf["current_step"] == 4
        gates = [g for g in engine.list_gates(workflow_id=wf["id"]) if g["status"] == "OPEN"]
        assert gates[0]["gate_type"] == "SCHEME_CONFIRMATION"

    asyncio.run(run())


def test_all_workflows_and_docx_export(runtime):
    _, _, db, _, _, _, engine, exporter = runtime
    project_id = create_project(db)
    settings = runtime[0]
    add_standard_materials(settings, db, project_id)

    async def run():
        for workflow_type in [
            "WF-1_PROJECT_INTAKE",
            "WF-2_TEMPLATE_EXTRACTION",
            "WF-3_HYBRID_ONLINE_ASSIST",
            "WF-4_PROPOSAL_AUTHORING",
            "WF-5_SECURITY_REVIEW_AND_EXPORT",
        ]:
            wf = await finish_workflow(engine, project_id, workflow_type)
            assert wf["status"] == "COMPLETED", wf["state"].get("last_error")

    asyncio.run(run())
    path = exporter.export(project_id)
    assert path.exists()
    assert path.stat().st_size > 10_000
    package = exporter.export_package(project_id)
    assert package.exists()
    assert package.stat().st_size > 10_000
