from __future__ import annotations

import copy
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
    questions = [
        {
            "question_id": "question-1",
            "question": "provide value",
            "target_paths": ["payload.target"],
            "answer_schema": {"type": "STRING"},
            "blocking": True,
        }
    ]
    engine = _GateEngine(db)
    workflow = engine.get("workflow-1")
    context_hash = engine._gate_context_hash_v2(
        workflow,
        gate_type="PROJECT_GAP_RESOLUTION",
        target_id="run-needs-input",
        questions=questions,
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
            context_hash,
            2,
            "PROJECT_OWNER",
            json.dumps(["PROVIDE_INFORMATION"]),
            json.dumps(questions),
            "INTERNAL",
            "OPEN",
            None,
            now,
            now,
        ),
    )


def _decide_information_gate(engine: _GateEngine) -> dict[str, Any]:
    gate = engine.db.fetchone("SELECT context_hash FROM gates WHERE id='gate-1'")
    assert gate is not None
    return engine.decide_gate(
        "gate-1",
        action="PROVIDE_INFORMATION",
        decided_by="pytest",
        decided_role="PROJECT_OWNER",
        answers=[{"question_id": "question-1", "value": "confirmed"}],
        context_hash=gate["context_hash"],
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
    assert state["human_resolution_artifact_ids"]["step:0:P-TEST"] == [
        artifacts[0]["id"]
    ]
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


def test_core_update_rejects_reopening_terminal_workflow(tmp_path: Path) -> None:
    db = _db(tmp_path)
    now = utc_now()
    db.execute(
        "UPDATE workflows SET status='COMPLETED',updated_at=? WHERE id='workflow-1'",
        (now,),
    )
    row = db.fetchone("SELECT * FROM workflows WHERE id='workflow-1'")
    assert row is not None
    row["state"] = json.loads(row.pop("state_json"))
    engine = WorkflowGateMixin()
    engine.db = db

    with pytest.raises(ValueError, match="illegal workflow status transition"):
        engine._update(row, status="RUNNING", state=row["state"])

    assert db.fetchone(
        "SELECT status FROM workflows WHERE id='workflow-1'"
    ) == {"status": "COMPLETED"}


def test_gate_creation_is_exactly_idempotent_and_replaces_other_open_target(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    engine = _GateEngine(db)
    workflow = engine.get("workflow-1")
    questions = [
        {
            "question_id": "question-1",
            "question": "provide value",
            "target_paths": ["payload.target"],
            "answer_schema": {"type": "STRING"},
            "blocking": True,
        }
    ]

    first = engine._create_gate(
        workflow,
        "PROJECT_GAP_RESOLUTION",
        target_id="run-1",
        questions=questions,
    )
    repeated = engine._create_gate(
        engine.get("workflow-1"),
        "PROJECT_GAP_RESOLUTION",
        target_id="run-1",
        questions=questions,
    )
    assert repeated == first
    assert db.fetchone(
        "SELECT COUNT(*) AS n FROM gates WHERE workflow_id='workflow-1' AND status='OPEN'"
    ) == {"n": 1}

    replacement = engine._create_gate(
        engine.get("workflow-1"),
        "PROJECT_GAP_RESOLUTION",
        target_id="run-2",
        questions=questions,
    )
    assert replacement != first
    assert db.fetchone("SELECT status FROM gates WHERE id=?", (first,)) == {
        "status": "CANCELLED"
    }
    assert db.fetchone(
        "SELECT id,target_id FROM gates WHERE workflow_id='workflow-1' AND status='OPEN'"
    ) == {"id": replacement, "target_id": "run-2"}


def test_gate_creation_widens_underpowered_boolean_and_enum_controls(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    engine = _GateEngine(db)
    gate_id = engine._create_gate(
        engine.get("workflow-1"),
        "PROJECT_GAP_RESOLUTION",
        target_id="run-composite-answers",
        questions=[
            {
                "question_id": "question-open-ended",
                "question": "模块名称与状态是什么？请给出规模区间。",
                "target_paths": ["payload.confirmed_facts"],
                "answer_schema": {"type": "BOOLEAN", "allowed_values": []},
                "blocking": True,
            },
            {
                "question_id": "question-multi-value",
                "question": "哪些指标采用离线回放，哪些保留人员实验？",
                "target_paths": ["payload.project_subgraph"],
                "answer_schema": {
                    "type": "ENUM",
                    "allowed_values": ["速度", "覆盖度", "人员"],
                },
                "blocking": True,
            },
        ],
    )

    row = db.fetchone("SELECT questions_json FROM gates WHERE id=?", (gate_id,))
    questions = json.loads(row["questions_json"])

    assert [question["answer_schema"] for question in questions] == [
        {"type": "STRING"},
        {"type": "STRING"},
    ]


def test_gate_decision_rejects_stale_server_context_without_client_hash(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    _insert_open_information_gate(db)
    workflow = _GateEngine(db).get("workflow-1")
    workflow["state"]["checkpoint_changed"] = True
    _GateEngine(db)._update(workflow, state=workflow["state"])

    with pytest.raises(ValueError, match="stale|superseded"):
        _GateEngine(db).decide_gate(
            "gate-1",
            action="PROVIDE_INFORMATION",
            decided_by="pytest",
            decided_role="PROJECT_OWNER",
            answers=[{"question_id": "question-1", "value": "confirmed"}],
        )

    assert db.fetchone("SELECT status FROM gates WHERE id='gate-1'") == {
        "status": "CANCELLED"
    }
    assert db.fetchone("SELECT status FROM workflows WHERE id='workflow-1'") == {
        "status": "RUNNING"
    }
    assert db.fetchone(
        "SELECT id FROM artifacts WHERE artifact_type='HUMAN_RESOLUTION'"
    ) is None


def test_open_gate_reconciliation_restores_waiting_gate_after_crash_window(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    engine = _GateEngine(db)
    gate_id = engine._create_gate(
        engine.get("workflow-1"),
        "PROJECT_GAP_RESOLUTION",
        target_id="run-1",
        questions=[],
    )
    assert db.fetchone("SELECT status FROM workflows WHERE id='workflow-1'") == {
        "status": "RUNNING"
    }

    current = engine.get("workflow-1")
    reconciled = engine._reconcile_open_gates(current)

    assert reconciled is not None and reconciled["id"] == gate_id
    assert db.fetchone("SELECT status FROM workflows WHERE id='workflow-1'") == {
        "status": "WAITING_GATE"
    }


def test_terminal_workflow_cancels_residual_open_gate_without_reopening(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    engine = _GateEngine(db)
    gate_id = engine._create_gate(
        engine.get("workflow-1"),
        "PROJECT_GAP_RESOLUTION",
        target_id="run-1",
        questions=[],
    )
    db.execute("UPDATE workflows SET status='COMPLETED' WHERE id='workflow-1'")

    assert engine._reconcile_open_gates(engine.get("workflow-1")) is None
    assert db.fetchone("SELECT status FROM workflows WHERE id='workflow-1'") == {
        "status": "COMPLETED"
    }
    assert db.fetchone("SELECT status FROM gates WHERE id=?", (gate_id,)) == {
        "status": "CANCELLED"
    }


def test_reconciliation_collapses_multiple_legacy_open_gates_to_latest_current(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    engine = _GateEngine(db)
    first = engine._create_gate(
        engine.get("workflow-1"),
        "PROJECT_GAP_RESOLUTION",
        target_id="run-old",
        questions=[],
    )
    workflow = engine.get("workflow-1")
    second = "gate-newer-legacy"
    context_hash = engine._gate_context_hash_v2(
        workflow,
        gate_type="PROJECT_GAP_RESOLUTION",
        target_id="run-new",
        questions=[],
    )
    db.execute(
        """INSERT INTO gates(id,project_id,workflow_id,gate_type,target_id,target_version,context_hash,
           question_version,required_role,allowed_actions_json,questions_json,security_level,status,
           decision_json,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            second,
            "project-1",
            "workflow-1",
            "PROJECT_GAP_RESOLUTION",
            "run-new",
            1,
            context_hash,
            2,
            "PROJECT_OWNER",
            json.dumps(["CONFIRM"]),
            "[]",
            "INTERNAL",
            "OPEN",
            None,
            "9999-01-01T00:00:00Z",
            "9999-01-01T00:00:00Z",
        ),
    )

    kept = engine._reconcile_open_gates(engine.get("workflow-1"))

    assert kept is not None and kept["id"] == second
    assert db.fetchone("SELECT status FROM gates WHERE id=?", (first,)) == {
        "status": "CANCELLED"
    }
    assert db.fetchone(
        "SELECT COUNT(*) AS n FROM gates WHERE workflow_id='workflow-1' AND status='OPEN'"
    ) == {"n": 1}


def test_gate_target_run_prompt_wins_over_stale_step_result(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _insert_open_information_gate(db)
    now = utc_now()
    db.execute(
        """INSERT INTO prompt_runs(id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,
           input_hash,output_hash,input_json,output_json,error,duration_ms,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "run-needs-input",
            "project-1",
            "workflow-1",
            "P-EXACT-TARGET",
            "PASS",
            "simulated",
            "offline",
            "input-hash",
            "output-hash",
            json.dumps({"payload": {}}, ensure_ascii=False),
            json.dumps({"status": "NEED_USER_INPUT"}, ensure_ascii=False),
            None,
            1,
            now,
        ),
    )

    gate = _decide_information_gate(_GateEngine(db))
    assert gate["status"] == "APPROVED"
    state = json.loads(
        db.fetchone("SELECT state_json FROM workflows WHERE id='workflow-1'")["state_json"]
    )
    assert "step:0:P-EXACT-TARGET" in state["human_resolution_artifact_ids"]
    assert "step:0:P-TEST" not in state["human_resolution_artifact_ids"]
    artifact = db.fetchone(
        "SELECT prompt_id,content_json FROM artifacts WHERE artifact_type='HUMAN_RESOLUTION'"
    )
    assert artifact["prompt_id"] == "P-EXACT-TARGET"
    assert json.loads(artifact["content_json"])["prompt_id"] == "P-EXACT-TARGET"


def test_section_need_user_input_confirm_reruns_same_phase_without_repair_budget(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    now = utc_now()
    state = {
        "section_progress": {
            "section-a": {
                "phase": "CONTENT_CRITIC",
                "status": "WAITING_GATE",
                "runs": [
                    {
                        "run_id": "run-section-input",
                        "prompt_id": "P-WRITE-CRITIC",
                        "role": "INITIAL_REVIEW",
                    }
                ],
            }
        },
        "section_input_gate": {
            "section_id": "section-a",
            "phase": "CONTENT_CRITIC",
            "next_phase": "POLISH",
            "prompt_id": "P-WRITE-CRITIC",
            "run_id": "run-section-input",
        },
        "repair_attempts": {"P-WRITE-CRITIC": 1},
    }
    db.execute(
        "UPDATE workflows SET status='WAITING_GATE',current_step=5,state_json=?,updated_at=? WHERE id='workflow-1'",
        (json.dumps(state, ensure_ascii=False), now),
    )
    db.execute(
        """INSERT INTO prompt_runs(id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,
           input_hash,output_hash,input_json,output_json,error,duration_ms,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "run-section-input",
            "project-1",
            "workflow-1",
            "P-WRITE-CRITIC",
            "PASS",
            "simulated",
            "offline",
            "input-hash",
            "output-hash",
            json.dumps(
                {"payload": {"source_section": {"section_id": "section-a"}}},
                ensure_ascii=False,
            ),
            json.dumps({"status": "NEED_USER_INPUT"}, ensure_ascii=False),
            None,
            1,
            now,
        ),
    )
    questions = [
        {
            "question_id": "section-answer",
            "question": "补充章节事实",
            "target_paths": ["payload.source_section.confirmed_fact"],
            "answer_schema": {"type": "STRING"},
            "required": True,
        }
    ]
    engine = _GateEngine(db)
    workflow = engine.get("workflow-1")
    gate_id = engine._create_gate(
        workflow,
        "PROJECT_GAP_RESOLUTION",
        target_id="run-section-input",
        questions=questions,
    )
    engine.decide_gate(
        gate_id,
        action="CONFIRM",
        decided_by="pytest",
        decided_role="PROJECT_OWNER",
        answers=[{"question_id": "section-answer", "value": "已补充"}],
    )

    updated = engine.get("workflow-1")
    progress = updated["state"]["section_progress"]["section-a"]
    assert updated["current_step"] == 5
    assert progress["phase"] == "CONTENT_CRITIC"
    assert progress["status"] == "RUNNING"
    assert "section_input_gate" not in updated["state"]
    assert updated["state"]["repair_attempts"] == {"P-WRITE-CRITIC": 1}
    assert updated["state"]["human_input_reruns"][
        "section:section-a:P-WRITE-CRITIC"
    ] == 1
    assert updated["state"]["rerun_from_human_input"]["scope_key"] == (
        "section:section-a:P-WRITE-CRITIC"
    )
    assert "section:section-a:P-WRITE-CRITIC" in updated["state"][
        "human_resolution_artifact_ids"
    ]


def test_stale_post_approval_waiting_write_is_rejected(tmp_path: Path) -> None:
    db = _db(tmp_path)
    engine = _GateEngine(db)
    stale = engine.get("workflow-1")
    gate_id = engine._create_gate(
        stale,
        "CANDIDATE_REVIEW",
        target_id="workflow-1",
        questions=[],
    )
    engine._reconcile_open_gates(engine.get("workflow-1"))
    engine.decide_gate(
        gate_id,
        action="CONFIRM",
        decided_by="pytest",
        decided_role="CONTENT_OPERATOR",
    )

    with pytest.raises(RuntimeError, match="workflow changed during atomic update"):
        engine._update(stale, status="WAITING_GATE", state=stale["state"])

    assert db.fetchone("SELECT status FROM workflows WHERE id='workflow-1'") == {
        "status": "RUNNING"
    }
    assert db.fetchone("SELECT status FROM gates WHERE id=?", (gate_id,)) == {
        "status": "APPROVED"
    }


def test_stale_gate_cancel_and_workflow_recovery_roll_back_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.db import DatabaseTransaction

    db = _db(tmp_path)
    engine = _GateEngine(db)
    workflow = engine.get("workflow-1")
    gate_id = engine._create_gate(
        workflow,
        "CANDIDATE_REVIEW",
        target_id="workflow-1",
        questions=[],
    )
    engine._update(workflow, status="WAITING_GATE", state=workflow["state"])
    changed = engine.get("workflow-1")
    changed["state"]["checkpoint_changed"] = True
    engine._update(changed, state=changed["state"])

    original_update = DatabaseTransaction.update_workflow

    def fail_after_gate_cancel(self, *args, **kwargs):
        raise RuntimeError("crash after gate cancellation")

    monkeypatch.setattr(DatabaseTransaction, "update_workflow", fail_after_gate_cancel)
    with pytest.raises(RuntimeError, match="crash after gate cancellation"):
        engine._reconcile_open_gates(engine.get("workflow-1"))
    monkeypatch.setattr(DatabaseTransaction, "update_workflow", original_update)

    assert db.fetchone("SELECT status FROM gates WHERE id=?", (gate_id,)) == {
        "status": "OPEN"
    }
    assert db.fetchone("SELECT status FROM workflows WHERE id='workflow-1'") == {
        "status": "WAITING_GATE"
    }
    assert db.fetchone(
        "SELECT id FROM audit_events WHERE event_type='GATE_CANCELLED_STALE' AND object_id=?",
        (gate_id,),
    ) is None


def test_reconciliation_uses_authoritative_workflow_state_not_caller_snapshot(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    engine = _GateEngine(db)
    stale = engine.get("workflow-1")
    gate_id = engine._create_gate(
        stale,
        "CANDIDATE_REVIEW",
        target_id="workflow-1",
        questions=[],
    )
    changed = engine.get("workflow-1")
    changed["state"]["checkpoint_changed"] = True
    engine._update(changed, state=changed["state"])

    assert engine._reconcile_open_gates(stale) is None
    assert db.fetchone("SELECT status FROM gates WHERE id=?", (gate_id,)) == {
        "status": "CANCELLED"
    }
    assert db.fetchone("SELECT status FROM workflows WHERE id='workflow-1'") == {
        "status": "RUNNING"
    }


def test_v2_gate_cannot_downgrade_to_legacy_v1_context_hash(tmp_path: Path) -> None:
    db = _db(tmp_path)
    engine = _GateEngine(db)
    workflow = engine.get("workflow-1")
    gate_id = engine._create_gate(
        workflow,
        "CANDIDATE_REVIEW",
        target_id="workflow-1",
        questions=[],
    )
    legacy_hash = engine._gate_context_hash_v1(engine.get("workflow-1"))
    db.execute(
        "UPDATE gates SET context_hash=? WHERE id=?",
        (legacy_hash, gate_id),
    )

    assert engine._reconcile_open_gates(engine.get("workflow-1")) is None
    assert db.fetchone("SELECT status FROM gates WHERE id=?", (gate_id,)) == {
        "status": "CANCELLED"
    }


def test_legacy_v1_gate_remains_reconcilable_for_migration(tmp_path: Path) -> None:
    db = _db(tmp_path)
    engine = _GateEngine(db)
    workflow = engine.get("workflow-1")
    gate_id = "gate-v1"
    now = utc_now()
    db.execute(
        """INSERT INTO gates(id,project_id,workflow_id,gate_type,target_id,target_version,context_hash,
           question_version,required_role,allowed_actions_json,questions_json,security_level,status,
           decision_json,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            gate_id,
            "project-1",
            "workflow-1",
            "CANDIDATE_REVIEW",
            "workflow-1",
            1,
            engine._gate_context_hash_v1(workflow),
            1,
            "CONTENT_OPERATOR",
            json.dumps(["CONFIRM"]),
            "[]",
            "INTERNAL",
            "OPEN",
            None,
            now,
            now,
        ),
    )

    kept = engine._reconcile_open_gates(workflow)
    assert kept is not None and kept["id"] == gate_id
    assert db.fetchone("SELECT status FROM workflows WHERE id='workflow-1'") == {
        "status": "WAITING_GATE"
    }


def test_concurrent_exact_gate_creation_keeps_one_open_gate(tmp_path: Path) -> None:
    db = _db(tmp_path)
    barrier = threading.Barrier(2)
    gate_ids: list[str] = []
    failures: list[BaseException] = []

    def worker() -> None:
        try:
            workflow = _GateEngine(db).get("workflow-1")
            barrier.wait(timeout=5)
            gate_ids.append(
                _GateEngine(db)._create_gate(
                    workflow,
                    "CANDIDATE_REVIEW",
                    target_id="workflow-1",
                    questions=[],
                )
            )
        except BaseException as exc:  # pragma: no cover - assertions report it
            failures.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert failures == []
    assert len(set(gate_ids)) == 1
    assert db.fetchone(
        "SELECT COUNT(*) AS n FROM gates WHERE workflow_id='workflow-1' AND status='OPEN'"
    ) == {"n": 1}


def test_gate_creation_can_commit_protected_workflow_checkpoint_atomically(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    engine = _GateEngine(db)
    workflow = engine.get("workflow-1")
    next_state = copy.deepcopy(workflow["state"])
    next_state["candidate_snapshot_ready"] = True

    gate_id = engine._create_gate(
        workflow,
        "CANDIDATE_REVIEW",
        target_id="workflow-1",
        questions=[],
        checkpoint_status="WAITING_GATE",
        checkpoint_step=1,
        checkpoint_state=next_state,
    )

    persisted = engine.get("workflow-1")
    assert persisted["status"] == "WAITING_GATE"
    assert persisted["current_step"] == 1
    assert persisted["state"]["candidate_snapshot_ready"] is True
    assert workflow["updated_at"] == persisted["updated_at"]
    gate = db.fetchone("SELECT * FROM gates WHERE id=?", (gate_id,))
    assert gate is not None
    assert engine._gate_context_is_current(gate, persisted)


def test_atomic_gate_checkpoint_rolls_back_workflow_when_audit_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.db import DatabaseTransaction

    db = _db(tmp_path)
    engine = _GateEngine(db)
    workflow = engine.get("workflow-1")
    original = copy.deepcopy(workflow)
    next_state = copy.deepcopy(workflow["state"])
    next_state["candidate_snapshot_ready"] = True

    def fail_audit(self, *args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(DatabaseTransaction, "audit", fail_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        engine._create_gate(
            workflow,
            "CANDIDATE_REVIEW",
            target_id="workflow-1",
            questions=[],
            checkpoint_status="WAITING_GATE",
            checkpoint_step=1,
            checkpoint_state=next_state,
        )

    persisted = engine.get("workflow-1")
    assert persisted["status"] == original["status"]
    assert persisted["current_step"] == original["current_step"]
    assert persisted["state"] == original["state"]
    assert db.fetchone("SELECT id FROM gates WHERE workflow_id='workflow-1'") is None
    assert db.fetchone("SELECT id FROM audit_events WHERE event_type='GATE_CREATED'") is None


def test_gate_creation_failure_rolls_back_gate_and_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.db import DatabaseTransaction

    db = _db(tmp_path)
    engine = _GateEngine(db)

    def fail_audit(self, *args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(DatabaseTransaction, "audit", fail_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        engine._create_gate(
            engine.get("workflow-1"),
            "CANDIDATE_REVIEW",
            target_id="workflow-1",
            questions=[],
        )

    assert db.fetchone("SELECT id FROM gates WHERE workflow_id='workflow-1'") is None
    assert db.fetchone("SELECT id FROM audit_events WHERE event_type='GATE_CREATED'") is None


def test_need_user_input_cannot_be_confirmed_without_a_concrete_answer(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
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
    questions = [
        {
            "question_id": "optional-looking-question",
            "question": "请提供必要信息",
            "target_paths": ["payload.target"],
            "answer_schema": {"type": "STRING"},
            # Provider output may incorrectly mark every question non-blocking;
            # the NEED_USER_INPUT checkpoint itself still requires an answer.
            "blocking": False,
        }
    ]
    engine = _GateEngine(db)
    workflow = engine.get("workflow-1")
    context_hash = engine._gate_context_hash_v2(
        workflow,
        gate_type="PROJECT_GAP_RESOLUTION",
        target_id="run-needs-input",
        questions=questions,
    )
    db.execute(
        """INSERT INTO gates(id,project_id,workflow_id,gate_type,target_id,target_version,
           context_hash,question_version,required_role,allowed_actions_json,questions_json,
           security_level,status,decision_json,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "gate-empty-answer",
            "project-1",
            "workflow-1",
            "PROJECT_GAP_RESOLUTION",
            "run-needs-input",
            1,
            context_hash,
            2,
            "PROJECT_OWNER",
            json.dumps(["CONFIRM"]),
            json.dumps(questions, ensure_ascii=False),
            "INTERNAL",
            "OPEN",
            None,
            now,
            now,
        ),
    )

    for answers in (
        [],
        [{"question_id": "optional-looking-question", "value": "   "}],
    ):
        with pytest.raises(ValueError, match="至少提交一个有效回答"):
            engine.decide_gate(
                "gate-empty-answer",
                action="CONFIRM",
                decided_by="pytest",
                decided_role="PROJECT_OWNER",
                answers=answers,
            )

    assert db.fetchone("SELECT status FROM gates WHERE id='gate-empty-answer'") == {
        "status": "OPEN"
    }
    persisted = db.fetchone(
        "SELECT status,state_json FROM workflows WHERE id='workflow-1'"
    )
    assert persisted["status"] == "WAITING_GATE"
    assert json.loads(persisted["state_json"])["step_results"]["0"]["status"] == (
        "NEED_USER_INPUT"
    )


def test_need_user_input_gate_without_questions_cannot_be_empty_confirmed(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
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
    engine = _GateEngine(db)
    workflow = engine.get("workflow-1")
    context_hash = engine._gate_context_hash_v2(
        workflow,
        gate_type="PROJECT_GAP_RESOLUTION",
        target_id="run-needs-input",
        questions=[],
    )
    db.execute(
        """INSERT INTO gates(id,project_id,workflow_id,gate_type,target_id,target_version,
           context_hash,question_version,required_role,allowed_actions_json,questions_json,
           security_level,status,decision_json,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "gate-no-questions",
            "project-1",
            "workflow-1",
            "PROJECT_GAP_RESOLUTION",
            "run-needs-input",
            1,
            context_hash,
            2,
            "PROJECT_OWNER",
            json.dumps(["CONFIRM"]),
            "[]",
            "INTERNAL",
            "OPEN",
            None,
            now,
            now,
        ),
    )

    with pytest.raises(ValueError, match="没有可回答的问题"):
        engine.decide_gate(
            "gate-no-questions",
            action="CONFIRM",
            decided_by="pytest",
            decided_role="PROJECT_OWNER",
            answers=[],
        )

    assert db.fetchone("SELECT status FROM gates WHERE id='gate-no-questions'") == {
        "status": "OPEN"
    }
