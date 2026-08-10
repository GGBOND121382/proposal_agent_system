from __future__ import annotations

import asyncio
import hashlib
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter

from app.db import Database
from app.staged_workflows import RUN_ROOT_OWNER_FILE, STAGED_WORKFLOW_TYPE, StagedWorkflowCoordinator
from app.util import utc_now
from stage8_tools import export_final as exporter


def _coordinator(tmp_path: Path) -> StagedWorkflowCoordinator:
    settings = SimpleNamespace(data_dir=tmp_path / "data", root_dir=tmp_path)
    return StagedWorkflowCoordinator(Database(tmp_path / "runtime.sqlite3"), settings)


def _state(tmp_path: Path) -> tuple[dict, Path]:
    run_root = tmp_path / "run"
    source = run_root / "stage7" / "outputs" / "stage7_integrated_proposal.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Proposal\n", encoding="utf-8")
    return {
        "run_root": str(run_root),
        "current_stage": "stage8",
        "stage_runs": {"stage7": str(run_root / "stage7")},
        "step_results": {},
    }, source


def _minimal_docx(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body/></w:document>',
        )


def _minimal_pdf(path: Path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as handle:
        writer.write(handle)


def _write_bundle(
    coordinator: StagedWorkflowCoordinator,
    state: dict,
    source: Path,
    outputs: Path,
    *,
    workflow_id: str | None = None,
    project_id: str | None = None,
) -> dict:
    outputs.mkdir(parents=True)
    docx = outputs / "proposal.docx"
    pdf = outputs / "proposal.pdf"
    _minimal_docx(docx)
    _minimal_pdf(pdf)
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
    (outputs / "stage8_export_metadata.json").write_text(
        json.dumps(metadata), encoding="utf-8"
    )
    return metadata


def _complete_bundle(
    coordinator: StagedWorkflowCoordinator,
    state: dict,
    source: Path,
    *,
    workflow_id: str | None = None,
    project_id: str | None = None,
) -> Path:
    run_dir = coordinator._run_dir(state, "stage8")
    metadata = _write_bundle(
        coordinator,
        state,
        source,
        run_dir / "outputs",
        workflow_id=workflow_id,
        project_id=project_id,
    )
    coordinator._write_stage8_completion(
        run_dir,
        metadata,
        workflow_id=workflow_id,
        project_id=project_id,
    )
    return run_dir


def _owner_marker(
    coordinator: StagedWorkflowCoordinator,
    state: dict,
    *,
    workflow_id: str = "wf-stage8",
    project_id: str = "project-1",
) -> None:
    root = Path(state["run_root"]).resolve()
    coordinator._atomic_write_json(
        root / RUN_ROOT_OWNER_FILE,
        {
            "schema_version": "1.0",
            "workflow_id": workflow_id,
            "project_id": project_id,
            "run_root": str(root),
            "claimed_at": utc_now(),
        },
    )


def _insert_project_and_workflow(
    coordinator: StagedWorkflowCoordinator,
    state: dict,
    *,
    status: str = "RUNNING",
) -> None:
    now = utc_now()
    coordinator.db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        ("project-1", "Proposal", "test", "INTERNAL", json.dumps({}), now, now),
    )
    coordinator.db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            "wf-stage8",
            "project-1",
            STAGED_WORKFLOW_TYPE,
            status,
            11,
            json.dumps(state),
            now,
            now,
        ),
    )


@pytest.mark.parametrize("mutation", ["zero_docx", "tampered_pdf", "extra_docx"])
def test_stage8_never_reuses_incomplete_or_ambiguous_delivery(tmp_path: Path, mutation: str) -> None:
    coordinator = _coordinator(tmp_path)
    state, source = _state(tmp_path)
    run_dir = _complete_bundle(coordinator, state, source)
    if mutation == "zero_docx":
        (run_dir / "outputs" / "proposal.docx").write_bytes(b"")
    elif mutation == "tampered_pdf":
        with (run_dir / "outputs" / "proposal.pdf").open("ab") as handle:
            handle.write(b"tampered")
    else:
        _minimal_docx(run_dir / "outputs" / "unexpected.docx")

    prepared, reused = coordinator._prepare_stage8_dir(state)

    assert prepared == run_dir
    assert reused is False
    assert list(prepared.iterdir()) == []


@pytest.mark.parametrize("kind", ["docx", "pdf"])
def test_stage8_rejects_structurally_corrupt_files_even_after_rehash(
    tmp_path: Path,
    kind: str,
) -> None:
    coordinator = _coordinator(tmp_path)
    state, source = _state(tmp_path)
    run_dir = _complete_bundle(coordinator, state, source)
    metadata_path = run_dir / "outputs" / "stage8_export_metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    target = run_dir / "outputs" / f"proposal.{kind}"
    target.write_bytes(f"not-a-{kind}".encode())
    metadata[f"{kind}_size"] = target.stat().st_size
    metadata[f"{kind}_sha256"] = coordinator._sha256(target)
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    coordinator._write_stage8_completion(
        run_dir,
        metadata,
        workflow_id=None,
        project_id=None,
    )

    _prepared, reused = coordinator._prepare_stage8_dir(state)

    assert reused is False


def test_stage8_adopts_verified_outputs_when_completion_state_is_missing(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    state, source = _state(tmp_path)
    run_dir = _complete_bundle(coordinator, state, source)
    (run_dir / "LATEST_STATE.json").unlink()

    prepared, reused = coordinator._prepare_stage8_dir(state)

    assert prepared == run_dir
    assert reused is True
    assert json.loads((run_dir / "LATEST_STATE.json").read_text())["status"] == "COMPLETED"


def test_stage8_adopts_verified_pending_outputs_after_publish_interruption(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    state, source = _state(tmp_path)
    run_dir = coordinator._run_dir(state, "stage8")
    _write_bundle(coordinator, state, source, run_dir / ".outputs.pending")

    prepared, reused = coordinator._prepare_stage8_dir(state)

    assert prepared == run_dir
    assert reused is True
    assert (run_dir / "outputs" / "proposal.docx").is_file()
    assert not (run_dir / ".outputs.pending").exists()


def test_running_stage8_with_invalid_delivery_requires_rebuild(tmp_path: Path, monkeypatch) -> None:
    coordinator = _coordinator(tmp_path)
    state, source = _state(tmp_path)
    _owner_marker(coordinator, state)
    _insert_project_and_workflow(coordinator, state)
    run_dir = _complete_bundle(
        coordinator,
        state,
        source,
        workflow_id="wf-stage8",
        project_id="project-1",
    )
    (run_dir / "outputs" / "proposal.pdf").write_bytes(b"corrupt")

    workflow = coordinator.get("wf-stage8")
    assert workflow["status"] == "RUNNING"
    assert workflow["state"]["stage8_recovery_required"]

    def fail_rebuild(*_args, **_kwargs):
        raise RuntimeError("rebuild failed")

    monkeypatch.setattr(coordinator, "_initialize_stage", fail_rebuild)
    blocked = asyncio.run(coordinator.advance("wf-stage8"))
    assert blocked["status"] == "BLOCKED_TECHNICAL"
    assert "rebuild failed" in blocked["state"]["last_error"]


def test_completed_stage8_reports_corruption_without_reopening(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    state, source = _state(tmp_path)
    _owner_marker(coordinator, state)
    _insert_project_and_workflow(coordinator, state, status="COMPLETED")
    run_dir = _complete_bundle(
        coordinator,
        state,
        source,
        workflow_id="wf-stage8",
        project_id="project-1",
    )
    (run_dir / "outputs" / "proposal.docx").write_bytes(b"")

    workflow = coordinator.get("wf-stage8")

    assert workflow["status"] == "COMPLETED"
    assert workflow["delivery_integrity"]["valid"] is False


def test_custom_run_root_cannot_be_shared_by_two_workflows(tmp_path: Path) -> None:
    db = Database(tmp_path / "runtime.sqlite3")
    now = utc_now()
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        ("project-1", "Proposal", "test", "INTERNAL", json.dumps({}), now, now),
    )
    settings = SimpleNamespace(data_dir=tmp_path / "data", root_dir=tmp_path)
    coordinator = StagedWorkflowCoordinator(db, settings)
    shared = tmp_path / "shared"

    first = coordinator.start("project-1", {"run_root": str(shared)})
    with pytest.raises(RuntimeError, match="already owned by another workflow"):
        coordinator.start("project-1", {"run_root": str(shared)})

    owner = json.loads((shared / RUN_ROOT_OWNER_FILE).read_text(encoding="utf-8"))
    assert owner["workflow_id"] == first["id"]


def test_nonempty_unowned_custom_run_root_is_preserved(tmp_path: Path) -> None:
    db = Database(tmp_path / "runtime.sqlite3")
    now = utc_now()
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        ("project-1", "Proposal", "test", "INTERNAL", json.dumps({}), now, now),
    )
    settings = SimpleNamespace(data_dir=tmp_path / "data", root_dir=tmp_path)
    coordinator = StagedWorkflowCoordinator(db, settings)
    custom = tmp_path / "human-directory"
    custom.mkdir()
    sentinel = custom / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(RuntimeError, match="non-empty unowned"):
        coordinator.start("project-1", {"run_root": str(custom)})

    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert db.fetchone("SELECT COUNT(*) AS count FROM workflows")["count"] == 0


def test_stage8_exporter_freezes_source_bytes_before_rendering(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "proposal.md"
    original = b"# Original\n"
    source.write_bytes(original)
    output = tmp_path / "outputs"
    rendered: dict[str, bytes] = {}

    def fake_build_docx(md_path: Path, out_docx: Path, _asset_dir: Path) -> dict:
        rendered["source"] = md_path.read_bytes()
        source.write_text("# Changed during export\n", encoding="utf-8")
        _minimal_docx(out_docx)
        return {
            "docx_sha256": exporter.sha256(out_docx),
            "docx_size": out_docx.stat().st_size,
            "title": "Original",
            "chapter_count": 1,
            "table_count": 0,
            "figure_count": 0,
        }

    def fake_convert(_docx_path: Path, out_pdf: Path) -> dict:
        _minimal_pdf(out_pdf)
        return {
            "pdf_sha256": exporter.sha256(out_pdf),
            "pdf_size": out_pdf.stat().st_size,
            "page_count": 1,
            "stdout": "",
            "stderr": "",
        }

    monkeypatch.setattr(exporter, "build_docx", fake_build_docx)
    monkeypatch.setattr(exporter, "convert_pdf", fake_convert)
    monkeypatch.setattr(exporter, "page_locations", lambda *_args, **_kwargs: {})

    metadata = exporter.export_final(
        source,
        output,
        workflow_id="wf-1",
        project_id="project-1",
    )

    assert rendered["source"] == original
    assert metadata["input_sha256"] == hashlib.sha256(original).hexdigest()
    assert metadata["input_size"] == len(original)
    assert source.read_bytes() != original
    assert metadata["workflow_id"] == "wf-1"
    assert metadata["project_id"] == "project-1"


def test_stage8_initialization_publishes_verified_pending_bundle(tmp_path: Path, monkeypatch) -> None:
    coordinator = _coordinator(tmp_path)
    state, source = _state(tmp_path)
    state["current_stage"] = "stage7"
    _owner_marker(coordinator, state)
    _insert_project_and_workflow(coordinator, state)

    def fake_subprocess(cmd, **_kwargs):
        pending = Path(cmd[cmd.index("--out-dir") + 1])
        workflow_id = cmd[cmd.index("--workflow-id") + 1]
        project_id = cmd[cmd.index("--project-id") + 1]
        _write_bundle(
            coordinator,
            state,
            source,
            pending,
            workflow_id=workflow_id,
            project_id=project_id,
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("app.staged_workflows.subprocess.run", fake_subprocess)

    coordinator._initialize_stage("wf-stage8", state, "stage8")
    workflow = coordinator.get("wf-stage8")
    run_dir = coordinator._run_dir(state, "stage8")

    assert workflow["status"] == "COMPLETED"
    assert (run_dir / "outputs" / "proposal.docx").is_file()
    assert not (run_dir / ".outputs.pending").exists()
    assert json.loads((run_dir / "LATEST_STATE.json").read_text())["status"] == "COMPLETED"
