from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from .util import new_id, utc_now
from .secret_redaction import redact_secret_text
from .workflow_status import (
    WorkflowStatus,
    clear_terminal_runtime_transients,
    coerce_workflow_status,
    ensure_transition,
    is_recoverable_block,
    is_terminal,
)


STAGED_WORKFLOW_TYPE = "WF-STAGED_PROPOSAL"
RUN_ROOT_OWNER_FILE = ".proposal_agent_workflow_owner.json"
STAGE8_METADATA_VERSION = "2.0"
STAGED_STEPS = [
    "stage1", "stage2", "stage3", "stage4", "stage4a", "stage5",
    "stage6a", "stage6b", "stage6c", "stage6d", "stage7", "stage8",
]


class StagedWorkflowCoordinator:
    """Register and advance the file-bridged Stage 1–8 pipeline in the main workflow store.

    Model responses remain immutable files handled by the stage tools. This
    coordinator gives both execution styles one project ID, workflow ID, API
    entry point, status model and audit trail.
    """

    def __init__(self, db, settings, dependency_preflight=None):
        self.db = db
        self.settings = settings
        self.dependency_preflight = dependency_preflight
        self.root = Path(settings.data_dir) / "staged_workflows"
        self.root.mkdir(parents=True, exist_ok=True)

    def _pause_for_configuration(
        self,
        row: dict[str, Any],
        state: dict[str, Any],
        report,
        *,
        source: str,
    ) -> dict[str, Any]:
        payload = report.as_dict()
        payload.update(
            {
                "workflow_id": row["id"],
                "workflow_type": STAGED_WORKFLOW_TYPE,
                "resume_step": int(row["current_step"]),
                "resume_stage": state.get("current_stage", "stage1"),
                "source": source,
                "first_detected_at": (state.get("configuration_wait") or {}).get(
                    "first_detected_at",
                    utc_now(),
                ),
                "last_checked_at": utc_now(),
            }
        )
        state["configuration_wait"] = payload
        state["last_error"] = report.summary()
        self._save(row, status="WAITING_CONFIGURATION", state=state)
        self.db.audit(
            "STAGED_WORKFLOW_WAITING_CONFIGURATION",
            project_id=row["project_id"],
            object_id=row["id"],
            metadata=payload,
        )
        return self.get(row["id"])

    def _project(self, project_id: str) -> dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM projects WHERE id=?", (project_id,))
        if not row:
            raise KeyError(f"Project not found: {project_id}")
        return row

    def start(self, project_id: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
        project = self._project(project_id)
        options = dict(options or {})
        workflow_id = new_id("wf")
        run_root = Path(options.get("run_root") or self.root / workflow_id).expanduser()
        options["run_root"] = str(run_root)
        now = utc_now()
        state = {
            "workflow_type": STAGED_WORKFLOW_TYPE,
            "workflow_id": workflow_id,
            "project_id": project_id,
            "execution_style": "FILE_BRIDGED_STAGES",
            "options": options,
            "run_root": str(run_root),
            "current_stage": "stage1",
            "stage_runs": {},
            "step_results": {},
        }
        status = "RUNNING"
        report = None
        if self.dependency_preflight is not None:
            report = self.dependency_preflight.workflow_report(
                project_id,
                STAGED_WORKFLOW_TYPE,
                options,
            )
            if report.blocking_issues:
                status = "WAITING_CONFIGURATION"
                state["configuration_wait"] = {
                    **report.as_dict(),
                    "workflow_id": workflow_id,
                    "workflow_type": STAGED_WORKFLOW_TYPE,
                    "resume_step": 0,
                    "resume_stage": "stage1",
                    "source": "WORKFLOW_START_PREFLIGHT",
                    "first_detected_at": now,
                    "last_checked_at": now,
                }
                state["last_error"] = report.summary()
        run_root = self._claim_run_root(workflow_id, project_id, run_root)
        options["run_root"] = str(run_root)
        state["run_root"] = str(run_root)
        self.db.execute(
            "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (workflow_id, project_id, STAGED_WORKFLOW_TYPE, status, 0, json.dumps(state, ensure_ascii=False), now, now),
        )
        if status == "RUNNING":
            title = str(options.get("project_title") or project["name"])
            self._initialize_stage(workflow_id, state, "stage1", project_title=title)
        else:
            self.db.audit(
                "STAGED_WORKFLOW_WAITING_CONFIGURATION",
                project_id=project_id,
                object_id=workflow_id,
                metadata=state["configuration_wait"],
            )
        self.db.audit("STAGED_WORKFLOW_STARTED", project_id=project_id, object_id=workflow_id, metadata={"run_root": str(run_root)})
        return self.get(workflow_id)

    def _row(self, workflow_id: str) -> dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM workflows WHERE id=?", (workflow_id,))
        if not row or row["workflow_type"] != STAGED_WORKFLOW_TYPE:
            raise KeyError(f"Staged workflow not found: {workflow_id}")
        row["state"] = json.loads(row.pop("state_json"))
        return row

    def _save(self, row: dict[str, Any], *, status: str | None = None, current_step: int | None = None, state: dict[str, Any] | None = None) -> None:
        next_status = ensure_transition(row["status"], status or row["status"]).value
        next_step = row["current_step"] if current_step is None else current_step
        next_state = row["state"] if state is None else state
        clear_terminal_runtime_transients(next_state, next_status)
        updated_at = utc_now()
        self.db.execute(
            "UPDATE workflows SET status=?,current_step=?,state_json=?,updated_at=? WHERE id=?",
            (
                next_status,
                next_step,
                json.dumps(next_state, ensure_ascii=False),
                updated_at,
                row["id"],
            ),
        )
        row["status"] = next_status
        row["current_step"] = next_step
        row["state"] = next_state
        row["updated_at"] = updated_at

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    def _run_dir(self, state: dict[str, Any], stage: str) -> Path:
        return Path(state["run_root"]) / stage

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{new_id('tmp')}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _resolve_without_symlink_components(path: Path) -> Path:
        candidate = path.expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        candidate = candidate.absolute()
        current = Path(candidate.anchor)
        for part in candidate.parts[1:]:
            current = current / part
            if current.exists() and current.is_symlink():
                raise RuntimeError(f"Refusing symlinked workflow path component: {current}")
        return candidate.resolve()

    @staticmethod
    def _owner_payload(workflow_id: str, project_id: str, run_root: Path) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "workflow_id": workflow_id,
            "project_id": project_id,
            "run_root": str(run_root),
            "claimed_at": utc_now(),
        }

    def _read_run_root_owner(self, marker: Path) -> dict[str, Any]:
        if marker.is_symlink() or not marker.is_file():
            raise RuntimeError(f"Invalid workflow owner marker: {marker}")
        try:
            return self._read_json(marker)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Invalid workflow owner marker: {marker}") from exc

    @staticmethod
    def _owner_matches(
        owner: dict[str, Any],
        *,
        workflow_id: str,
        project_id: str,
        run_root: Path,
    ) -> bool:
        try:
            owner_root = Path(str(owner.get("run_root") or "")).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return False
        return (
            str(owner.get("workflow_id") or "") == workflow_id
            and str(owner.get("project_id") or "") == project_id
            and owner_root == run_root
        )

    def _claim_run_root(self, workflow_id: str, project_id: str, run_root: Path) -> Path:
        root = self._resolve_without_symlink_components(run_root)
        if root.exists() and not root.is_dir():
            raise RuntimeError(f"Workflow run_root is not a directory: {root}")
        root.mkdir(parents=True, exist_ok=True)
        marker = root / RUN_ROOT_OWNER_FILE
        if marker.exists() or marker.is_symlink():
            owner = self._read_run_root_owner(marker)
            if not self._owner_matches(
                owner,
                workflow_id=workflow_id,
                project_id=project_id,
                run_root=root,
            ):
                raise RuntimeError(
                    f"Workflow run_root is already owned by another workflow: {root}"
                )
            return root
        existing = [item for item in root.iterdir() if item.name != RUN_ROOT_OWNER_FILE]
        if existing:
            raise RuntimeError(
                f"Refusing to claim a non-empty unowned workflow run_root: {root}"
            )
        payload = self._owner_payload(workflow_id, project_id, root)
        try:
            with marker.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            owner = self._read_run_root_owner(marker)
            if not self._owner_matches(
                owner,
                workflow_id=workflow_id,
                project_id=project_id,
                run_root=root,
            ):
                raise RuntimeError(
                    f"Workflow run_root is already owned by another workflow: {root}"
                )
        return root

    def _assert_run_root_owned(
        self,
        state: dict[str, Any],
        *,
        workflow_id: str,
        project_id: str,
    ) -> Path:
        root = self._resolve_without_symlink_components(Path(state["run_root"]))
        marker = root / RUN_ROOT_OWNER_FILE
        if not marker.exists():
            canonical_default = self._resolve_without_symlink_components(self.root / workflow_id)
            if root != canonical_default:
                raise RuntimeError(
                    f"Legacy custom run_root has no workflow ownership marker: {root}"
                )
            root.mkdir(parents=True, exist_ok=True)
            payload = self._owner_payload(workflow_id, project_id, root)
            payload["migrated_legacy_default_root"] = True
            try:
                with marker.open("x", encoding="utf-8") as handle:
                    handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError:
                pass
        owner = self._read_run_root_owner(marker)
        if not self._owner_matches(
            owner,
            workflow_id=workflow_id,
            project_id=project_id,
            run_root=root,
        ):
            raise RuntimeError(f"Workflow run_root ownership mismatch: {root}")
        return root

    def _private_stage_dir(
        self,
        state: dict[str, Any],
        stage: str,
        *,
        workflow_id: str | None = None,
        project_id: str | None = None,
    ) -> Path:
        """Return a workflow-private stage directory safe for deterministic cleanup."""

        if workflow_id and project_id:
            run_root = self._assert_run_root_owned(
                state,
                workflow_id=workflow_id,
                project_id=project_id,
            )
        else:
            run_root = self._resolve_without_symlink_components(Path(state["run_root"]))
        run_dir = run_root / stage
        if run_dir.is_symlink():
            raise RuntimeError(f"Refusing to use symlinked stage directory: {run_dir}")
        if run_dir.parent.resolve() != run_root:
            raise RuntimeError(f"Stage directory escapes workflow run root: {run_dir}")
        return run_dir

    @staticmethod
    def _safe_named_output(outputs: Path, value: Any, suffix: str) -> Path:
        name = str(value or "").strip()
        if not name or Path(name).name != name or not name.lower().endswith(suffix):
            raise RuntimeError(f"Invalid Stage 8 output filename: {name!r}")
        path = outputs / name
        if path.is_symlink() or not path.is_file() or path.resolve().parent != outputs.resolve():
            raise RuntimeError(f"Unsafe or missing Stage 8 output: {name}")
        if path.stat().st_size <= 0:
            raise RuntimeError(f"Stage 8 output is empty: {name}")
        return path

    def _validate_stage8_export_bundle(
        self,
        state: dict[str, Any],
        outputs: Path,
        *,
        workflow_id: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        if workflow_id and project_id:
            run_root = self._assert_run_root_owned(
                state,
                workflow_id=workflow_id,
                project_id=project_id,
            )
        else:
            run_root = self._resolve_without_symlink_components(Path(state["run_root"]))
        source = Path(self._output(state, "stage7", "stage7_integrated_proposal.md"))
        if source.is_symlink() or not source.is_file():
            raise RuntimeError(f"Stage 7 integrated proposal is missing or symlinked: {source}")
        if outputs.is_symlink() or not outputs.is_dir():
            raise RuntimeError(f"Stage 8 outputs directory is missing or symlinked: {outputs}")
        if not source.resolve().is_relative_to(run_root):
            raise RuntimeError("Stage 7 integrated proposal escapes the workflow run_root")
        if not outputs.resolve().is_relative_to((run_root / "stage8").resolve()):
            raise RuntimeError("Stage 8 outputs escape the workflow-private stage directory")
        metadata_path = outputs / "stage8_export_metadata.json"
        if metadata_path.is_symlink() or not metadata_path.is_file():
            raise RuntimeError("Stage 8 export metadata is missing or symlinked")
        try:
            metadata = self._read_json(metadata_path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Stage 8 export metadata is invalid JSON") from exc
        if str(metadata.get("metadata_version") or "") != STAGE8_METADATA_VERSION:
            raise RuntimeError("Stage 8 export metadata version is missing or unsupported")
        if workflow_id and str(metadata.get("workflow_id") or "") != workflow_id:
            raise RuntimeError("Stage 8 export metadata belongs to another workflow")
        if project_id and str(metadata.get("project_id") or "") != project_id:
            raise RuntimeError("Stage 8 export metadata belongs to another project")
        if str(metadata.get("input_file") or "") != str(source.resolve()):
            raise RuntimeError("Stage 8 export metadata points to a different input file")
        if int(metadata.get("input_size") or -1) != source.stat().st_size:
            raise RuntimeError("Stage 8 input size no longer matches the exported snapshot")
        if str(metadata.get("input_sha256") or "") != self._sha256(source):
            raise RuntimeError("Stage 8 input hash no longer matches the exported snapshot")

        docx = self._safe_named_output(outputs, metadata.get("docx_file"), ".docx")
        pdf = self._safe_named_output(outputs, metadata.get("pdf_file"), ".pdf")
        if {item.name for item in outputs.glob("*.docx")} != {docx.name}:
            raise RuntimeError("Stage 8 outputs contain an unexpected DOCX set")
        if {item.name for item in outputs.glob("*.pdf")} != {pdf.name}:
            raise RuntimeError("Stage 8 outputs contain an unexpected PDF set")
        if int(metadata.get("docx_size") or -1) != docx.stat().st_size:
            raise RuntimeError("Stage 8 DOCX size does not match metadata")
        if int(metadata.get("pdf_size") or -1) != pdf.stat().st_size:
            raise RuntimeError("Stage 8 PDF size does not match metadata")
        if str(metadata.get("docx_sha256") or "") != self._sha256(docx):
            raise RuntimeError("Stage 8 DOCX hash does not match metadata")
        if str(metadata.get("pdf_sha256") or "") != self._sha256(pdf):
            raise RuntimeError("Stage 8 PDF hash does not match metadata")

        if not zipfile.is_zipfile(docx):
            raise RuntimeError("Stage 8 DOCX is not a valid OOXML ZIP container")
        with zipfile.ZipFile(docx) as archive:
            required = {"[Content_Types].xml", "word/document.xml"}
            if not required.issubset(set(archive.namelist())) or archive.testzip() is not None:
                raise RuntimeError("Stage 8 DOCX container is incomplete or corrupt")
        try:
            page_count = len(PdfReader(str(pdf), strict=False).pages)
        except Exception as exc:
            raise RuntimeError("Stage 8 PDF is unreadable") from exc
        if page_count <= 0 or int(metadata.get("page_count") or -1) != page_count:
            raise RuntimeError("Stage 8 PDF page count does not match metadata")
        return metadata

    def _completion_payload(
        self,
        metadata: dict[str, Any],
        metadata_path: Path,
        *,
        workflow_id: str | None,
        project_id: str | None,
    ) -> dict[str, Any]:
        return {
            "schema_version": "2.0",
            "stage": "STAGE_8_FINAL_EXPORT",
            "status": "COMPLETED",
            "phase": "STAGE_8_COMPLETE",
            "workflow_id": workflow_id or metadata.get("workflow_id"),
            "project_id": project_id or metadata.get("project_id"),
            "input_sha256": metadata["input_sha256"],
            "docx_sha256": metadata["docx_sha256"],
            "pdf_sha256": metadata["pdf_sha256"],
            "export_metadata_sha256": self._sha256(metadata_path),
            "updated_at": utc_now(),
        }

    def _write_stage8_completion(
        self,
        run_dir: Path,
        metadata: dict[str, Any],
        *,
        workflow_id: str | None,
        project_id: str | None,
    ) -> None:
        metadata_path = run_dir / "outputs" / "stage8_export_metadata.json"
        self._atomic_write_json(
            run_dir / "LATEST_STATE.json",
            self._completion_payload(
                metadata,
                metadata_path,
                workflow_id=workflow_id,
                project_id=project_id,
            ),
        )

    def _stage8_validation(
        self,
        state: dict[str, Any],
        run_dir: Path,
        *,
        workflow_id: str | None = None,
        project_id: str | None = None,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        try:
            latest = run_dir / "LATEST_STATE.json"
            if latest.is_symlink() or not latest.is_file():
                raise RuntimeError("Stage 8 completion state is missing or symlinked")
            latest_payload = self._read_json(latest)
            if (
                latest_payload.get("status") != "COMPLETED"
                or latest_payload.get("phase") != "STAGE_8_COMPLETE"
            ):
                raise RuntimeError("Stage 8 completion state is not final")
            metadata = self._validate_stage8_export_bundle(
                state,
                run_dir / "outputs",
                workflow_id=workflow_id,
                project_id=project_id,
            )
            metadata_path = run_dir / "outputs" / "stage8_export_metadata.json"
            for key in ("input_sha256", "docx_sha256", "pdf_sha256"):
                if str(latest_payload.get(key) or "") != str(metadata.get(key) or ""):
                    raise RuntimeError(f"Stage 8 completion state has stale {key}")
            if str(latest_payload.get("export_metadata_sha256") or "") != self._sha256(metadata_path):
                raise RuntimeError("Stage 8 completion state has stale export metadata")
            if workflow_id and str(latest_payload.get("workflow_id") or "") != workflow_id:
                raise RuntimeError("Stage 8 completion state belongs to another workflow")
            if project_id and str(latest_payload.get("project_id") or "") != project_id:
                raise RuntimeError("Stage 8 completion state belongs to another project")
            return True, "", metadata
        except (OSError, ValueError, TypeError, json.JSONDecodeError, RuntimeError) as exc:
            return False, str(exc), None

    def _stage8_outputs_complete(
        self,
        state: dict[str, Any],
        run_dir: Path,
        *,
        workflow_id: str | None = None,
        project_id: str | None = None,
    ) -> bool:
        valid, _reason, _metadata = self._stage8_validation(
            state,
            run_dir,
            workflow_id=workflow_id,
            project_id=project_id,
        )
        return valid

    def _prepare_stage8_dir(
        self,
        state: dict[str, Any],
        *,
        workflow_id: str | None = None,
        project_id: str | None = None,
    ) -> tuple[Path, bool]:
        """Reuse only a fully verified delivery; rebuild every ambiguous bundle."""

        run_dir = self._private_stage_dir(
            state,
            "stage8",
            workflow_id=workflow_id,
            project_id=project_id,
        )
        if not run_dir.exists():
            run_dir.mkdir(parents=True, exist_ok=False)
            return run_dir, False
        if not run_dir.is_dir():
            raise RuntimeError(f"Stage 8 path is not a directory: {run_dir}")
        if self._stage8_outputs_complete(
            state,
            run_dir,
            workflow_id=workflow_id,
            project_id=project_id,
        ):
            return run_dir, True

        outputs = run_dir / "outputs"
        if outputs.exists() and not outputs.is_symlink():
            try:
                metadata = self._validate_stage8_export_bundle(
                    state,
                    outputs,
                    workflow_id=workflow_id,
                    project_id=project_id,
                )
            except RuntimeError:
                pass
            else:
                self._write_stage8_completion(
                    run_dir,
                    metadata,
                    workflow_id=workflow_id,
                    project_id=project_id,
                )
                return run_dir, True

        pending = run_dir / ".outputs.pending"
        if not outputs.exists() and pending.exists() and not pending.is_symlink():
            try:
                metadata = self._validate_stage8_export_bundle(
                    state,
                    pending,
                    workflow_id=workflow_id,
                    project_id=project_id,
                )
            except RuntimeError:
                pass
            else:
                os.replace(pending, outputs)
                self._write_stage8_completion(
                    run_dir,
                    metadata,
                    workflow_id=workflow_id,
                    project_id=project_id,
                )
                return run_dir, True

        shutil.rmtree(run_dir)
        run_dir.mkdir(parents=True, exist_ok=False)
        return run_dir, False

    def _output(self, state: dict[str, Any], stage: str, filename: str) -> str:
        return str(self._run_dir(state, stage) / "outputs" / filename)

    def _evidence_output(self, state: dict[str, Any]) -> str:
        generated = Path(self._output(state, "stage4a", "stage4a_evidence_completion.json"))
        if generated.exists():
            return str(generated)
        configured = str((state.get("options") or {}).get("evidence_completion") or "").strip()
        if configured and Path(configured).expanduser().resolve().exists():
            return str(Path(configured).expanduser().resolve())
        raise ValueError("A completed Stage 4A artifact or options.evidence_completion is required")

    def _initialize_stage(self, workflow_id: str, state: dict[str, Any], stage: str, **extra: Any) -> None:
        workflow_row = self.db.fetchone(
            "SELECT project_id FROM workflows WHERE id=?",
            (workflow_id,),
        )
        if not workflow_row:
            raise KeyError(f"Staged workflow not found: {workflow_id}")
        project_id = str(workflow_row["project_id"])
        self._assert_run_root_owned(
            state,
            workflow_id=workflow_id,
            project_id=project_id,
        )
        run_dir = self._run_dir(state, stage)
        options = state.get("options") or {}
        if stage == "stage1":
            from stage1_tools.stage1_design_input import init_cmd
            init_cmd(argparse.Namespace(run_dir=str(run_dir), title=extra["project_title"]))
        elif stage == "stage2":
            from stage2_tools.stage2_guide_fact_base import init_cmd
            init_cmd(argparse.Namespace(run_dir=str(run_dir), design_input=self._output(state, "stage1", "stage1_design_input.json")))
        elif stage == "stage3":
            from stage3_tools.stage3_project_definition import init_cmd
            init_cmd(argparse.Namespace(run_dir=str(run_dir), design_input=self._output(state, "stage1", "stage1_design_input.json"), guide_fact_base=self._output(state, "stage2", "stage2_guide_fact_base.json")))
        elif stage == "stage4":
            from stage4_tools.stage4_argument_architecture import init_cmd
            init_cmd(argparse.Namespace(run_dir=str(run_dir), design_input=self._output(state, "stage1", "stage1_design_input.json"), guide_fact_base=self._output(state, "stage2", "stage2_guide_fact_base.json"), project_definition=self._output(state, "stage3", "stage3_project_definition.json")))
        elif stage == "stage4a":
            from stage4a_tools.stage4a_evidence_completion import init_cmd
            evidence_inputs = options.get("evidence_inputs")
            if not evidence_inputs:
                raise ValueError("Stage 4A requires options.evidence_inputs")
            init_cmd(argparse.Namespace(run_dir=str(run_dir), argument_architecture=self._output(state, "stage4", "stage4_argument_architecture.json"), evidence_inputs=str(Path(evidence_inputs).resolve())))
        elif stage == "stage5":
            from stage5_tools.stage5_section_planning import init_cmd
            evidence = self._evidence_output(state)
            init_cmd(argparse.Namespace(run_dir=str(run_dir), design_input=self._output(state, "stage1", "stage1_design_input.json"), project_definition=self._output(state, "stage3", "stage3_project_definition.json"), argument_architecture=self._output(state, "stage4", "stage4_argument_architecture.json"), evidence_completion=evidence))
        elif stage == "stage6a":
            from stage6a_tools.stage6a_drafting import init_cmd
            init_cmd(argparse.Namespace(run_dir=str(run_dir), design_input=self._output(state, "stage1", "stage1_design_input.json"), project_definition=self._output(state, "stage3", "stage3_project_definition.json"), argument_architecture=self._output(state, "stage4", "stage4_argument_architecture.json"), evidence_completion=self._evidence_output(state), section_plan=self._output(state, "stage5", "stage5_section_plan.json")))
        elif stage == "stage6b":
            from stage6b_tools.stage6b_drafting import init_cmd
            init_cmd(argparse.Namespace(run_dir=str(run_dir), design_input=self._output(state, "stage1", "stage1_design_input.json"), project_definition=self._output(state, "stage3", "stage3_project_definition.json"), argument_architecture=self._output(state, "stage4", "stage4_argument_architecture.json"), evidence_completion=self._evidence_output(state), section_plan=self._output(state, "stage5", "stage5_section_plan.json"), stage6a_draft=self._output(state, "stage6a", "stage6a_batch_draft.json")))
        elif stage == "stage6c":
            from stage6c_tools.stage6c_drafting import init_cmd
            init_cmd(argparse.Namespace(run_dir=str(run_dir), design_input=self._output(state, "stage1", "stage1_design_input.json"), project_definition=self._output(state, "stage3", "stage3_project_definition.json"), argument_architecture=self._output(state, "stage4", "stage4_argument_architecture.json"), evidence_completion=self._evidence_output(state), section_plan=self._output(state, "stage5", "stage5_section_plan.json"), stage6a_draft=self._output(state, "stage6a", "stage6a_batch_draft.json"), stage6b_draft=self._output(state, "stage6b", "stage6b_batch_draft.json")))
        elif stage == "stage6d":
            from stage6d_tools.stage6d_drafting import init_cmd
            init_cmd(argparse.Namespace(run_dir=str(run_dir), design_input=self._output(state, "stage1", "stage1_design_input.json"), project_definition=self._output(state, "stage3", "stage3_project_definition.json"), argument_architecture=self._output(state, "stage4", "stage4_argument_architecture.json"), evidence_completion=self._evidence_output(state), section_plan=self._output(state, "stage5", "stage5_section_plan.json"), stage6a_draft=self._output(state, "stage6a", "stage6a_batch_draft.json"), stage6b_draft=self._output(state, "stage6b", "stage6b_batch_draft.json"), stage6c_draft=self._output(state, "stage6c", "stage6c_batch_draft.json")))
        elif stage == "stage7":
            from stage7_tools.stage7_integration import init_cmd
            init_cmd(argparse.Namespace(run_dir=str(run_dir), project_definition=self._output(state, "stage3", "stage3_project_definition.json"), argument_architecture=self._output(state, "stage4", "stage4_argument_architecture.json"), evidence_completion=self._evidence_output(state), section_plan=self._output(state, "stage5", "stage5_section_plan.json"), stage6a=self._output(state, "stage6a", "stage6a_batch_draft.json"), stage6b=self._output(state, "stage6b", "stage6b_batch_draft.json"), stage6c=self._output(state, "stage6c", "stage6c_batch_draft.json"), stage6d=self._output(state, "stage6d", "stage6d_batch_draft.json")))
        elif stage == "stage8":
            run_dir, reused = self._prepare_stage8_dir(
                state,
                workflow_id=workflow_id,
                project_id=project_id,
            )
            if not reused:
                source = self._output(state, "stage7", "stage7_integrated_proposal.md")
                pending = run_dir / ".outputs.pending"
                if pending.exists() or pending.is_symlink():
                    raise RuntimeError(f"Unexpected Stage 8 pending output path: {pending}")
                cmd = [
                    sys.executable,
                    str(Path(self.settings.root_dir) / "stage8_tools" / "export_final.py"),
                    "--input",
                    source,
                    "--out-dir",
                    str(pending),
                    "--workflow-id",
                    workflow_id,
                    "--project-id",
                    project_id,
                ]
                completed = subprocess.run(cmd, cwd=self.settings.root_dir, capture_output=True, text=True, timeout=300)
                if completed.returncode != 0:
                    raise RuntimeError(completed.stderr or completed.stdout or "Stage 8 export failed")
                metadata = self._validate_stage8_export_bundle(
                    state,
                    pending,
                    workflow_id=workflow_id,
                    project_id=project_id,
                )
                outputs = run_dir / "outputs"
                if outputs.exists() or outputs.is_symlink():
                    raise RuntimeError(f"Unexpected Stage 8 published output path: {outputs}")
                os.replace(pending, outputs)
                self._write_stage8_completion(
                    run_dir,
                    metadata,
                    workflow_id=workflow_id,
                    project_id=project_id,
                )
                valid, reason, _validated = self._stage8_validation(
                    state,
                    run_dir,
                    workflow_id=workflow_id,
                    project_id=project_id,
                )
                if not valid:
                    raise RuntimeError(f"Stage 8 published bundle failed validation: {reason}")
        else:
            raise KeyError(stage)
        state["current_stage"] = stage
        state.setdefault("stage_runs", {})[stage] = str(run_dir)
        state.setdefault("step_results", {})[stage] = {"initialized_at": utc_now(), "run_dir": str(run_dir)}
        row = self._row(workflow_id)
        row["state"] = state
        self._save(row, status="RUNNING", current_step=STAGED_STEPS.index(stage), state=state)

    def _sync(self, row: dict[str, Any]) -> dict[str, Any]:
        canonical_status = coerce_workflow_status(row["status"]).value
        if canonical_status != row["status"]:
            self._save(row, status=canonical_status)
        state = row["state"]

        # Terminal workflow state remains immutable.  Stage 8 delivery integrity
        # is still reported so post-completion corruption is visible without
        # reopening the workflow.
        if is_terminal(row["status"]):
            if state.get("current_stage") == "stage8":
                valid, reason, metadata = self._stage8_validation(
                    state,
                    self._run_dir(state, "stage8"),
                    workflow_id=row["id"],
                    project_id=row["project_id"],
                )
                row["delivery_integrity"] = {
                    "valid": valid,
                    "reason": reason or None,
                    "metadata": metadata,
                }
            return row

        # A staged tool may still have a completed LATEST_STATE.json from the
        # previous stage while the outer workflow is intentionally paused for
        # a missing runtime dependency.
        if row.get("status") == "WAITING_CONFIGURATION":
            return row

        stage = state["current_stage"]
        latest = self._run_dir(state, stage) / "LATEST_STATE.json"
        if latest.exists() or latest.is_symlink():
            try:
                staged_state = self._read_json(latest)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                state["last_error"] = f"Invalid staged state JSON at {latest}: {exc}"
                state["staged_status_error"] = {
                    "stage": stage,
                    "internal_status": "UNREADABLE",
                    "phase": "STATE_JSON_INVALID",
                    "detected_at": utc_now(),
                }
                if stage == "stage8":
                    state["current_stage_state"] = {
                        "status": "INCOMPLETE",
                        "phase": "STAGE_8_RECOVERY_REQUIRED",
                    }
                    state["stage8_delivery_integrity"] = {
                        "valid": False,
                        "reason": state["last_error"],
                    }
                    if not (
                        is_recoverable_block(row["status"])
                        and state.get("stage8_recovery_required")
                    ):
                        state["stage8_recovery_required"] = {
                            "reason": state["last_error"],
                            "detected_at": utc_now(),
                        }
                    next_status = (
                        row["status"]
                        if is_recoverable_block(row["status"])
                        else WorkflowStatus.RUNNING.value
                    )
                else:
                    next_status = WorkflowStatus.BLOCKED_TECHNICAL.value
                self._save(row, status=next_status, state=state)
                return self._row(row["id"])

            state["current_stage_state"] = staged_state
            phase = str(staged_state.get("phase") or "")
            internal_status = str(staged_state.get("status") or "")

            if stage == "stage8" and internal_status == "COMPLETED":
                valid, reason, metadata = self._stage8_validation(
                    state,
                    self._run_dir(state, "stage8"),
                    workflow_id=row["id"],
                    project_id=row["project_id"],
                )
                if valid:
                    state.pop("stage8_recovery_required", None)
                    state.pop("last_error", None)
                    state["stage8_delivery"] = metadata or {}
                    self._save(row, status=WorkflowStatus.COMPLETED.value, state=state)
                else:
                    state["current_stage_state"] = {
                        "status": "INCOMPLETE",
                        "phase": "STAGE_8_RECOVERY_REQUIRED",
                    }
                    state["stage8_delivery_integrity"] = {
                        "valid": False,
                        "reason": reason,
                    }
                    if not (
                        is_recoverable_block(row["status"])
                        and state.get("stage8_recovery_required")
                    ):
                        state["stage8_recovery_required"] = {
                            "reason": reason,
                            "detected_at": utc_now(),
                        }
                        state["last_error"] = reason
                    next_status = (
                        row["status"]
                        if is_recoverable_block(row["status"])
                        else WorkflowStatus.RUNNING.value
                    )
                    self._save(row, status=next_status, state=state)
                return self._row(row["id"])

            if internal_status == "BLOCKED":
                if "SCHEMA" in phase or "CONTRACT" in phase:
                    status = WorkflowStatus.BLOCKED_CONTRACT.value
                elif any(
                    marker in phase
                    for marker in (
                        "CRITIC",
                        "REPAIR_EXHAUSTED",
                        "DETERMINISTIC",
                        "CONFIRMATION",
                        "REVALIDATION",
                    )
                ):
                    status = WorkflowStatus.BLOCKED_CONTENT.value
                else:
                    status = WorkflowStatus.BLOCKED_TECHNICAL.value
            else:
                status_map = {
                    "RUNNING": WorkflowStatus.RUNNING.value,
                    "WAITING_MODEL": WorkflowStatus.WAITING_PROVIDER.value,
                    "WAITING_HUMAN": WorkflowStatus.WAITING_GATE.value,
                    "WAITING_GATE": WorkflowStatus.WAITING_GATE.value,
                    "WAITING_CONFIGURATION": WorkflowStatus.WAITING_CONFIGURATION.value,
                    "COMPLETED": WorkflowStatus.RUNNING.value,
                }
                status = status_map.get(internal_status)
                if status is None:
                    status = WorkflowStatus.BLOCKED_TECHNICAL.value
                    state["last_error"] = (
                        f"Unknown staged workflow status {internal_status!r} "
                        f"at {stage or 'unknown stage'}; automatic advancement is refused."
                    )
                    state["staged_status_error"] = {
                        "stage": stage,
                        "internal_status": internal_status,
                        "phase": phase,
                        "detected_at": utc_now(),
                    }
            self._save(row, status=status, state=state)
        elif stage == "stage8":
            reason = "Stage 8 completion state is missing"
            state["current_stage_state"] = {
                "status": "INCOMPLETE",
                "phase": "STAGE_8_RECOVERY_REQUIRED",
            }
            state["stage8_delivery_integrity"] = {
                "valid": False,
                "reason": reason,
            }
            if not (
                is_recoverable_block(row["status"])
                and state.get("stage8_recovery_required")
            ):
                state["stage8_recovery_required"] = {
                    "reason": reason,
                    "detected_at": utc_now(),
                }
                state["last_error"] = reason
            next_status = (
                row["status"]
                if is_recoverable_block(row["status"])
                else WorkflowStatus.RUNNING.value
            )
            self._save(row, status=next_status, state=state)
        return self._row(row["id"])

    def get(self, workflow_id: str) -> dict[str, Any]:
        row = self._sync(self._row(workflow_id))
        row["steps"] = [{"type": "STAGED", "stage": item} for item in STAGED_STEPS]
        return row

    @staticmethod
    def _next_stage_for_state(state: dict[str, Any]) -> str | None:
        stage = str(state.get("current_stage") or "stage1")
        if stage == "stage8":
            return None
        staged_state = state.get("current_stage_state") or {}
        next_stage = STAGED_STEPS[STAGED_STEPS.index(stage) + 1]
        if stage == "stage4" and "STAGE_5" in str(staged_state.get("next_stage")):
            next_stage = "stage5"
        return next_stage

    async def advance(self, workflow_id: str) -> dict[str, Any]:
        row = self._sync(self._row(workflow_id))
        state = row["state"]
        if row["status"] == "WAITING_CONFIGURATION" and self.dependency_preflight is not None:
            # A running staged workflow necessarily has a non-empty run_root.
            # Rechecking the start-only constraint would make transition waits
            # impossible to resume.  Recheck the exact pending transition once
            # Stage 1 has been initialized; otherwise use the start preflight.
            if state.get("stage_runs"):
                next_stage = self._next_stage_for_state(state)
                report = (
                    self.dependency_preflight.staged_transition_report(next_stage, state)
                    if next_stage
                    else self.dependency_preflight.application_report(require_export=True)
                )
            else:
                report = self.dependency_preflight.workflow_report(
                    row["project_id"],
                    STAGED_WORKFLOW_TYPE,
                    state.get("options") or {},
                )
            if report.blocking_issues:
                return self._pause_for_configuration(
                    row,
                    state,
                    report,
                    source="WAITING_CONFIGURATION_RECHECK",
                )
            previous = state.pop("configuration_wait", None)
            state.pop("last_error", None)
            if previous:
                state["configuration_recovered"] = {
                    "recovered_at": utc_now(),
                    "previous": previous,
                }
            self._save(row, status="RUNNING", state=state)
            row = self._row(workflow_id)
            state = row["state"]
            if not state.get("stage_runs"):
                claimed = self._assert_run_root_owned(
                    state,
                    workflow_id=row["id"],
                    project_id=row["project_id"],
                )
                state["run_root"] = str(claimed)
                project = self._project(row["project_id"])
                title = str((state.get("options") or {}).get("project_title") or project["name"])
                self._initialize_stage(workflow_id, state, "stage1", project_title=title)
                return self.get(workflow_id)
        stage = state["current_stage"]
        if stage == "stage8" and state.get("stage8_recovery_required"):
            state.pop("stage8_recovery_required", None)
            state.pop("last_error", None)
            try:
                self._initialize_stage(workflow_id, state, "stage8")
            except Exception as exc:
                row = self._row(workflow_id)
                state = row["state"]
                state["last_error"] = redact_secret_text(str(exc))
                state["stage8_recovery_required"] = {
                    "reason": redact_secret_text(str(exc)),
                    "detected_at": utc_now(),
                }
                self._save(
                    row,
                    status=WorkflowStatus.BLOCKED_TECHNICAL.value,
                    state=state,
                )
                self.db.audit(
                    "STAGED_WORKFLOW_BLOCKED",
                    project_id=row["project_id"],
                    object_id=workflow_id,
                    metadata={"from": "stage8", "to": "stage8", "error": str(exc)},
                )
            return self.get(workflow_id)
        staged_state = state.get("current_stage_state") or {}
        if staged_state.get("status") != "COMPLETED":
            return self.get(workflow_id)
        if stage == "stage8":
            self._save(row, status="COMPLETED", state=state)
            return self.get(workflow_id)
        next_stage = self._next_stage_for_state(state)
        if next_stage is None:
            self._save(row, status="COMPLETED", state=state)
            return self.get(workflow_id)
        if self.dependency_preflight is not None:
            report = self.dependency_preflight.staged_transition_report(next_stage, state)
            if report.blocking_issues:
                return self._pause_for_configuration(
                    row,
                    state,
                    report,
                    source=f"STAGED_TRANSITION:{stage}->{next_stage}",
                )
        try:
            self._initialize_stage(workflow_id, state, next_stage)
        except Exception as exc:
            if self.dependency_preflight is not None:
                report = self.dependency_preflight.report_from_runtime_error(
                    exc,
                    scope=f"STAGED_TRANSITION_RUNTIME:{stage}->{next_stage}",
                )
                if report is not None:
                    return self._pause_for_configuration(
                        row,
                        state,
                        report,
                        source=f"STAGED_TRANSITION_RUNTIME:{stage}->{next_stage}",
                    )
            state["last_error"] = redact_secret_text(str(exc))
            state["blocked_transition"] = {"from": stage, "to": next_stage}
            self._save(
                row,
                status=WorkflowStatus.BLOCKED_TECHNICAL.value,
                state=state,
            )
            self.db.audit("STAGED_WORKFLOW_BLOCKED", project_id=row["project_id"], object_id=workflow_id, metadata=state["blocked_transition"] | {"error": str(exc)})
        return self.get(workflow_id)

    def files(self, workflow_id: str) -> dict[str, Any]:
        row = self.get(workflow_id)
        state = row["state"]
        run_dir = self._run_dir(state, state["current_stage"])
        def collect(folder: str) -> list[str]:
            root = run_dir / folder
            return [str(path) for path in sorted(root.glob("**/*")) if path.is_file()] if root.exists() else []
        return {
            "workflow_id": workflow_id,
            "current_stage": state["current_stage"],
            "run_dir": str(run_dir),
            "requests": collect("requests"),
            "human_gates": collect("human_gate"),
            "outputs": collect("outputs"),
            "latest_state": state.get("current_stage_state"),
        }
