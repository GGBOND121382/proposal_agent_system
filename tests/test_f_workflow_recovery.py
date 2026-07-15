from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.config import Settings
from app.context import ContextBuilder
from app.db import Database
from app.documents import parse_document
from app.executor import PromptExecutor
from app.exporter import DocxExporter
from app.llm import ModelGateway
from app.pack import PromptPack
from app.research import PublicResearchService
from app.security import SecurityRouter
from app.util import utc_now
from app.workflows import WorkflowEngine

ROOT = Path(__file__).resolve().parents[1]


def build_runtime(data_dir: Path, monkeypatch, *, quality_guard: bool = True):
    monkeypatch.setenv("MODEL_RUNTIME_MODE", "SIMULATED")
    monkeypatch.setenv("APP_DATA_DIR", str(data_dir))
    monkeypatch.setenv("PROMPT_PACK_DIR", str(ROOT / "prompt_pack"))
    settings = Settings.load()
    pack = PromptPack(settings.prompt_pack_dir)
    db = Database(settings.db_path)
    executor = PromptExecutor(
        db,
        pack,
        SecurityRouter(pack),
        ModelGateway(settings, pack),
        quality_guard_enabled=quality_guard,
    )
    engine = WorkflowEngine(db, pack, ContextBuilder(db, pack), executor, PublicResearchService(settings))
    return settings, pack, db, executor, engine, DocxExporter(db, settings)


def seed_project(settings: Settings, db: Database, *, section_titles: list[str]) -> str:
    project_id = "project-f-track-fixed"
    now = utc_now()
    config = {
        "internet_access_allowed": True,
        "anonymized_external_processing_allowed": True,
        "allowed_public_topics": ["公开政策"],
        "prohibited_external_fields": ["真实项目名称"],
        "recipient_scope": ["内部用户"],
        "allowed_model_endpoint_ids": ["offline-primary", "online-public-primary"],
        "retention_days": 365,
        "task_instruction": None,
    }
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (project_id, "F轨道测试项目", "", "INTERNAL", json.dumps(config, ensure_ascii=False), now, now),
    )
    materials = [
        ("guide.md", "APPLICATION_GUIDE", "# 申报指南\n突出研究问题、方法、创新、验证和研究基础。"),
        ("brief.md", "PROJECT_BRIEF", "# 项目任务\n研究动态运输优化中的约束映射与低扰动增量重规划。"),
        ("reference.md", "REFERENCE_PROPOSAL", "# 立项依据\n从代表工作能力边界推出差距。\n# 研究方案\n问题绑定方法、基线和实验。"),
        ("evidence.md", "EVIDENCE_MATERIAL", "# 前期成果\n已完成组合优化原型和动态调度实验代码，形成可复现实验记录。"),
    ]
    draft = "\n".join(f"# {title}\n待编写。" for title in section_titles)
    materials.append(("draft.md", "CURRENT_PROPOSAL", draft))
    for index, (filename, role, text) in enumerate(materials):
        raw = text.encode("utf-8")
        parsed = parse_document(filename, raw, role, "INTERNAL")
        parsed["document_id"] = f"document-f-{index:02d}"
        path = settings.uploads_dir / filename
        path.write_bytes(raw)
        db.execute(
            "INSERT INTO documents(id,project_id,filename,role,security_level,document_hash,file_path,parsed_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                parsed["document_id"], project_id, filename, role, "INTERNAL",
                parsed["document_hash"], str(path), json.dumps(parsed, ensure_ascii=False), now,
            ),
        )
    return project_id


def counts(db: Database, workflow_id: str) -> dict[str, int]:
    result = {}
    for table in ("prompt_runs", "artifacts", "gates", "audit_events"):
        if table == "audit_events":
            row = db.fetchone("SELECT COUNT(*) AS n FROM audit_events")
        else:
            row = db.fetchone(f"SELECT COUNT(*) AS n FROM {table} WHERE workflow_id=?", (workflow_id,))
        result[table] = int(row["n"])
    return result


def signature(db: Database, workflow_id: str) -> dict:
    workflow = db.fetchone("SELECT status,current_step FROM workflows WHERE id=?", (workflow_id,))
    runs = db.fetchall(
        "SELECT prompt_id,status FROM prompt_runs WHERE workflow_id=? ORDER BY created_at,rowid",
        (workflow_id,),
    )
    artifacts = db.fetchall(
        "SELECT artifact_type,prompt_id,status FROM artifacts WHERE workflow_id=? ORDER BY created_at,rowid",
        (workflow_id,),
    )
    open_gates = db.fetchone(
        "SELECT COUNT(*) AS n FROM gates WHERE workflow_id=? AND status='OPEN'", (workflow_id,)
    )["n"]
    return {
        "workflow": workflow,
        "runs": runs,
        "artifacts": artifacts,
        "counts": counts(db, workflow_id),
        "open_gates": int(open_gates),
    }


async def finish(engine: WorkflowEngine, workflow_id: str, *, restart=None):
    for _ in range(600):
        workflow = await engine.advance(workflow_id)
        if restart:
            engine = restart()
            workflow = engine.get(workflow_id)
        if workflow["status"] == "WAITING_GATE":
            before = counts(engine.db, workflow_id)
            duplicate = await engine.advance(workflow_id)
            assert duplicate["status"] == "WAITING_GATE"
            assert counts(engine.db, workflow_id) == before
            if restart:
                engine = restart()
            gate = next(g for g in engine.list_gates(workflow_id=workflow_id) if g["status"] == "OPEN")
            action = "APPROVE" if "APPROVE" in gate["allowed_actions"] else "CONFIRM"
            engine.decide_gate(
                gate["id"], action=action, decided_by="pytest", decided_role=gate["required_role"]
            )
            if restart:
                engine = restart()
            continue
        if workflow["status"] in {"COMPLETED", "BLOCKED", "CANCELLED"}:
            return workflow, engine
    raise AssertionError("workflow did not terminate")


def run_wf1(data_dir: Path, monkeypatch, *, restart_each_checkpoint: bool) -> dict:
    settings, _, db, _, engine, _ = build_runtime(data_dir, monkeypatch)
    project_id = seed_project(settings, db, section_titles=["研究内容"])
    workflow = engine.start(project_id, "WF-1_PROJECT_INTAKE")

    def restart():
        return build_runtime(data_dir, monkeypatch)[4]

    completed, final_engine = asyncio.run(
        finish(engine, workflow["id"], restart=restart if restart_each_checkpoint else None)
    )
    assert completed["status"] == "COMPLETED", completed["state"].get("last_error")
    return signature(final_engine.db, workflow["id"])


def test_workflow_recovers_from_every_persisted_checkpoint(tmp_path: Path, monkeypatch):
    uninterrupted = run_wf1(tmp_path / "reference", monkeypatch, restart_each_checkpoint=False)
    restarted = run_wf1(tmp_path / "restarted", monkeypatch, restart_each_checkpoint=True)
    assert restarted == uninterrupted


async def run_authoring_chain(
    engine: WorkflowEngine,
    project_id: str,
    *,
    target_section_titles: list[str] | None = None,
    single_section_complete_chain: bool = False,
):
    for workflow_type in (
        "WF-1_PROJECT_INTAKE",
        "WF-2_TEMPLATE_EXTRACTION",
        "WF-4_PROPOSAL_AUTHORING",
        "WF-5_SECURITY_REVIEW_AND_EXPORT",
    ):
        options = None
        if workflow_type == "WF-4_PROPOSAL_AUTHORING" and target_section_titles:
            options = {"target_section_titles": target_section_titles}
            if single_section_complete_chain:
                options["single_section_complete_chain"] = True
        workflow = engine.start(project_id, workflow_type, options=options)
        workflow, engine = await finish(engine, workflow["id"])
        assert workflow["status"] == "COMPLETED", workflow["state"].get("last_error")


def assert_exported_document(exporter: DocxExporter, project_id: str):
    document = exporter.export(project_id)
    assert document.is_file()
    assert document.stat().st_size > 5_000


def test_small_single_section_authoring_export_chain(tmp_path: Path, monkeypatch):
    # A single-section E2E still needs a valid surrounding proposal architecture.
    # The WF-4 target option narrows generation to one section without pretending
    # that a one-heading document is a complete research proposal.
    data_dir = tmp_path / "single-section"
    settings, _, db, _, engine, exporter = build_runtime(data_dir, monkeypatch)
    project_id = seed_project(
        settings,
        db,
        section_titles=["选题背景", "研究内容", "技术路线"],
    )
    asyncio.run(
        run_authoring_chain(
            engine,
            project_id,
            target_section_titles=["研究内容"],
            single_section_complete_chain=True,
        )
    )
    authoring = db.fetchone(
        "SELECT id,state_json FROM workflows WHERE project_id=? AND workflow_type='WF-4_PROPOSAL_AUTHORING' ORDER BY created_at DESC LIMIT 1",
        (project_id,),
    )
    state = json.loads(authoring["state_json"])
    assert len(state["section_results"]) == 1
    assert state["section_results"][0]["title"] == "研究内容"
    runs = db.fetchall(
        "SELECT prompt_id,input_json,status FROM prompt_runs WHERE workflow_id=? AND prompt_id IN "
        "('P-WRITE-BLUEPRINT','P-WRITE-BLUEPRINT-CRITIC','P-WRITE-CONTENT','P-WRITE-CRITIC','P-EXPRESSION-POLISH','P-EXPRESSION-CRITIC','P-TARGETED-REPAIR') "
        "ORDER BY created_at,rowid",
        (authoring["id"],),
    )
    assert [row["prompt_id"] for row in runs] == [
        "P-WRITE-BLUEPRINT",
        "P-WRITE-BLUEPRINT-CRITIC",
        "P-WRITE-CONTENT",
        "P-WRITE-CRITIC",
        "P-EXPRESSION-POLISH",
        "P-EXPRESSION-CRITIC",
    ]
    assert all(row["status"] == "PASS" for row in runs)
    assert {json.loads(row["input_json"])["payload"]["source_section"]["title"] for row in runs} == {"研究内容"}
    assert len(exporter._candidate_runs(project_id)) == 1
    assert_exported_document(exporter, project_id)


def test_small_three_section_authoring_export_chain(tmp_path: Path, monkeypatch):
    data_dir = tmp_path / "three-sections"
    settings, _, db, _, engine, exporter = build_runtime(data_dir, monkeypatch)
    project_id = seed_project(
        settings,
        db,
        section_titles=["选题背景", "研究内容", "技术路线"],
    )
    asyncio.run(run_authoring_chain(engine, project_id))
    assert_exported_document(exporter, project_id)
