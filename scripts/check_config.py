#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.db import Database
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


def probe_openai(name: str, base_url: str, api_key: str) -> dict:
    if not base_url:
        return {"name": name, "status": "SKIP", "reason": "base_url empty"}
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        response = httpx.get(f"{base_url.rstrip('/')}/models", headers=headers, timeout=15)
        response.raise_for_status()
        payload = response.json()
        ids = [item.get("id") for item in payload.get("data", []) if isinstance(item, dict)]
        return {"name": name, "status": "PASS", "models": ids[:20]}
    except Exception as exc:
        return {"name": name, "status": "FAIL", "reason": str(exc)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--probe", action="store_true", help="Probe configured model/search endpoints")
    parser.add_argument("--render-mermaid", action="store_true", help="Render a local Mermaid smoke diagram")
    args = parser.parse_args()
    load_env(args.env_file)
    settings = Settings.load()
    report = {
        "runtime_mode": settings.runtime_mode,
        "data_dir": str(settings.data_dir),
        "prompt_pack_dir": str(settings.prompt_pack_dir),
        "mermaid_js_exists": settings.mermaid_js_path.exists(),
        "public_search_provider": settings.public_search_provider,
        "checks": [],
    }
    if args.probe:
        report["checks"].append(probe_openai("offline", os.getenv("OFFLINE_LLM_BASE_URL", ""), os.getenv("OFFLINE_LLM_API_KEY", "")))
        if os.getenv("ONLINE_LLM_ENABLED", "false").lower() == "true":
            report["checks"].append(probe_openai("online", os.getenv("ONLINE_LLM_BASE_URL", ""), os.getenv("ONLINE_LLM_API_KEY", "")))
        if settings.public_search_provider == "searxng" and settings.public_search_base_url:
            try:
                response = httpx.get(f"{settings.public_search_base_url}/search", params={"q": "test", "format": "json"}, timeout=15)
                response.raise_for_status()
                report["checks"].append({"name": "searxng", "status": "PASS"})
            except Exception as exc:
                report["checks"].append({"name": "searxng", "status": "FAIL", "reason": str(exc)})
    if args.render_mermaid:
        db = Database(settings.db_path)
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
            report["checks"].append({"name": "mermaid", "status": "PASS", "png": result.output["png_path"], "source": result.output["source_path"]})
        except Exception as exc:
            report["checks"].append({"name": "mermaid", "status": "FAIL", "reason": str(exc)})
    report["status"] = "PASS" if all(item.get("status") != "FAIL" for item in report["checks"]) and report["mermaid_js_exists"] else "FAIL"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
