from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.db import Database
from app.util import new_id, utc_now
from app.workflow_lifecycle import WorkflowLifecycleService
from app.workflows import WorkflowEngine


REQUIRES = {
    "WF-1_PROJECT_INTAKE": [],
    "WF-2_TEMPLATE_EXTRACTION": [],
    "WF-3_HYBRID_ONLINE_ASSIST": ["WF-1_PROJECT_INTAKE"],
    "WF-4_PROPOSAL_AUTHORING": ["WF-1_PROJECT_INTAKE", "WF-2_TEMPLATE_EXTRACTION"],
    "WF-5_SECURITY_REVIEW_AND_EXPORT": ["WF-4_PROPOSAL_AUTHORING"],
}


def add_project(db: Database, name: str = "p") -> str:
    project_id = new_id("project")
    now = utc_now()
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (project_id, name, "", "INTERNAL", json.dumps({"require_public_research": False}), now, now),
    )
    return project_id


def add_workflow(
    db: Database,
    project_id: str,
    workflow_type: str,
    *,
    status: str = "COMPLETED",
    prerequisites: dict[str, str] | None = None,
    created_at: str | None = None,
) -> str:
    workflow_id = new_id("wf")
    now = created_at or utc_now()
    state = {
        "workflow_type": workflow_type,
        "options": {},
        "step_results": {},
        "prerequisite_workflow_ids": prerequisites or {},
    }
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (workflow_id, project_id, workflow_type, status, 0, json.dumps(state), now, now),
    )
    return workflow_id


class FakeRuntime:
    def _required_workflow_types(self, project_id, workflow_type, options):
        del project_id, options
        return list(REQUIRES[workflow_type])


class CompletingWorkflows:
    def __init__(self, db: Database):
        self.db = db
        self.runtime = FakeRuntime()
        self.start_calls: list[dict] = []

    def start(
        self,
        project_id,
        workflow_type,
        options=None,
        *,
        prerequisite_workflow_ids=None,
        lifecycle_context=None,
    ):
        workflow_id = new_id("wf")
        now = utc_now()
        state = {
            "workflow_type": workflow_type,
            "options": dict(options or {}),
            "step_results": {},
            "prerequisite_workflow_ids": dict(prerequisite_workflow_ids or {}),
            "prerequisite_binding_mode": "EXPLICIT_FROZEN",
            "workflow_lifecycle": dict(lifecycle_context or {}),
        }
        self.db.execute(
            "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (workflow_id, project_id, workflow_type, "RUNNING", 0, json.dumps(state), now, now),
        )
        self.start_calls.append(
            {
                "id": workflow_id,
                "workflow_type": workflow_type,
                "prerequisites": dict(prerequisite_workflow_ids or {}),
            }
        )
        return self.get(workflow_id)

    def get(self, workflow_id):
        row = self.db.fetchone("SELECT * FROM workflows WHERE id=?", (workflow_id,))
        if row is None:
            raise KeyError(workflow_id)
        row["state"] = json.loads(row.pop("state_json"))
        return row

    async def advance(self, workflow_id):
        row = self.get(workflow_id)
        self.db.execute(
            "UPDATE workflows SET status='COMPLETED',updated_at=? WHERE id=?",
            (utc_now(), workflow_id),
        )
        return self.get(workflow_id)


def test_rebuild_completed_wf3_cascades_with_frozen_branch_remap(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    project_id = add_project(db)
    wf1 = add_workflow(db, project_id, "WF-1_PROJECT_INTAKE")
    wf2 = add_workflow(db, project_id, "WF-2_TEMPLATE_EXTRACTION")
    wf3 = add_workflow(
        db,
        project_id,
        "WF-3_HYBRID_ONLINE_ASSIST",
        prerequisites={"WF-1_PROJECT_INTAKE": wf1},
    )
    wf4 = add_workflow(
        db,
        project_id,
        "WF-4_PROPOSAL_AUTHORING",
        prerequisites={
            "WF-1_PROJECT_INTAKE": wf1,
            "WF-2_TEMPLATE_EXTRACTION": wf2,
            "WF-3_HYBRID_ONLINE_ASSIST": wf3,
        },
    )
    wf5 = add_workflow(
        db,
        project_id,
        "WF-5_SECURITY_REVIEW_AND_EXPORT",
        prerequisites={"WF-4_PROPOSAL_AUTHORING": wf4},
    )
    workflows = CompletingWorkflows(db)
    service = WorkflowLifecycleService(db, workflows)

    operation = asyncio.run(service.rebuild(wf3))
    assert operation["status"] == "COMPLETED"
    nodes = operation["plan"]["nodes"]
    assert [node["source_workflow_id"] for node in nodes] == [wf3, wf4, wf5]
    new3, new4, new5 = [node["new_workflow_id"] for node in nodes]

    # Completed history is immutable.
    assert db.fetchone("SELECT status FROM workflows WHERE id=?", (wf3,))["status"] == "COMPLETED"
    assert db.fetchone("SELECT status FROM workflows WHERE id=?", (wf4,))["status"] == "COMPLETED"
    assert db.fetchone("SELECT status FROM workflows WHERE id=?", (wf5,))["status"] == "COMPLETED"

    assert workflows.get(new3)["state"]["prerequisite_workflow_ids"] == {
        "WF-1_PROJECT_INTAKE": wf1
    }
    assert workflows.get(new4)["state"]["prerequisite_workflow_ids"] == {
        "WF-1_PROJECT_INTAKE": wf1,
        "WF-2_TEMPLATE_EXTRACTION": wf2,
        "WF-3_HYBRID_ONLINE_ASSIST": new3,
    }
    assert workflows.get(new5)["state"]["prerequisite_workflow_ids"] == {
        "WF-4_PROPOSAL_AUTHORING": new4
    }

    lineage = db.fetchall(
        "SELECT parent_workflow_id,child_workflow_id,relation_type FROM workflow_lineage WHERE operation_id=? ORDER BY created_at,id",
        (operation["id"],),
    )
    assert [(row["parent_workflow_id"], row["child_workflow_id"], row["relation_type"]) for row in lineage] == [
        (wf3, new3, "RERUN_OF"),
        (wf4, new4, "REBUILD_OF"),
        (wf5, new5, "REBUILD_OF"),
    ]


def test_rebuild_active_source_restarts_and_cancels_only_source(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    project_id = add_project(db)
    wf1 = add_workflow(db, project_id, "WF-1_PROJECT_INTAKE")
    active = add_workflow(
        db,
        project_id,
        "WF-3_HYBRID_ONLINE_ASSIST",
        status="BLOCKED_CONTRACT",
        prerequisites={"WF-1_PROJECT_INTAKE": wf1},
    )
    workflows = CompletingWorkflows(db)
    service = WorkflowLifecycleService(db, workflows)

    operation = asyncio.run(service.rebuild(active, scope="SELF"))
    node = operation["plan"]["nodes"][0]
    assert node["relation_type"] == "RESTART_OF"
    old = db.fetchone("SELECT status,state_json FROM workflows WHERE id=?", (active,))
    assert old["status"] == "CANCELLED"
    marker = json.loads(old["state_json"])["cancelled_for_rebuild"]
    assert marker["operation_id"] == operation["id"]
    assert workflows.get(node["new_workflow_id"])["status"] == "COMPLETED"


def test_rebuild_selects_latest_downstream_consumer_only(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    project_id = add_project(db)
    wf1 = add_workflow(db, project_id, "WF-1_PROJECT_INTAKE")
    wf2 = add_workflow(db, project_id, "WF-2_TEMPLATE_EXTRACTION")
    wf3 = add_workflow(db, project_id, "WF-3_HYBRID_ONLINE_ASSIST", prerequisites={"WF-1_PROJECT_INTAKE": wf1})
    older = add_workflow(
        db,
        project_id,
        "WF-4_PROPOSAL_AUTHORING",
        prerequisites={"WF-1_PROJECT_INTAKE": wf1, "WF-2_TEMPLATE_EXTRACTION": wf2, "WF-3_HYBRID_ONLINE_ASSIST": wf3},
        created_at="2026-08-01T00:00:00+00:00",
    )
    newer = add_workflow(
        db,
        project_id,
        "WF-4_PROPOSAL_AUTHORING",
        prerequisites={"WF-1_PROJECT_INTAKE": wf1, "WF-2_TEMPLATE_EXTRACTION": wf2, "WF-3_HYBRID_ONLINE_ASSIST": wf3},
        created_at="2026-08-02T00:00:00+00:00",
    )
    workflows = CompletingWorkflows(db)
    service = WorkflowLifecycleService(db, workflows)

    operation = asyncio.run(service.rebuild(wf3))
    assert [node["source_workflow_id"] for node in operation["plan"]["nodes"]] == [wf3, newer]
    assert older in operation["plan"]["ignored_older_downstream_consumers"]


def test_explicit_prerequisite_start_never_resolves_to_newer_latest(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    project_id = add_project(db)
    wf1_old = add_workflow(db, project_id, "WF-1_PROJECT_INTAKE", created_at="2026-08-01T00:00:00+00:00")
    _wf1_new = add_workflow(db, project_id, "WF-1_PROJECT_INTAKE", created_at="2026-08-03T00:00:00+00:00")
    wf2 = add_workflow(db, project_id, "WF-2_TEMPLATE_EXTRACTION")
    engine = WorkflowEngine(db, SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace())

    created = engine.start(
        project_id,
        "WF-4_PROPOSAL_AUTHORING",
        {},
        prerequisite_workflow_ids={
            "WF-1_PROJECT_INTAKE": wf1_old,
            "WF-2_TEMPLATE_EXTRACTION": wf2,
        },
        lifecycle_context={"rebuild_operation_id": "rebuild-test"},
    )
    assert created["state"]["prerequisite_binding_mode"] == "EXPLICIT_FROZEN"
    assert created["state"]["prerequisite_workflow_ids"]["WF-1_PROJECT_INTAKE"] == wf1_old


def test_explicit_prerequisite_rejects_cross_project_binding(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    p1 = add_project(db, "p1")
    p2 = add_project(db, "p2")
    foreign_wf1 = add_workflow(db, p2, "WF-1_PROJECT_INTAKE")
    wf2 = add_workflow(db, p1, "WF-2_TEMPLATE_EXTRACTION")
    engine = WorkflowEngine(db, SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
    with pytest.raises(ValueError, match="跨项目"):
        engine.start(
            p1,
            "WF-4_PROPOSAL_AUTHORING",
            {},
            prerequisite_workflow_ids={
                "WF-1_PROJECT_INTAKE": foreign_wf1,
                "WF-2_TEMPLATE_EXTRACTION": wf2,
            },
        )


class BlockingOnceWorkflows(CompletingWorkflows):
    def __init__(self, db: Database):
        super().__init__(db)
        self.blocked_once = False

    async def advance(self, workflow_id):
        current = self.get(workflow_id)
        if not self.blocked_once:
            self.blocked_once = True
            self.db.execute(
                "UPDATE workflows SET status='BLOCKED_CONTRACT',updated_at=? WHERE id=?",
                (utc_now(), workflow_id),
            )
            return self.get(workflow_id)
        return await super().advance(workflow_id)


def test_resume_restarts_blocked_branch_node_then_continues(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    project_id = add_project(db)
    wf1 = add_workflow(db, project_id, "WF-1_PROJECT_INTAKE")
    wf3 = add_workflow(
        db,
        project_id,
        "WF-3_HYBRID_ONLINE_ASSIST",
        prerequisites={"WF-1_PROJECT_INTAKE": wf1},
    )
    workflows = BlockingOnceWorkflows(db)
    service = WorkflowLifecycleService(db, workflows)

    first = asyncio.run(service.rebuild(wf3, scope="SELF"))
    assert first["status"] == "PAUSED"
    blocked_id = first["plan"]["nodes"][0]["new_workflow_id"]
    assert workflows.get(blocked_id)["status"] == "BLOCKED_CONTRACT"

    resumed = asyncio.run(service.resume(first["id"]))
    assert resumed["status"] == "COMPLETED"
    node = resumed["plan"]["nodes"][0]
    replacement_id = node["new_workflow_id"]
    assert replacement_id != blocked_id
    assert workflows.get(blocked_id)["status"] == "CANCELLED"
    assert workflows.get(replacement_id)["status"] == "COMPLETED"
    assert node["restart_history"][0]["workflow_id"] == blocked_id
    restart_edge = db.fetchone(
        "SELECT parent_workflow_id,relation_type FROM workflow_lineage WHERE child_workflow_id=?",
        (replacement_id,),
    )
    assert restart_edge == {
        "parent_workflow_id": blocked_id,
        "relation_type": "RESTART_OF",
    }


def test_rebuild_without_auto_advance_still_creates_and_binds_root(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    project_id = add_project(db)
    wf1 = add_workflow(db, project_id, "WF-1_PROJECT_INTAKE")
    wf3 = add_workflow(
        db,
        project_id,
        "WF-3_HYBRID_ONLINE_ASSIST",
        prerequisites={"WF-1_PROJECT_INTAKE": wf1},
    )
    workflows = CompletingWorkflows(db)
    service = WorkflowLifecycleService(db, workflows)

    operation = asyncio.run(service.rebuild(wf3, scope="SELF", auto_advance=False))
    assert operation["status"] == "PAUSED"
    node = operation["plan"]["nodes"][0]
    assert node["new_workflow_id"]
    assert workflows.get(node["new_workflow_id"])["status"] == "RUNNING"
    assert node["new_status"] == "RUNNING"


def test_rebuild_of_lifecycle_branch_uses_exact_branch_membership(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    project_id = add_project(db)
    wf1 = add_workflow(db, project_id, "WF-1_PROJECT_INTAKE")
    wf2 = add_workflow(db, project_id, "WF-2_TEMPLATE_EXTRACTION")
    wf3 = add_workflow(db, project_id, "WF-3_HYBRID_ONLINE_ASSIST", prerequisites={"WF-1_PROJECT_INTAKE": wf1})
    wf4 = add_workflow(
        db,
        project_id,
        "WF-4_PROPOSAL_AUTHORING",
        prerequisites={"WF-1_PROJECT_INTAKE": wf1, "WF-2_TEMPLATE_EXTRACTION": wf2, "WF-3_HYBRID_ONLINE_ASSIST": wf3},
    )
    wf5 = add_workflow(db, project_id, "WF-5_SECURITY_REVIEW_AND_EXPORT", prerequisites={"WF-4_PROPOSAL_AUTHORING": wf4})
    workflows = CompletingWorkflows(db)
    service = WorkflowLifecycleService(db, workflows)

    first = asyncio.run(service.rebuild(wf3))
    new3, new4, new5 = [node["new_workflow_id"] for node in first["plan"]["nodes"]]

    second = asyncio.run(service.rebuild(new4))
    assert [node["source_workflow_id"] for node in second["plan"]["nodes"]] == [new4, new5]
    newer4, newer5 = [node["new_workflow_id"] for node in second["plan"]["nodes"]]
    assert workflows.get(newer4)["state"]["prerequisite_workflow_ids"] == {
        "WF-1_PROJECT_INTAKE": wf1,
        "WF-2_TEMPLATE_EXTRACTION": wf2,
        "WF-3_HYBRID_ONLINE_ASSIST": new3,
    }
    assert workflows.get(newer5)["state"]["prerequisite_workflow_ids"] == {
        "WF-4_PROPOSAL_AUTHORING": newer4
    }


def test_rebuild_refuses_unrelated_active_slot_before_mutation(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    project_id = add_project(db)
    wf1 = add_workflow(db, project_id, "WF-1_PROJECT_INTAKE")
    source = add_workflow(
        db,
        project_id,
        "WF-3_HYBRID_ONLINE_ASSIST",
        prerequisites={"WF-1_PROJECT_INTAKE": wf1},
    )
    unrelated = add_workflow(
        db,
        project_id,
        "WF-3_HYBRID_ONLINE_ASSIST",
        status="RUNNING",
        prerequisites={"WF-1_PROJECT_INTAKE": wf1},
    )
    workflows = CompletingWorkflows(db)
    service = WorkflowLifecycleService(db, workflows)

    with pytest.raises(ValueError, match="不属于本次源分支"):
        asyncio.run(service.rebuild(source, scope="SELF"))
    assert db.fetchone("SELECT status FROM workflows WHERE id=?", (source,))["status"] == "COMPLETED"
    assert db.fetchone("SELECT status FROM workflows WHERE id=?", (unrelated,))["status"] == "RUNNING"
    assert db.fetchone("SELECT COUNT(*) AS n FROM workflow_rebuild_operations")["n"] == 0


def test_list_operations_recovers_persisted_rebuild_for_frontend(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    project_id = add_project(db)
    wf1 = add_workflow(db, project_id, "WF-1_PROJECT_INTAKE")
    wf3 = add_workflow(
        db,
        project_id,
        "WF-3_HYBRID_ONLINE_ASSIST",
        prerequisites={"WF-1_PROJECT_INTAKE": wf1},
    )
    workflows = CompletingWorkflows(db)
    service = WorkflowLifecycleService(db, workflows)

    operation = asyncio.run(service.rebuild(wf3, scope="SELF"))
    listed = service.list_operations(project_id)

    assert listed
    assert listed[0]["id"] == operation["id"]
    assert listed[0]["project_id"] == project_id
    assert listed[0]["plan"]["root_source_workflow_id"] == wf3
    assert listed[0]["created_workflow_ids"] == operation["created_workflow_ids"]


def test_abort_rebuild_restores_source_checkpoint_atomically(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    project_id = add_project(db)
    wf1 = add_workflow(db, project_id, "WF-1_PROJECT_INTAKE")
    source = add_workflow(
        db,
        project_id,
        "WF-3_HYBRID_ONLINE_ASSIST",
        status="BLOCKED_CONTRACT",
        prerequisites={"WF-1_PROJECT_INTAKE": wf1},
    )
    source_row = db.fetchone("SELECT state_json FROM workflows WHERE id=?", (source,))
    source_state = json.loads(source_row["state_json"])
    source_state["checkpoint_sentinel"] = {"saved_sources": 80}
    db.execute(
        "UPDATE workflows SET current_step=5,state_json=? WHERE id=?",
        (json.dumps(source_state), source),
    )
    workflows = CompletingWorkflows(db)
    service = WorkflowLifecycleService(db, workflows)

    operation = asyncio.run(service.rebuild(source, scope="SELF", auto_advance=False))
    child_id = operation["plan"]["nodes"][0]["new_workflow_id"]
    assert operation["status"] == "PAUSED"
    assert workflows.get(source)["status"] == "CANCELLED"
    assert workflows.get(child_id)["status"] == "RUNNING"

    aborted = service.abort_and_restore(operation["id"])
    restored = workflows.get(source)
    assert aborted["status"] == "ABORTED"
    assert aborted["active_node"] is None
    assert restored["status"] == "BLOCKED_CONTRACT"
    assert restored["current_step"] == 5
    assert restored["state"]["checkpoint_sentinel"] == {"saved_sources": 80}
    assert "cancelled_for_rebuild" not in restored["state"]
    assert restored["state"]["rebuild_restore_history"][-1]["operation_id"] == operation["id"]
    assert workflows.get(child_id)["status"] == "CANCELLED"
    assert aborted["plan"]["abort"]["restored_source_workflow_ids"] == [source]
    assert aborted["plan"]["abort"]["cancelled_child_workflow_ids"] == [child_id]
    audit = db.fetchone(
        "SELECT event_type FROM audit_events WHERE object_id=? ORDER BY id DESC LIMIT 1",
        (operation["id"],),
    )
    assert audit["event_type"] == "WORKFLOW_REBUILD_ABORTED_AND_SOURCE_RESTORED"

    # The operation is idempotent and can never be resumed after restoration.
    assert service.abort_and_restore(operation["id"])["status"] == "ABORTED"
    with pytest.raises(ValueError, match="cannot be resumed"):
        asyncio.run(service.resume(operation["id"]))


def test_abort_rebuild_refuses_completed_replacement_without_mutation(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    project_id = add_project(db)
    wf1 = add_workflow(db, project_id, "WF-1_PROJECT_INTAKE")
    source = add_workflow(
        db,
        project_id,
        "WF-3_HYBRID_ONLINE_ASSIST",
        status="BLOCKED_CONTRACT",
        prerequisites={"WF-1_PROJECT_INTAKE": wf1},
    )
    workflows = CompletingWorkflows(db)
    service = WorkflowLifecycleService(db, workflows)
    operation = asyncio.run(service.rebuild(source, scope="SELF", auto_advance=False))
    child_id = operation["plan"]["nodes"][0]["new_workflow_id"]
    db.execute(
        "UPDATE workflows SET status='COMPLETED',updated_at=? WHERE id=?",
        (utc_now(), child_id),
    )

    with pytest.raises(ValueError, match="completed replacement"):
        service.abort_and_restore(operation["id"])

    assert workflows.get(source)["status"] == "CANCELLED"
    assert workflows.get(child_id)["status"] == "COMPLETED"
    assert service.get_operation(operation["id"])["status"] == "PAUSED"
