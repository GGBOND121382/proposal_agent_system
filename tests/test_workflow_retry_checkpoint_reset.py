from __future__ import annotations

import json

import pytest

from app.db import Database
from app.util import utc_now
from scripts.reset_workflow_technical_retry import reset_checkpoint


def _seed(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    db = Database(path)
    now = utc_now()
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        ("project-1", "test", "test", "INTERNAL", "{}", now, now),
    )
    state = {
        "active_section_id": "new-abstract",
        "section_progress": {
            "new-abstract": {"phase": "BLUEPRINT_CRITIC"}
        },
        "technical_retry_attempts": {
            "5:new-abstract:BLUEPRINT_CRITIC": 2,
            "5:other-section:BLUEPRINT_CRITIC": 1,
        },
        "repair_attempts": {
            "section:new-abstract:P-WRITE-BLUEPRINT-CRITIC": 2
        },
    }
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            "wf-1",
            "project-1",
            "WF-4_PROPOSAL_AUTHORING",
            "BLOCKED",
            5,
            json.dumps(state, ensure_ascii=False),
            now,
            now,
        ),
    )
    return path, db


def test_retry_checkpoint_reset_is_dry_run_by_default(tmp_path) -> None:
    path, db = _seed(tmp_path)
    result = reset_checkpoint(
        path,
        "wf-1",
        expected_step=5,
        expected_section_id="new-abstract",
        expected_phase="BLUEPRINT_CRITIC",
    )
    assert result["eligible"] is True
    assert result["applied"] is False
    state = json.loads(db.fetchone("SELECT state_json FROM workflows WHERE id='wf-1'")["state_json"])
    assert state["technical_retry_attempts"]["5:new-abstract:BLUEPRINT_CRITIC"] == 2


def test_retry_checkpoint_reset_is_atomic_scoped_and_audited(tmp_path) -> None:
    path, db = _seed(tmp_path)
    result = reset_checkpoint(
        path,
        "wf-1",
        expected_step=5,
        expected_section_id="new-abstract",
        expected_phase="BLUEPRINT_CRITIC",
        apply=True,
    )
    assert result["applied"] is True
    row = db.fetchone("SELECT status,state_json FROM workflows WHERE id='wf-1'")
    state = json.loads(row["state_json"])
    assert row["status"] == "BLOCKED"
    assert "5:new-abstract:BLUEPRINT_CRITIC" not in state["technical_retry_attempts"]
    assert state["technical_retry_attempts"]["5:other-section:BLUEPRINT_CRITIC"] == 1
    assert state["repair_attempts"]["section:new-abstract:P-WRITE-BLUEPRINT-CRITIC"] == 2
    assert state["technical_retry_reset_history"][-1]["removed_count"] == 2
    audit = db.fetchone(
        "SELECT metadata_json FROM audit_events WHERE event_type='TECHNICAL_RETRY_CHECKPOINT_RESET' AND object_id='wf-1'"
    )
    assert json.loads(audit["metadata_json"])["retry_key"] == "5:new-abstract:BLUEPRINT_CRITIC"


def test_retry_checkpoint_reset_refuses_changed_phase(tmp_path) -> None:
    path, db = _seed(tmp_path)
    with pytest.raises(ValueError, match="phase changed"):
        reset_checkpoint(
            path,
            "wf-1",
            expected_step=5,
            expected_section_id="new-abstract",
            expected_phase="CONTENT_CRITIC",
            apply=True,
        )
    state = json.loads(db.fetchone("SELECT state_json FROM workflows WHERE id='wf-1'")["state_json"])
    assert state["technical_retry_attempts"]["5:new-abstract:BLUEPRINT_CRITIC"] == 2
