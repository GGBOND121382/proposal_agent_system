from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.db import Database
from app.util import utc_now
from scripts import migrate_runtime_semantics_v1 as migration


def _seed(tmp_path: Path, *, security_level: str = "CONFIDENTIAL") -> Path:
    path = tmp_path / "runtime.sqlite3"
    db = Database(path)
    now = utc_now()
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        ("project-1", "test", "test", security_level, "{}", now, now),
    )
    state = {
        "human_resolutions": {
            "P-TEST": [
                {
                    "resolution_id": "human-1",
                    "gate_id": "gate-1",
                    "prompt_id": "P-TEST",
                    "question_id": "question-1",
                    "question": "confirm",
                    "target_paths": ["payload.target"],
                    "answer": "confirmed",
                    "decided_by": "pytest",
                    "decided_role": "PROJECT_OWNER",
                }
            ]
        },
        "human_input_overrides": {
            "P-TEST": {
                "payload.target": "confirmed",
                "payload.extra": "additional",
            }
        },
        "active_section_id": "section-1",
        "repair_overrides": {
            "section:section-1:P-WRITE-BLUEPRINT": {
                "blueprint_id": "BP-1",
                "paragraphs": [{"paragraph_id": "P-1", "text": "repaired"}],
            }
        },
    }
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            "workflow-1",
            "project-1",
            "WF-TEST",
            "RUNNING",
            0,
            json.dumps(state, ensure_ascii=False),
            now,
            now,
        ),
    )
    return path


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def test_migration_preflight_rejects_incompatible_database(tmp_path: Path) -> None:
    path = tmp_path / "invalid.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE workflows(id TEXT PRIMARY KEY)")
    conn.commit()
    conn.row_factory = sqlite3.Row
    with pytest.raises(ValueError, match="preflight"):
        migration.preflight(conn, path)
    conn.close()


def test_dry_run_and_apply_share_deterministic_plan_and_inherit_security(tmp_path: Path) -> None:
    path = _seed(tmp_path)
    dry_run_1 = migration.migrate(path, apply=False)
    dry_run_2 = migration.migrate(path, apply=False)

    assert dry_run_1["plan_id"] == dry_run_2["plan_id"]
    assert dry_run_1["artifact_ids"] == dry_run_2["artifact_ids"]
    assert dry_run_1["planned_new_artifacts"] == 3
    with _connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0

    applied = migration.migrate(path, apply=True)
    assert applied["plan_id"] == dry_run_1["plan_id"]
    assert applied["artifact_ids"] == dry_run_1["artifact_ids"]
    assert applied["status"] == "APPLIED"
    backup = Path(applied["backup_path"])
    assert backup.exists()

    with _connect(path) as conn:
        artifacts = conn.execute(
            "SELECT id,artifact_type,security_level,content_json FROM artifacts ORDER BY artifact_type,version"
        ).fetchall()
        assert len(artifacts) == 3
        assert {row["security_level"] for row in artifacts} == {"CONFIDENTIAL"}
        human_payloads = [
            json.loads(row["content_json"])
            for row in artifacts
            if row["artifact_type"] == "HUMAN_RESOLUTION"
        ]
        assert {payload["migration"]["source_path"] for payload in human_payloads} == {
            "human_resolutions.P-TEST[0]",
            "human_input_overrides.P-TEST.payload.extra",
        }
        assert {payload["scope_key"] for payload in human_payloads} == {
            "section:section-1:P-TEST"
        }
        assert {payload["migration"]["schema_version"] for payload in human_payloads} == {
            "2.1.0"
        }
        repair_row = next(
            row for row in artifacts if row["artifact_type"] == "REPAIR_APPLICATION"
        )
        repair_payload = json.loads(repair_row["content_json"])
        assert repair_payload["target_key"] == "section:section-1:P-WRITE-BLUEPRINT"
        assert repair_payload["application_status"] == "APPLIED"
        state = json.loads(
            conn.execute(
                "SELECT state_json FROM workflows WHERE id='workflow-1'"
            ).fetchone()[0]
        )
        assert "human_resolutions" not in state
        assert "human_input_overrides" not in state
        assert len(
            state["human_resolution_artifact_ids"]["section:section-1:P-TEST"]
        ) == 2
        assert len(
            state["repair_application_artifact_ids"][
                "section:section-1:P-WRITE-BLUEPRINT"
            ]
        ) == 1
        assert "repair_overrides" not in state


def test_migration_is_idempotent_after_successful_apply(tmp_path: Path) -> None:
    path = _seed(tmp_path)
    first = migration.migrate(path, apply=True)
    second = migration.migrate(path, apply=True)

    assert first["status"] == "APPLIED"
    assert second["status"] == "NO_CHANGES"
    assert second["planned_new_artifacts"] == 0
    with _connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 3
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE event_type='RUNTIME_SEMANTICS_MIGRATED'"
            ).fetchone()[0]
            == 1
        )


def test_legacy_override_migration_artifacts_are_consumed_by_context(tmp_path: Path) -> None:
    from app.context_base import ContextBuilder

    path = _seed(tmp_path)
    migration.migrate(path, apply=True)
    db = Database(path)
    state_row = db.fetchone("SELECT state_json FROM workflows WHERE id='workflow-1'")
    state = json.loads(state_row["state_json"])
    builder = ContextBuilder(db, object())

    resolutions = builder._human_resolutions_for_prompt(
        state, "P-TEST", "workflow-1"
    )
    assert {item["answer"] for item in resolutions} == {"confirmed", "additional"}

    repaired = builder._repair_override(
        state,
        "P-WRITE-BLUEPRINT",
        workflow_id="workflow-1",
    )
    assert repaired["blueprint_id"] == "BP-1"
    assert repaired["paragraphs"][0]["text"] == "repaired"


def test_apply_failure_rollback_preserves_artifact_state_and_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _seed(tmp_path)
    with _connect(path) as conn:
        original_state = conn.execute(
            "SELECT state_json FROM workflows WHERE id='workflow-1'"
        ).fetchone()[0]

    def fail_verification(conn, plan):
        raise RuntimeError("verification failed")

    monkeypatch.setattr(migration, "_verify_applied_plan", fail_verification)
    with pytest.raises(RuntimeError, match="verification failed"):
        migration.migrate(path, apply=True)

    with _connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT state_json FROM workflows WHERE id='workflow-1'"
            ).fetchone()[0]
            == original_state
        )
