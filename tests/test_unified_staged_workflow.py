from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter

from app.db import Database
from app.staged_workflows import (
    RUN_ROOT_OWNER_FILE,
    STAGED_WORKFLOW_TYPE,
    StagedWorkflowCoordinator,
)
from app.unified_workflows import UnifiedWorkflowEngine
from app.util import utc_now


def _stage8_state(tmp_path):
    run_root = tmp_path / "run"
    source = run_root / "stage7" / "outputs" / "stage7_integrated_proposal.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Proposal\n", encoding="utf-8")
    return {
        "run_root": str(run_root),
        "current_stage": "stage7",
        "stage_runs": {"stage7": str(run_root / "stage7")},
        "step_results": {},
    }, source


def _write_minimal_docx(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body/></w:document>',
        )


def _write_minimal_pdf(path: Path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as handle:
        writer.write(handle)


def _write_complete_stage8(
    coordinator,
    state,
    source,
    *,
    workflow_id: str | None = None,
    project_id: str | None = None,
):
    run_dir = coordinator._run_dir(state, "stage8")
    outputs = run_dir / "outputs"
    outputs.mkdir(parents=True)
    docx = outputs / "proposal.docx"
    pdf = outputs / "proposal.pdf"
    _write_minimal_docx(docx)
    _write_minimal_pdf(pdf)
    metadata = {
        "metadata_version": "2.0",
        "input_file": str(source.resolve()),
        "input_size": source.stat().st_size,
        "input_sha256": coordinator._sha256(source),
        "docx_file": docx.name,
        "pdf_file": pdf.name,
        "docx_size": docx.stat().st_size,
        "pdf_size": pdf.stat().st_size,
        "docx_sha256": coordinator._sha256(docx),
        "pdf_sha256": coordinator._sha256(pdf),
        "page_count": 1,
    }
    if workflow_id:
        metadata["workflow_id"] = workflow_id
    if project_id:
        metadata["project_id"] = project_id
    metadata_path = outputs / "stage8_export_metadata.json"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    coordinator._write_stage8_completion(
        run_dir,
        metadata,
        workflow_id=workflow_id,
        project_id=project_id,
    )
    return run_dir


def _write_owner_marker(
    coordinator,
    state,
    *,
    workflow_id: str = "wf-stage8",
    project_id: str = "project-1",
) -> None:
    run_root = Path(state["run_root"]).resolve()
    coordinator._atomic_write_json(
        run_root / RUN_ROOT_OWNER_FILE,
        {
            "schema_version": "1.0",
            "workflow_id": workflow_id,
            "project_id": project_id,
            "run_root": str(run_root),
            "claimed_at": utc_now(),
        },
    )


def test_unified_engine_delegates_wf3b_topic_input_to_runtime() -> None:
    calls = []
    engine = object.__new__(UnifiedWorkflowEngine)
    engine.runtime = SimpleNamespace(
        provide_wf3b_topic=lambda workflow_id, topic: calls.append(
            (workflow_id, topic)
        )
        or {"id": workflow_id, "topic": topic}
    )

    result = engine.provide_wf3b_topic("wf-3b", "智慧水务")

    assert result == {"id": "wf-3b", "topic": "智慧水务"}
    assert calls == [("wf-3b", "智慧水务")]


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
    assert stored == {"workflow_type": STAGED_WORKFLOW_TYPE, "status": "WAITING_PROVIDER"}
    assert workflow["state"]["current_stage"] == "stage1"
    assert workflow["state"]["current_stage_state"]["status"] == "WAITING_MODEL"
    files = coordinator.files(workflow["id"])
    assert files["requests"]
    request = json.loads(open(files["requests"][0], encoding="utf-8").read())
    assert "动态项目标题" in json.dumps(request, ensure_ascii=False)


def test_staged_terminal_status_cannot_be_reopened_by_stale_stage_file(tmp_path):
    db = Database(tmp_path / "runtime.sqlite3")
    now = utc_now()
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        ("project-1", "动态项目标题", "test", "INTERNAL", json.dumps({}), now, now),
    )
    settings = SimpleNamespace(data_dir=tmp_path / "data", root_dir=tmp_path)
    coordinator = StagedWorkflowCoordinator(db, settings)
    workflow = coordinator.start("project-1")
    db.execute(
        "UPDATE workflows SET status='COMPLETED' WHERE id=?",
        (workflow["id"],),
    )

    latest = (
        tmp_path
        / "data"
        / "staged_workflows"
        / workflow["id"]
        / "stage1"
        / "LATEST_STATE.json"
    )
    latest.write_text(
        json.dumps({"status": "WAITING_MODEL", "phase": "STALE_MODEL_CALL"}),
        encoding="utf-8",
    )

    assert coordinator.get(workflow["id"])["status"] == "COMPLETED"


def test_staged_schema_block_maps_to_contract_block(tmp_path):
    db = Database(tmp_path / "runtime.sqlite3")
    now = utc_now()
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        ("project-1", "动态项目标题", "test", "INTERNAL", json.dumps({}), now, now),
    )
    settings = SimpleNamespace(data_dir=tmp_path / "data", root_dir=tmp_path)
    coordinator = StagedWorkflowCoordinator(db, settings)
    workflow = coordinator.start("project-1")
    latest = (
        tmp_path
        / "data"
        / "staged_workflows"
        / workflow["id"]
        / "stage1"
        / "LATEST_STATE.json"
    )
    latest.write_text(
        json.dumps({"status": "BLOCKED", "phase": "OUTPUT_SCHEMA_FAILED"}),
        encoding="utf-8",
    )

    assert coordinator.get(workflow["id"])["status"] == "BLOCKED_CONTRACT"


def test_unknown_staged_status_fails_closed(tmp_path):
    db = Database(tmp_path / "runtime.sqlite3")
    now = utc_now()
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        ("project-1", "动态项目标题", "test", "INTERNAL", json.dumps({}), now, now),
    )
    settings = SimpleNamespace(data_dir=tmp_path / "data", root_dir=tmp_path)
    coordinator = StagedWorkflowCoordinator(db, settings)
    workflow = coordinator.start("project-1")
    latest = (
        tmp_path / "data" / "staged_workflows" / workflow["id"] / "stage1" / "LATEST_STATE.json"
    )
    latest.write_text(
        json.dumps({"status": "WAITING_SOMETHING_NEW", "phase": "UNKNOWN_PHASE"}),
        encoding="utf-8",
    )

    blocked = coordinator.get(workflow["id"])

    assert blocked["status"] == "BLOCKED_TECHNICAL"
    assert blocked["state"]["staged_status_error"]["internal_status"] == "WAITING_SOMETHING_NEW"


def test_stage8_reuses_complete_crash_left_export(tmp_path):
    settings = SimpleNamespace(data_dir=tmp_path / "data", root_dir=tmp_path)
    coordinator = StagedWorkflowCoordinator(Database(tmp_path / "runtime.sqlite3"), settings)
    state, source = _stage8_state(tmp_path)
    run_dir = _write_complete_stage8(coordinator, state, source)
    sentinel = run_dir / "outputs" / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")

    prepared, reused = coordinator._prepare_stage8_dir(state)

    assert prepared == run_dir
    assert reused is True
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_stage8_rebuilds_only_incomplete_private_directory(tmp_path):
    settings = SimpleNamespace(data_dir=tmp_path / "data", root_dir=tmp_path)
    coordinator = StagedWorkflowCoordinator(Database(tmp_path / "runtime.sqlite3"), settings)
    state, _source = _stage8_state(tmp_path)
    run_dir = coordinator._run_dir(state, "stage8")
    run_dir.mkdir(parents=True)
    (run_dir / "partial.tmp").write_text("partial", encoding="utf-8")

    prepared, reused = coordinator._prepare_stage8_dir(state)

    assert prepared == run_dir
    assert reused is False
    assert prepared.is_dir()
    assert list(prepared.iterdir()) == []


def test_stage8_does_not_reuse_outputs_for_changed_source(tmp_path):
    settings = SimpleNamespace(data_dir=tmp_path / "data", root_dir=tmp_path)
    coordinator = StagedWorkflowCoordinator(Database(tmp_path / "runtime.sqlite3"), settings)
    state, source = _stage8_state(tmp_path)
    run_dir = _write_complete_stage8(coordinator, state, source)
    source.write_text("# Changed proposal\n", encoding="utf-8")

    prepared, reused = coordinator._prepare_stage8_dir(state)

    assert prepared == run_dir
    assert reused is False
    assert list(prepared.iterdir()) == []


def test_stage8_refuses_symlinked_recovery_directory(tmp_path, monkeypatch):
    settings = SimpleNamespace(data_dir=tmp_path / "data", root_dir=tmp_path)
    coordinator = StagedWorkflowCoordinator(Database(tmp_path / "runtime.sqlite3"), settings)
    state, _source = _stage8_state(tmp_path)
    run_dir = coordinator._run_dir(state, "stage8")
    run_dir.mkdir(parents=True)
    real_is_symlink = Path.is_symlink

    def fake_is_symlink(path: Path) -> bool:
        if path == run_dir:
            return True
        return real_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)

    with pytest.raises(RuntimeError, match="symlinked stage directory"):
        coordinator._prepare_stage8_dir(state)


def test_stage8_initialization_adopts_completed_export_without_subprocess(tmp_path, monkeypatch):
    db = Database(tmp_path / "runtime.sqlite3")
    now = utc_now()
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        ("project-1", "Proposal", "test", "INTERNAL", json.dumps({}), now, now),
    )
    settings = SimpleNamespace(data_dir=tmp_path / "data", root_dir=tmp_path)
    coordinator = StagedWorkflowCoordinator(db, settings)
    state, source = _stage8_state(tmp_path)
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            "wf-stage8",
            "project-1",
            STAGED_WORKFLOW_TYPE,
            "RUNNING",
            10,
            json.dumps(state),
            now,
            now,
        ),
    )
    _write_owner_marker(coordinator, state)
    _write_complete_stage8(
        coordinator,
        state,
        source,
        workflow_id="wf-stage8",
        project_id="project-1",
    )

    def unexpected_subprocess(*_args, **_kwargs):
        raise AssertionError("complete Stage 8 output must be adopted without rerunning export")

    monkeypatch.setattr("app.staged_workflows.subprocess.run", unexpected_subprocess)

    coordinator._initialize_stage("wf-stage8", state, "stage8")
    workflow = coordinator.get("wf-stage8")

    assert workflow["status"] == "COMPLETED"
    assert workflow["state"]["current_stage"] == "stage8"
    assert workflow["current_step"] == 11
