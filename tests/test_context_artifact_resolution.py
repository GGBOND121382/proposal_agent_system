from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.context_base import ContextBuilder
from app.db import Database
from app.util import sha256_json, utc_now


class _PackStub:
    pass


def _runtime(tmp_path: Path) -> tuple[Database, ContextBuilder]:
    db = Database(tmp_path / "runtime.sqlite3")
    now = utc_now()
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        ("project-1", "test", "test", "INTERNAL", "{}", now, now),
    )
    for workflow_id in ("workflow-1", "workflow-2"):
        db.execute(
            "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (workflow_id, "project-1", "WF-TEST", "RUNNING", 0, "{}", now, now),
        )
    return db, ContextBuilder(db, _PackStub())


def _resolution(
    *,
    resolution_id: str,
    answer: Any,
    target_paths: list[str],
    question_id: str = "question-1",
) -> dict[str, Any]:
    return {
        "resolution_id": resolution_id,
        "gate_id": "gate-1",
        "prompt_id": "P-TEST",
        "question_id": question_id,
        "question": "confirm",
        "target_paths": target_paths,
        "answer": answer,
        "decided_by": "pytest",
        "decided_role": "PROJECT_OWNER",
    }


def _insert_resolution(
    db: Database,
    *,
    artifact_id: str,
    workflow_id: str,
    prompt_id: str,
    version: int,
    resolution: dict[str, Any],
    scope_key: str | None = None,
    section_id: str | None = None,
) -> None:
    payload = {
        "schema_version": "1.0.0",
        "gate_id": "gate-1",
        "workflow_id": workflow_id,
        "prompt_id": prompt_id,
        "scope_key": scope_key,
        "section_id": section_id,
        "workflow_step": 0,
        "resolution": resolution,
        "authority": "HUMAN_GATE_DECISION",
        "supersedes_state_override": True,
    }
    db.execute(
        """INSERT INTO artifacts(id,project_id,workflow_id,artifact_type,prompt_id,version,status,security_level,context_hash,content_json,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            artifact_id,
            "project-1",
            workflow_id,
            "HUMAN_RESOLUTION",
            prompt_id,
            version,
            "PASS",
            "INTERNAL",
            sha256_json(payload),
            json.dumps(payload, ensure_ascii=False),
            utc_now(),
        ),
    )


def test_section_scoped_human_resolution_cannot_leak_to_another_section(
    tmp_path: Path,
) -> None:
    db, builder = _runtime(tmp_path)
    _insert_resolution(
        db,
        artifact_id="artifact-section-a",
        workflow_id="workflow-1",
        prompt_id="P-TEST",
        version=1,
        resolution=_resolution(
            resolution_id="resolution-section-a",
            answer="answer-a",
            target_paths=["payload.target"],
        ),
        scope_key="section:section-a:P-TEST",
        section_id="section-a",
    )
    state = {
        "active_section_id": "section-a",
        "human_resolution_artifact_ids": {
            "section:section-a:P-TEST": ["artifact-section-a"]
        },
    }
    assert builder._human_resolutions_for_prompt(
        state, "P-TEST", "workflow-1"
    )[0]["answer"] == "answer-a"

    state["active_section_id"] = "section-b"
    assert builder._human_resolutions_for_prompt(
        state, "P-TEST", "workflow-1"
    ) == []


def test_latest_applied_human_resolution_wins_per_target_path(tmp_path: Path) -> None:
    db, builder = _runtime(tmp_path)
    _insert_resolution(
        db,
        artifact_id="artifact-old",
        workflow_id="workflow-1",
        prompt_id="P-TEST",
        version=1,
        resolution=_resolution(
            resolution_id="resolution-old",
            answer="old",
            target_paths=["payload.target"],
        ),
    )
    _insert_resolution(
        db,
        artifact_id="artifact-new",
        workflow_id="workflow-1",
        prompt_id="P-TEST",
        version=2,
        resolution=_resolution(
            resolution_id="resolution-new",
            answer="new",
            target_paths=["payload.target"],
        ),
    )
    _insert_resolution(
        db,
        artifact_id="artifact-other-target",
        workflow_id="workflow-1",
        prompt_id="P-TEST",
        version=3,
        resolution=_resolution(
            resolution_id="resolution-other",
            answer="other",
            target_paths=["payload.other"],
            question_id="question-2",
        ),
    )

    state = {
        "human_resolution_artifact_ids": {
            "P-TEST": ["artifact-old", "artifact-new", "artifact-other-target"]
        }
    }
    resolutions = builder._human_resolutions_for_prompt(state, "P-TEST", "workflow-1")

    assert [(item["target_paths"], item["answer"]) for item in resolutions] == [
        (["/payload/target"], "new"),
        (["/payload/other"], "other"),
    ]


def test_human_resolution_artifact_scope_is_workflow_prompt_and_index_bound(tmp_path: Path) -> None:
    db, builder = _runtime(tmp_path)
    _insert_resolution(
        db,
        artifact_id="artifact-allowed",
        workflow_id="workflow-1",
        prompt_id="P-TEST",
        version=1,
        resolution=_resolution(
            resolution_id="resolution-allowed",
            answer="allowed",
            target_paths=["payload.target"],
        ),
    )
    _insert_resolution(
        db,
        artifact_id="artifact-other-workflow",
        workflow_id="workflow-2",
        prompt_id="P-TEST",
        version=2,
        resolution=_resolution(
            resolution_id="resolution-other-workflow",
            answer="wrong-workflow",
            target_paths=["payload.target"],
        ),
    )
    _insert_resolution(
        db,
        artifact_id="artifact-other-prompt",
        workflow_id="workflow-1",
        prompt_id="P-OTHER",
        version=2,
        resolution=_resolution(
            resolution_id="resolution-other-prompt",
            answer="wrong-prompt",
            target_paths=["payload.target"],
        ),
    )

    state = {"human_resolution_artifact_ids": {"P-TEST": ["artifact-allowed"]}}
    resolutions = builder._human_resolutions_for_prompt(state, "P-TEST", "workflow-1")

    assert [item["answer"] for item in resolutions] == ["allowed"]


def test_human_artifact_resolution_precedes_legacy_state_and_legacy_remains_read_only_fallback(
    tmp_path: Path,
) -> None:
    db, builder = _runtime(tmp_path)
    legacy = _resolution(
        resolution_id="resolution-legacy",
        answer="legacy",
        target_paths=["payload.target"],
    )
    _insert_resolution(
        db,
        artifact_id="artifact-current",
        workflow_id="workflow-1",
        prompt_id="P-TEST",
        version=1,
        resolution=_resolution(
            resolution_id="resolution-current",
            answer="artifact",
            target_paths=["payload.target"],
        ),
    )

    state = {
        "human_resolution_artifact_ids": {"P-TEST": ["artifact-current"]},
        "human_resolutions": {"P-TEST": [legacy]},
    }
    assert builder._human_resolutions_for_prompt(state, "P-TEST", "workflow-1")[0][
        "answer"
    ] == "artifact"
    assert state["human_resolutions"]["P-TEST"][0]["answer"] == "legacy"

    fallback = {"human_resolutions": {"P-TEST": [legacy]}}
    fallback_resolution = builder._human_resolutions_for_prompt(
        fallback, "P-TEST", "workflow-2"
    )[0]
    assert fallback_resolution["answer"] == "legacy"
    assert fallback_resolution["target_paths"] == ["/payload/target"]
    assert legacy["target_paths"] == ["payload.target"]


def test_human_resolution_lookup_skips_database_without_visibility_index(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db, builder = _runtime(tmp_path)
    legacy = _resolution(
        resolution_id="resolution-legacy",
        answer="legacy",
        target_paths=["payload.target"],
    )

    def unexpected_query(*args, **kwargs):
        raise AssertionError("artifact table must not be queried without a visibility index")

    monkeypatch.setattr(db, "fetchall", unexpected_query)
    state = {"human_resolutions": {"P-TEST": [legacy]}}

    resolution = builder._human_resolutions_for_prompt(
        state, "P-TEST", "workflow-1"
    )[0]
    assert resolution["answer"] == "legacy"
    assert resolution["target_paths"] == ["/payload/target"]
    assert legacy["target_paths"] == ["payload.target"]


def test_workflow_artifact_scope_is_cached_per_context_build(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from app.context_base import _WORKFLOW_ARTIFACT_SOURCE_CACHE

    _, builder = _runtime(tmp_path)
    calls = 0
    original = builder._workflow_lineage_ids

    def counted_lineage(workflow_id):
        nonlocal calls
        calls += 1
        return original(workflow_id)

    monkeypatch.setattr(builder, "_workflow_lineage_ids", counted_lineage)
    token = _WORKFLOW_ARTIFACT_SOURCE_CACHE.set({})
    try:
        first = builder._workflow_artifact_source_ids("workflow-1")
        second = builder._workflow_artifact_source_ids("workflow-1")
    finally:
        _WORKFLOW_ARTIFACT_SOURCE_CACHE.reset(token)

    assert first == ["workflow-1"]
    assert second == first
    assert calls == 1


def _insert_repair_application(
    db: Database,
    *,
    artifact_id: str,
    version: int,
    status: str,
    application_status: str,
    repaired_value: Any,
    workflow_id: str = "workflow-1",
) -> None:
    payload = {
        "schema_version": "1.0.0",
        "workflow_id": workflow_id,
        "producer_prompt": "P-WRITE-BLUEPRINT",
        "critic_prompt": "P-WRITE-BLUEPRINT-CRITIC",
        "target_key": "section:section-1:P-WRITE-BLUEPRINT",
        "section_id": "section-1",
        "repair_run_id": f"run-{version}",
        "application_status": application_status,
        "original_object_hash": "a" * 64,
        "repaired_value_hash": sha256_json(repaired_value),
        "repaired_value": repaired_value,
        "finding_codes": ["TEST"],
        "allowed_paths": ["/content"],
        "collection_key": None,
        "authority": "TARGETED_REPAIR_APPLICATION",
    }
    db.execute(
        """INSERT INTO artifacts(id,project_id,workflow_id,artifact_type,prompt_id,version,status,security_level,context_hash,content_json,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            artifact_id,
            "project-1",
            workflow_id,
            "REPAIR_APPLICATION",
            "P-WRITE-BLUEPRINT",
            version,
            status,
            "INTERNAL",
            sha256_json(payload),
            json.dumps(payload, ensure_ascii=False),
            utc_now(),
        ),
    )


def test_repair_application_context_uses_latest_pass_applied_artifact(tmp_path: Path) -> None:
    db, builder = _runtime(tmp_path)
    _insert_repair_application(
        db,
        artifact_id="repair-v1",
        version=1,
        status="PASS",
        application_status="APPLIED",
        repaired_value={"blueprint_id": "BP-1", "value": "accepted-old"},
    )
    _insert_repair_application(
        db,
        artifact_id="repair-v2-candidate",
        version=2,
        status="PASS",
        application_status="CANDIDATE",
        repaired_value={"blueprint_id": "BP-1", "value": "candidate"},
    )
    _insert_repair_application(
        db,
        artifact_id="repair-v3-revise",
        version=3,
        status="REVISE",
        application_status="APPLIED",
        repaired_value={"blueprint_id": "BP-1", "value": "rejected"},
    )
    _insert_repair_application(
        db,
        artifact_id="repair-v4",
        version=4,
        status="PASS",
        application_status="APPLIED",
        repaired_value={"blueprint_id": "BP-1", "value": "accepted-new"},
    )
    state = {
        "active_section_id": "section-1",
        "repair_application_artifact_ids": {
            "section:section-1:P-WRITE-BLUEPRINT": [
                "repair-v1",
                "repair-v2-candidate",
                "repair-v3-revise",
                "repair-v4",
            ]
        },
    }

    repaired = builder._repair_override(
        state,
        "P-WRITE-BLUEPRINT",
        workflow_id="workflow-1",
    )

    assert repaired == {"blueprint_id": "BP-1", "value": "accepted-new"}


def _insert_prompt_run(
    db: Database,
    *,
    run_id: str,
    prompt_id: str,
    input_payload: dict[str, Any],
    output_result: dict[str, Any] | None,
) -> None:
    input_json = {"payload": input_payload}
    output_json = None if output_result is None else {"status": "PASS", "result": output_result}
    db.execute(
        """INSERT INTO prompt_runs(
               id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,
               input_hash,output_hash,input_json,output_json,error,duration_ms,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            run_id,
            "project-1",
            "workflow-1",
            prompt_id,
            "PASS",
            "simulated",
            "offline-primary",
            sha256_json(input_json),
            sha256_json(output_json) if output_json is not None else None,
            json.dumps(input_json, ensure_ascii=False),
            json.dumps(output_json, ensure_ascii=False) if output_json is not None else None,
            None,
            1,
            utc_now(),
        ),
    )


def test_content_candidates_use_candidate_consumed_by_final_expression_review(
    tmp_path: Path,
) -> None:
    from app.context import ContextBuilder as RuntimeContextBuilder

    db, base_builder = _runtime(tmp_path)
    section = {"section_id": "section-1", "title": "研究内容"}
    accepted_candidate = {"candidate_id": "candidate-accepted", "paragraphs": []}
    later_unreviewed_candidate = {"candidate_id": "candidate-unreviewed", "paragraphs": []}

    _insert_prompt_run(
        db,
        run_id="run-content",
        prompt_id="P-WRITE-CONTENT",
        input_payload={"source_section": section},
        output_result={"candidate_id": "candidate-content", "paragraphs": []},
    )
    _insert_prompt_run(
        db,
        run_id="run-polish",
        prompt_id="P-EXPRESSION-POLISH",
        input_payload={"source_section": section},
        output_result=accepted_candidate,
    )
    _insert_prompt_run(
        db,
        run_id="run-final-review",
        prompt_id="P-EXPRESSION-CRITIC",
        input_payload={
            "source_section": section,
            "polished_candidate": accepted_candidate,
        },
        output_result={"verdict": "ACCEPT"},
    )
    _insert_prompt_run(
        db,
        run_id="run-later-unreviewed",
        prompt_id="P-EXPRESSION-POLISH",
        input_payload={"source_section": section},
        output_result=later_unreviewed_candidate,
    )
    section_results = [{
        "section_id": "section-1",
        "runs": [
            {"run_id": "run-content", "prompt_id": "P-WRITE-CONTENT", "status": "PASS"},
            {"run_id": "run-polish", "prompt_id": "P-EXPRESSION-POLISH", "status": "PASS"},
            {"run_id": "run-final-review", "prompt_id": "P-EXPRESSION-CRITIC", "status": "PASS"},
            {"run_id": "run-later-unreviewed", "prompt_id": "P-EXPRESSION-POLISH", "status": "PASS"},
        ],
    }]

    for builder in (base_builder, RuntimeContextBuilder(db, _PackStub())):
        selected = builder._content_candidates(
            "project-1",
            "workflow-1",
            section_results=section_results,
        )
        assert len(selected) == 1
        assert selected[0]["run_id"] == "run-final-review"
        assert selected[0]["candidate"] == accepted_candidate


def test_repair_application_lookup_uses_explicit_workflow_outside_build_scope(
    tmp_path: Path,
) -> None:
    db, builder = _runtime(tmp_path)
    _insert_repair_application(
        db,
        artifact_id="repair-workflow-1",
        version=1,
        status="PASS",
        application_status="APPLIED",
        repaired_value={"blueprint_id": "BP-WF1", "value": "workflow-1"},
        workflow_id="workflow-1",
    )
    _insert_repair_application(
        db,
        artifact_id="repair-workflow-2",
        version=1,
        status="PASS",
        application_status="APPLIED",
        repaired_value={"blueprint_id": "BP-WF2", "value": "workflow-2"},
        workflow_id="workflow-2",
    )
    state = {
        "active_section_id": "section-1",
        "repair_application_artifact_ids": {
            "section:section-1:P-WRITE-BLUEPRINT": [
                "repair-workflow-1",
                "repair-workflow-2",
            ]
        },
    }

    from app.context_base import _CURRENT_WORKFLOW_ID

    token = _CURRENT_WORKFLOW_ID.set("workflow-2")
    try:
        assert builder._repair_override(
            state,
            "P-WRITE-BLUEPRINT",
            workflow_id="workflow-1",
        ) == {"blueprint_id": "BP-WF1", "value": "workflow-1"}
    finally:
        _CURRENT_WORKFLOW_ID.reset(token)
    assert builder._repair_override(
        state,
        "P-WRITE-BLUEPRINT",
        workflow_id="workflow-2",
    ) == {"blueprint_id": "BP-WF2", "value": "workflow-2"}


def test_retargeted_question_supersedes_its_old_target_path(tmp_path: Path) -> None:
    db, builder = _runtime(tmp_path)
    _insert_resolution(
        db,
        artifact_id="artifact-old-target",
        workflow_id="workflow-1",
        prompt_id="P-TEST",
        version=1,
        resolution=_resolution(
            resolution_id="resolution-old-target",
            answer="old",
            target_paths=["payload.old_target"],
            question_id="same-question",
        ),
        scope_key="step:0:P-TEST",
    )
    _insert_resolution(
        db,
        artifact_id="artifact-new-target",
        workflow_id="workflow-1",
        prompt_id="P-TEST",
        version=2,
        resolution=_resolution(
            resolution_id="resolution-new-target",
            answer="new",
            target_paths=["payload.new_target"],
            question_id="same-question",
        ),
        scope_key="step:0:P-TEST",
    )
    state = {
        "human_resolution_artifact_ids": {
            "step:0:P-TEST": ["artifact-old-target", "artifact-new-target"]
        }
    }

    resolutions = builder._human_resolutions_for_prompt(
        state, "P-TEST", "workflow-1"
    )

    assert [(item["target_paths"], item["answer"]) for item in resolutions] == [
        (["/payload/new_target"], "new")
    ]


def test_authoritative_artifact_index_blocks_legacy_state_fallback(
    tmp_path: Path,
) -> None:
    _, builder = _runtime(tmp_path)
    legacy = _resolution(
        resolution_id="resolution-legacy",
        answer="legacy-must-not-resurrect",
        target_paths=["payload.target"],
    )
    state = {
        "human_resolution_artifact_ids": {
            "step:0:P-TEST": ["missing-or-rolled-back-artifact"]
        },
        "human_resolutions": {"P-TEST": [legacy]},
    }

    assert builder._human_resolutions_for_prompt(
        state, "P-TEST", "workflow-1"
    ) == []
