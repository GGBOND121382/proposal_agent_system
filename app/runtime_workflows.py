from __future__ import annotations

import json
from typing import Any

from .runtime_executor import RecoverablePromptExecutionError
from .runtime_evidence import InjectedFailure
from .secret_redaction import redact_secret_text
from .util import sha256_json, utc_now
from .workflows import WorkflowEngine as BaseWorkflowEngine
from .workflow_status import (
    WorkflowStatus,
    is_recoverable_block,
    is_terminal,
)

WF3B_IMPORT_PROJECTION_RECOVERY_VERSION = (
    "2026-09-04.v2-claim-bound-sources-request-identity"
)


class RecoverableWorkflowEngine(BaseWorkflowEngine):
    """Workflow facade that resumes stale RUNNING/WAITING_GATE/recoverable BLOCKED states."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._active_workflow_ids: set[str] = set()

    def start(
        self,
        project_id: str,
        workflow_type: str,
        options: dict[str, Any] | None = None,
        *,
        prerequisite_workflow_ids: dict[str, str] | None = None,
        lifecycle_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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
                    existing_bindings = state.get("prerequisite_workflow_ids") or {}
                    if (
                        prerequisite_workflow_ids is not None
                        and dict(existing_bindings) != dict(prerequisite_workflow_ids)
                    ):
                        raise ValueError(
                            "幂等启动键对应的既有工作流前置绑定与本次显式绑定不一致"
                        )
                    return self.get(row["id"])
        result = super().start(
            project_id,
            workflow_type,
            options,
            prerequisite_workflow_ids=prerequisite_workflow_ids,
            lifecycle_context=lifecycle_context,
        )
        if idempotency_key:
            state = result["state"]
            state["start_idempotency_key"] = idempotency_key
            self._update(result, state=state)
            result = self.get(result["id"])
        return result

    def _recover_status(self, wf: dict[str, Any]) -> dict[str, Any]:
        state = wf["state"]
        # WAITING_GATE is reconciled by WorkflowGateMixin.  Missing or stale
        # Gate records are fail-closed there rather than silently reopened.
        recoverable_context_build = str(state.get("last_error") or "").startswith((
            "LIVE context builder produced invalid input:",
            "LIVE context for ",
        ))
        research_failure = (
            state.get("public_research_failure")
            if isinstance(state.get("public_research_failure"), dict)
            else {}
        )
        recoverable_plan_contract = (
            wf["status"] == WorkflowStatus.BLOCKED_CONTRACT.value
            and str(research_failure.get("category") or "").upper() == "PLAN_CONTRACT"
        )
        research_failure_details = (
            research_failure.get("details")
            if isinstance(research_failure.get("details"), dict)
            else {}
        )
        research_sufficiency = (
            research_failure_details.get("research_sufficiency")
            if isinstance(research_failure_details.get("research_sufficiency"), dict)
            else {}
        )
        recoverable_web_provider_alias = (
            wf["status"] == WorkflowStatus.BLOCKED_PROVIDER.value
            and str(research_failure.get("category") or "").upper() == "RETRIEVAL"
            and "REQUIRED_PROVIDER_NOT_EXECUTED:web_search"
            in [str(item) for item in research_sufficiency.get("blocking_reasons") or []]
        )
        last_error = str(state.get("last_error") or "")
        recoverable_wf3b_passage_alias = (
            wf["status"] == WorkflowStatus.BLOCKED_CONTRACT.value
            and str(state.get("workflow_type") or "")
            == "WF-3B_TOPIC_BACKGROUND_RESEARCH"
            and int(wf.get("current_step") or 0) == 5
            and last_error.startswith("Provider output contract validation failed:")
            and "Output provenance is not backed by the trusted input envelope"
            in last_error
        )
        recoverable_wf3b_import_projection = (
            wf["status"] == WorkflowStatus.BLOCKED_CONTRACT.value
            and str(state.get("workflow_type") or "")
            == "WF-3B_TOPIC_BACKGROUND_RESEARCH"
            and int(wf.get("current_step") or 0) == 7
            and last_error.startswith(
                "WF-3 provider request exceeds its deterministic node budget"
            )
            and str(
                (state.get("wf3b_import_projection_recovery") or {}).get("version")
                if isinstance(state.get("wf3b_import_projection_recovery"), dict)
                else ""
            )
            != WF3B_IMPORT_PROJECTION_RECOVERY_VERSION
        )
        if (
            (
                wf["status"] == WorkflowStatus.BLOCKED_TECHNICAL.value
                and (state.get("runtime_recoverable") or recoverable_context_build)
            )
            or recoverable_plan_contract
            or recoverable_web_provider_alias
            or recoverable_wf3b_passage_alias
            or recoverable_wf3b_import_projection
        ):
            state["recovered_from"] = (
                state.get("runtime_failure_point")
                or (
                    "PUBLIC_RESEARCH_PLAN_CONTRACT"
                    if recoverable_plan_contract
                    else (
                        "PUBLIC_RESEARCH_WEB_PROVIDER_ALIAS"
                        if recoverable_web_provider_alias
                        else (
                            "WF3B_PASSAGE_SOURCE_ALIAS"
                            if recoverable_wf3b_passage_alias
                            else (
                                "WF3B_IMPORT_CLAIM_BOUND_SOURCE_PROJECTION"
                                if recoverable_wf3b_import_projection
                                else "RECOVERABLE_BLOCK"
                            )
                        )
                    )
                )
            )
            state["runtime_recoverable"] = False
            state.pop("last_error", None)
            if recoverable_plan_contract or recoverable_web_provider_alias:
                state.pop("public_research_failure", None)
            if recoverable_wf3b_import_projection:
                state["wf3b_import_projection_recovery"] = {
                    "version": WF3B_IMPORT_PROJECTION_RECOVERY_VERSION,
                    "recovered_at": utc_now(),
                }
            self._update(wf, status=WorkflowStatus.RUNNING.value, state=state)
            return self.get(wf["id"])
        return wf

    async def advance(self, workflow_id: str) -> dict[str, Any]:
        if workflow_id in self._active_workflow_ids:
            return self.get(workflow_id)
        self._active_workflow_ids.add(workflow_id)
        try:
            return await self._advance_once(workflow_id)
        finally:
            self._active_workflow_ids.discard(workflow_id)

    async def _advance_once(self, workflow_id: str) -> dict[str, Any]:
        wf = self._recover_status(self.get(workflow_id))
        if is_terminal(wf["status"]):
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
            if is_recoverable_block(result["status"]) and last_error.startswith("INJECTED_FAILURE:"):
                parts = last_error.split(":", 2)
                state["runtime_recoverable"] = True
                state["runtime_failure_point"] = parts[1] if len(parts) > 1 else "UNKNOWN"
                state["runtime_blocked_at"] = utc_now()
                self._update(result, status=WorkflowStatus.BLOCKED_TECHNICAL.value, state=state)
                result = self.get(workflow_id)
            if result["status"] == WorkflowStatus.WAITING_GATE.value and faults:
                faults.hit("after_gate_created", call_key)
            if faults:
                faults.hit("after_workflow_advance", call_key)
            return result
        except (InjectedFailure, RecoverablePromptExecutionError) as exc:
            current = self.get(workflow_id)
            state = current["state"]
            state["last_error"] = redact_secret_text(str(exc))
            state["runtime_recoverable"] = True
            state["runtime_failure_point"] = getattr(exc, "point", "WORKFLOW_ADVANCE")
            state["runtime_blocked_at"] = utc_now()
            self._update(current, status=WorkflowStatus.BLOCKED_TECHNICAL.value, state=state)
            return self.get(workflow_id)
        except Exception as exc:
            current = self.get(workflow_id)
            state = current["state"]
            report = self._runtime_configuration_report(
                exc,
                scope="UNEXPECTED_RUNTIME_CONFIGURATION",
            )
            if report is not None:
                return self._pause_for_configuration(
                    current,
                    state,
                    report,
                    source="UNEXPECTED_RUNTIME_CONFIGURATION",
                )
            state["last_error"] = redact_secret_text(f"UNEXPECTED_RUNTIME_ERROR: {type(exc).__name__}: {exc}")
            state["runtime_recoverable"] = True
            state["runtime_failure_point"] = "WORKFLOW_ADVANCE"
            state["runtime_blocked_at"] = utc_now()
            self._update(current, status=WorkflowStatus.BLOCKED_TECHNICAL.value, state=state)
            self.db.audit(
                "WORKFLOW_RUNTIME_EXCEPTION",
                project_id=current["project_id"],
                object_id=workflow_id,
                metadata={"exception_type": type(exc).__name__, "message": str(exc)},
            )
            return self.get(workflow_id)
