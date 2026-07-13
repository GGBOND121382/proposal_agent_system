from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.main import db, exporter, workflows
from app.util import new_id, utc_now


async def main() -> None:
    project_id = new_id("project")
    now = utc_now()
    config = {
        "internet_access_allowed": True,
        "anonymized_external_processing_allowed": True,
        "allowed_public_topics": ["公开政策", "公开学术资料"],
        "prohibited_external_fields": ["真实项目名称"],
        "recipient_scope": ["内部用户"],
        "allowed_model_endpoint_ids": ["offline-primary"],
        "retention_days": 365,
        "task_instruction": None,
    }
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (project_id, "演示项目", "REPLAY 模式演示", "INTERNAL", json.dumps(config, ensure_ascii=False), now, now),
    )

    for workflow_type in [
        "WF-1_PROJECT_INTAKE",
        "WF-2_TEMPLATE_EXTRACTION",
        "WF-3_HYBRID_ONLINE_ASSIST",
        "WF-4_PROPOSAL_AUTHORING",
        "WF-5_SECURITY_REVIEW_AND_EXPORT",
    ]:
        workflow = workflows.start(project_id, workflow_type)
        for _ in range(30):
            workflow = await workflows.advance(workflow["id"])
            if workflow["status"] == "WAITING_GATE":
                gate = [g for g in workflows.list_gates(workflow_id=workflow["id"]) if g["status"] == "OPEN"][0]
                action = "APPROVE" if "APPROVE" in gate["allowed_actions"] else "CONFIRM"
                workflows.decide_gate(gate["id"], action=action, decided_by="demo-user", decided_role=gate["required_role"])
                continue
            break
        print(workflow_type, workflow["status"])

    package = exporter.export_package(project_id)
    print("Export package:", package)


if __name__ == "__main__":
    asyncio.run(main())
