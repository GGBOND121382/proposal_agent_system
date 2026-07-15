from __future__ import annotations

import json
from typing import Any

from .runtime_executor import RecoverablePromptExecutionError
from .runtime_evidence import InjectedFailure
from .util import sha256_json, utc_now
from .workflows import WorkflowEngine as BaseWorkflowEngine


class RecoverableWorkflowEngine(BaseWorkflowEngine):
    """Workflow facade that resumes stale RUNNING/WAITING_GATE/recoverable BLOCKED states."""

    def start(self, project_id: str, workflow_type: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
        options = options or {}
        idempotency_key = str(options.get("idempotency_key") or "").strip()
        if idempotency_key:
            existing = self.db.fetchall(
                "SELECT * FROM workflows WHERE project_id=? AND workflow_type=? ORDER BY created_at DESC",
                (project_id, workflow_type),
            )
            for row in existing:
                state = json.loads(row["state_json"])
                if state.get("start_idempotency_key") == idempotency_key:
                    return self.get(row["id"])
        result = super().start(project_id, workflow_type, options)
        if idempotency_key:
            state = result["state"]
            state["start_idempotency_key"] = idempotency_key
            self._update(result, state=state)
            result = self.get(result["id"])
        return result

    def _recover_status(self, wf: dict[str, Any]) -> dict[str, Any]:
        state = wf["state"]
        if wf["status"] == "WAITING_GATE" and not self._open_gate(wf["id"]):
            state["recovered_from"] = "WAITING_GATE_WITHOUT_OPEN_GATE"
            self._update(wf, status="RUNNING", state=state)
            return self.get(wf["id"])
        if wf["status"] == "BLOCKED" and state.get("runtime_recoverable"):
            state["recovered_from"] = state.get("runtime_failure_point") or "RECOVERABLE_BLOCK"
            state["runtime_recoverable"] = False
            state.pop("last_error", None)
            self._update(wf, status="RUNNING", state=state)
            return self.get(wf["id"])
        return wf

    async def advance(self, workflow_id: str) -> dict[str, Any]:
        wf = self._recover_status(self.get(workflow_id))
        if wf["status"] in {"COMPLETED", "CANCELLED"}:
            return wf
        call_key = "workflow-" + sha256_json(
            {"workflow_id": workflow_id, "step": wf["current_step"], "status": wf["status"]}
        )[:24]
        faults = getattr(getattr(self.executor, "evidence_store", None), "faults", None)
        try:
            if faults:
                faults.hit("before_workflow_advance", call_key)
            result = await super().advance(workflow_id)
            state = result["state"]
            last_error = str(state.get("last_error") or "")
            if result["status"] == "BLOCKED" and last_error.startswith("INJECTED_FAILURE:"):
                parts = last_error.split(":", 2)
                state["runtime_recoverable"] = True
                state["runtime_failure_point"] = parts[1] if len(parts) > 1 else "UNKNOWN"
                state["runtime_blocked_at"] = utc_now()
                self._update(result, status="BLOCKED", state=state)
                result = self.get(workflow_id)
            if result["status"] == "WAITING_GATE" and faults:
                faults.hit("after_gate_created", call_key)
            if faults:
                faults.hit("after_workflow_advance", call_key)
            return result
        except (InjectedFailure, RecoverablePromptExecutionError) as exc:
            current = self.get(workflow_id)
            state = current["state"]
            state["last_error"] = str(exc)
            state["runtime_recoverable"] = True
            state["runtime_failure_point"] = getattr(exc, "point", "WORKFLOW_ADVANCE")
            state["runtime_blocked_at"] = utc_now()
            self._update(current, status="BLOCKED", state=state)
            return self.get(workflow_id)
