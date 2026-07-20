from __future__ import annotations

import json

from app.runtime_context import LiveContextBuilder
from app.util import utc_now
from tests.test_runtime import create_project, runtime


def test_live_task_identity_is_stable_within_attempt_and_rotates_between_attempts():
    common = {
        "prompt_id": "P-WRITE-CONTENT",
        "project_id": "project-1",
        "workflow_id": "wf-group-1",
        "active_section_id": "sec-1",
        "attempt": 1,
    }
    first = LiveContextBuilder._stable_task_id(
        **common, generation_attempt_id="generation-repair-a"
    )
    restarted = LiveContextBuilder._stable_task_id(
        **common, generation_attempt_id="generation-repair-a"
    )
    regenerated = LiveContextBuilder._stable_task_id(
        **common, generation_attempt_id="generation-repair-b"
    )
    assert first == restarted
    assert first != regenerated


def test_cross_section_repair_reset_is_idempotent_for_same_attempt(runtime, monkeypatch):
    *_, engine, _ = runtime
    updates: list[dict] = []

    def record_update(wf, **kwargs):
        updates.append({"workflow": wf, **kwargs})

    monkeypatch.setattr(engine, "_update", record_update)
    child = {
        "id": "wf-child",
        "status": "COMPLETED",
        "current_step": 6,
        "state": {
            "options": {"target_section_ids": ["s1", "s2"]},
            "generation_attempt_id": "generation-original",
            "section_results": [
                {"section_id": "s1", "status": "COMPLETED"},
                {"section_id": "s2", "status": "COMPLETED"},
            ],
            "section_progress": {
                "s1": {"phase": "DONE"},
                "s2": {"phase": "DONE"},
            },
            "repair_overrides": {
                "section:s1:P-WRITE-CONTENT": {"candidate_id": "old"},
                "section:s2:P-WRITE-CONTENT": {"candidate_id": "keep"},
            },
        },
    }
    parent_state = {
        "full_proposal_repair_attempt_id": "generation-repair-1",
        "integration_repair_findings": [{"code": "QG_DOCUMENT_TEMPLATE_REPETITION"}],
    }

    engine._reset_full_proposal_child_for_repair(
        child, parent_state, "wf-parent", {"s1"}
    )
    assert child["state"]["generation_attempt_id"] == "generation-repair-1"
    assert [item["section_id"] for item in child["state"]["section_results"]] == ["s2"]
    assert "s1" not in child["state"]["section_progress"]
    assert child["state"]["section_progress"]["s2"] == {"phase": "DONE"}
    assert "section:s1:P-WRITE-CONTENT" not in child["state"]["repair_overrides"]
    assert len(updates) == 1

    # Simulate progress made after restart. Re-entering the same repair round must
    # preserve it instead of clearing the section or rotating model-call identity.
    child["state"]["section_progress"]["s1"] = {"phase": "CONTENT_DONE"}
    engine._reset_full_proposal_child_for_repair(
        child, parent_state, "wf-parent", {"s1"}
    )
    assert child["state"]["section_progress"]["s1"] == {"phase": "CONTENT_DONE"}
    assert len(updates) == 1


def test_legacy_full_proposal_repair_collision_recovers_only_owned_repair(runtime):
    _, _, db, _, _, _, engine, _ = runtime
    project_id = create_project(db, internet=False)
    now = utc_now()
    parent_id = "wf-parent-legacy-repair"
    child_id = "wf-child-legacy-repair"
    child_state = {
        "workflow_type": "WF-4_PROPOSAL_AUTHORING",
        "parent_workflow_id": parent_id,
        "options": {"concurrent_group_child": True, "target_section_ids": ["s1"]},
    }
    parent_state = {
        "workflow_type": "WF-4_PROPOSAL_AUTHORING",
        "options": {
            "full_proposal_concurrent": True,
            "integration_scope": "FULL_PROPOSAL_CONCURRENT",
        },
        "integration_repair_section_ids": ["s1"],
        "integration_repair_findings": [{"code": "QG_DOCUMENT_TEMPLATE_REPETITION"}],
        "full_proposal_children": {
            "GROUP_1_BACKGROUND_AND_PROBLEM": {
                "workflow_id": child_id,
                "section_ids": ["s1"],
                "status": "BLOCKED",
            }
        },
        "last_error": (
            "完整申请书并发组失败：Fresh generation refused committed result reuse: call-old"
        ),
    }
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            child_id,
            project_id,
            "WF-4_PROPOSAL_AUTHORING",
            "BLOCKED",
            5,
            json.dumps(child_state),
            now,
            now,
        ),
    )
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            parent_id,
            project_id,
            "WF-4_PROPOSAL_AUTHORING",
            "BLOCKED",
            5,
            json.dumps(parent_state),
            now,
            now,
        ),
    )

    recovered = engine._recover_status(engine.get(parent_id))
    assert recovered["status"] == "RUNNING"
    assert recovered["state"]["recovered_from"] == "LEGACY_FULL_PROPOSAL_REPAIR_IDENTITY"
    assert recovered["state"]["full_proposal_repair_attempt_id"].startswith("generation-repair-")
    assert "last_error" not in recovered["state"]

    unrelated_id = "wf-unrelated-fresh-collision"
    unrelated_state = {
        "workflow_type": "WF-4_PROPOSAL_AUTHORING",
        "options": {},
        "last_error": "Fresh generation refused committed result reuse: call-unrelated",
    }
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            unrelated_id,
            project_id,
            "WF-4_PROPOSAL_AUTHORING",
            "BLOCKED",
            0,
            json.dumps(unrelated_state),
            now,
            now,
        ),
    )
    unchanged = engine._recover_status(engine.get(unrelated_id))
    assert unchanged["status"] == "BLOCKED"
    assert "full_proposal_repair_attempt_id" not in unchanged["state"]
