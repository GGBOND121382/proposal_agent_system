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
    monkeypatch.setenv("MODEL_RUNTIME_MODE", "REPLAY")
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
        "allowed_model_endpoint_ids": ["offline-primary"],
        "retention_days": 365,
        "task_instruction": None,
    }
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (project_id, "测试项目", "", "INTERNAL", json.dumps(config, ensure_ascii=False), now, now),
    )
    return project_id


def test_prompt_pack_and_all_normal_replays(runtime):
    _, pack, *_ = runtime
    assert len(pack.prompt_ids()) == 26
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

    async def finish(workflow_type: str):
        wf = engine.start(project_id, workflow_type)
        for _ in range(30):
            wf = await engine.advance(wf["id"])
            if wf["status"] == "WAITING_GATE":
                gate = [g for g in engine.list_gates(workflow_id=wf["id"]) if g["status"] == "OPEN"][0]
                action = "APPROVE" if "APPROVE" in gate["allowed_actions"] else "CONFIRM"
                engine.decide_gate(gate["id"], action=action, decided_by="pytest", decided_role=gate["required_role"])
                continue
            break
        assert wf["status"] == "COMPLETED", wf["state"].get("last_error")

    async def run():
        for workflow_type in [
            "WF-1_PROJECT_INTAKE",
            "WF-2_TEMPLATE_EXTRACTION",
            "WF-3_HYBRID_ONLINE_ASSIST",
            "WF-4_PROPOSAL_AUTHORING",
            "WF-5_SECURITY_REVIEW_AND_EXPORT",
        ]:
            await finish(workflow_type)

    asyncio.run(run())
    path = exporter.export(project_id)
    assert path.exists()
    assert path.stat().st_size > 10_000
    package = exporter.export_package(project_id)
    assert package.exists()
    assert package.stat().st_size > 10_000
