from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.candidate_integrity import canonical_candidate, visible_candidate_snapshot, visible_document_snapshot
from app.context_base import ContextBuilder
from app.db import Database
from app.exporter_base import ExportBaseMixin, ExportDenied
from app.util import sha256_json, utc_now


def _candidate(candidate_id: str, first: str = "第一段", second: str = "第二段") -> dict:
    return {
        "candidate_id": candidate_id,
        "candidate_text": f"{first}\n\n{second}",
        "paragraphs": [
            {"paragraph_id": f"{candidate_id}-p2", "sequence": 2, "text": second},
            {"paragraph_id": f"{candidate_id}-p1", "sequence": 1, "text": first},
        ],
        "trace_links": [],
        "term_usage": [],
        "unresolved_items": [],
        "claim_advancement": {},
    }


def _seed_project(db: Database, project_id: str = "project-1") -> None:
    now = utc_now()
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (project_id, "项目", "", "INTERNAL", "{}", now, now),
    )


def _insert_run(
    db: Database,
    *,
    run_id: str,
    project_id: str,
    workflow_id: str,
    prompt_id: str,
    section_id: str,
    candidate: dict,
    created_at: str,
) -> None:
    payload = {"source_section": {"section_id": section_id, "title": "研究内容"}}
    if prompt_id == "P-EXPRESSION-CRITIC":
        payload["polished_candidate"] = candidate
        result = {"verdict": "ACCEPT"}
    else:
        result = candidate
    envelope = {"payload": payload}
    output = {"status": "PASS", "result": result}
    db.execute(
        """INSERT INTO prompt_runs(
               id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,
               input_hash,output_hash,input_json,output_json,error,duration_ms,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            run_id,
            project_id,
            workflow_id,
            prompt_id,
            "PASS",
            "test",
            "offline",
            sha256_json(envelope),
            sha256_json(output),
            json.dumps(envelope, ensure_ascii=False),
            json.dumps(output, ensure_ascii=False),
            None,
            1,
            created_at,
        ),
    )


def test_final_review_candidate_source_uses_frozen_authoring_index(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    _seed_project(db)
    frozen = _candidate("candidate-frozen")
    newer = _candidate("candidate-newer", "新第一段", "新第二段")
    now = utc_now()
    section_results = [
        {
            "section_id": "section-1",
            "title": "研究内容",
            "status": "COMPLETED",
            "runs": [
                {"prompt_id": "P-EXPRESSION-POLISH", "run_id": "polish-frozen", "status": "PASS"},
                {"prompt_id": "P-EXPRESSION-CRITIC", "run_id": "critic-frozen", "status": "PASS"},
            ],
        }
    ]
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            "wf-authoring",
            "project-1",
            "WF-4_PROPOSAL_AUTHORING",
            "COMPLETED",
            7,
            json.dumps({"section_results": section_results}, ensure_ascii=False),
            now,
            now,
        ),
    )
    _insert_run(
        db,
        run_id="polish-frozen",
        project_id="project-1",
        workflow_id="wf-authoring",
        prompt_id="P-EXPRESSION-POLISH",
        section_id="section-1",
        candidate=frozen,
        created_at="2026-08-01T00:00:00+00:00",
    )
    _insert_run(
        db,
        run_id="critic-frozen",
        project_id="project-1",
        workflow_id="wf-authoring",
        prompt_id="P-EXPRESSION-CRITIC",
        section_id="section-1",
        candidate=frozen,
        created_at="2026-08-01T00:00:01+00:00",
    )
    _insert_run(
        db,
        run_id="polish-newer",
        project_id="project-1",
        workflow_id="wf-unbound",
        prompt_id="P-EXPRESSION-POLISH",
        section_id="section-1",
        candidate=newer,
        created_at="2026-08-02T00:00:00+00:00",
    )

    builder = ContextBuilder(db, SimpleNamespace())
    authoring_id, frozen_results = builder._bound_authoring_section_results(
        "project-1",
        {"prerequisite_workflow_ids": {"WF-4_PROPOSAL_AUTHORING": "wf-authoring"}},
    )
    selected = builder._content_candidates(
        "project-1",
        authoring_id,
        section_results=frozen_results,
    )
    global_latest = builder._content_candidates("project-1")

    assert selected[0]["candidate"]["candidate_id"] == "candidate-frozen"
    assert global_latest[0]["candidate"]["candidate_id"] == "candidate-newer"


def _seed_completed_wf5(
    db: Database,
    *,
    workflow_id: str,
    candidate: dict,
    content_gate: bool = True,
    export_gate: bool = True,
) -> dict:
    now = utc_now()
    snapshot = visible_document_snapshot(
        [{"section_id": "section-1", "title": "研究内容", "candidate": candidate}]
    )
    candidate_set_snapshot = visible_candidate_snapshot(
        [{"section_id": "section-1", "title": "研究内容", "candidate": candidate}]
    )
    state = {
        "final_review_run_id": f"review-{workflow_id}",
        "final_review_candidate_snapshot": snapshot,
        "final_review_candidate_set_snapshot": candidate_set_snapshot,
        "prerequisite_workflow_ids": {"WF-4_PROPOSAL_AUTHORING": "wf-authoring"},
    }
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            workflow_id,
            "project-1",
            "WF-5_SECURITY_REVIEW_AND_EXPORT",
            "COMPLETED",
            2,
            json.dumps(state, ensure_ascii=False),
            now,
            now,
        ),
    )
    gate_values = (
        "PROJECT_OWNER",
        json.dumps(["APPROVE"]),
        "[]",
        "INTERNAL",
        "APPROVED",
        json.dumps({"action": "APPROVE"}),
        now,
        now,
    )
    if content_gate:
        db.execute(
            """INSERT INTO gates(
                   id,project_id,workflow_id,gate_type,target_id,target_version,context_hash,
                   question_version,required_role,allowed_actions_json,questions_json,security_level,
                   status,decision_json,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"content-{workflow_id}", "project-1", workflow_id,
                "FINAL_CONTENT_SECURITY_APPROVAL", f"review-{workflow_id}", 1, "h", 2,
                *gate_values,
            ),
        )
    if export_gate:
        db.execute(
            """INSERT INTO gates(
                   id,project_id,workflow_id,gate_type,target_id,target_version,context_hash,
                   question_version,required_role,allowed_actions_json,questions_json,security_level,
                   status,decision_json,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"export-{workflow_id}", "project-1", workflow_id,
                "FINAL_EXPORT_APPROVAL", workflow_id, 1, "h", 2,
                *gate_values,
            ),
        )
    return snapshot


def _export_candidate(candidate: dict) -> dict:
    canonical = canonical_candidate(candidate)
    return {
        "section_id": "section-1",
        "section_title": "研究内容",
        "candidate_id": canonical["candidate_id"],
        "run_id": "polish-1",
        "expression_critic_run_id": "critic-1",
        "paragraphs": [item["text"] for item in canonical["paragraphs"]],
        "paragraph_ids": [item["paragraph_id"] for item in canonical["paragraphs"]],
    }


def test_export_rejects_candidate_drift_after_final_review(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    _seed_project(db)
    reviewed = _candidate("candidate-reviewed")
    changed = _candidate("candidate-changed", "改变后的第一段", "改变后的第二段")
    _seed_completed_wf5(db, workflow_id="wf5", candidate=reviewed)
    exporter = ExportBaseMixin(db, SimpleNamespace(data_dir=tmp_path, exports_dir=tmp_path))
    gates = exporter._approved_gate_ids("project-1")

    exporter._assert_reviewed_candidate_snapshot(
        "project-1",
        [_export_candidate(reviewed)],
        gates,
    )
    with pytest.raises(ExportDenied, match="changed after final confidentiality review"):
        exporter._assert_reviewed_candidate_snapshot(
            "project-1",
            [_export_candidate(changed)],
            gates,
        )


def test_export_approval_gates_cannot_be_spliced_across_wf5_runs(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    _seed_project(db)
    reviewed = _candidate("candidate-reviewed")
    _seed_completed_wf5(db, workflow_id="wf5-old", candidate=reviewed, export_gate=False)
    _seed_completed_wf5(db, workflow_id="wf5-new", candidate=reviewed, content_gate=False)
    exporter = ExportBaseMixin(db, SimpleNamespace(data_dir=tmp_path, exports_dir=tmp_path))

    with pytest.raises(ExportDenied, match="same WF-5 workflow"):
        exporter._approved_gate_ids("project-1")


def test_visible_snapshot_uses_sequence_order_and_candidate_identity() -> None:
    first = _candidate("candidate-1")
    reordered = dict(first)
    reordered["paragraphs"] = list(reversed(first["paragraphs"]))
    same = visible_candidate_snapshot(
        [{"section_id": "s1", "title": "标题", "candidate": first}]
    )
    reordered_snapshot = visible_candidate_snapshot(
        [{"section_id": "s1", "title": "标题", "candidate": reordered}]
    )
    changed_id = visible_candidate_snapshot(
        [{"section_id": "s1", "title": "标题", "candidate": {**first, "candidate_id": "candidate-2"}}]
    )

    assert same["visible_candidate_set_hash"] == reordered_snapshot["visible_candidate_set_hash"]
    assert sha256_json(canonical_candidate(first)) == sha256_json(canonical_candidate(reordered))
    assert same["visible_candidate_set_hash"] != changed_id["visible_candidate_set_hash"]


def test_export_rejects_same_text_with_replaced_candidate_identity(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    _seed_project(db)
    reviewed = _candidate("candidate-reviewed")
    replacement = _candidate("candidate-replacement")
    replacement["paragraphs"] = [dict(item) for item in reviewed["paragraphs"]]
    replacement["candidate_text"] = reviewed["candidate_text"]
    _seed_completed_wf5(db, workflow_id="wf5", candidate=reviewed)
    exporter = ExportBaseMixin(db, SimpleNamespace(data_dir=tmp_path, exports_dir=tmp_path))
    gates = exporter._approved_gate_ids("project-1")

    assert visible_document_snapshot([_export_candidate(reviewed)])["reviewed_document_hash"] == (
        visible_document_snapshot([_export_candidate(replacement)])["reviewed_document_hash"]
    )
    with pytest.raises(ExportDenied, match="changed after final confidentiality review"):
        exporter._assert_reviewed_candidate_snapshot(
            "project-1",
            [_export_candidate(replacement)],
            gates,
        )


def test_reviewed_document_hash_binds_paragraph_block_identity() -> None:
    candidate = _candidate("candidate-1")
    original = _export_candidate(candidate)
    changed = dict(original)
    changed["paragraph_ids"] = ["replacement-p1", "replacement-p2"]

    assert (
        visible_document_snapshot([original])["reviewed_document_hash"]
        != visible_document_snapshot([changed])["reviewed_document_hash"]
    )
