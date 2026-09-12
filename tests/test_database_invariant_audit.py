from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.db import Database
from app.util import utc_now
from scripts.audit_database_invariants import DatabaseInvariantAuditor


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _project(db: Database, project_id: str = "project-a") -> None:
    now = utc_now()
    db.execute(
        """INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?)""",
        (project_id, "Audit", "Audit fixture", "INTERNAL", "{}", now, now),
    )


def _workflow(
    db: Database,
    *,
    workflow_id: str = "wf-a",
    project_id: str = "project-a",
    status: str = "RUNNING",
    current_step: int = 0,
    state: dict | None = None,
) -> None:
    now = utc_now()
    payload = state or {
        "workflow_type": "WF-1_PROJECT_INTAKE",
        "options": {},
        "step_results": {},
        "repair_attempts": {},
        "public_search_results": None,
    }
    db.execute(
        """INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (
            workflow_id,
            project_id,
            "WF-1_PROJECT_INTAKE",
            status,
            current_step,
            json.dumps(payload, ensure_ascii=False),
            now,
            now,
        ),
    )


def _codes(report: dict) -> list[str]:
    return [item["code"] for item in report["findings"]]


def test_clean_database_reports_only_unconstrained_workflow_reference_risk(tmp_path: Path) -> None:
    path = tmp_path / "clean.sqlite3"
    Database(path)
    before_entries = sorted(item.name for item in tmp_path.iterdir())

    report = DatabaseInvariantAuditor(path).audit()

    assert sorted(item.name for item in tmp_path.iterdir()) == before_entries
    assert _codes(report) == ["UNCONSTRAINED_WORKFLOW_REFERENCE_COLUMNS"]
    assert report["summary"]["severity_counts"] == {"WARNING": 1}


def test_audit_is_read_only_and_reports_legacy_state_risks(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    db = Database(path)
    _project(db)
    _workflow(
        db,
        workflow_id="wf-blocked-1",
        status="BLOCKED",
        state={
            "workflow_type": "WF-1_PROJECT_INTAKE",
            "options": {},
            "step_results": {},
            "repair_attempts": {},
            "public_search_results": None,
        },
    )
    _workflow(
        db,
        workflow_id="wf-blocked-2",
        status="BLOCKED",
        state={
            "workflow_type": "WF-1_PROJECT_INTAKE",
            "options": {},
            "step_results": {},
            "repair_attempts": {},
            "public_search_results": None,
            "last_error": "legacy failure",
        },
    )
    before = _digest(path)

    report = DatabaseInvariantAuditor(path).audit()

    assert _digest(path) == before
    assert _codes(report).count("LEGACY_GENERIC_BLOCKED") == 2
    assert _codes(report).count("LEGACY_BLOCKED_WITHOUT_ERROR") == 1
    assert _codes(report).count("MULTIPLE_ACTIVE_PARENT_WORKFLOWS") == 1
    assert _codes(report).count("LEGACY_BLOCKED_TECHNICAL_EVIDENCE") == 2
    assert _codes(report).count("UNCONSTRAINED_WORKFLOW_REFERENCE_COLUMNS") == 1
    assert report["summary"]["severity_counts"] == {"WARNING": 7}


def test_audit_detects_terminal_transient_checkpoint(tmp_path: Path) -> None:
    path = tmp_path / "terminal.sqlite3"
    db = Database(path)
    _project(db)
    _workflow(
        db,
        status="COMPLETED",
        current_step=9,
        state={
            "workflow_type": "WF-1_PROJECT_INTAKE",
            "options": {},
            "step_results": {},
            "repair_attempts": {},
            "public_search_results": None,
            "runtime_failure_point": "WORKFLOW_ADVANCE",
            "runtime_recoverable": False,
        },
    )

    report = DatabaseInvariantAuditor(path).audit()

    finding = next(
        item
        for item in report["findings"]
        if item["code"] == "TERMINAL_WORKFLOW_HAS_TRANSIENT_STATE"
    )
    assert finding["object_id"] == "wf-a"
    assert finding["details"]["fields"] == [
        "runtime_failure_point",
        "runtime_recoverable",
    ]


def test_audit_detects_duplicate_prompt_artifact_version(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.sqlite3"
    db = Database(path)
    _project(db)
    _workflow(db)
    now = utc_now()
    payload = json.dumps(
        {
            "prompt_id": "P-SECURITY-CLASSIFY",
            "version": 1,
            "status": "ERROR",
        },
        ensure_ascii=False,
    )
    for artifact_id in ("artifact-1", "artifact-2"):
        db.execute(
            """INSERT INTO artifacts(id,project_id,workflow_id,artifact_type,prompt_id,version,status,
                                     security_level,context_hash,content_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                artifact_id,
                "project-a",
                "wf-a",
                "PROMPT_TRACE",
                "P-SECURITY-CLASSIFY",
                1,
                "ERROR",
                "INTERNAL",
                "a" * 64,
                payload,
                now,
            ),
        )

    report = DatabaseInvariantAuditor(path).audit()

    assert "DUPLICATE_PROMPT_ARTIFACT_VERSION" in _codes(report)
    assert report["summary"]["severity_counts"]["ERROR"] >= 1


def test_audit_detects_cross_project_unconstrained_artifact_reference(tmp_path: Path) -> None:
    path = tmp_path / "cross-project.sqlite3"
    db = Database(path)
    _project(db, "project-a")
    _project(db, "project-b")
    _workflow(db, project_id="project-a")
    db.execute(
        """INSERT INTO artifacts(id,project_id,workflow_id,artifact_type,prompt_id,version,status,
                                 security_level,context_hash,content_json,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "artifact-cross-project",
            "project-b",
            "wf-a",
            "PROMPT_OUTPUT",
            "P-SECURITY-CLASSIFY",
            1,
            "PASS",
            "INTERNAL",
            "b" * 64,
            json.dumps(
                {
                    "prompt_id": "P-SECURITY-CLASSIFY",
                    "status": "PASS",
                }
            ),
            utc_now(),
        ),
    )

    report = DatabaseInvariantAuditor(path).audit()

    assert "CROSS_PROJECT_WORKFLOW_REFERENCE" in _codes(report)


def test_audit_classifies_legacy_blocked_from_persisted_evidence(tmp_path: Path) -> None:
    path = tmp_path / "classified.sqlite3"
    db = Database(path)
    _project(db)
    _workflow(
        db,
        workflow_id="wf-human",
        status="BLOCKED",
        current_step=0,
        state={
            "workflow_type": "WF-1_PROJECT_INTAKE",
            "options": {},
            "step_results": {
                "0": {
                    "prompt_id": "P-SECURITY-CLASSIFY",
                    "run_id": "run-human",
                    "status": "BLOCK",
                }
            },
            "repair_attempts": {},
            "public_search_results": None,
        },
    )
    now = utc_now()
    output = {
        "prompt_id": "P-SECURITY-CLASSIFY",
        "status": "BLOCK",
        "user_questions": [
            {
                "question_id": "q-1",
                "question": "Provide the missing value",
                "blocking": True,
            }
        ],
    }
    envelope = {"prompt_id": "P-SECURITY-CLASSIFY"}
    db.execute(
        """INSERT INTO prompt_runs(id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,
                                   input_hash,output_hash,input_json,output_json,error,duration_ms,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "run-human",
            "project-a",
            "wf-human",
            "P-SECURITY-CLASSIFY",
            "BLOCK",
            None,
            None,
            hashlib.sha256(
                json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            hashlib.sha256(
                json.dumps(output, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            json.dumps(envelope),
            json.dumps(output),
            None,
            1,
            now,
        ),
    )
    _workflow(
        db,
        workflow_id="wf-provider",
        status="BLOCKED",
        state={
            "workflow_type": "WF-1_PROJECT_INTAKE",
            "options": {},
            "step_results": {},
            "repair_attempts": {},
            "public_search_results": None,
            "last_error": "LLM transport failed (ConnectError)",
        },
    )
    _workflow(
        db,
        workflow_id="wf-contract",
        status="BLOCKED",
        state={
            "workflow_type": "WF-1_PROJECT_INTAKE",
            "options": {},
            "step_results": {},
            "repair_attempts": {},
            "public_search_results": None,
            "last_error": "Output schema validation failed",
        },
    )

    report = DatabaseInvariantAuditor(path).audit()
    by_code = {}
    for finding in report["findings"]:
        by_code.setdefault(finding["code"], []).append(finding)

    assert by_code["LEGACY_BLOCKED_HUMAN_INPUT_EVIDENCE"][0]["details"][
        "suggested_status"
    ] == "WAITING_GATE"
    assert by_code["LEGACY_BLOCKED_PROVIDER_EVIDENCE"][0]["details"][
        "suggested_status"
    ] == "WAITING_PROVIDER"
    assert by_code["LEGACY_BLOCKED_CONTRACT_EVIDENCE"][0]["details"][
        "suggested_status"
    ] == "BLOCKED_CONTRACT"


def test_audit_detects_nested_cross_workflow_run_reference(tmp_path: Path) -> None:
    path = tmp_path / "cross-workflow-state.sqlite3"
    db = Database(path)
    _project(db)
    _workflow(db, workflow_id="wf-a")
    _workflow(db, workflow_id="wf-b")
    now = utc_now()
    envelope = {"prompt_id": "P-SECURITY-CLASSIFY"}
    output = {"prompt_id": "P-SECURITY-CLASSIFY", "status": "PASS"}
    db.execute(
        """INSERT INTO prompt_runs(id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,
                                   input_hash,output_hash,input_json,output_json,error,duration_ms,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "run-other",
            "project-a",
            "wf-b",
            "P-SECURITY-CLASSIFY",
            "PASS",
            None,
            None,
            hashlib.sha256(
                json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            hashlib.sha256(
                json.dumps(output, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            json.dumps(envelope),
            json.dumps(output),
            None,
            1,
            now,
        ),
    )
    row = db.fetchone("SELECT state_json FROM workflows WHERE id='wf-a'")
    state = json.loads(row["state_json"])
    state["deterministic_repairs"] = [{"source_run_id": "run-other"}]
    db.execute(
        "UPDATE workflows SET state_json=? WHERE id='wf-a'",
        (json.dumps(state),),
    )

    report = DatabaseInvariantAuditor(path).audit()

    assert "STATE_CROSS_WORKFLOW_RUN_REFERENCE" in _codes(report)


def test_audit_accepts_aggregate_metadata_for_non_prompt_workflow_step(tmp_path: Path) -> None:
    path = tmp_path / "write-sections.sqlite3"
    db = Database(path)
    _project(db)
    now = utc_now()
    state = {
        "workflow_type": "WF-4_PROPOSAL_AUTHORING",
        "options": {},
        "step_results": {
            "5": {"model_status": "PASS", "effective_status": "PASS"}
        },
        "repair_attempts": {},
        "public_search_results": None,
    }
    db.execute(
        """INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (
            "wf-write-sections",
            "project-a",
            "WF-4_PROPOSAL_AUTHORING",
            "COMPLETED",
            7,
            json.dumps(state, ensure_ascii=False),
            now,
            now,
        ),
    )

    report = DatabaseInvariantAuditor(path).audit()

    assert "STEP_RESULT_DANGLING_RUN" not in _codes(report)
