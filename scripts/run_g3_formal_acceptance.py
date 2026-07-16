from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.context import ContextBuilder
from app.db import Database
from app.diagram_enrichment import DiagramEnrichmentService
from app.executor import PromptExecutor
from app.exporter import DocxExporter
from app.g3_acceptance import preflight_environment, validate_g3_run
from app.g3_runtime import _author_with_cross_reviews, _export_inputs_and_runs, _finish, _prepare_project
from app.util import utc_now, write_json
from app.llm import ModelGateway
from app.pack import PromptPack
from app.post_export_acceptance import PostExportAcceptanceManager
from app.research import PublicResearchService
from app.security import SecurityRouter
from app.skills.executor import SkillExecutor
from app.skills.g3_crossref import G3CrossrefResearchSkill
from app.skills.mermaid import MermaidRenderSkill
from app.skills.registry import SkillRegistry
from app.workflows import WorkflowEngine


def _source_commit() -> str:
    if os.getenv("GITHUB_SHA"):
        return str(os.environ["GITHUB_SHA"])
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else "UNAVAILABLE"


def _write_blocked(output_dir: Path, preflight: dict[str, Any], *, error: str | None = None) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "schema_version": "1.0",
        "gate": "G3",
        "status": "BLOCKED_CONFIGURATION" if preflight.get("status") != "PASS" else "FAIL",
        "source_commit": _source_commit(),
        "created_at": utc_now(),
        "preflight": preflight,
        "error": error,
    }
    write_json(output_dir / "G3_ACCEPTANCE.json", report)
    (output_dir / "G3_ACCEPTANCE.md").write_text(
        "# G3 正式能力验收\n\n"
        f"- 状态：**{report['status']}**\n"
        f"- 原因：{error or '真实模型、真实检索或操作人配置不完整。'}\n",
        encoding="utf-8",
    )
    return report


async def _run(output_dir: Path) -> dict[str, Any]:
    preflight = preflight_environment().as_dict()
    write_json(output_dir / "G3_PREFLIGHT.json", preflight)
    if preflight["status"] != "PASS":
        return _write_blocked(output_dir, preflight)

    runtime_dir = output_dir / "runtime"
    os.environ["APP_DATA_DIR"] = str(runtime_dir)
    os.environ["PROMPT_PACK_DIR"] = str(ROOT / "prompt_pack")
    os.environ["MODEL_CALL_EVIDENCE_DIR"] = str(output_dir / "model_calls")
    settings = Settings.load()
    pack = PromptPack(settings.prompt_pack_dir)
    db = Database(settings.db_path)
    router = SecurityRouter(pack)
    gateway = ModelGateway(settings, pack)
    builder = ContextBuilder(db, pack)
    executor = PromptExecutor(db, pack, router, gateway, quality_guard_enabled=True)
    registry = SkillRegistry()
    registry.register(MermaidRenderSkill(settings))
    registry.register(G3CrossrefResearchSkill(settings))
    skill_executor = SkillExecutor(db, registry, settings)
    research = PublicResearchService(settings, skill_executor)
    diagram = DiagramEnrichmentService(db, pack, skill_executor)
    engine = WorkflowEngine(db, pack, builder, executor, research, diagram)

    project_id, manifest = _prepare_project(settings, db)
    material_root = output_dir / "formal_materials"
    material_root.mkdir(parents=True, exist_ok=True)
    write_json(material_root / "material_manifest.json", manifest)

    wf1 = await _finish(engine, project_id, "WF-1_PROJECT_INTAKE")
    wf2 = await _finish(engine, project_id, "WF-2_TEMPLATE_EXTRACTION")
    wf3 = await _finish(engine, project_id, "WF-3_HYBRID_ONLINE_ASSIST")
    if any(item["status"] != "COMPLETED" for item in (wf1, wf2, wf3)):
        raise RuntimeError("G3 prerequisite workflow failed")
    wf4, cross_reviews, cross_history = await _author_with_cross_reviews(engine, builder, executor, project_id)
    wf5 = await _finish(engine, project_id, "WF-5_SECURITY_REVIEW_AND_EXPORT")
    if wf5["status"] != "COMPLETED":
        raise RuntimeError(wf5["state"].get("last_error") or wf5["status"])

    exporter = DocxExporter(db, settings)
    post_manager = PostExportAcceptanceManager(db, settings, exporter)
    post = post_manager.run(project_id, workflow_id=wf4["id"], reuse_verified=False)
    if post["status"] != "PASS":
        raise RuntimeError(f"Post-export acceptance returned {post['status']}")
    restarted = PostExportAcceptanceManager(db, settings).run(project_id, workflow_id=wf4["id"])
    post["reused_after_restart"] = bool(restarted.get("reused_after_restart"))

    workflow_ids = {
        "WF-1_PROJECT_INTAKE": wf1["id"],
        "WF-2_TEMPLATE_EXTRACTION": wf2["id"],
        "WF-3_HYBRID_ONLINE_ASSIST": wf3["id"],
        "WF-4_PROPOSAL_AUTHORING": wf4["id"],
        "WF-5_SECURITY_REVIEW_AND_EXPORT": wf5["id"],
    }
    write_json(output_dir / "cross_chapter_review_history.json", {"final": cross_reviews, "history": cross_history})
    _export_inputs_and_runs(db, project_id, output_dir)
    report = validate_g3_run(
        db=db,
        settings=settings,
        project_id=project_id,
        workflow_ids=workflow_ids,
        cross_chapter_reviews=cross_reviews,
        post_export_report=post,
        output_dir=output_dir,
        source_commit=_source_commit(),
    )
    report["preflight"] = preflight
    report["environment"] = {
        "python": sys.version,
        "platform": platform.platform(),
        "runtime_mode": settings.runtime_mode,
        "public_search_provider": settings.public_search_provider,
    }
    report["artifacts"] = {
        "database": str(settings.db_path),
        "material_manifest": str(material_root / "material_manifest.json"),
        "cross_chapter_history": str(output_dir / "cross_chapter_review_history.json"),
        "docx": post.get("document"),
        "pdf": post.get("pdf"),
        "package": post.get("package"),
    }
    write_json(output_dir / "G3_ACCEPTCEPTANCE.json", report)
    write_json(output_dir / "G3_ACCEPTANCE.json", report)
    shutil.copy2(settings.db_path, output_dir / "workflow_checkpoint.sqlite")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run G3 formal LIVE capability acceptance.")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    preflight = preflight_environment().as_dict()
    try:
        report = asyncio.run(_run(output_dir))
    except Exception as exc:
        traceback_text = traceback.format_exc()
        (output_dir / "failure.log").write_text(traceback_text, encoding="utf-8")
        report = _write_blocked(output_dir, preflight, error=str(exc))
        report["traceback_file"] = str(output_dir / "failure.log")
        write_json(output_dir / "G3_ACCEPTANCE.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
