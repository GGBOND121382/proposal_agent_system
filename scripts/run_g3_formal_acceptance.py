from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.context import ContextBuilder
from app.db import Database
from app.diagram_enrichment import DiagramEnrichmentService
from app.documents import parse_document
from app.executor import PromptExecutor
from app.exporter import DocxExporter
from app.g3_acceptance import evaluate_g3, g3_preflight
from app.llm import ModelGateway
from app.pack import PromptPack
from app.post_export_acceptance import PostExportAcceptanceManager
from app.research import PublicResearchService
from app.security import SecurityRouter
from app.skill_setup import build_skill_executor
from app.track_b import TrackBAgentPromptValidator
from app.util import new_id, utc_now, write_json
from app.workflows import WorkflowEngine
from scripts.build_g3_public_materials import build as build_materials


def _source_commit() -> str:
    if os.getenv("GITHUB_SHA"):
        return str(os.environ["GITHUB_SHA"])
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else "UNAVAILABLE"


def _build_runtime():
    settings = Settings.load()
    pack = PromptPack(settings.prompt_pack_dir)
    db = Database(settings.db_path)
    router = SecurityRouter(pack)
    gateway = ModelGateway(settings, pack)
    builder = ContextBuilder(db, pack)
    executor = PromptExecutor(
        db,
        pack,
        router,
        gateway,
        quality_guard=TrackBAgentPromptValidator(pack),
        quality_guard_enabled=settings.proposal_quality_guard_enabled,
    )
    skills = build_skill_executor(db, settings)
    research = PublicResearchService(settings, skills)
    diagrams = DiagramEnrichmentService(db, pack, skills)
    engine = WorkflowEngine(db, pack, builder, executor, research, diagrams)
    exporter = DocxExporter(db, settings)
    return settings, pack, db, engine, exporter


def _create_project(db: Database) -> str:
    project_id = new_id("project")
    now = utc_now()
    config = {
        "internet_access_allowed": True,
        "anonymized_external_processing_allowed": True,
        "allowed_public_topics": [
            "动态车辆路径",
            "城市配送",
            "增量优化",
            "局部搜索",
            "计划稳定性",
            "图增强组合优化",
        ],
        "prohibited_external_fields": [],
        "recipient_scope": ["G3公开能力基准"],
        "allowed_model_endpoint_ids": ["offline-primary", "online-public-primary"],
        "retention_days": 90,
        "task_instruction": (
            "在完整公开基准材料上，以真实模型和实时学术检索完成申请书全流程；"
            "未提供的事实必须保留UNKNOWN，不得补写团队成果。"
        ),
        "require_public_research": True,
    }
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (
            project_id,
            "动态城市配送关系感知协同优化公开能力基准",
            "完整公开材料上的G3正式能力验收，不包含个人信息或非公开数据。",
            "PUBLIC",
            json.dumps(config, ensure_ascii=False),
            now,
            now,
        ),
    )
    return project_id


def _upload_materials(
    settings: Settings,
    db: Database,
    project_id: str,
    materials_dir: Path,
) -> list[dict[str, Any]]:
    manifest = json.loads((materials_dir / "material_manifest.json").read_text(encoding="utf-8"))
    records = []
    for item in manifest["files"]:
        path = materials_dir / item["filename"]
        raw = path.read_bytes()
        parsed = parse_document(path.name, raw, item["role"], "PUBLIC")
        stored_dir = settings.uploads_dir / project_id
        stored_dir.mkdir(parents=True, exist_ok=True)
        stored = stored_dir / f"{parsed['document_id']}-{path.name}"
        stored.write_bytes(raw)
        db.execute(
            "INSERT INTO documents(id,project_id,filename,role,security_level,document_hash,file_path,parsed_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                parsed["document_id"],
                project_id,
                path.name,
                item["role"],
                "PUBLIC",
                parsed["document_hash"],
                str(stored),
                json.dumps(parsed, ensure_ascii=False),
                utc_now(),
            ),
        )
        records.append(
            {
                **item,
                "document_id": parsed["document_id"],
                "document_hash": parsed["document_hash"],
            }
        )
    return records


def _decide_open_gate(engine: WorkflowEngine, workflow_id: str) -> None:
    gates = [
        item
        for item in engine.list_gates(workflow_id=workflow_id)
        if item["status"] == "OPEN"
    ]
    if len(gates) != 1:
        raise RuntimeError(f"Expected one open review checkpoint, received {len(gates)}")
    gate = gates[0]
    action = "APPROVE" if "APPROVE" in gate["allowed_actions"] else "CONFIRM"
    engine.decide_gate(
        gate["id"],
        action=action,
        decided_by="g3-public-benchmark-operator",
        decided_role=gate["required_role"],
        comment="公开基准材料的固定流程确认；不修改模型生成正文。",
        context_hash=gate["context_hash"],
    )


async def _finish(
    engine: WorkflowEngine,
    project_id: str,
    workflow_type: str,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    workflow = engine.start(
        project_id,
        workflow_type,
        {**(options or {}), "idempotency_key": f"g3-{workflow_type.lower()}"},
    )
    for _ in range(3000):
        workflow = await engine.advance(workflow["id"])
        if workflow["status"] == "WAITING_GATE":
            _decide_open_gate(engine, workflow["id"])
            continue
        if workflow["status"] in {"COMPLETED", "BLOCKED", "CANCELLED"}:
            break
    if workflow["status"] != "COMPLETED":
        raise RuntimeError(
            f"{workflow_type} did not complete: {workflow['status']} | "
            + str((workflow.get("state") or {}).get("last_error") or "")
        )
    return workflow


def _export_database_evidence(db: Database, project_id: str, output_dir: Path) -> None:
    for table in (
        "documents",
        "workflows",
        "gates",
        "prompt_runs",
        "skill_runs",
        "artifacts",
        "audit_events",
    ):
        rows = db.fetchall(
            f"SELECT * FROM {table} WHERE project_id=? ORDER BY created_at,id",
            (project_id,),
        )
        (output_dir / f"{table}.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )


async def _run(output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    settings, pack, db, engine, exporter = _build_runtime()
    preflight = g3_preflight(settings, pack)
    write_json(output_dir / "G3_PREFLIGHT.json", preflight.as_dict())
    if preflight.status != "READY":
        blocked = {
            "schema_version": "1.0",
            "gate": "G3_FORMAL_CAPABILITY_ACCEPTANCE",
            "status": "BLOCKED_CONFIGURATION",
            "generated_at": utc_now(),
            "source_commit": _source_commit(),
            "preflight": preflight.as_dict(),
        }
        write_json(output_dir / "G3_FORMAL_CAPABILITY_ACCEPTANCE.json", blocked)
        return blocked

    materials_dir = output_dir / "input_materials"
    material_manifest = build_materials(materials_dir)
    project_id = _create_project(db)
    uploaded = _upload_materials(settings, db, project_id, materials_dir)
    write_json(
        output_dir / "input_material_manifest.json",
        {**material_manifest, "uploaded_documents": uploaded},
    )

    workflows: dict[str, dict[str, Any]] = {}
    workflows["WF-1_PROJECT_INTAKE"] = await _finish(
        engine, project_id, "WF-1_PROJECT_INTAKE"
    )
    workflows["WF-2_TEMPLATE_EXTRACTION"] = await _finish(
        engine, project_id, "WF-2_TEMPLATE_EXTRACTION"
    )
    workflows["WF-3_HYBRID_ONLINE_ASSIST"] = await _finish(
        engine, project_id, "WF-3_HYBRID_ONLINE_ASSIST"
    )
    workflows["WF-4_PROPOSAL_AUTHORING"] = await _finish(
        engine,
        project_id,
        "WF-4_PROPOSAL_AUTHORING",
        {
            "full_proposal_concurrent": True,
            "integration_scope": "FULL_PROPOSAL_CONCURRENT",
            "require_public_research": True,
            "g3_formal_acceptance": True,
            "cross_chapter_batch_size": 3,
        },
    )
    workflows["WF-5_SECURITY_REVIEW_AND_EXPORT"] = await _finish(
        engine, project_id, "WF-5_SECURITY_REVIEW_AND_EXPORT"
    )

    manager = PostExportAcceptanceManager(db, settings, exporter)
    post_export = manager.run(
        project_id,
        workflow_id=workflows["WF-4_PROPOSAL_AUTHORING"]["id"],
        reuse_verified=False,
    )
    if post_export.get("status") != "PASS":
        raise RuntimeError(
            "Post-export acceptance did not pass: " + str(post_export.get("status"))
        )

    report = evaluate_g3(
        db=db,
        project_id=project_id,
        output_dir=output_dir,
        post_export=post_export,
        source_commit=_source_commit(),
    )
    _export_database_evidence(db, project_id, output_dir)
    shutil.copy2(settings.db_path, output_dir / "workflow_checkpoint.sqlite")
    environment = {
        "schema_version": "1.0",
        "generated_at": utc_now(),
        "source_commit": _source_commit(),
        "python": sys.version,
        "platform": platform.platform(),
        "runtime_mode": settings.runtime_mode,
        "public_search_provider": settings.public_search_provider,
        "model_max_output_tokens": settings.model_max_output_tokens,
        "credentials_recorded": False,
        "preflight": preflight.as_dict(),
    }
    write_json(output_dir / "environment_manifest.json", environment)
    (output_dir / "source_commit.txt").write_text(
        environment["source_commit"] + "\n", encoding="utf-8"
    )
    (output_dir / "acceptance_report.md").write_text(
        "# G3 正式能力验收\n\n"
        f"- 状态：**{report['status']}**\n"
        f"- 真实模型调用：{report['metrics']['prompt_run_count']}\n"
        f"- 实时公开来源：{report['metrics']['research_source_count']}\n"
        f"- 完整章节：{report['metrics']['section_count']}\n"
        f"- 每三章审查批次：{report['metrics']['cross_chapter_batch_count']}\n"
        f"- PDF页面证据：{report['metrics']['page_count']}\n"
        f"- 开放质量问题：{report['metrics']['open_quality_finding_count']}\n",
        encoding="utf-8",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the G3 real-model, live-research capability acceptance."
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(_run(args.output_dir.resolve()))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report.get("status") == "PASS":
        return 0
    if report.get("status") == "BLOCKED_CONFIGURATION":
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
