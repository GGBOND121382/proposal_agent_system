from __future__ import annotations

import json
from typing import Any

from .runtime_executor import RecoverablePromptExecutionError
from .runtime_evidence import InjectedFailure
from .util import new_id, sha256_json, utc_now
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
        last_error = str(state.get("last_error") or "")
        if (
            wf["status"] == "BLOCKED"
            and wf.get("workflow_type") == "WF-5_SECURITY_REVIEW_AND_EXPORT"
            and "存在未完成独立复审的P0/P1质量问题" in last_error
        ):
            # A code upgrade may replace the obsolete project-wide acceptance query
            # with exact frozen-lineage validation.  Reopen only after the current
            # quality manager proves that the bound WF-4/WF-5 lineage is clean; the
            # historical rejected-candidate findings remain append-only evidence.
            try:
                self.quality_manager.assert_no_active_lineage_blockers(
                    wf["project_id"], review_workflow_id=wf["id"]
                )
            except Exception:
                return wf
            state["recovered_from"] = "ACTIVE_LINEAGE_QUALITY_REVALIDATED"
            state["invalid_project_wide_quality_error"] = last_error
            state.pop("last_error", None)
            state.pop("quality_blocker_ids", None)
            self._update(wf, status="RUNNING", state=state)
            self.db.audit(
                "WORKFLOW_ACTIVE_LINEAGE_QUALITY_REVALIDATED",
                project_id=wf["project_id"],
                object_id=wf["id"],
                metadata={"previous_error": last_error},
            )
            return self.get(wf["id"])
        if (
            wf["status"] == "BLOCKED"
            and "Fresh generation refused committed result reuse:" in last_error
            and state.get("integration_repair_section_ids")
            and state.get("full_proposal_children")
        ):
            # Checkpoint migration for full-proposal repair rounds created before
            # generation-attempt scoping existed.  The executor's strict fresh-mode
            # contamination guard remains unchanged; we reopen only a persisted,
            # explicitly scheduled writing repair and give that repair one durable
            # new identity.  Ordinary duplicate calls stay blocked.
            child_ids = {
                str(item.get("workflow_id"))
                for item in (state.get("full_proposal_children") or {}).values()
                if isinstance(item, dict) and item.get("workflow_id")
            }
            owned = True
            for child_id in child_ids:
                row = self.db.fetchone(
                    "SELECT state_json FROM workflows WHERE id=? AND project_id=?",
                    (child_id, wf["project_id"]),
                )
                if not row:
                    owned = False
                    break
                try:
                    child_state = json.loads(row.get("state_json") or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    owned = False
                    break
                if str(child_state.get("parent_workflow_id") or "") != wf["id"]:
                    owned = False
                    break
            if child_ids and owned:
                state["full_proposal_repair_attempt_id"] = (
                    str(state.get("full_proposal_repair_attempt_id") or "").strip()
                    or new_id("generation-repair")
                )
                state["recovered_from"] = "LEGACY_FULL_PROPOSAL_REPAIR_IDENTITY"
                state["invalid_generation_collision_error"] = last_error
                state.pop("last_error", None)
                self._update(wf, status="RUNNING", state=state)
                self.db.audit(
                    "WORKFLOW_FULL_PROPOSAL_REPAIR_IDENTITY_MIGRATED",
                    project_id=wf["project_id"],
                    object_id=wf["id"],
                    metadata={
                        "repair_attempt_id": state["full_proposal_repair_attempt_id"],
                        "child_workflow_ids": sorted(child_ids),
                    },
                )
                return self.get(wf["id"])
        if wf["status"] == "BLOCKED" and last_error == "PUBLIC_SEARCH_PROVIDER is disabled":
            # A portable run may attach a public-search transport after the
            # workflow was created. Reopen only when the currently configured
            # research service can prove that its provider is usable.
            ready = getattr(self.research_service, "provider_ready", lambda: False)()
            if ready:
                state["recovered_from"] = "PUBLIC_SEARCH_PROVIDER_REVALIDATED"
                state["invalid_public_search_error"] = last_error
                state.pop("last_error", None)
                self._update(wf, status="RUNNING", state=state)
                self.db.audit(
                    "WORKFLOW_PUBLIC_SEARCH_PROVIDER_REVALIDATED",
                    project_id=wf["project_id"],
                    object_id=wf["id"],
                    metadata={"previous_error": last_error},
                )
                return self.get(wf["id"])
        if wf["status"] == "BLOCKED" and last_error.startswith("No eligible model route:"):
            # Model transports can be replaced between restarts (for example an
            # audited HTTP endpoint may be replaced by CHAT_BRIDGE). Reopen the
            # workflow only after rebuilding the exact current Prompt and proving
            # that the current router accepts it under the unchanged security
            # context. The original routing failure remains append-only evidence.
            from .workflow_defs import WORKFLOWS
            steps = WORKFLOWS.get(wf["workflow_type"], [])
            if 0 <= int(wf["current_step"]) < len(steps):
                prompt_id = steps[int(wf["current_step"])].get("prompt_id")
                if prompt_id:
                    try:
                        envelope = self.context_builder.build(
                            str(prompt_id),
                            wf["project_id"],
                            workflow_id=wf["id"],
                            workflow_state=state,
                        )
                        self.executor.router.route(str(prompt_id), envelope)
                    except (ValueError, KeyError, RuntimeError):
                        return wf
                    state["recovered_from"] = "MODEL_ROUTE_REVALIDATED"
                    state["invalid_route_error"] = last_error
                    state.pop("last_error", None)
                    self._update(wf, status="RUNNING", state=state)
                    self.db.audit(
                        "WORKFLOW_MODEL_ROUTE_REVALIDATED",
                        project_id=wf["project_id"],
                        object_id=wf["id"],
                        metadata={"prompt_id": prompt_id, "previous_error": last_error},
                    )
                    return self.get(wf["id"])
        if wf["status"] == "BLOCKED" and last_error.startswith("LIVE context builder produced invalid input:"):
            # A provider-independent context/schema defect may be fixed between
            # restarts.  Rebuild the exact current Prompt before reopening the
            # workflow; persistent invalid input remains blocked and visible.
            from .workflow_defs import WORKFLOWS
            steps = WORKFLOWS.get(wf["workflow_type"], [])
            if 0 <= int(wf["current_step"]) < len(steps):
                prompt_id = steps[int(wf["current_step"])].get("prompt_id")
                if prompt_id:
                    try:
                        self.context_builder.build(
                            str(prompt_id),
                            wf["project_id"],
                            workflow_id=wf["id"],
                            workflow_state=state,
                        )
                    except (ValueError, KeyError):
                        return wf
                    state["recovered_from"] = "CONTEXT_SCHEMA_REVALIDATED"
                    state["invalid_context_error"] = last_error
                    state.pop("last_error", None)
                    self._update(wf, status="RUNNING", state=state)
                    self.db.audit(
                        "WORKFLOW_CONTEXT_REVALIDATED",
                        project_id=wf["project_id"],
                        object_id=wf["id"],
                        metadata={"prompt_id": prompt_id, "previous_error": last_error},
                    )
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
