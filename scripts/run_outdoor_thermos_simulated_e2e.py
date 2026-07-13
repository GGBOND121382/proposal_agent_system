#!/usr/bin/env python3
"""Run a deterministic 12-section proposal workflow without external model APIs."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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
from app.util import new_id, utc_now, write_json
from app.workflows import WorkflowEngine

WORKFLOWS = [
    "WF-1_PROJECT_INTAKE",
    "WF-2_TEMPLATE_EXTRACTION",
    "WF-3_HYBRID_ONLINE_ASSIST",
    "WF-4_PROPOSAL_AUTHORING",
    "WF-5_SECURITY_REVIEW_AND_EXPORT",
]


def build_runtime(output_dir: Path):
    os.environ["MODEL_RUNTIME_MODE"] = "REPLAY"
    os.environ["APP_DATA_DIR"] = str(output_dir)
    os.environ["PROMPT_PACK_DIR"] = str(ROOT / "prompt_pack")
    settings = Settings.load()
    pack = PromptPack(settings.prompt_pack_dir)
    db = Database(settings.db_path)
    router = SecurityRouter(pack)
    gateway = ModelGateway(settings, pack)
    builder = ContextBuilder(db, pack)
    executor = PromptExecutor(db, pack, router, gateway)
    engine = WorkflowEngine(db, pack, builder, executor, PublicResearchService(settings))
    return settings, db, engine, DocxExporter(db, settings)


def create_project(db: Database, fixture: dict) -> str:
    project_id = new_id("project")
    now = utc_now()
    config = {
        "internet_access_allowed": True,
        "anonymized_external_processing_allowed": True,
        "allowed_public_topics": ["公开保温容器技术与试验方法"],
        "prohibited_external_fields": ["人员姓名", "组织名称", "详细地址", "联系电话", "电子邮箱"],
        "recipient_scope": ["项目组内部测试人员"],
        "allowed_model_endpoint_ids": ["offline-primary"],
        "external_redaction_entities": [
            {"value": "林晓岚", "entity_type": "PERSON", "placeholder": "[PERSON_1]", "field_label": "人员姓名"},
            {"value": "云岭户外用品研发中心", "entity_type": "ORG", "placeholder": "[ORG_1]", "field_label": "组织名称"}
        ],
        "retention_days": 365,
        "task_instruction": None
    }
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (project_id, fixture["project_name"], "确定性模拟端到端测试", "INTERNAL", json.dumps(config, ensure_ascii=False), now, now),
    )
    return project_id


def add_draft(settings: Settings, db: Database, project_id: str, fixture: dict) -> None:
    markdown = "# 全文\n用于驱动逐章编制。\n\n" + "\n\n".join(
        f"# {title}\n请根据已确认材料编写本章。" for title in fixture["sections"]
    ) + "\n"
    raw = markdown.encode("utf-8")
    parsed = parse_document("outdoor_thermos_draft.md", raw, "CURRENT_PROPOSAL", "INTERNAL")
    path = settings.uploads_dir / "outdoor_thermos_draft.md"
    path.write_bytes(raw)
    db.execute(
        "INSERT INTO documents(id,project_id,filename,role,security_level,document_hash,file_path,parsed_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (parsed["document_id"], project_id, path.name, "CURRENT_PROPOSAL", "INTERNAL", parsed["document_hash"], str(path), json.dumps(parsed, ensure_ascii=False), utc_now()),
    )


async def finish(engine: WorkflowEngine, project_id: str, workflow_type: str) -> dict:
    workflow = engine.start(project_id, workflow_type)
    for _ in range(100):
        workflow = await engine.advance(workflow["id"])
        if workflow["status"] == "WAITING_GATE":
            gate = next(item for item in engine.list_gates(workflow_id=workflow["id"]) if item["status"] == "OPEN")
            action = "APPROVE" if "APPROVE" in gate["allowed_actions"] else "CONFIRM"
            engine.decide_gate(gate["id"], action=action, decided_by="simulated-e2e", decided_role=gate["required_role"])
            continue
        break
    if workflow["status"] != "COMPLETED":
        raise RuntimeError(f"{workflow_type} failed: {workflow['state'].get('last_error')}")
    return workflow


async def run(output_dir: Path) -> dict:
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)
    fixture = json.loads((ROOT / "tests/fixtures/outdoor_thermos_application_sections.json").read_text(encoding="utf-8"))
    settings, db, engine, exporter = build_runtime(output_dir)
    project_id = create_project(db, fixture)
    add_draft(settings, db, project_id, fixture)
    results = {}
    for workflow_type in WORKFLOWS:
        workflow = await finish(engine, project_id, workflow_type)
        results[workflow_type] = workflow["status"]
    document = exporter.export(project_id)
    package = exporter.export_package(project_id, document)
    write_runs = db.fetchone(
        "SELECT COUNT(*) AS n FROM prompt_runs WHERE project_id=? AND prompt_id='P-WRITE-CONTENT' AND status='PASS'",
        (project_id,),
    )["n"]
    report = {
        "pass": results == {name: "COMPLETED" for name in WORKFLOWS}
        and write_runs == fixture["expected"]["write_content_runs"]
        and document.exists()
        and package.exists(),
        "project_id": project_id,
        "workflows": results,
        "write_content_runs": write_runs,
        "document": str(document),
        "package": str(package),
    }
    write_json(output_dir / "simulated_e2e_report.json", report)
    if not report["pass"]:
        raise RuntimeError(report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/outdoor_thermos_simulated_e2e")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.output_dir.resolve())), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
