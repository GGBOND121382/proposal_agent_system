from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from .util import new_id, utc_now


STAGED_WORKFLOW_TYPE = "WF-STAGED_PROPOSAL"
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

    def __init__(self, db, settings):
        self.db = db
        self.settings = settings
        self.root = Path(settings.data_dir) / "staged_workflows"
        self.root.mkdir(parents=True, exist_ok=True)

    def _project(self, project_id: str) -> dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM projects WHERE id=?", (project_id,))
        if not row:
            raise KeyError(f"Project not found: {project_id}")
        return row

    def start(self, project_id: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
        project = self._project(project_id)
        options = dict(options or {})
        workflow_id = new_id("wf")
        run_root = Path(options.get("run_root") or self.root / workflow_id).resolve()
        if run_root.exists() and any(run_root.iterdir()):
            raise ValueError(f"staged run root must be empty: {run_root}")
        run_root.mkdir(parents=True, exist_ok=True)
        now = utc_now()
        state = {
            "workflow_type": STAGED_WORKFLOW_TYPE,
            "execution_style": "FILE_BRIDGED_STAGES",
            "options": options,
            "run_root": str(run_root),
            "current_stage": "stage1",
            "stage_runs": {},
            "step_results": {},
        }
        self.db.execute(
            "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (workflow_id, project_id, STAGED_WORKFLOW_TYPE, "RUNNING", 0, json.dumps(state, ensure_ascii=False), now, now),
        )
        title = str(options.get("project_title") or project["name"])
        self._initialize_stage(workflow_id, state, "stage1", project_title=title)
        self.db.audit("STAGED_WORKFLOW_STARTED", project_id=project_id, object_id=workflow_id, metadata={"run_root": str(run_root)})
        return self.get(workflow_id)

    def _row(self, workflow_id: str) -> dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM workflows WHERE id=?", (workflow_id,))
        if not row or row["workflow_type"] != STAGED_WORKFLOW_TYPE:
            raise KeyError(f"Staged workflow not found: {workflow_id}")
        row["state"] = json.loads(row.pop("state_json"))
        return row

    def _save(self, row: dict[str, Any], *, status: str | None = None, current_step: int | None = None, state: dict[str, Any] | None = None) -> None:
        self.db.execute(
            "UPDATE workflows SET status=?,current_step=?,state_json=?,updated_at=? WHERE id=?",
            (
                status or row["status"],
                row["current_step"] if current_step is None else current_step,
                json.dumps(state or row["state"], ensure_ascii=False),
                utc_now(),
                row["id"],
            ),
        )

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    def _run_dir(self, state: dict[str, Any], stage: str) -> Path:
        return Path(state["run_root"]) / stage

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
            run_dir.mkdir(parents=True, exist_ok=False)
            source = self._output(state, "stage7", "stage7_integrated_proposal.md")
            cmd = [sys.executable, str(Path(self.settings.root_dir) / "stage8_tools" / "export_final.py"), "--input", source, "--out-dir", str(run_dir / "outputs")]
            completed = subprocess.run(cmd, cwd=self.settings.root_dir, capture_output=True, text=True, timeout=300)
            if completed.returncode != 0:
                raise RuntimeError(completed.stderr or completed.stdout or "Stage 8 export failed")
            (run_dir / "LATEST_STATE.json").write_text(json.dumps({"schema_version": "1.0", "stage": "STAGE_8_FINAL_EXPORT", "status": "COMPLETED", "phase": "STAGE_8_COMPLETE", "updated_at": utc_now()}, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            raise KeyError(stage)
        state["current_stage"] = stage
        state.setdefault("stage_runs", {})[stage] = str(run_dir)
        state.setdefault("step_results", {})[stage] = {"initialized_at": utc_now(), "run_dir": str(run_dir)}
        row = self._row(workflow_id)
        row["state"] = state
        self._save(row, status="RUNNING", current_step=STAGED_STEPS.index(stage), state=state)

    def _sync(self, row: dict[str, Any]) -> dict[str, Any]:
        state = row["state"]
        stage = state["current_stage"]
        latest = self._run_dir(state, stage) / "LATEST_STATE.json"
        if latest.exists():
            staged_state = self._read_json(latest)
            state["current_stage_state"] = staged_state
            status_map = {"WAITING_MODEL": "WAITING_MODEL", "WAITING_HUMAN": "WAITING_GATE", "WAITING_GATE": "WAITING_GATE", "BLOCKED": "BLOCKED", "COMPLETED": "RUNNING"}
            status = status_map.get(str(staged_state.get("status")), "RUNNING")
            if stage == "stage8" and staged_state.get("status") == "COMPLETED":
                status = "COMPLETED"
            self._save(row, status=status, state=state)
        return self._row(row["id"])

    def get(self, workflow_id: str) -> dict[str, Any]:
        row = self._sync(self._row(workflow_id))
        row["steps"] = [{"type": "STAGED", "stage": item} for item in STAGED_STEPS]
        return row

    async def advance(self, workflow_id: str) -> dict[str, Any]:
        row = self._sync(self._row(workflow_id))
        state = row["state"]
        stage = state["current_stage"]
        staged_state = state.get("current_stage_state") or {}
        if staged_state.get("status") != "COMPLETED":
            return self.get(workflow_id)
        if stage == "stage8":
            self._save(row, status="COMPLETED", state=state)
            return self.get(workflow_id)
        next_stage = STAGED_STEPS[STAGED_STEPS.index(stage) + 1]
        if stage == "stage4" and "STAGE_5" in str(staged_state.get("next_stage")):
            next_stage = "stage5"
        try:
            self._initialize_stage(workflow_id, state, next_stage)
        except Exception as exc:
            state["last_error"] = str(exc)
            state["blocked_transition"] = {"from": stage, "to": next_stage}
            self._save(row, status="BLOCKED", state=state)
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
