#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.db import Database
from app.dependency_preflight import DependencyIssue, RuntimeDependencyPreflight
from app.pack import PromptPack
from app.skill_setup import build_skill_executor


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--probe", action="store_true", help="Probe configured model/search endpoints")
    parser.add_argument("--render-mermaid", action="store_true", help="Render a local Mermaid smoke diagram")
    args = parser.parse_args()
    load_env(args.env_file)
    settings = Settings.load()
    pack = PromptPack(settings.prompt_pack_dir)
    db = Database(settings.db_path)
    preflight = RuntimeDependencyPreflight(settings, pack, db)
    dependency_report = preflight.probe() if args.probe else preflight.application_report(require_export=False)
    if args.render_mermaid:
        skills = build_skill_executor(db, settings)
        project_id = "config-check"
        if not db.fetchone("SELECT id FROM projects WHERE id=?", (project_id,)):
            from app.util import utc_now
            db.execute(
                "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (project_id, "配置检查", "", "INTERNAL", "{}", utc_now(), utc_now()),
            )
        try:
            result = skills.execute(
                "mermaid.render",
                {
                    "section_id": "smoke",
                    "caption": "Mermaid配置检查",
                    "width_cm": 12,
                    "mermaid_source": "flowchart LR\nA[输入] --> B[渲染] --> C[输出]",
                },
                project_id=project_id,
                workflow_id=None,
                security_level="INTERNAL",
            )
            dependency_report.checks.append({"name": "MERMAID_RENDER", "status": "PASS", "png": result.output["png_path"], "source": result.output["source_path"]})
        except Exception as exc:
            dependency_report.issues.append(
                DependencyIssue(
                    code="MERMAID_RENDER_PROBE_FAILED",
                    dependency="MERMAID",
                    message=f"Mermaid 渲染探测失败：{type(exc).__name__}: {exc}",
                    required_settings=("MERMAID_JS_PATH", "MERMAID_BROWSER_EXECUTABLE"),
                )
            )
    report = dependency_report.as_dict()
    report.update(
        {
            "runtime_mode": settings.runtime_mode,
            "data_dir": str(settings.data_dir),
            "prompt_pack_dir": str(settings.prompt_pack_dir),
            "public_search_provider": settings.public_search_provider,
        }
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if not dependency_report.blocking_issues else 1)


if __name__ == "__main__":
    main()
