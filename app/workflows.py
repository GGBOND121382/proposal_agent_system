from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

from .dependency_preflight import DependencyIssue, DependencyReport
from .executor import PromptExecutionError
from .llm import MODEL_RESPONSE_PROTOCOL_VERSION
from .decision_arbiter import DecisionArbiter
from .repair_ledger import RepairLedger
from .retry_policy import ProviderRetriesExhausted, RetryPolicy
from .runtime_failures import (
    FailureCategory,
    classify_runtime_failure,
    semantic_revise_classification,
)
from .quality import QualityGateBlocked, QualityLifecycleManager
from .quality_guard import QualityGuardContractError, require_guard_report
from .research import PublicResearchError
from .util import new_id, sha256_json, utc_now
from .workflow_authoring import WorkflowAuthoringMixin
from .workflow_defs import CRITIC_PRODUCER, WORKFLOWS
from .workflow_gates import WorkflowGateMixin
from .workflow_repair import WorkflowRepairMixin
from .wf3_input import WorkflowInputRequired
from .workflow_status import (
    WorkflowStatus,
    is_terminal,
    occupies_workflow_slot,
)


def technical_retry_key(
    step_key: str,
    state: dict[str, Any],
    *,
    is_section_step: bool,
) -> str:
    """Return the retry identity for one technical execution phase.

    Section authoring retries are isolated by section and phase.  Other
    workflow steps retain their ordinary step key even if a stale active
    section remains in workflow state.
    """

    if not is_section_step:
        return step_key
    section_id = str(state.get("active_section_id") or "").strip()
    progress = (
        (state.get("section_progress") or {}).get(section_id)
        if section_id
        else None
    )
    phase = (
        str(progress.get("phase") or "").strip()
        if isinstance(progress, dict)
        else ""
    )
    if section_id and phase:
        return f"{step_key}:{section_id}:{phase}"
    return step_key


class WorkflowEngine(WorkflowAuthoringMixin, WorkflowRepairMixin, WorkflowGateMixin):
    def __init__(self, db, pack, context_builder, executor, research_service, diagram_enrichment=None, quality_manager=None, dependency_preflight=None):
        self.db = db
        self.pack = pack
        self.context_builder = context_builder
        self.executor = executor
        self.research_service = research_service
        self.diagram_enrichment = diagram_enrichment
        self.quality_manager = quality_manager or QualityLifecycleManager(db)
        self.dependency_preflight = dependency_preflight
        self.decision_arbiter = DecisionArbiter()

    def _record_runtime_failure(
        self,
        wf: dict[str, Any],
        state: dict[str, Any],
        *,
        prompt_id: str,
        exc: BaseException,
    ) -> dict[str, Any]:
        classification = classify_runtime_failure(exc)
        payload = {
            **classification.to_dict(),
            "prompt_id": prompt_id,
            "workflow_id": wf["id"],
            "step": wf["current_step"],
            "error_type": exc.__class__.__name__,
            "error": str(exc),
            "recorded_at": utc_now(),
        }
        state.setdefault("runtime_failure_history", []).append(payload)
        del state["runtime_failure_history"][:-100]
        row = self.db.fetchone(
            "SELECT COALESCE(MAX(version),0) AS v FROM artifacts WHERE project_id=? AND workflow_id=? AND artifact_type='RUNTIME_FAILURE'",
            (wf["project_id"], wf["id"]),
        )
        self.db.execute(
            """INSERT INTO artifacts(id,project_id,workflow_id,artifact_type,prompt_id,version,status,security_level,context_hash,content_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                new_id("artifact"), wf["project_id"], wf["id"], "RUNTIME_FAILURE",
                prompt_id, int((row or {}).get("v") or 0) + 1, classification.category.value,
                self._project_level(wf["project_id"]),
                __import__("hashlib").sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                json.dumps(payload, ensure_ascii=False), payload["recorded_at"],
            ),
        )
        return payload

    async def _execute_prompt_with_provider_retry(
        self,
        wf: dict[str, Any],
        state: dict[str, Any],
        *,
        prompt_id: str,
        envelope: dict[str, Any],
        call_key: str | None = None,
        retry_categories: frozenset[FailureCategory] | None = None,
    ) -> dict[str, Any]:
        """Execute one business node with a finite provider retry policy.

        The retry loop surrounds only the provider-backed prompt execution.  It
        does not rebuild workflow state, re-enter repair bookkeeping, or consume
        semantic repair budget.  Each failed provider attempt is persisted as a
        RUNTIME_FAILURE artifact and each actual retry is an independent ledger
        event.
        """

        policy = RetryPolicy.from_options(state.get("options") or {})
        section_id = str(state.get("active_section_id") or "").strip()
        section_progress = (
            (state.get("section_progress") or {}).get(section_id)
            if section_id
            else None
        )
        section_phase = (
            str(section_progress.get("phase") or "").strip()
            if isinstance(section_progress, dict)
            else ""
        )
        retry_key = f"{wf['current_step']}:{prompt_id}"
        if section_id and section_phase:
            retry_key = f"{retry_key}:{section_id}:{section_phase}"
        input_hash = sha256_json(envelope)
        cycles = state.setdefault("provider_call_cycles", {})
        prior_cycle = cycles.get(retry_key) or {}
        if (
            prior_cycle.get("input_hash") != input_hash
            or prior_cycle.get("protocol_version") != MODEL_RESPONSE_PROTOCOL_VERSION
        ):
            generation = int(prior_cycle.get("generation") or 0) + 1
            cycle_id = sha256_json(
                {
                    "workflow_id": wf["id"],
                    "retry_key": retry_key,
                    "input_hash": input_hash,
                    "protocol_version": MODEL_RESPONSE_PROTOCOL_VERSION,
                    "generation": generation,
                }
            )[:16]
            prior_cycle = {
                "cycle_id": cycle_id,
                "generation": generation,
                "input_hash": input_hash,
                "protocol_version": MODEL_RESPONSE_PROTOCOL_VERSION,
                "created_at": utc_now(),
            }
            cycles[retry_key] = prior_cycle
            # Persist before the provider call.  A crash may safely replay the
            # same attempt, while a later retry receives a distinct attempt key.
            self._update(wf, state=state)
        cycle_id = str(prior_cycle["cycle_id"])
        base_call_key = call_key or (
            "call-provider-" + sha256_json(
                {
                    "workflow_id": wf["id"],
                    "retry_key": retry_key,
                    "input_hash": input_hash,
                    "protocol_version": MODEL_RESPONSE_PROTOCOL_VERSION,
                }
            )[:24]
        )
        completed_attempts = 0

        while True:
            completed_attempts += 1
            attempt_call_key = (
                f"{base_call_key}-cycle-{cycle_id}-attempt-{completed_attempts}"
            )
            try:
                result = await self.executor.execute(
                    prompt_id,
                    envelope,
                    project_id=wf["project_id"],
                    workflow_id=wf["id"],
                    original_environment=state.get("original_environment"),
                    call_key=attempt_call_key,
                )
            except (PromptExecutionError, ValueError, KeyError) as exc:
                classification = classify_runtime_failure(exc)
                if (
                    not classification.retryable
                    or (
                        retry_categories is not None
                        and classification.category not in retry_categories
                    )
                ):
                    raise

                failure = self._record_runtime_failure(
                    wf,
                    state,
                    prompt_id=prompt_id,
                    exc=exc,
                )
                decision = policy.decide(
                    classification,
                    completed_attempts=completed_attempts,
                )
                state["provider_wait"] = {
                    "retry_key": retry_key,
                    "completed_attempts": completed_attempts,
                    "retry_number": decision.retry_number,
                    "max_retries": decision.max_retries,
                    "max_attempts": decision.max_attempts,
                    "delay_seconds": decision.delay_seconds,
                    "prompt_id": prompt_id,
                    "failure_kind": classification.failure_kind,
                    "http_status": classification.http_status,
                    "retry_after_seconds": classification.retry_after_seconds,
                    "decision": decision.to_dict(),
                }

                if not decision.should_retry:
                    raise ProviderRetriesExhausted(
                        exc,
                        classification=classification,
                        decision=decision,
                        failure_payload=failure,
                    ) from exc

                RepairLedger.provider_retry(
                    state,
                    retry_key,
                    details={
                        "prompt_id": prompt_id,
                        "completed_attempts": completed_attempts,
                        "next_attempt": completed_attempts + 1,
                        "failure_kind": classification.failure_kind,
                        "http_status": classification.http_status,
                        "delay_seconds": decision.delay_seconds,
                        "reason": decision.reason,
                    },
                )
                self._update(wf, state=state)
                if decision.delay_seconds > 0:
                    await asyncio.sleep(decision.delay_seconds)
                continue

            if completed_attempts > 1:
                RepairLedger.provider_recovered(
                    state,
                    retry_key,
                    details={
                        "prompt_id": prompt_id,
                        "completed_attempts": completed_attempts,
                        "retries_used": completed_attempts - 1,
                    },
                )
                state.pop("provider_wait", None)
                self._update(wf, state=state)
            return result

    def _block_provider_retries_exhausted(
        self,
        wf: dict[str, Any],
        state: dict[str, Any],
        exc: ProviderRetriesExhausted,
        *,
        boundary: str,
    ) -> dict[str, Any]:
        """Persist one typed provider-exhaustion outcome at any node boundary."""

        state["last_error"] = str(exc.original_exception)
        state["provider_wait"] = {
            **(state.get("provider_wait") or {}),
            "exhausted": True,
            "exhausted_status": exc.decision.exhausted_status,
            "failure": exc.failure_payload,
            "boundary": boundary,
        }
        state.pop("runtime_recoverable", None)
        state.pop("runtime_failure_point", None)
        state.pop("runtime_blocked_at", None)
        self._update(
            wf,
            status=exc.decision.exhausted_status,
            state=state,
        )
        self.db.audit(
            "PROVIDER_RETRIES_EXHAUSTED",
            project_id=wf["project_id"],
            object_id=wf["id"],
            metadata={
                "boundary": boundary,
                "prompt_id": exc.failure_payload.get("prompt_id"),
                "category": exc.classification.category.value,
                "failure_kind": exc.classification.failure_kind,
                "completed_attempts": exc.decision.completed_attempts,
                "max_retries": exc.decision.max_retries,
                "workflow_status": exc.decision.exhausted_status,
            },
        )
        return self.get(wf["id"])

    def _recover_provider_block_after_protocol_upgrade(
        self,
        wf: dict[str, Any],
        state: dict[str, Any],
    ) -> bool:
        """Resume an exhausted provider node only when its wire protocol changed.

        A response-protocol deployment creates a new generation of the same
        logical provider call.  Persisted semantic progress remains valid, but
        the old generation's exhausted wait state must not prevent the new
        adapter from receiving its own finite retry cycle.
        """

        if wf["status"] not in {
            WorkflowStatus.BLOCKED_PROVIDER.value,
            WorkflowStatus.BLOCKED_CONTRACT.value,
        }:
            return False
        wait = state.get("provider_wait") or {}
        if not wait.get("exhausted"):
            return False
        failure = wait.get("failure") or {}
        if not bool(failure.get("retryable")):
            return False
        retry_key = str(wait.get("retry_key") or "").strip()
        prior_cycle = (state.get("provider_call_cycles") or {}).get(retry_key) or {}
        prior_protocol = str(prior_cycle.get("protocol_version") or "").strip()
        if not prior_protocol or prior_protocol == MODEL_RESPONSE_PROTOCOL_VERSION:
            return False

        recovered_at = utc_now()
        recovery = {
            "reason": "MODEL_RESPONSE_PROTOCOL_UPGRADED",
            "retry_key": retry_key,
            "from_status": wf["status"],
            "to_status": WorkflowStatus.RUNNING.value,
            "from_protocol_version": prior_protocol,
            "to_protocol_version": MODEL_RESPONSE_PROTOCOL_VERSION,
            "preserved_section_id": state.get("active_section_id"),
            "preserved_phase": (
                ((state.get("section_progress") or {}).get(
                    str(state.get("active_section_id") or "")
                ) or {}).get("phase")
            ),
            "recovered_at": recovered_at,
        }
        state.setdefault("checkpoint_recovery_history", []).append(recovery)
        RepairLedger.record(
            state,
            bucket="provider_retries",
            key=retry_key,
            event="PROVIDER_PROTOCOL_UPGRADED",
            details=recovery,
        )
        state["recovered_from"] = wf["status"]
        state.pop("provider_wait", None)
        state.pop("last_error", None)
        self._update(
            wf,
            status=WorkflowStatus.RUNNING.value,
            state=state,
        )
        self.db.audit(
            "PROVIDER_PROTOCOL_CHECKPOINT_RECOVERED",
            project_id=wf["project_id"],
            object_id=wf["id"],
            metadata=recovery,
        )
        return True

    def _record_decision(
        self,
        wf: dict[str, Any],
        state: dict[str, Any],
        prompt_id: str,
        result: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
        output = result.get("output") or {}
        raw_status = str(result.get("status") or output.get("status") or "ERROR")
        is_critic = prompt_id in CRITIC_PRODUCER or "CRITIC" in prompt_id

        try:
            guard_report = require_guard_report(
                result,
                prompt_id=prompt_id,
                model_output=output,
                default_guard_enabled=getattr(
                    self.executor, "quality_guard_enabled", None
                ),
            )
        except QualityGuardContractError as exc:
            raise PromptExecutionError(str(exc)) from exc
        if not is_critic and str(guard_report.get("status") or "PASS") == "PASS":
            return None, raw_status, copy.deepcopy(output)
        record = self.decision_arbiter.arbitrate(
            output, guard_report, prompt_id=prompt_id
        )
        payload = record.to_dict()
        effective_status, effective_output = self._effective_critic_result(result, payload)
        step_result = state.setdefault("step_results", {}).setdefault(
            str(wf["current_step"]), {}
        )
        step_result["model_status"] = raw_status
        step_result["effective_status"] = effective_status
        artifact_id, updated_at = self.decision_arbiter.persist(
            self.db,
            project_id=wf["project_id"],
            workflow_id=wf["id"],
            prompt_id=prompt_id,
            record=record,
            security_level=self._project_level(wf["project_id"]),
            workflow_state=state,
            workflow_status=wf["status"],
            current_step=wf["current_step"],
            expected_updated_at=wf.get("updated_at"),
        )
        wf["updated_at"] = updated_at
        payload["artifact_id"] = artifact_id
        return payload, effective_status, effective_output


    @staticmethod
    def _effective_critic_result(
        result: dict[str, Any],
        decision: dict[str, Any] | None,
    ) -> tuple[str, dict[str, Any]]:
        output = copy.deepcopy(result.get("output") or {})
        if not decision:
            return str(result.get("status") or output.get("status") or "ERROR"), output
        mapping = {
            "PASS": "PASS",
            "REVISE": "REVISE",
            "BLOCK": "BLOCK",
            "WAITING_HUMAN_INPUT": "NEED_USER_INPUT",
            "CONTRACT_CONFLICT": "BLOCK",
        }
        effective_status = mapping.get(
            str(decision.get("decision") or ""),
            str(result.get("status") or output.get("status") or "ERROR"),
        )
        actionable = []
        for entry in (decision.get("decision_basis") or {}).get("actionable_findings") or []:
            if isinstance(entry, dict) and isinstance(entry.get("finding"), dict):
                finding = copy.deepcopy(entry["finding"])
                # The decision source is audit metadata, not part of the common
                # Finding schema passed to repair prompts.
                finding.pop("rule_id", None)
                finding.pop("responsibility", None)
                finding.pop("source", None)
                actionable.append(finding)
        output["status"] = effective_status
        output["findings"] = actionable
        return effective_status, output


    def _observe_quality_result(self, wf: dict[str, Any], state: dict[str, Any], prompt_id: str, result: dict[str, Any]) -> None:
        quality_workflow_id = str(state.get("quality_parent_workflow_id") or wf["id"])
        self.quality_manager.observe_prompt_result(
            project_id=wf["project_id"],
            workflow_id=quality_workflow_id,
            prompt_id=prompt_id,
            run_id=result["run_id"],
            status=result["status"],
            output=result["output"],
            workflow_state=state,
        )

    @staticmethod
    def _unaccepted_completion_blockers(
        blockers: list[dict[str, Any]],
        state: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Keep accepted intake gaps open without blocking completion of the stage.

        A gate decision does not verify or close a quality finding. It can accept
        a NEED_USER_INPUT/REVISE result as the documented output of an intake
        stage. The finding stays open for downstream repair and final export.
        Deterministic QG findings are never eligible for this stage acceptance.
        """
        accepted_run_ids = {
            str(item.get("run_id"))
            for item in (state.get("accepted_step_results") or {}).values()
            if isinstance(item, dict) and item.get("run_id")
        }
        remaining: list[dict[str, Any]] = []
        accepted: list[dict[str, Any]] = []
        for record in blockers:
            finding = record.get("finding") or {}
            code = str(finding.get("code") or "")
            opened_run_id = str(
                (record.get("lifecycle") or {}).get("opened_by", {}).get("run_id") or ""
            )
            if opened_run_id in accepted_run_ids and not code.startswith("QG_"):
                accepted.append(record)
            else:
                remaining.append(record)
        return remaining, accepted

    def _required_workflow_types(
        self,
        project_id: str,
        workflow_type: str,
        options: dict[str, Any],
    ) -> list[str]:
        required: list[str] = []
        if workflow_type == "WF-3_HYBRID_ONLINE_ASSIST":
            required = ["WF-1_PROJECT_INTAKE"]
        elif workflow_type == "WF-4_PROPOSAL_AUTHORING":
            required = ["WF-1_PROJECT_INTAKE", "WF-2_TEMPLATE_EXTRACTION"]
            project = self.db.fetchone("SELECT config_json FROM projects WHERE id=?", (project_id,)) or {}
            config = json.loads(project.get("config_json") or "{}")
            if bool(options.get("require_public_research", config.get("require_public_research", False))):
                required.append("WF-3_HYBRID_ONLINE_ASSIST")
        elif workflow_type == "WF-5_SECURITY_REVIEW_AND_EXPORT":
            required = ["WF-4_PROPOSAL_AUTHORING"]
        return required

    def _resolve_prerequisite_workflows(
        self,
        project_id: str,
        workflow_type: str,
        options: dict[str, Any],
    ) -> tuple[dict[str, str], list[str]]:
        """Resolve and freeze the concrete completed workflows consumed downstream."""
        bindings: dict[str, str] = {}
        missing: list[str] = []
        for required_type in self._required_workflow_types(project_id, workflow_type, options):
            if required_type == "WF-4_PROPOSAL_AUTHORING":
                rows = self.db.fetchall(
                    "SELECT id,state_json FROM workflows WHERE project_id=? AND workflow_type=? AND status='COMPLETED' ORDER BY updated_at DESC",
                    (project_id, required_type),
                )
                row = next(
                    (
                        item
                        for item in rows
                        if not json.loads(item.get("state_json") or "{}").get("parent_workflow_id")
                    ),
                    None,
                )
            else:
                row = self.db.fetchone(
                    "SELECT id FROM workflows WHERE project_id=? AND workflow_type=? AND status='COMPLETED' ORDER BY updated_at DESC LIMIT 1",
                    (project_id, required_type),
                )
            if row:
                bindings[required_type] = str(row["id"])
            else:
                missing.append(required_type)
        # WF-3 is optional for authoring, but an existing completed and approved
        # research workflow is still a valid evidence source. Freeze it into the
        # WF-4 lineage even when the caller did not make public research mandatory.
        if (
            workflow_type == "WF-4_PROPOSAL_AUTHORING"
            and "WF-3_HYBRID_ONLINE_ASSIST" not in bindings
        ):
            optional_public_research = self.db.fetchone(
                """SELECT id FROM workflows
                    WHERE project_id=?
                      AND workflow_type='WF-3_HYBRID_ONLINE_ASSIST'
                      AND status='COMPLETED'
                    ORDER BY updated_at DESC LIMIT 1""",
                (project_id,),
            )
            if optional_public_research:
                bindings["WF-3_HYBRID_ONLINE_ASSIST"] = str(
                    optional_public_research["id"]
                )
        return bindings, missing

    @staticmethod
    def _prerequisite_error(missing: list[str]) -> str | None:
        if not missing:
            return None
        return (
            "工作流前置条件未满足："
            + "、".join(missing)
            + "。不得使用Replay样例或空上下文代替已完成的前序结果。"
        )

    def _pause_for_configuration(
        self,
        wf: dict[str, Any],
        state: dict[str, Any],
        report: DependencyReport,
        *,
        source: str,
    ) -> dict[str, Any]:
        payload = report.as_dict()
        payload.update(
            {
                "workflow_id": wf["id"],
                "workflow_type": wf["workflow_type"],
                "resume_step": int(wf["current_step"]),
                "source": source,
            }
        )
        previous = state.get("configuration_wait") or {}
        if previous.get("first_detected_at"):
            payload["first_detected_at"] = previous["first_detected_at"]
        else:
            payload["first_detected_at"] = utc_now()
        payload["last_checked_at"] = utc_now()
        state["configuration_wait"] = payload
        state["last_error"] = report.summary()
        state.pop("runtime_recoverable", None)
        state.pop("runtime_failure_point", None)
        self._update(wf, status="WAITING_CONFIGURATION", state=state)
        self.db.audit(
            "WORKFLOW_WAITING_CONFIGURATION",
            project_id=wf["project_id"],
            object_id=wf["id"],
            metadata={
                "source": source,
                "resume_step": int(wf["current_step"]),
                "issues": [item.as_dict() for item in report.blocking_issues],
            },
        )
        return self.get(wf["id"])

    @staticmethod
    def _clear_configuration_wait(state: dict[str, Any]) -> None:
        previous = state.pop("configuration_wait", None)
        if previous:
            state["configuration_recovered"] = {
                "recovered_at": utc_now(),
                "previous": previous,
            }
        if str(state.get("last_error") or "").startswith("运行依赖未满足："):
            state.pop("last_error", None)

    def _workflow_dependency_report(
        self,
        project_id: str,
        workflow_type: str,
        options: dict[str, Any],
    ) -> DependencyReport | None:
        if self.dependency_preflight is None:
            return None
        return self.dependency_preflight.workflow_report(
            project_id,
            workflow_type,
            options,
        )

    def _configuration_recheck_report(
        self,
        wf: dict[str, Any],
        state: dict[str, Any],
    ) -> DependencyReport | None:
        """Recheck workflow-wide and current-step dependencies before resume."""
        if self.dependency_preflight is None:
            return None
        report = self.dependency_preflight.workflow_report(
            wf["project_id"],
            wf["workflow_type"],
            state.get("options") or {},
        )
        steps = WORKFLOWS.get(wf["workflow_type"], [])
        current_step = int(wf.get("current_step") or 0)
        if current_step < len(steps):
            step = steps[current_step]
            public_plan = None
            if step.get("type") == "PUBLIC_SEARCH" and hasattr(
                self.context_builder, "_result"
            ):
                public_plan = self._context_result(
                    wf["project_id"],
                    "P-PUBLIC-RESEARCH-PLAN",
                    workflow_id=wf["id"],
                    exact_workflow=True,
                ) or {}
            report.extend(
                self.dependency_preflight.step_report(
                    wf["project_id"],
                    wf["workflow_type"],
                    step,
                    state,
                    public_research_plan=public_plan,
                )
            )
        return report

    def _runtime_configuration_report(
        self,
        exc: Exception | str,
        *,
        dependency_hint: str | None = None,
        scope: str,
    ) -> DependencyReport | None:
        if self.dependency_preflight is None:
            return None
        return self.dependency_preflight.report_from_runtime_error(
            exc,
            dependency_hint=dependency_hint,
            scope=scope,
        )

    def start(self, project_id: str, workflow_type: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
        if workflow_type not in WORKFLOWS:
            raise KeyError(f"Unknown workflow: {workflow_type}")
        if not self.db.fetchone("SELECT id FROM projects WHERE id=?", (project_id,)):
            raise KeyError(f"Project not found: {project_id}")
        candidate_rows = self.db.fetchall(
            """SELECT id,status,state_json FROM workflows
               WHERE project_id=? AND workflow_type=?
               ORDER BY created_at DESC""",
            (project_id, workflow_type),
        )
        active_rows = [
            row for row in candidate_rows if occupies_workflow_slot(row["status"])
        ]
        active_parent = next(
            (
                row
                for row in active_rows
                if not json.loads(row.get("state_json") or "{}").get("parent_workflow_id")
            ),
            None,
        )
        if active_parent:
            raise ValueError(
                f"同一项目已有未结束的 {workflow_type} 工作流：{active_parent['id']} "
                f"（{active_parent['status']}）。请继续或取消该工作流，不要并发启动重复实例。"
            )
        workflow_id = new_id("wf")
        now = utc_now()
        prerequisite_bindings, missing_prerequisites = self._resolve_prerequisite_workflows(
            project_id,
            workflow_type,
            options or {},
        )
        state = {
            "workflow_type": workflow_type,
            "options": options or {},
            "step_results": {},
            "repair_attempts": {},
            "public_search_results": None,
            "prerequisite_workflow_ids": prerequisite_bindings,
        }
        prerequisite_error = self._prerequisite_error(missing_prerequisites)
        status = (
            WorkflowStatus.WAITING_PREREQUISITE.value
            if prerequisite_error
            else WorkflowStatus.RUNNING.value
        )
        if prerequisite_error:
            state["last_error"] = prerequisite_error
            state["waiting_prerequisite"] = True
        elif self.dependency_preflight is not None:
            report = self._workflow_dependency_report(project_id, workflow_type, options or {})
            if report is not None and report.blocking_issues:
                status = WorkflowStatus.WAITING_CONFIGURATION.value
                state["configuration_wait"] = {
                    **report.as_dict(),
                    "workflow_id": workflow_id,
                    "workflow_type": workflow_type,
                    "resume_step": 0,
                    "source": "WORKFLOW_START_PREFLIGHT",
                    "first_detected_at": now,
                    "last_checked_at": now,
                }
                state["last_error"] = report.summary()
        self.db.execute(
            "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (workflow_id, project_id, workflow_type, status, 0, json.dumps(state, ensure_ascii=False), now, now),
        )
        self.db.audit("WORKFLOW_STARTED", project_id=project_id, object_id=workflow_id, metadata={"workflow_type": workflow_type})
        if status == WorkflowStatus.WAITING_CONFIGURATION.value:
            self.db.audit(
                "WORKFLOW_WAITING_CONFIGURATION",
                project_id=project_id,
                object_id=workflow_id,
                metadata=state["configuration_wait"],
            )
        return self.get(workflow_id)

    def _workflow_prerequisite_error(self, project_id: str, workflow_type: str, options: dict[str, Any]) -> str | None:
        _, missing = self._resolve_prerequisite_workflows(project_id, workflow_type, options)
        return self._prerequisite_error(missing)

    @staticmethod
    def _has_nonconfirmable_quality_failure(output: dict[str, Any]) -> bool:
        """Return true when confirmation cannot repair the generated object.

        QG findings are produced by deterministic proposal-quality validation.  A
        human may supply missing source material or make an explicit project
        decision, but merely confirming the unchanged model output cannot repair
        cloned plans, incomplete critic coverage, document-type drift or invalid
        source mappings.
        """
        for item in output.get("findings", []):
            if not isinstance(item, dict) or not str(item.get("code", "")).startswith("QG_"):
                continue
            if not item.get("blocking", True):
                continue
            suggested = str(item.get("suggested_route") or "")
            if suggested not in {"USER", "PROJECT_OWNER"}:
                return True
        return False

    @staticmethod
    def _is_legacy_wf3_input_block(wf: dict[str, Any], state: dict[str, Any]) -> bool:
        if wf.get("workflow_type") != "WF-3_HYBRID_ONLINE_ASSIST" or int(wf.get("current_step") or 0) != 0:
            return False
        if str(wf.get("status") or "") != "BLOCKED":
            return False
        if str(0) in (state.get("step_results") or {}):
            return False
        last_error = str(state.get("last_error") or "")
        return (
            "P-SAFE-ONLINE-PACKAGE" in last_error
            and (
                "unresolved schema scaffold" in last_error
                or "payload.research_need" in last_error
                or "WF-3 缺少可批准的公开研究问题" in last_error
            )
        )

    def _pause_for_workflow_input(
        self,
        wf: dict[str, Any],
        state: dict[str, Any],
        exc: WorkflowInputRequired,
    ) -> dict[str, Any]:
        prompt_id = exc.prompt_id
        state["last_error"] = str(exc)
        state["workflow_input_required"] = {
            "prompt_id": prompt_id,
            "gate_type": exc.gate_type,
            "missing_paths": exc.missing_paths,
        }
        state.setdefault("technical_retry_attempts", {}).pop(str(wf["current_step"]), None)
        self._update(wf, state=state)
        refreshed = self.get(wf["id"])
        gate_id = self._create_gate(
            refreshed,
            exc.gate_type,
            target_id=f"input:{prompt_id}:{wf['id']}",
            questions=exc.questions,
        )
        self.db.audit(
            "WORKFLOW_INPUT_REQUIRED",
            project_id=wf["project_id"],
            object_id=gate_id,
            metadata={
                "workflow_id": wf["id"],
                "prompt_id": prompt_id,
                "gate_type": exc.gate_type,
                "missing_paths": exc.missing_paths,
            },
        )
        self._update(refreshed, status="WAITING_GATE", state=state)
        return self.get(wf["id"])

    def get(self, workflow_id: str) -> dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM workflows WHERE id=?", (workflow_id,))
        if not row:
            raise KeyError(f"Workflow not found: {workflow_id}")
        row["state"] = json.loads(row.pop("state_json"))
        row["steps"] = WORKFLOWS[row["workflow_type"]]
        return row

    async def advance(self, workflow_id: str) -> dict[str, Any]:
        wf = self.get(workflow_id)
        if is_terminal(wf["status"]):
            return wf
        state = wf["state"]
        if self._recover_provider_block_after_protocol_upgrade(wf, state):
            wf = self.get(workflow_id)
            state = wf["state"]
        if wf["status"] == WorkflowStatus.WAITING_PROVIDER.value:
            wait = state.get("provider_wait") or {}
            retry_key = str(wait.get("retry_key") or f"{wf['current_step']}:provider")
            retry_limit = int(wait.get("retry_limit") or 2)
            attempts = RepairLedger.count(state, "provider_retries", retry_key)
            if attempts >= retry_limit:
                state["last_error"] = (
                    f"Provider retry limit exhausted for {retry_key}: {attempts}/{retry_limit}"
                )
                self._update(wf, status=WorkflowStatus.BLOCKED_PROVIDER.value, state=state)
                return self.get(workflow_id)
            state["recovered_from"] = "WAITING_PROVIDER"
            state.pop("provider_wait", None)
            state.pop("last_error", None)
            self._update(wf, status=WorkflowStatus.RUNNING.value, state=state)
            wf = self.get(workflow_id)
            state = wf["state"]
        legacy_prerequisite_block = (
            wf["status"] == "BLOCKED"
            and str(state.get("last_error") or "").startswith("工作流前置条件未满足：")
            and not state.get("step_results")
        )
        needs_binding_migration = (
            "prerequisite_workflow_ids" not in state
            and bool(self._required_workflow_types(
                wf["project_id"],
                wf["workflow_type"],
                state.get("options") or {},
            ))
        )
        if (
            wf["status"] == WorkflowStatus.WAITING_PREREQUISITE.value
            or legacy_prerequisite_block
            or needs_binding_migration
        ):
            prerequisite_bindings, missing_prerequisites = self._resolve_prerequisite_workflows(
                wf["project_id"],
                wf["workflow_type"],
                state.get("options") or {},
            )
            prerequisite_error = self._prerequisite_error(missing_prerequisites)
            state["prerequisite_workflow_ids"] = prerequisite_bindings
            if prerequisite_error:
                state["last_error"] = prerequisite_error
                state["waiting_prerequisite"] = True
                self._update(wf, status=WorkflowStatus.WAITING_PREREQUISITE.value, state=state)
                return self.get(workflow_id)
            state.pop("last_error", None)
            state.pop("waiting_prerequisite", None)
            state["recovered_from"] = (
                "WAITING_PREREQUISITE"
                if wf["status"] in {
                    WorkflowStatus.WAITING_PREREQUISITE.value,
                    WorkflowStatus.BLOCKED.value,
                }
                else "PREREQUISITE_BINDING_MIGRATION"
            )
            self._update(wf, status=WorkflowStatus.RUNNING.value, state=state)
            wf = self.get(workflow_id)
        legacy_configuration_report = None
        if wf["status"] == "BLOCKED" and self.dependency_preflight is not None:
            legacy_configuration_report = self._runtime_configuration_report(
                state.get("last_error") or "",
                scope="LEGACY_BLOCKED_CONFIGURATION",
            )
        if (
            wf["status"] == WorkflowStatus.WAITING_CONFIGURATION.value
            or legacy_configuration_report is not None
        ):
            state = wf["state"]
            report = self._configuration_recheck_report(wf, state)
            if report is not None and report.blocking_issues:
                return self._pause_for_configuration(
                    wf,
                    state,
                    report,
                    source=(
                        "WAITING_CONFIGURATION_RECHECK"
                        if wf["status"] == WorkflowStatus.WAITING_CONFIGURATION.value
                        else "LEGACY_BLOCKED_CONFIGURATION_MIGRATION"
                    ),
                )
            self._clear_configuration_wait(state)
            state["recovered_from"] = (
                "WAITING_CONFIGURATION"
                if wf["status"] == WorkflowStatus.WAITING_CONFIGURATION.value
                else "LEGACY_CONFIGURATION_BLOCK"
            )
            self._update(wf, status=WorkflowStatus.RUNNING.value, state=state)
            wf = self.get(workflow_id)
        if self._is_legacy_wf3_input_block(wf, wf["state"]):
            state = wf["state"]
            state["recovered_from"] = state.get("last_error") or "LEGACY_WF3_INPUT_BLOCK"
            state.pop("last_error", None)
            state.pop("runtime_recoverable", None)
            state.setdefault("technical_retry_attempts", {}).pop(str(wf["current_step"]), None)
            self._update(wf, status="RUNNING", state=state)
            wf = self.get(workflow_id)
        if wf["status"] == "BLOCKED":
            state = wf["state"]
            step_key = str(wf["current_step"])
            steps = WORKFLOWS[wf["workflow_type"]]
            normalizer_version = str(
                getattr(self.executor, "output_normalizer_version", "") or ""
            )
            current_step_result = (state.get("step_results") or {}).get(step_key)
            if (
                wf["current_step"] < len(steps)
                and isinstance(current_step_result, dict)
                and current_step_result.get("status") == "BLOCK"
                and normalizer_version
            ):
                migration_versions = state.setdefault(
                    "business_block_migration_versions",
                    {},
                )
                run = self.db.fetchone(
                    "SELECT output_json FROM prompt_runs WHERE id=?",
                    (current_step_result.get("run_id"),),
                )
                try:
                    blocked_output = json.loads((run or {}).get("output_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    blocked_output = {}
                blocking_questions = [
                    question
                    for question in blocked_output.get("user_questions") or []
                    if isinstance(question, dict) and bool(question.get("blocking"))
                ]
                if (
                    blocking_questions
                    and str(migration_versions.get(step_key) or "")
                    != normalizer_version
                ):
                    # A BLOCK that already contains an actionable, blocking
                    # project-owner question is a human gate, not an
                    # unrecoverable runtime dead end.  Reclassify workflow
                    # control state without mutating the immutable historical
                    # model run or calling the provider again.
                    current_step_result["status"] = "NEED_USER_INPUT"
                    migration_versions[step_key] = normalizer_version
                    state["business_block_migration"] = {
                        "step": int(step_key),
                        "prompt_id": current_step_result.get("prompt_id"),
                        "run_id": current_step_result.get("run_id"),
                        "from_status": "BLOCK",
                        "to_status": "NEED_USER_INPUT",
                        "output_normalizer_version": normalizer_version,
                        "reason": "blocking user questions require an actionable human gate",
                        "migrated_at": utc_now(),
                    }
                    state.pop("last_error", None)
                    self._update(wf, status="RUNNING", state=state)
                    refreshed = self.get(workflow_id)
                    prompt_id = str(current_step_result.get("prompt_id") or "")
                    gate_type = (
                        self.pack.entry(prompt_id).get("next_human_gate")
                        if prompt_id
                        else None
                    ) or "PROJECT_GAP_RESOLUTION"
                    self._create_gate(
                        refreshed,
                        gate_type,
                        target_id=str(current_step_result.get("run_id") or workflow_id),
                        questions=blocked_output.get("user_questions", []),
                    )
                    self._update(refreshed, status="WAITING_GATE", state=state)
                    self.db.audit(
                        "BUSINESS_BLOCK_MIGRATED_TO_HUMAN_GATE",
                        project_id=wf["project_id"],
                        object_id=workflow_id,
                        metadata=state["business_block_migration"],
                    )
                    return self.get(workflow_id)
            retries = state.setdefault("technical_retry_attempts", {})
            retry_limit = 6 if state.get("options", {}).get("acceptance_run") else 2
            is_section_step = (
                wf["current_step"] < len(steps)
                and steps[wf["current_step"]].get("type") == "WRITE_SECTIONS"
            )
            retry_key = technical_retry_key(
                step_key,
                state,
                is_section_step=is_section_step,
            )
            current_prompt_id = (
                str(steps[wf["current_step"]].get("prompt_id") or "")
                if wf["current_step"] < len(steps)
                else ""
            )
            if (
                not current_prompt_id
                and is_section_step
            ):
                active_section_id = str(state.get("active_section_id") or "")
                active_progress = (
                    (state.get("section_progress") or {}).get(active_section_id)
                    if active_section_id
                    else None
                )
                active_phase = (
                    str(active_progress.get("phase") or "")
                    if isinstance(active_progress, dict)
                    else ""
                )
                phase_prompt = getattr(self, "SECTION_PHASES", {}).get(
                    active_phase
                )
                if phase_prompt:
                    current_prompt_id = str(phase_prompt[0] or "")
            current_scope = f"stage:{current_prompt_id}" if current_prompt_id else ""
            has_current_deterministic_blocker = any(
                str((record.get("finding") or {}).get("code") or "").startswith("QG_")
                and str(record.get("scope_key") or "") == current_scope
                for record in self.quality_manager.open_blockers(
                    wf["project_id"],
                    workflow_id=workflow_id,
                )
            )
            deterministic_recheck = (
                wf["current_step"] < len(steps)
                and step_key in state.get("step_results", {})
                and (
                    "确定性质量校验" in str(state.get("last_error") or "")
                    or has_current_deterministic_blocker
                )
                and int(retries.get(retry_key, 0)) < retry_limit
            )
            migration_versions = state.setdefault(
                "contract_migration_retry_versions",
                {},
            )
            failed_provider_output_exists = False
            if (
                normalizer_version
                and current_prompt_id
                and step_key not in state.get("step_results", {})
            ):
                failed_provider_output_exists = bool(
                    self.db.fetchone(
                        """SELECT id FROM prompt_runs
                           WHERE project_id=? AND workflow_id=? AND prompt_id=?
                             AND status='ERROR' AND output_json IS NOT NULL
                           ORDER BY created_at DESC LIMIT 1""",
                        (wf["project_id"], workflow_id, current_prompt_id),
                    )
                )
            contract_migration_retryable = (
                failed_provider_output_exists
                and str(migration_versions.get(retry_key) or "") != normalizer_version
            )
            technical_retryable = (
                wf["current_step"] < len(steps)
                and (
                    step_key not in state.get("step_results", {})
                    or deterministic_recheck
                )
                and (
                    int(retries.get(retry_key, 0)) < retry_limit
                    or contract_migration_retryable
                )
            )
            completion_recheck = (
                wf["current_step"] >= len(steps)
                and bool(state.get("quality_blocker_ids"))
            )
            if not technical_retryable and not completion_recheck:
                return wf
            if technical_retryable:
                if contract_migration_retryable:
                    migration_versions[retry_key] = normalizer_version
                    state["contract_migration_recovery"] = {
                        "step": int(step_key),
                        "retry_key": retry_key,
                        "prompt_id": current_prompt_id,
                        "output_normalizer_version": normalizer_version,
                        "reason": "revalidate persisted provider output under the upgraded contract layer",
                    }
                else:
                    retries[retry_key] = int(retries.get(retry_key, 0)) + 1
                if deterministic_recheck:
                    previous = state.get("step_results", {}).pop(step_key)
                    state.setdefault("superseded_step_results", {}).setdefault(
                        step_key,
                        [],
                    ).append(previous)
            state["recovered_from"] = state.get("last_error") or "TECHNICAL_STEP_FAILURE"
            state.pop("last_error", None)
            self._update(wf, status="RUNNING", state=state)
            wf = self.get(workflow_id)
        if self._open_gate(workflow_id):
            self._update(wf, status="WAITING_GATE")
            return self.get(workflow_id)

        wf["status"] = "RUNNING"
        steps = WORKFLOWS[wf["workflow_type"]]
        state = wf["state"]
        while wf["current_step"] < len(steps):
            step = steps[wf["current_step"]]
            if self.dependency_preflight is not None:
                public_plan = None
                if step.get("type") == "PUBLIC_SEARCH":
                    public_plan = self._context_result(
                        wf["project_id"],
                        "P-PUBLIC-RESEARCH-PLAN",
                        workflow_id=wf["id"],
                        exact_workflow=True,
                    ) or {}
                report = self.dependency_preflight.step_report(
                    wf["project_id"],
                    wf["workflow_type"],
                    step,
                    state,
                    public_research_plan=public_plan,
                )
                if report.blocking_issues:
                    return self._pause_for_configuration(
                        wf,
                        state,
                        report,
                        source=f"STEP_PREFLIGHT:{wf['current_step']}",
                    )
            if step.get("type") == "PUBLIC_SEARCH":
                try:
                    await self._run_public_search(wf, state)
                except PublicResearchError as exc:
                    report = self._runtime_configuration_report(
                        exc,
                        dependency_hint="PUBLIC_SEARCH",
                        scope="PUBLIC_SEARCH_RUNTIME",
                    )
                    if report is not None:
                        return self._pause_for_configuration(
                            wf,
                            state,
                            report,
                            source="PUBLIC_SEARCH_RUNTIME",
                        )
                    state["last_error"] = str(exc)
                    state["public_research_failure"] = {
                        "category": str(getattr(exc, "category", "RUNTIME") or "RUNTIME"),
                        "error_code": str(getattr(exc, "error_code", "PUBLIC_RESEARCH_RUNTIME_ERROR") or "PUBLIC_RESEARCH_RUNTIME_ERROR"),
                        "message": str(exc),
                        "details": dict(getattr(exc, "details", {}) or {}),
                        "step": int(wf["current_step"]),
                        "recorded_at": utc_now(),
                    }
                    self._update(wf, status="BLOCKED", state=state)
                    return self.get(workflow_id)
                state.pop("public_research_failure", None)
                state.pop("last_error", None)
                wf["current_step"] += 1
                self._update(wf, current_step=wf["current_step"], state=state)
                continue
            if step.get("type") == "WRITE_SECTIONS":
                try:
                    result = await self._write_sections(wf, state)
                except WorkflowInputRequired as exc:
                    return self._pause_for_workflow_input(wf, state, exc)
                except ProviderRetriesExhausted as exc:
                    return self._block_provider_retries_exhausted(
                        wf,
                        state,
                        exc,
                        boundary="WRITE_SECTIONS",
                    )
                except (ValueError, KeyError) as exc:
                    report = self._runtime_configuration_report(
                        exc,
                        scope="WRITE_SECTIONS_RUNTIME",
                    )
                    if report is not None:
                        return self._pause_for_configuration(
                            wf,
                            state,
                            report,
                            source="WRITE_SECTIONS_RUNTIME",
                        )
                    state["last_error"] = str(exc)
                    self._update(wf, status="BLOCKED", state=state)
                    return self.get(workflow_id)
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
            pending_rereview = (state.get("pending_repair_rereviews") or {}).get(
                prompt_id
            )
            try:
                if isinstance(pending_rereview, dict):
                    self._start_repair_rereview(
                        state, pending_rereview, critic_prompt=prompt_id
                    )
                    self._update(wf, state=state)
                envelope = self.context_builder.build(prompt_id, wf["project_id"], workflow_id=workflow_id, workflow_state=state)
                if prompt_id == "P-INTEGRATION-CRITIC":
                    self._validate_three_section_integration_envelope(state, envelope)
                    self._validate_full_proposal_integration_envelope(state, envelope)
                result = await self._execute_prompt_with_provider_retry(
                    wf,
                    state,
                    prompt_id=prompt_id,
                    envelope=envelope,
                )
            except WorkflowInputRequired as exc:
                return self._pause_for_workflow_input(wf, state, exc)
            except ProviderRetriesExhausted as exc:
                return self._block_provider_retries_exhausted(
                    wf,
                    state,
                    exc,
                    boundary=f"PROMPT:{prompt_id}",
                )
            except (PromptExecutionError, ValueError, KeyError) as exc:
                required_environment = str(
                    self.pack.entry(prompt_id).get("required_environment") or ""
                )
                report = self._runtime_configuration_report(
                    exc,
                    dependency_hint=required_environment,
                    scope=f"PROMPT_RUNTIME:{prompt_id}",
                )
                if report is not None:
                    return self._pause_for_configuration(
                        wf,
                        state,
                        report,
                        source=f"PROMPT_RUNTIME:{prompt_id}",
                    )
                failure = self._record_runtime_failure(
                    wf, state, prompt_id=prompt_id, exc=exc
                )
                state["last_error"] = str(exc)
                status = failure["workflow_status"]
                self._update(wf, status=status, state=state)
                return self.get(workflow_id)

            state["step_results"][str(wf["current_step"])] = {"prompt_id": prompt_id, "run_id": result["run_id"], "status": result["status"]}
            state["original_environment"] = result["route"]["environment"]
            if isinstance(pending_rereview, dict):
                self._complete_repair_rereview(
                    state,
                    pending_rereview,
                    critic_prompt=prompt_id,
                    review_run_id=str(result.get("run_id") or "") or None,
                    status=str(result.get("status") or ""),
                )
                pending = state.get("pending_repair_rereviews")
                if isinstance(pending, dict):
                    pending.pop(prompt_id, None)
                    if not pending:
                        state.pop("pending_repair_rereviews", None)
            output = result["output"]
            if prompt_id == "P-PUBLIC-RESEARCH-SYNTHESIS" and result["status"] == "PASS":
                claim_validation = self.research_service.validate_synthesis(
                    output.get("result") or {},
                    state.get("public_search_results") or {},
                )
                state["public_claim_validation"] = claim_validation
                if claim_validation.get("status") != "PASS":
                    codes = [str(item.get("code") or "PUBLIC_CLAIM_INVALID") for item in claim_validation.get("findings", [])]
                    state["last_error"] = (
                        "公开研究综合未通过确定性 Claim—来源绑定校验："
                        + "、".join(codes[:12])
                        + "。不得进入公开结果导入 Gate。"
                    )
                    self._update(wf, status="BLOCKED", state=state)
                    return self.get(workflow_id)
                self._update(wf, state=state)
            decision, effective_status, effective_output = self._record_decision(
                wf, state, prompt_id, result
            )
            observed_result = copy.deepcopy(result)
            observed_result["status"] = effective_status
            observed_result["output"] = copy.deepcopy(effective_output)
            self._observe_quality_result(
                wf, state, prompt_id, observed_result
            )
            if decision and decision.get("decision") == "CONTRACT_CONFLICT":
                state["last_error"] = (
                    f"Decision responsibility protocol is inconsistent for {prompt_id}; "
                    "the immutable critic output and guard report were preserved in DECISION_RECORD."
                )
                self._update(wf, status="BLOCKED_CONTRACT", state=state)
                return self.get(workflow_id)
            if effective_status == "REVISE":
                state.setdefault("semantic_failure_history", []).append({
                    **semantic_revise_classification().to_dict(),
                    "prompt_id": prompt_id,
                    "run_id": result.get("run_id"),
                    "recorded_at": utc_now(),
                })
                del state["semantic_failure_history"][:-100]
            if prompt_id == "P-INTEGRATION-CRITIC" and self._three_section_mode(state):
                state.setdefault("cross_section_review_history", []).append({
                    "run_id": result["run_id"],
                    "status": effective_status,
                    "finding_codes": [
                        str(item.get("code") or "")
                        for item in output.get("findings") or []
                        if isinstance(item, dict)
                    ],
                    "contract_section_ids": [
                        str(item.get("section_id"))
                        for item in (state.get("three_section_contract") or {}).get("sections") or []
                        if isinstance(item, dict) and item.get("section_id")
                    ],
                })
                self._update(wf, state=state)
            if prompt_id == "P-INTEGRATION-CRITIC" and self._full_proposal_mode(state):
                try:
                    self._record_full_integration_review(wf, state, result)
                except ValueError as exc:
                    state["last_error"] = str(exc)
                    self._update(wf, status="BLOCKED", state=state)
                    return self.get(workflow_id)
            if effective_status == "BLOCK":
                self._update(wf, status="BLOCKED", state=state)
                return self.get(workflow_id)
            if prompt_id == "P-INTEGRATION-CRITIC" and effective_status == "REVISE":
                repair_state = self._prepare_integration_repair(wf, state, effective_output)
                if repair_state == "SCHEDULED":
                    wf = self.get(workflow_id)
                    state = wf["state"]
                    continue
                if repair_state == "EXHAUSTED":
                    return self.get(workflow_id)
            if effective_status == "REVISE" and self._can_auto_repair(prompt_id, state):
                repaired = await self._auto_repair(wf, prompt_id, envelope, effective_output, state)
                if repaired:
                    state.setdefault("pending_repair_rereviews", {})[prompt_id] = (
                        self._repair_rereview_checkpoint(repaired)
                    )
                    self._update(wf, state=state)
                    continue
                if isinstance(state.get("last_targeted_repair_failure"), dict):
                    state["last_error"] = self._targeted_repair_failure_message(
                        state,
                        prompt_id=prompt_id,
                        fallback=f"{prompt_id} targeted repair failed",
                    )
                    self._update(wf, status="BLOCKED_CONTRACT", state=state)
                    return self.get(workflow_id)
            if effective_status == "REVISE" and self._has_nonconfirmable_quality_failure(effective_output):
                codes = [str(item.get("code")) for item in effective_output.get("findings", []) if str(item.get("code", "")).startswith("QG_")]
                state["last_error"] = (
                    f"{prompt_id} 未通过确定性质量校验：" + "、".join(codes[:8])
                    + "。该问题必须由对应生产/审查阶段重新生成或补充证据，不能通过人工空确认覆盖。"
                )
                self._update(wf, status="BLOCKED", state=state)
                return self.get(workflow_id)
            if effective_status in {"REVISE", "NEED_USER_INPUT"}:
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

        quality_scope = None if wf["workflow_type"] == "WF-5_SECURITY_REVIEW_AND_EXPORT" else workflow_id
        blockers = (
            self.quality_manager.open_delivery_blockers(wf["project_id"])
            if wf["workflow_type"] == "WF-5_SECURITY_REVIEW_AND_EXPORT"
            else self.quality_manager.open_blockers(
                wf["project_id"],
                workflow_id=quality_scope,
            )
        )
        accepted_blockers: list[dict[str, Any]] = []
        if wf["workflow_type"] != "WF-5_SECURITY_REVIEW_AND_EXPORT":
            blockers, accepted_blockers = self._unaccepted_completion_blockers(blockers, state)
        try:
            if blockers:
                raise QualityGateBlocked(blockers)
        except QualityGateBlocked as exc:
            state["last_error"] = str(exc) + "。必须记录修复运行并由独立Critic复审，人工确认或直接改库均不能放行。"
            state["quality_blocker_ids"] = [item.get("finding_id") for item in exc.findings]
            self._update(wf, status="BLOCKED", state=state)
            return self.get(workflow_id)
        if accepted_blockers:
            state["accepted_open_quality_finding_ids"] = [
                item.get("finding_id") for item in accepted_blockers
            ]
            if wf["workflow_type"] == "WF-1_PROJECT_INTAKE":
                state["completion_scope"] = "CONTENT_VALIDATION_ONLY"
        state.pop("last_error", None)
        state.pop("quality_blocker_ids", None)
        self._update(wf, status="COMPLETED", state=state)
        self.db.audit("WORKFLOW_COMPLETED", project_id=wf["project_id"], object_id=workflow_id, metadata={"workflow_type": wf["workflow_type"]})
        return self.get(workflow_id)
