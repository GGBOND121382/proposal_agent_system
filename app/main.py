from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api_models import GateDecisionRequest, ProjectCreate, PromptExecuteRequest, WorkflowStartRequest
from .config import Settings
from .context import ContextBuilder
from .db import Database
from .diagram_enrichment import DiagramEnrichmentService
from .documents import ALLOWED_EXTENSIONS, parse_document
from .executor import PromptExecutionError, PromptExecutor
from .exporter import DocxExporter, ExportDenied
from .llm import ModelGateway
from .pack import PromptPack
from .post_export_acceptance import PostExportAcceptanceError, PostExportAcceptanceManager
from .research import PublicResearchService
from .skill_setup import build_skill_executor
from .security import SecurityRouter
from .track_b import TrackBAgentPromptValidator
from .util import new_id, safe_filename, sha256_json, utc_now
from .workflows import WORKFLOWS, WorkflowEngine

settings = Settings.load()
pack = PromptPack(settings.prompt_pack_dir)
db = Database(settings.db_path)
router = SecurityRouter(pack)
gateway = ModelGateway(settings, pack)
context_builder = ContextBuilder(db, pack)
executor = PromptExecutor(
    db,
    pack,
    router,
    gateway,
    quality_guard=TrackBAgentPromptValidator(pack),
    quality_guard_enabled=settings.proposal_quality_guard_enabled,
)
skill_executor = build_skill_executor(db, settings)
research = PublicResearchService(settings, skill_executor)
diagram_enrichment = DiagramEnrichmentService(db, pack, skill_executor)
workflows = WorkflowEngine(db, pack, context_builder, executor, research, diagram_enrichment)
exporter = DocxExporter(db, settings)
post_export_acceptance = PostExportAcceptanceManager(db, settings, exporter)

app = FastAPI(title="项目申请书智能体系统", version="0.6.0")
app.mount("/static", StaticFiles(directory=settings.root_dir / "app" / "static"), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(settings.root_dir / "app" / "static" / "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    report = json.loads((settings.prompt_pack_dir / "BUILD_REPORT.json").read_text(encoding="utf-8"))
    return {"status": "ok", "runtime_mode": settings.runtime_mode, "prompt_pack_status": report.get("status"), "prompt_count": len(pack.prompt_ids()), "database": str(settings.db_path.name)}


@app.get("/api/config/status")
def config_status() -> dict[str, Any]:
    endpoints = []
    for item in pack.endpoints["endpoints"]:
        endpoints.append({"endpoint_id": item["endpoint_id"], "environment": item["environment"], "enabled": item.get("enabled", False), "base_url_configured": bool(item.get("base_url")), "internet_access": item.get("network_policy", {}).get("internet_access", False)})
    return {"runtime_mode": settings.runtime_mode, "public_search_provider": settings.public_search_provider, "endpoints": endpoints}


@app.get("/api/prompts")
def list_prompts() -> list[dict[str, Any]]:
    return [pack.entry(pid) for pid in pack.prompt_ids()]


@app.get("/api/skills")
def list_skills() -> list[dict[str, str]]:
    return skill_executor.registry.list()


@app.get("/api/prompts/{prompt_id}/schema")
def prompt_schema(prompt_id: str) -> dict[str, Any]:
    try:
        return {"input": pack.schema(prompt_id, "input"), "output": pack.schema(prompt_id, "output"), "replay_input": pack.replay_input(prompt_id)}
    except (KeyError, FileNotFoundError) as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/projects")
def create_project(req: ProjectCreate) -> dict[str, Any]:
    project_id = new_id("project")
    now = utc_now()
    config = {
        "internet_access_allowed": req.internet_access_allowed,
        "anonymized_external_processing_allowed": req.anonymized_external_processing_allowed,
        "allowed_public_topics": req.allowed_public_topics,
        "prohibited_external_fields": req.prohibited_external_fields,
        "recipient_scope": req.recipient_scope,
        "allowed_model_endpoint_ids": ["offline-primary"] + (["online-public-primary"] if req.internet_access_allowed and req.anonymized_external_processing_allowed else []),
        "retention_days": 365,
        "task_instruction": req.task_instruction,
    }
    db.execute("INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (project_id, req.name, req.description, req.security_level, json.dumps(config, ensure_ascii=False), now, now))
    db.audit("PROJECT_CREATED", project_id=project_id, object_id=project_id, metadata={"security_level": req.security_level})
    return get_project(project_id)


@app.get("/api/projects")
def list_projects() -> list[dict[str, Any]]:
    rows = db.fetchall("SELECT * FROM projects ORDER BY created_at DESC")
    for row in rows:
        row["config"] = json.loads(row.pop("config_json"))
    return rows


@app.get("/api/projects/{project_id}")
def get_project(project_id: str) -> dict[str, Any]:
    row = db.fetchone("SELECT * FROM projects WHERE id=?", (project_id,))
    if not row:
        raise HTTPException(404, "Project not found")
    row["config"] = json.loads(row.pop("config_json"))
    row["document_count"] = db.fetchone("SELECT COUNT(*) AS n FROM documents WHERE project_id=?", (project_id,))["n"]
    return row


@app.post("/api/projects/{project_id}/documents")
async def upload_document(
    project_id: str,
    file: UploadFile = File(...),
    role: str = Form("OTHER"),
    security_level: str = Form("INTERNAL"),
) -> dict[str, Any]:
    project = db.fetchone("SELECT * FROM projects WHERE id=?", (project_id,))
    if not project:
        raise HTTPException(404, "Project not found")
    filename = safe_filename(file.filename or "upload.bin")
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type: {ext}")
    content = await file.read()
    if len(content) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"File exceeds {settings.max_upload_mb} MB")
    try:
        parsed = parse_document(filename, content, role, security_level)
    except Exception as exc:
        raise HTTPException(400, f"Document parsing failed: {exc}") from exc
    stored_dir = settings.uploads_dir / project_id
    stored_dir.mkdir(parents=True, exist_ok=True)
    stored_path = stored_dir / f"{parsed['document_id']}-{filename}"
    stored_path.write_bytes(content)
    safe_name = parsed.pop("safe_filename")
    db.execute(
        "INSERT INTO documents(id,project_id,filename,role,security_level,document_hash,file_path,parsed_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (parsed["document_id"], project_id, safe_name, role, security_level, parsed["document_hash"], str(stored_path), json.dumps(parsed, ensure_ascii=False), utc_now()),
    )
    db.audit("DOCUMENT_UPLOADED", project_id=project_id, object_id=parsed["document_id"], metadata={"filename": safe_name, "role": role, "security_level": security_level, "document_hash": parsed["document_hash"]})
    return parsed


@app.get("/api/projects/{project_id}/documents")
def list_documents(project_id: str) -> list[dict[str, Any]]:
    rows = db.fetchall("SELECT id,filename,role,security_level,document_hash,parsed_json,created_at FROM documents WHERE project_id=? ORDER BY created_at DESC", (project_id,))
    for row in rows:
        parsed = json.loads(row.pop("parsed_json"))
        row["title"] = parsed.get("title")
        row["section_count"] = len(parsed.get("sections", []))
    return rows


@app.post("/api/prompts/{prompt_id}/execute")
async def execute_prompt(prompt_id: str, req: PromptExecuteRequest) -> dict[str, Any]:
    try:
        envelope = req.input_data or context_builder.build(prompt_id, req.project_id, workflow_id=req.workflow_id, overrides=req.overrides)
        return await executor.execute(prompt_id, envelope, project_id=req.project_id, workflow_id=req.workflow_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (PromptExecutionError, ValueError) as exc:
        raise HTTPException(422, {"message": str(exc), "validation_errors": getattr(exc, "validation_errors", [])}) from exc


@app.post("/api/replay/{prompt_id}/{case_type}")
async def execute_replay(prompt_id: str, case_type: str, project_id: str = Query(...)) -> dict[str, Any]:
    try:
        case = pack.replay_case(prompt_id, case_type)
        return await executor.execute(prompt_id, case["input"], project_id=project_id)
    except Exception as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/workflows")
async def start_workflow(req: WorkflowStartRequest) -> dict[str, Any]:
    try:
        wf = workflows.start(req.project_id, req.workflow_type, req.options)
        return await workflows.advance(wf["id"]) if req.auto_advance else wf
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/workflows/{workflow_id}/advance")
async def advance_workflow(workflow_id: str) -> dict[str, Any]:
    try:
        return await workflows.advance(workflow_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/workflows")
def list_workflows(project_id: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM workflows"
    params: tuple[Any, ...] = ()
    if project_id:
        sql += " WHERE project_id=?"; params = (project_id,)
    sql += " ORDER BY created_at DESC"
    rows = db.fetchall(sql, params)
    for row in rows:
        row["state"] = json.loads(row.pop("state_json"))
    return rows


@app.get("/api/workflows/{workflow_id}")
def get_workflow(workflow_id: str) -> dict[str, Any]:
    try:
        return workflows.get(workflow_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/gates")
def list_gates(project_id: str | None = None, workflow_id: str | None = None) -> list[dict[str, Any]]:
    return workflows.list_gates(project_id, workflow_id)


@app.post("/api/gates/{gate_id}/decide")
def decide_gate(gate_id: str, req: GateDecisionRequest) -> dict[str, Any]:
    try:
        return workflows.decide_gate(gate_id, action=req.action, decided_by=req.decided_by, decided_role=req.decided_role, comment=req.comment, answers=req.answers, context_hash=req.context_hash)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/skill-runs")
def list_skill_runs(project_id: str | None = None, workflow_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    sql = "SELECT id,project_id,workflow_id,skill_id,skill_version,status,input_hash,output_hash,error,duration_ms,created_at FROM skill_runs WHERE 1=1"
    params: list[Any] = []
    if project_id:
        sql += " AND project_id=?"; params.append(project_id)
    if workflow_id:
        sql += " AND workflow_id=?"; params.append(workflow_id)
    sql += " ORDER BY created_at DESC LIMIT ?"; params.append(min(max(limit, 1), 500))
    return db.fetchall(sql, tuple(params))


@app.get("/api/projects/{project_id}/research-archives")
def list_research_archives(project_id: str) -> list[dict[str, Any]]:
    root = settings.data_dir / "research_archive" / safe_filename(project_id)
    if not root.exists():
        return []
    result = []
    for manifest in sorted(root.glob("*/manifest.json"), reverse=True):
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception:
            continue
        result.append({
            "session_id": payload.get("session_id"),
            "retrieval_mode": payload.get("retrieval_mode"),
            "provider": payload.get("provider"),
            "created_at": payload.get("created_at"),
            "source_count": payload.get("source_count"),
            "warning_count": payload.get("warning_count"),
            "manifest_path": str(manifest),
        })
    return result


@app.get("/api/projects/{project_id}/research-archives/{session_id}/download")
def download_research_archive(project_id: str, session_id: str) -> FileResponse:
    root = settings.data_dir / "research_archive" / safe_filename(project_id) / safe_filename(session_id)
    manifest = root / "manifest.json"
    if not manifest.exists():
        raise HTTPException(404, "Research archive not found")
    archive = shutil.make_archive(str(settings.exports_dir / f"{safe_filename(project_id)}-{safe_filename(session_id)}"), "zip", root)
    path = Path(archive)
    return FileResponse(path, media_type="application/zip", filename=path.name)


@app.get("/api/runs")
def list_runs(project_id: str | None = None, workflow_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    sql = "SELECT id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,input_hash,output_hash,error,duration_ms,created_at FROM prompt_runs WHERE 1=1"
    params: list[Any] = []
    if project_id:
        sql += " AND project_id=?"; params.append(project_id)
    if workflow_id:
        sql += " AND workflow_id=?"; params.append(workflow_id)
    sql += " ORDER BY created_at DESC LIMIT ?"; params.append(min(max(limit, 1), 500))
    return db.fetchall(sql, tuple(params))


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    row = db.fetchone("SELECT * FROM prompt_runs WHERE id=?", (run_id,))
    if not row:
        raise HTTPException(404, "Run not found")
    row["input"] = json.loads(row.pop("input_json"))
    row["output"] = json.loads(row.pop("output_json")) if row.get("output_json") else None
    return row


@app.get("/api/projects/{project_id}/quality-findings")
def list_quality_findings(
    project_id: str,
    workflow_id: str | None = None,
    state: str | None = None,
) -> list[dict[str, Any]]:
    if not db.fetchone("SELECT id FROM projects WHERE id=?", (project_id,)):
        raise HTTPException(404, "Project not found")
    states = {state.upper()} if state else None
    return workflows.quality_manager.list_findings(project_id, workflow_id=workflow_id, states=states)


@app.get("/api/projects/{project_id}/quality-matrix")
def get_quality_matrix(project_id: str, workflow_id: str | None = None) -> dict[str, Any]:
    if not db.fetchone("SELECT id FROM projects WHERE id=?", (project_id,)):
        raise HTTPException(404, "Project not found")
    return workflows.quality_manager.quality_matrix(project_id, workflow_id=workflow_id)


@app.post("/api/projects/{project_id}/quality/delivery-findings")
def ingest_delivery_findings(project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not db.fetchone("SELECT id FROM projects WHERE id=?", (project_id,)):
        raise HTTPException(404, "Project not found")
    validation_run_id = str(payload.get("validation_run_id") or "").strip()
    findings = payload.get("findings")
    if not validation_run_id or not isinstance(findings, list):
        raise HTTPException(422, "validation_run_id and findings[] are required")
    records = workflows.quality_manager.ingest_delivery_findings(
        project_id=project_id,
        workflow_id=payload.get("workflow_id"),
        validation_run_id=validation_run_id,
        findings=findings,
    )
    return {
        "records": records,
        "quality_matrix": workflows.quality_manager.quality_matrix(project_id),
    }


@app.post("/api/projects/{project_id}/export")
def export_project(project_id: str) -> FileResponse:
    try:
        path = exporter.export(project_id)
        return FileResponse(path, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", filename=path.name)
    except KeyError as exc:
        raise HTTPException(404, "Project not found") from exc
    except ExportDenied as exc:
        raise HTTPException(403, str(exc)) from exc


@app.post("/api/projects/{project_id}/export-package")
def export_project_package(project_id: str) -> FileResponse:
    try:
        path = exporter.export_package(project_id)
        return FileResponse(path, media_type="application/zip", filename=path.name)
    except KeyError as exc:
        raise HTTPException(404, "Project not found") from exc
    except ExportDenied as exc:
        raise HTTPException(403, str(exc)) from exc


@app.post("/api/projects/{project_id}/post-export-acceptance")
def run_post_export_acceptance(project_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    values = payload or {}
    try:
        return post_export_acceptance.run(
            project_id,
            workflow_id=values.get("workflow_id"),
            engineering_repair_id=values.get("engineering_repair_id"),
            expected_candidate_set_hash=values.get("expected_candidate_set_hash"),
            reuse_verified=bool(values.get("reuse_verified", True)),
        )
    except KeyError as exc:
        raise HTTPException(404, "Project not found") from exc
    except (ExportDenied, PostExportAcceptanceError) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/projects/{project_id}/post-export-acceptance/latest")
def latest_post_export_acceptance(project_id: str) -> dict[str, Any]:
    if not db.fetchone("SELECT id FROM projects WHERE id=?", (project_id,)):
        raise HTTPException(404, "Project not found")
    result = post_export_acceptance.latest_attempt(project_id)
    if result is None:
        raise HTTPException(404, "No post-export acceptance attempt")
    return result


@app.get("/api/workflow-types")
def workflow_types() -> dict[str, Any]:
    return WORKFLOWS
