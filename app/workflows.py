from __future__ import annotations

import json
from typing import Any

from .executor import PromptExecutionError
from .research import PublicResearchError
from .util import new_id, utc_now
from .workflow_authoring import WorkflowAuthoringMixin
from .workflow_defs import WORKFLOWS
from .workflow_gates import WorkflowGateMixin
from .workflow_repair import WorkflowRepairMixin


class WorkflowEngine(WorkflowAuthoringMixin, WorkflowRepairMixin, WorkflowGateMixin):
    def __init__(self, db, pack, context_builder, executor, research_service, diagram_enrichment=None):
        self.db = db
        self.pack = pack
        self.context_builder = context_builder
        self.executor = executor
        self.research_service = research_service
        self.diagram_enrichment = diagram_enrichment

    def start(self, project_id: str, workflow_type: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
        if workflow_type not in WORKFLOWS:
            raise KeyError(f"Unknown workflow: {workflow_type}")
        if not self.db.fetchone("SELECT id FROM projects WHERE id=?", (project_id,)):
            raise KeyError(f"Project not found: {project_id}")
        workflow_id = new_id("wf")
        now = utc_now()
        state = {
            "workflow_type": workflow_type,
            "options": options or {},
            "step_results": {},
            "repair_attempts": {},
            "repair_overrides": {},
            "public_search_results": None,
        }
        self.db.execute(
            "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (workflow_id, project_id, workflow_type, "RUNNING", 0, json.dumps(state, ensure_ascii=False), now, now),
        )
        self.db.audit("WORKFLOW_STARTED", project_id=project_id, object_id=workflow_id, metadata={"workflow_type": workflow_type})
        return self.get(workflow_id)

    def get(self, workflow_id: str) -> dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM workflows WHERE id=?", (workflow_id,))
        if not row:
            raise KeyError(f"Workflow not found: {workflow_id}")
        row["state"] = json.loads(row.pop("state_json"))
        row["steps"] = WORKFLOWS[row["workflow_type"]]
        return row

    async def advance(self, workflow_id: str) -> dict[str, Any]:
        wf = self.get(workflow_id)
        if wf["status"] in {"COMPLETED", "BLOCKED", "CANCELLED"}:
            return wf
        if self._open_gate(workflow_id):
            self._update(wf, status="WAITING_GATE")
            return self.get(workflow_id)

        wf["status"] = "RUNNING"
        steps = WORKFLOWS[wf["workflow_type"]]
        state = wf["state"]
        while wf["current_step"] < len(steps):
            step = steps[wf["current_step"]]
            if step.get("type") == "PUBLIC_SEARCH":
                try:
                    await self._run_public_search(wf, state)
                except PublicResearchError as exc:
                    state["last_error"] = str(exc)
                    self._update(wf, status="BLOCKED", state=state)
                    return self.get(workflow_id)
                wf["current_step"] += 1
                self._update(wf, current_step=wf["current_step"], state=state)
                continue
            if step.get("type") == "WRITE_SECTIONS":
                result = await self._write_sections(wf, state)
                if result is not None:
                    return result
                wf = self.get(workflow_id)
                state = wf["state"]
                continue
            if step.get("type") == "GATE":
                wf["current_step"] += 1
                self._update(wf, current_step=wf["current_step"], state=state)
                refreshed = self.get(workflow_id)
                self._create_gate(refreshed, step["gate_type"], target_id=workflow_id, questions=[])
                self._update(refreshed, status="WAITING_GATE", state=state)
                return self.get(workflow_id)

            prompt_id = step["prompt_id"]
            try:
                envelope = self.context_builder.build(prompt_id, wf["project_id"], workflow_id=workflow_id, workflow_state=state)
                result = await self.executor.execute(prompt_id, envelope, project_id=wf["project_id"], workflow_id=workflow_id, original_environment=state.get("original_environment"))
            except (PromptExecutionError, ValueError, KeyError) as exc:
                state["last_error"] = str(exc)
                self._update(wf, status="BLOCKED", state=state)
                return self.get(workflow_id)

            state["step_results"][str(wf["current_step"])] = {"prompt_id": prompt_id, "run_id": result["run_id"], "status": result["status"]}
            state["original_environment"] = result["route"]["environment"]
            output = result["output"]
            if result["status"] == "BLOCK":
                self._update(wf, status="BLOCKED", state=state)
                return self.get(workflow_id)
            if result["status"] == "REVISE" and self._can_auto_repair(prompt_id, state):
                repaired = await self._auto_repair(wf, prompt_id, envelope, output, state)
                if repaired:
                    continue
            if result["status"] in {"REVISE", "NEED_USER_INPUT"}:
                gate_type = self.pack.entry(prompt_id).get("next_human_gate") or "PROJECT_GAP_RESOLUTION"
                self._create_gate(wf, gate_type, target_id=result["run_id"], questions=output.get("user_questions", []))
                self._update(wf, status="WAITING_GATE", state=state)
                return self.get(workflow_id)

            next_gate = self.pack.entry(prompt_id).get("next_human_gate")
            wf["current_step"] += 1
            self._update(wf, current_step=wf["current_step"], state=state)
            if next_gate:
                self._create_gate(wf, next_gate, target_id=result["run_id"], questions=output.get("user_questions", []))
                self._update(wf, status="WAITING_GATE", state=state)
                return self.get(workflow_id)

        self._update(wf, status="COMPLETED", state=state)
        self.db.audit("WORKFLOW_COMPLETED", project_id=wf["project_id"], object_id=workflow_id, metadata={"workflow_type": wf["workflow_type"]})
        return self.get(workflow_id)

