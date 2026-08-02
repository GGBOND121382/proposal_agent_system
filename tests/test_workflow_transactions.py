from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from app.db import Database
from app.util import utc_now
from app.workflow_gates import WorkflowGateMixin


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "runtime.sqlite3")
    now = utc_now()
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        ("project-1", "test", "test", "INTERNAL", "{}", now, now),
    )
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        ("workflow-1", "project-1", "WF-TEST", "RUNNING", 0, "{}", now, now),
    )
    return db


def _insert_artifact(tx, version: int, artifact_id: str) -> None:
    tx.execute(
        """INSERT INTO artifacts(id,project_id,workflow_id,artifact_type,prompt_id,version,status,security_level,context_hash,content_json,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            artifact_id,
            "project-1",
            "workflow-1",
            "DECISION_RECORD",
            "P-CRITIC",
            version,
            "PASS",
            "INTERNAL",
            "hash",
            "{}",
            utc_now(),
        ),
    )


def test_explicit_transaction_rolls_back_all_writes_and_audit(tmp_path: Path) -> None:
    db = _db(tmp_path)

    with pytest.raises(RuntimeError):
        with db.transaction() as tx:
            version = tx.next_artifact_version(
                project_id="project-1",
                workflow_id="workflow-1",
                artifact_type="DECISION_RECORD",
                prompt_id="P-CRITIC",
            )
            _insert_artifact(tx, version, "artifact-rollback")
            tx.audit(
                "DECISION_RECORDED",
                project_id="project-1",
                object_id="artifact-rollback",
                metadata={"version": version},
            )
            raise RuntimeError("force rollback")

    assert db.fetchone("SELECT id FROM artifacts WHERE id=?", ("artifact-rollback",)) is None
    assert db.fetchone(
        "SELECT id FROM audit_events WHERE object_id=?", ("artifact-rollback",)
    ) is None


def test_transaction_commit_and_connection_local_reads(tmp_path: Path) -> None:
    db = _db(tmp_path)

    with db.transaction() as tx:
        version = tx.next_artifact_version(
            project_id="project-1",
            workflow_id="workflow-1",
            artifact_type="DECISION_RECORD",
            prompt_id="P-CRITIC",
        )
        _insert_artifact(tx, version, "artifact-commit")
        row = tx.fetchone("SELECT version FROM artifacts WHERE id=?", ("artifact-commit",))
        assert row == {"version": 1}
        tx.audit(
            "DECISION_RECORDED",
            project_id="project-1",
            object_id="artifact-commit",
            metadata={"version": version},
        )

    assert db.fetchone("SELECT version FROM artifacts WHERE id=?", ("artifact-commit",)) == {"version": 1}
    event = db.fetchone("SELECT metadata_json FROM audit_events WHERE object_id=?", ("artifact-commit",))
    assert json.loads(event["metadata_json"]) == {"version": 1}


def test_immediate_transactions_serialize_artifact_version_allocation(tmp_path: Path) -> None:
    db = _db(tmp_path)
    barrier = threading.Barrier(2)
    versions: list[int] = []
    failures: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            with db.transaction() as tx:
                version = tx.next_artifact_version(
                    project_id="project-1",
                    workflow_id="workflow-1",
                    artifact_type="DECISION_RECORD",
                    prompt_id="P-CRITIC",
                )
                _insert_artifact(tx, version, f"artifact-{index}")
                versions.append(version)
        except BaseException as exc:  # pragma: no cover - assertion below reports it
            failures.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert failures == []
    assert sorted(versions) == [1, 2]
    rows = db.fetchall(
        "SELECT version FROM artifacts WHERE artifact_type='DECISION_RECORD' ORDER BY version"
    )
    assert [row["version"] for row in rows] == [1, 2]


def _decision_record():
    from app.decision_arbiter import DecisionArbiter
    from app.contracts import get_semantic_contract

    contract = get_semantic_contract()
    guard = {
        "schema_version": "1.0",
        "status": "PASS",
        "responsibility": "DETERMINISTIC_GUARD",
        "contract_version": contract.version,
        "contract_rule_registry_version": contract.rule_registry_version,
        "contract_hash": contract.contract_hash,
        "findings": [],
    }
    return DecisionArbiter().arbitrate(
        {"status": "PASS", "findings": [], "user_questions": []},
        guard,
        prompt_id="P-CRITIC",
    )


def test_decision_record_workflow_state_and_audit_commit_atomically(tmp_path: Path) -> None:
    from app.decision_arbiter import DecisionArbiter

    db = _db(tmp_path)
    state = {
        "step_results": {
            "0": {"model_status": "PASS", "effective_status": "PASS"}
        }
    }
    artifact_id, updated_at = DecisionArbiter.persist(
        db,
        project_id="project-1",
        workflow_id="workflow-1",
        prompt_id="P-CRITIC",
        record=_decision_record(),
        security_level="INTERNAL",
        workflow_state=state,
        workflow_status="RUNNING",
        current_step=0,
    )

    artifact = db.fetchone("SELECT version,status FROM artifacts WHERE id=?", (artifact_id,))
    assert artifact == {"version": 1, "status": "PASS"}
    workflow = db.fetchone("SELECT state_json FROM workflows WHERE id='workflow-1'")
    persisted_state = json.loads(workflow["state_json"])
    assert persisted_state["decision_record_ids"] == [artifact_id]
    assert state == persisted_state
    assert db.fetchone(
        "SELECT updated_at FROM workflows WHERE id='workflow-1'"
    ) == {"updated_at": updated_at}
    audit = db.fetchone(
        "SELECT metadata_json FROM audit_events WHERE event_type='DECISION_RECORDED' AND object_id=?",
        (artifact_id,),
    )
    assert json.loads(audit["metadata_json"])["version"] == 1


def test_decision_persistence_preserves_nested_checkpoint_identity(tmp_path: Path) -> None:
    """A live section loop must keep updating the persisted state tree."""
    from app.decision_arbiter import DecisionArbiter

    db = _db(tmp_path)
    progress = {"phase": "BLUEPRINT_CRITIC", "status": "RUNNING"}
    state = {"section_progress": {"section-1": progress}}

    DecisionArbiter.persist(
        db,
        project_id="project-1",
        workflow_id="workflow-1",
        prompt_id="P-CRITIC",
        record=_decision_record(),
        security_level="INTERNAL",
        workflow_state=state,
        workflow_status="RUNNING",
        current_step=0,
    )

    assert state["section_progress"]["section-1"] is progress
    progress["phase"] = "CONTENT"
    assert state["section_progress"]["section-1"]["phase"] == "CONTENT"


def test_decision_persistence_failure_rolls_back_artifact_state_and_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db import DatabaseTransaction
    from app.decision_arbiter import DecisionArbiter

    db = _db(tmp_path)
    state = {"step_results": {"0": {"effective_status": "PASS"}}}
    original_state = json.loads(json.dumps(state))

    def fail_audit(self, *args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(DatabaseTransaction, "audit", fail_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        DecisionArbiter.persist(
            db,
            project_id="project-1",
            workflow_id="workflow-1",
            prompt_id="P-CRITIC",
            record=_decision_record(),
            security_level="INTERNAL",
            workflow_state=state,
            workflow_status="RUNNING",
            current_step=0,
        )

    assert state == original_state
    assert db.fetchone("SELECT id FROM artifacts WHERE artifact_type='DECISION_RECORD'") is None
    workflow = db.fetchone("SELECT state_json FROM workflows WHERE id='workflow-1'")
    assert json.loads(workflow["state_json"]) == {}
    assert db.fetchone("SELECT id FROM audit_events WHERE event_type='DECISION_RECORDED'") is None


class _GateEngine(WorkflowGateMixin):
    def __init__(self, db: Database):
        self.db = db

    def get(self, workflow_id: str) -> dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM workflows WHERE id=?", (workflow_id,))
        if not row:
            raise KeyError(workflow_id)
        row["state"] = json.loads(row.pop("state_json"))
        return row


def test_regular_update_propagates_token_for_following_atomic_update(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    engine = _GateEngine(db)
    workflow = engine.get("workflow-1")

    engine._update(workflow, state={"phase": "before-repair"})
    persisted = db.fetchone(
        "SELECT updated_at,state_json FROM workflows WHERE id='workflow-1'"
    )
    assert workflow["updated_at"] == persisted["updated_at"]
    assert workflow["state"] == json.loads(persisted["state_json"])

    with db.transaction() as tx:
        next_token = tx.update_workflow(
            workflow_id="workflow-1",
            status="RUNNING",
            current_step=0,
            state={"phase": "repair-applied"},
            expected_updated_at=workflow["updated_at"],
        )

    assert db.fetchone(
        "SELECT updated_at FROM workflows WHERE id='workflow-1'"
    ) == {"updated_at": next_token}


def _insert_open_information_gate(db: Database) -> None:
    now = utc_now()
    state = {
        "step_results": {
            "0": {
                "prompt_id": "P-TEST",
                "run_id": "run-needs-input",
                "status": "NEED_USER_INPUT",
            }
        }
    }
    db.execute(
        "UPDATE workflows SET status='WAITING_GATE',state_json=?,updated_at=? WHERE id='workflow-1'",
        (json.dumps(state, ensure_ascii=False), now),
    )
    db.execute(
        """INSERT INTO gates(id,project_id,workflow_id,gate_type,target_id,target_version,context_hash,
           question_version,required_role,allowed_actions_json,questions_json,security_level,status,
           decision_json,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "gate-1",
            "project-1",
            "workflow-1",
            "PROJECT_GAP_RESOLUTION",
            "run-needs-input",
            1,
            "context-hash",
            1,
            "PROJECT_OWNER",
            json.dumps(["PROVIDE_INFORMATION"]),
            json.dumps(
                [
                    {
                        "question_id": "question-1",
                        "question": "provide value",
                        "target_paths": ["payload.target"],
                        "answer_schema": {"type": "STRING"},
                        "blocking": True,
                    }
                ]
            ),
            "INTERNAL",
            "OPEN",
            None,
            now,
            now,
        ),
    )


def _decide_information_gate(engine: _GateEngine) -> dict[str, Any]:
    return engine.decide_gate(
        "gate-1",
        action="PROVIDE_INFORMATION",
        decided_by="pytest",
        decided_role="PROJECT_OWNER",
        answers=[{"question_id": "question-1", "value": "confirmed"}],
        context_hash="context-hash",
    )


def test_gate_decision_artifact_workflow_and_audit_commit_atomically(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _insert_open_information_gate(db)
    gate = _decide_information_gate(_GateEngine(db))

    assert gate["status"] == "APPROVED"
    artifacts = db.fetchall(
        "SELECT id,version FROM artifacts WHERE artifact_type='HUMAN_RESOLUTION'"
    )
    assert len(artifacts) == 1
    assert artifacts[0]["version"] == 1
    workflow = db.fetchone("SELECT status,state_json FROM workflows WHERE id='workflow-1'")
    state = json.loads(workflow["state_json"])
    assert workflow["status"] == "RUNNING"
    assert state["human_resolution_artifact_ids"]["P-TEST"] == [artifacts[0]["id"]]
    assert "human_resolutions" not in state
    assert db.fetchone(
        "SELECT id FROM audit_events WHERE event_type='GATE_DECIDED' AND object_id='gate-1'"
    ) is not None


def test_gate_decision_failure_rolls_back_gate_artifact_workflow_and_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db import DatabaseTransaction

    db = _db(tmp_path)
    _insert_open_information_gate(db)
    before = db.fetchone("SELECT status,state_json FROM workflows WHERE id='workflow-1'")

    def fail_audit(self, *args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(DatabaseTransaction, "audit", fail_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        _decide_information_gate(_GateEngine(db))

    assert db.fetchone("SELECT status FROM gates WHERE id='gate-1'") == {"status": "OPEN"}
    assert db.fetchone("SELECT id FROM artifacts WHERE artifact_type='HUMAN_RESOLUTION'") is None
    assert db.fetchone("SELECT status,state_json FROM workflows WHERE id='workflow-1'") == before
    assert db.fetchone("SELECT id FROM audit_events WHERE object_id='gate-1'") is None


def test_gate_compare_and_swap_accepts_only_one_concurrent_decision(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _insert_open_information_gate(db)
    barrier = threading.Barrier(2)
    successes: list[str] = []
    failures: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            result = _decide_information_gate(_GateEngine(db))
            successes.append(result["status"])
        except BaseException as exc:  # pragma: no cover - assertions report details
            failures.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert successes == ["APPROVED"]
    assert len(failures) == 1
    assert isinstance(failures[0], ValueError)
    assert db.fetchone(
        "SELECT COUNT(*) AS n FROM artifacts WHERE artifact_type='HUMAN_RESOLUTION'"
    )["n"] == 1


def test_artifact_lookup_indexes_support_versioned_context_reads(tmp_path: Path) -> None:
    db = _db(tmp_path)
    indexes = {
        row["name"]
        for row in db.fetchall("PRAGMA index_list('artifacts')")
    }

    assert "idx_artifacts_project_prompt_type_version" in indexes
    assert "idx_artifacts_workflow_prompt_type_status_version" in indexes
