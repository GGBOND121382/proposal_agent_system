from __future__ import annotations

import json
from types import SimpleNamespace

from app.db import Database
from app.staged_workflows import STAGED_WORKFLOW_TYPE, StagedWorkflowCoordinator
from app.util import utc_now


def test_file_bridged_pipeline_is_registered_in_main_workflow_store(tmp_path):
    db = Database(tmp_path / "runtime.sqlite3")
    now = utc_now()
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        ("project-1", "动态项目标题", "test", "INTERNAL", json.dumps({}), now, now),
    )
    settings = SimpleNamespace(data_dir=tmp_path / "data", root_dir=tmp_path)
    coordinator = StagedWorkflowCoordinator(db, settings)

    workflow = coordinator.start("project-1")

    stored = db.fetchone("SELECT workflow_type,status FROM workflows WHERE id=?", (workflow["id"],))
    assert stored == {"workflow_type": STAGED_WORKFLOW_TYPE, "status": "WAITING_MODEL"}
    assert workflow["state"]["current_stage"] == "stage1"
    files = coordinator.files(workflow["id"])
    assert files["requests"]
    request = json.loads(open(files["requests"][0], encoding="utf-8").read())
    assert "动态项目标题" in json.dumps(request, ensure_ascii=False)
