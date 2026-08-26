from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .candidate_integrity import visible_document_snapshot
from .dependency_preflight import DependencyIssue, DependencyReport
from .executor import PromptExecutionError
from .llm import MODEL_RESPONSE_PROTOCOL_VERSION
from .decision_arbiter import DecisionArbiter
from .repair_ledger import RepairLedger
from .retry_policy import ProviderRetriesExhausted, RetryDecision, RetryPolicy
from .runtime_failures import (
    persistence_safe_failure_classification,
    FailureCategory,
    FailureClassification,
    classify_runtime_failure,
    semantic_revise_classification,
)
from .quality import QualityGateBlocked, QualityLifecycleManager
from .quality_guard import QualityGuardContractError, require_guard_report
from .research import PublicResearchError
from .secret_redaction import redact_secret_text, redact_secrets
from .util import new_id, sha256_json, utc_now
from .workflow_authoring import WorkflowAuthoringMixin
from .workflow_defs import CRITIC_PRODUCER, WORKFLOWS
from .workflow_gates import WorkflowGateMixin
from .workflow_repair import WorkflowRepairMixin
from .wf3_input import WorkflowInputRequired
from .wf3_contracts import WF3_RESEARCH_CRITIC, wf3_critic_routing_report
from .workflow_status import (
    WorkflowStatus,
    classify_legacy_blocked_error,
    coerce_workflow_status,
    is_recoverable_block,
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


def semantic_gap_revision_finding(
    gap: dict[str, Any],
    *,
    producer_prompt: str,
    round_number: int,
    index: int,
) -> dict[str, Any]:
    """Project one semantic gap into a complete canonical Finding.

    The model owns the gap description and requested action. Runtime owns the
    canonical category, route, repair policy, identity and target locator.
    """

    thread_index = (
        gap.get("thread_index")
        if isinstance(gap.get("thread_index"), int)
        and not isinstance(gap.get("thread_index"), bool)
        else None
    )
    explicit_target = str(
        gap.get("target_path_or_span") or gap.get("target_path") or ""
    ).strip()
    if explicit_target:
        target_path = explicit_target
    elif thread_index is not None:
        target_path = f"/result/research_design_matrix/{thread_index}"
    else:
        target_path = "/result/argument_architecture"
    reason = str(gap.get("reason") or "存在尚未闭合的语义或证据缺口")
    action = str(
        gap.get("suggested_source_or_question")
        or "利用当前可用材料补全该缺口；若证据确实不存在则保持未知，不得虚构。"
    )
    return {
        "finding_instance_id": (
            f"runtime-semantic-gap-{producer_prompt}-{round_number}-{index}"
        ),
        "defect_key": str(gap.get("defect_key") or "") or None,
        "code": str(
            gap.get("finding_code") or "RESEARCH_DESIGN_INCOMPLETE"
        ),
        "severity": "P1",
        "category": "ARGUMENT",
        "target_type": "ARGUMENT_SEMANTIC_COMPONENT",
        "target_path_or_span": target_path,
        "description": reason,
        "evidence_refs": [],
        # This path deliberately returns to the original producer. It is not a
        # local targeted-repair operation.
        "repairable": False,
        "repair_instruction": action,
        "semantic_component": str(
            gap.get("semantic_component") or "RESEARCH_DESIGN"
        ),
        "semantic_thread": thread_index,
        "semantic_review_unit_key": str(
            gap.get("semantic_review_unit_key") or ""
        )
        or None,
        "suggested_route": "ORIGINAL_PRODUCER",
        "blocking": True,
    }


class WorkflowEngine(WorkflowAuthoringMixin, WorkflowRepairMixin, WorkflowGateMixin):
    @staticmethod
    def _retry_not_before(delay_seconds: float) -> str | None:
        if delay_seconds <= 0:
            return None
        return (
            datetime.now(timezone.utc) + timedelta(seconds=float(delay_seconds))
        ).isoformat()

    @staticmethod
    async def _honor_persisted_retry_delay(wait: dict[str, Any]) -> None:
        raw = str(wait.get("retry_not_before") or "").strip()
        if not raw:
            return
        try:
            target = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
        except ValueError:
            return
        remaining = (target - datetime.now(timezone.utc)).total_seconds()
        if remaining > 0:
            # RetryPolicy caps persisted delays at 300 seconds.  Clamp legacy or
            # manually damaged timestamps as well so a malformed checkpoint
            # cannot suspend an advance call indefinitely.
            await asyncio.sleep(min(remaining, 300.0))

    @staticmethod
    def _semantic_retry_issues(
        prompt_id: str,
        exc: BaseException,
        *,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        if prompt_id != "P-ARGUMENT-ARCHITECTURE":
            return []

        phase = None
        current: BaseException | None = exc
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            candidate_phase = getattr(current, "provider_phase", None)
            if candidate_phase:
                phase = str(candidate_phase)
                break
            current = current.__cause__ or current.__context__
        if phase != "output_structure_validation":
            return []

        errors = [
            redact_secret_text(str(item)).strip()
            for item in (getattr(exc, "validation_errors", []) or [])
            if str(item).strip()
        ]
        unique_errors = list(dict.fromkeys(errors))[: max(0, int(limit))]
        return [
            {
                "problem": (
                    "上一轮 semantic output 未通过模型契约校验："
                    + error[:240]
                ),
                "severity": "P1",
                "component": "RESEARCH_DESIGN",
                "required_action": (
                    "仅修正该输出契约或引用错误；保持已有事实、范围和其他有效语义不变，"
                    "并只返回当前 semantic output schema 允许的业务字段。"
                ),
                "evidence_ids": [],
            }
            for error in unique_errors
        ]


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

    def _targeted_repair_failure_matches_checkpoint(
        self,
        wf: dict[str, Any],
        state: dict[str, Any],
        failure: dict[str, Any],
    ) -> bool:
        """Whether a saved repair failure belongs to the active workflow node."""
        failure_step = failure.get("workflow_step")
        active_section_id = str(state.get("active_section_id") or "").strip()
        failure_section_id = str(failure.get("section_id") or "").strip()

        if active_section_id:
            progress = (state.get("section_progress") or {}).get(active_section_id)
            phase = (
                str((progress or {}).get("phase") or "")
                if isinstance(progress, dict)
                else ""
            )
            phase_prompt = getattr(self, "SECTION_PHASES", {}).get(phase)
            expected_critic = str(phase_prompt[0] or "") if phase_prompt else ""
            saved_critic = str(failure.get("critic_prompt") or "").strip()
            if expected_critic and saved_critic != expected_critic:
                return False

            if int(failure.get("repair_checkpoint_version") or 0) >= 1:
                if failure_step is None or int(failure_step) != int(wf["current_step"]):
                    return False
                return (
                    failure_section_id == active_section_id
                    and str(failure.get("section_phase") or "") == phase
                )

            # Step-6-and-earlier repair failures did not persist section_id and
            # workflow_step, but their repair budget key was already section
            # scoped.  Accept only that deterministic legacy identity; a bare
            # prompt/run pair remains ambiguous and stays closed.
            expected_attempt_key = f"section:{active_section_id}:{saved_critic}"
            return bool(
                saved_critic
                and str(failure.get("repair_attempt_key") or "")
                == expected_attempt_key
                and not failure_section_id
                and failure_step is None
            )

        # A section-bound failure must never leak into a workflow-level prompt.
        if failure_section_id:
            return False
        if failure_step is not None and int(failure_step) != int(wf["current_step"]):
            return False
        return True

    @staticmethod
    def _section_checkpoint_identity(
        wf: dict[str, Any],
        state: dict[str, Any],
    ) -> tuple[str, str]:
        steps = WORKFLOWS.get(str(wf.get("workflow_type") or ""), [])
        step = int(wf.get("current_step") or 0)
        if step >= len(steps) or steps[step].get("type") != "WRITE_SECTIONS":
            return "", ""
        section_id = str(state.get("active_section_id") or "").strip()
        progress = (state.get("section_progress") or {}).get(section_id)
        phase = (
            str(progress.get("phase") or "").strip()
            if isinstance(progress, dict)
            else ""
        )
        return section_id, phase

    def _provider_wait_matches_checkpoint(
        self,
        wf: dict[str, Any],
        state: dict[str, Any],
        wait: dict[str, Any],
    ) -> bool:
        section_id, phase = self._section_checkpoint_identity(wf, state)
        if not section_id:
            return True
        saved_section = str(wait.get("section_id") or "").strip()
        saved_phase = str(wait.get("section_phase") or "").strip()
        if saved_section or saved_phase:
            return saved_section == section_id and saved_phase == phase
        retry_key = str(wait.get("retry_key") or "")
        return bool(phase and retry_key.endswith(f":{section_id}:{phase}"))

    def _blocked_failure_prompt_id(
        self,
        wf: dict[str, Any],
        state: dict[str, Any],
        steps: list[dict[str, Any]],
        *,
        is_section_step: bool,
    ) -> str:
        """Return the prompt that actually produced the persisted failure.

        A section remains positioned at its critic phase while targeted repair
        is running.  Older code therefore attributed a repair contract failure
        to the critic and searched the wrong prompt-run history, preventing a
        local normalizer migration from reusing the saved repair output.
        """

        repair_failure = state.get("last_targeted_repair_failure")
        if (
            isinstance(repair_failure, dict)
            and self._targeted_repair_failure_matches_checkpoint(
                wf, state, repair_failure
            )
            and any(
                repair_failure.get(field)
                for field in ("category", "error", "run_id", "validation_errors")
            )
        ):
            return "P-TARGETED-REPAIR"

        provider_wait = state.get("provider_wait")
        if (
            isinstance(provider_wait, dict)
            and self._provider_wait_matches_checkpoint(wf, state, provider_wait)
        ):
            prompt_id = str(provider_wait.get("prompt_id") or "").strip()
            if prompt_id:
                return prompt_id

        prompt_id = (
            str(steps[wf["current_step"]].get("prompt_id") or "")
            if wf["current_step"] < len(steps)
            else ""
        )
        if prompt_id or not is_section_step:
            return prompt_id

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
        phase_prompt = getattr(self, "SECTION_PHASES", {}).get(active_phase)
        return str(phase_prompt[0] or "") if phase_prompt else ""

    def _recover_contract_block_after_normalizer_upgrade(
        self,
        wf: dict[str, Any],
        state: dict[str, Any],
    ) -> bool:
        """Reopen one contract block only for a new local normalizer version.

        This is intentionally narrower than a technical retry.  It requires a
        persisted provider output, does not consume or reset retry counters,
        and merely lets RuntimePromptExecutor revalidate the immutable output
        under a newly deployed deterministic contract adapter.
        """

        if wf["status"] != WorkflowStatus.BLOCKED_CONTRACT.value:
            return False
        normalizer_version = str(
            getattr(self.executor, "output_normalizer_version", "") or ""
        )
        steps = WORKFLOWS[wf["workflow_type"]]
        if not normalizer_version or wf["current_step"] >= len(steps):
            return False
        step_key = str(wf["current_step"])
        if step_key in (state.get("step_results") or {}):
            return False
        is_section_step = steps[wf["current_step"]].get("type") == "WRITE_SECTIONS"
        retry_key = technical_retry_key(
            step_key,
            state,
            is_section_step=is_section_step,
        )
        failure_prompt_id = self._blocked_failure_prompt_id(
            wf,
            state,
            steps,
            is_section_step=is_section_step,
        )
        if not failure_prompt_id:
            return False
        migration_versions = state.setdefault(
            "contract_migration_retry_versions",
            {},
        )
        if str(migration_versions.get(retry_key) or "") == normalizer_version:
            return False
        exact_run_id = self._blocked_failure_run_id(
            wf,
            state,
            prompt_id=failure_prompt_id,
        )
        if exact_run_id:
            failed_output = self.db.fetchone(
                """SELECT id FROM prompt_runs
                   WHERE id=? AND project_id=? AND workflow_id=? AND prompt_id=?
                     AND status='ERROR' AND output_json IS NOT NULL""",
                (exact_run_id, wf["project_id"], wf["id"], failure_prompt_id),
            )
        else:
            # A section prompt is repeated under the same workflow step.  Old
            # rows without an exact run binding are therefore ambiguous and
            # must not be selected by recency.  Non-section prompts occur once
            # per workflow and retain the narrow compatibility fallback.
            if is_section_step:
                return False
            failed_output = self.db.fetchone(
                """SELECT id FROM prompt_runs
                   WHERE project_id=? AND workflow_id=? AND prompt_id=?
                     AND status='ERROR' AND output_json IS NOT NULL
                   ORDER BY created_at DESC LIMIT 1""",
                (wf["project_id"], wf["id"], failure_prompt_id),
            )
        if failed_output is None:
            return False
        prior_normalizer_version = self._failed_run_normalizer_version(
            wf["project_id"],
            str(failed_output.get("id") or ""),
        )
        if (
            not prior_normalizer_version
            or prior_normalizer_version == normalizer_version
        ):
            return False

        migration_versions[retry_key] = normalizer_version
        section_id, section_phase = self._section_checkpoint_identity(wf, state)
        state["contract_migration_recovery"] = {
            "checkpoint_identity_version": 1,
            "step": int(step_key),
            "section_id": section_id or None,
            "section_phase": section_phase or None,
            "retry_key": retry_key,
            "prompt_id": failure_prompt_id,
            "failed_run_id": failed_output.get("id"),
            "from_output_normalizer_version": prior_normalizer_version,
            "output_normalizer_version": normalizer_version,
            "reason": "revalidate persisted provider output under the upgraded contract layer",
        }
        state["recovered_from"] = (
            state.get("last_error") or WorkflowStatus.BLOCKED_CONTRACT.value
        )
        state.pop("last_error", None)
        self._update(
            wf,
            status=WorkflowStatus.RUNNING.value,
            state=state,
        )
        return True

    def _failed_run_normalizer_version(
        self,
        project_id: str,
        run_id: str,
    ) -> str:
        if not run_id:
            return ""
        row = self.db.fetchone(
            """SELECT metadata_json FROM audit_events
               WHERE project_id=? AND event_type='MODEL_CALL_FAILED'
                 AND json_extract(metadata_json,'$.run_id')=?
               ORDER BY id DESC LIMIT 1""",
            (project_id, run_id),
        )
        if not row:
            return ""
        try:
            metadata = json.loads(row.get("metadata_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            return ""
        return str(metadata.get("output_normalizer_version") or "")

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
            **persistence_safe_failure_classification(classification),
            "prompt_id": prompt_id,
            "workflow_id": wf["id"],
            "step": wf["current_step"],
            "section_id": str(state.get("active_section_id") or "") or None,
            "section_phase": str(
                ((state.get("section_progress") or {}).get(
                    str(state.get("active_section_id") or "")
                ) or {}).get("phase") or ""
            ) or None,
            "run_id": str(getattr(exc, "run_id", "") or "") or None,
            "error_type": exc.__class__.__name__,
            "error": redact_secret_text(str(exc)),
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

    def _blocked_failure_run_id(
        self,
        wf: dict[str, Any],
        state: dict[str, Any],
        *,
        prompt_id: str,
    ) -> str:
        """Return the failed run bound to the current workflow checkpoint.

        Repeated section prompts make ``workflow_id + prompt_id`` too broad:
        the same critic or producer is executed once per section.  Prefer the
        run captured by the active targeted-repair/provider checkpoint, then
        the latest runtime failure for the exact workflow step and prompt.
        Older databases may lack these fields; callers may use a narrow legacy
        fallback, but new failures are always checkpoint-bound.
        """

        repair_failure = state.get("last_targeted_repair_failure")
        if (
            prompt_id == "P-TARGETED-REPAIR"
            and isinstance(repair_failure, dict)
            and self._targeted_repair_failure_matches_checkpoint(
                wf, state, repair_failure
            )
        ):
            run_id = str(repair_failure.get("run_id") or "").strip()
            if run_id:
                return run_id

        provider_wait = state.get("provider_wait")
        if (
            isinstance(provider_wait, dict)
            and self._provider_wait_matches_checkpoint(wf, state, provider_wait)
        ):
            if str(provider_wait.get("prompt_id") or "") == prompt_id:
                run_id = str(provider_wait.get("failure_run_id") or "").strip()
                if run_id:
                    return run_id

        section_id, section_phase = self._section_checkpoint_identity(wf, state)
        for failure in reversed(state.get("runtime_failure_history") or []):
            if not isinstance(failure, dict):
                continue
            if str(failure.get("prompt_id") or "") != prompt_id:
                continue
            if int(failure.get("step", -1)) != int(wf["current_step"]):
                continue
            if section_id:
                if str(failure.get("section_id") or "") != section_id:
                    continue
                if str(failure.get("section_phase") or "") != section_phase:
                    continue
            run_id = str(failure.get("run_id") or "").strip()
            if run_id:
                return run_id
        return ""

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
        request_spec_hash_fn = getattr(self.executor, "provider_request_spec_hash", None)
        provider_request_spec_hash = (
            str(request_spec_hash_fn(prompt_id))
            if callable(request_spec_hash_fn)
            else ""
        )
        requested_base_call_key = call_key or (
            "call-provider-" + sha256_json(
                {
                    "workflow_id": wf["id"],
                    "retry_key": retry_key,
                    "input_hash": input_hash,
                    "protocol_version": MODEL_RESPONSE_PROTOCOL_VERSION,
                    "provider_request_spec_hash": provider_request_spec_hash,
                }
            )[:24]
        )
        cycles = state.setdefault("provider_call_cycles", {})
        prior_cycle = cycles.get(retry_key) or {}
        legacy_cycle_identity = bool(prior_cycle) and (
            "provider_request_spec_hash" not in prior_cycle
        )
        if legacy_cycle_identity:
            # Step-5-and-earlier checkpoints predate request-spec identity and
            # base-call-key persistence.  This audit patch does not alter the
            # Prompt Pack, so migrate the existing generation in place instead
            # of resetting its finite retry budget.  Reconstruct the exact old
            # deterministic base key so an in-flight attempt keeps its identity.
            legacy_base_call_key = call_key or (
                "call-provider-" + sha256_json(
                    {
                        "workflow_id": wf["id"],
                        "retry_key": retry_key,
                        "input_hash": input_hash,
                        "protocol_version": MODEL_RESPONSE_PROTOCOL_VERSION,
                    }
                )[:24]
            )
            prior_cycle["provider_request_spec_hash"] = provider_request_spec_hash
            prior_cycle.setdefault("base_call_key", legacy_base_call_key)
            prior_cycle["request_spec_identity_migrated_at"] = utc_now()
            legacy_wait = state.get("provider_wait")
            if (
                isinstance(legacy_wait, dict)
                and str(legacy_wait.get("retry_key") or "") == retry_key
                and str(legacy_wait.get("cycle_id") or "")
                == str(prior_cycle.get("cycle_id") or "")
                and str(legacy_wait.get("input_hash") or "") == input_hash
                and str(legacy_wait.get("protocol_version") or "")
                == MODEL_RESPONSE_PROTOCOL_VERSION
            ):
                legacy_wait["provider_request_spec_hash"] = provider_request_spec_hash
                legacy_wait.setdefault("base_call_key", legacy_base_call_key)
                attempt = int(
                    legacy_wait.get("attempt_in_flight")
                    or prior_cycle.get("attempt_in_flight")
                    or 0
                )
                if attempt > 0:
                    legacy_wait.setdefault(
                        "attempt_call_key",
                        f"{legacy_base_call_key}-cycle-"
                        f"{prior_cycle.get('cycle_id')}-attempt-{attempt}",
                    )
            self._update(wf, state=state)
        if (
            prior_cycle.get("input_hash") != input_hash
            or prior_cycle.get("protocol_version") != MODEL_RESPONSE_PROTOCOL_VERSION
            or str(prior_cycle.get("provider_request_spec_hash") or "")
            != provider_request_spec_hash
            or bool(prior_cycle.get("force_new_generation"))
        ):
            generation = int(prior_cycle.get("generation") or 0) + 1
            previous_cycle_id = str(prior_cycle.get("cycle_id") or "").strip() or None
            cycle_id = sha256_json(
                {
                    "workflow_id": wf["id"],
                    "retry_key": retry_key,
                    "input_hash": input_hash,
                    "protocol_version": MODEL_RESPONSE_PROTOCOL_VERSION,
                    "provider_request_spec_hash": provider_request_spec_hash,
                    "generation": generation,
                }
            )[:16]
            prior_cycle = {
                "cycle_id": cycle_id,
                "generation": generation,
                "input_hash": input_hash,
                "protocol_version": MODEL_RESPONSE_PROTOCOL_VERSION,
                "provider_request_spec_hash": provider_request_spec_hash,
                "base_call_key": requested_base_call_key,
                "previous_cycle_id": previous_cycle_id,
                "created_at": utc_now(),
            }
            cycles[retry_key] = prior_cycle
            # Persist before the provider call.  A crash may safely replay the
            # same attempt, while a later retry receives a distinct attempt key.
            self._update(wf, state=state)
        cycle_id = str(prior_cycle["cycle_id"])
        base_call_key = str(prior_cycle.get("base_call_key") or "").strip()
        if not base_call_key:
            base_call_key = requested_base_call_key
            prior_cycle["base_call_key"] = base_call_key
            prior_cycle["updated_at"] = utc_now()
            self._update(wf, state=state)
        persisted_wait = state.get("provider_wait")
        if not isinstance(persisted_wait, dict):
            persisted_wait = {}
        wait_matches_cycle = (
            str(persisted_wait.get("retry_key") or "") == retry_key
            and str(persisted_wait.get("cycle_id") or "") == cycle_id
            and str(persisted_wait.get("input_hash") or "") == input_hash
            and str(persisted_wait.get("protocol_version") or "")
            == MODEL_RESPONSE_PROTOCOL_VERSION
            and str(persisted_wait.get("provider_request_spec_hash") or "")
            == provider_request_spec_hash
        )
        migration = state.get("contract_migration_recovery")
        recovery_run_id = None
        if isinstance(migration, dict):
            migration_matches_checkpoint = (
                int(migration.get("checkpoint_identity_version") or 0) >= 1
                and int(migration["step"] if migration.get("step") is not None else -1) == int(wf["current_step"])
                and str(migration.get("prompt_id") or "") == prompt_id
                and str(migration.get("section_id") or "") == section_id
                and str(migration.get("section_phase") or "") == section_phase
            )
            if migration_matches_checkpoint:
                recovery_run_id = str(
                    migration.get("failed_run_id") or ""
                ).strip() or None
        completed_attempts = max(
            int(prior_cycle.get("completed_attempts") or 0),
            int(persisted_wait.get("completed_attempts") or 0)
            if wait_matches_cycle
            else 0,
        )
        migration_replays_failed_attempt = bool(
            recovery_run_id
            and wait_matches_cycle
            and str(persisted_wait.get("failure_run_id") or "")
            == recovery_run_id
        )
        if migration_replays_failed_attempt:
            replay_attempt = max(
                1,
                int(
                    persisted_wait.get("attempt_in_flight")
                    or persisted_wait.get("completed_attempts")
                    or completed_attempts
                    or 1
                ),
            )
            # Revalidate the immutable failed response as the same provider
            # attempt.  It must not consume a new retry slot or receive a new
            # call checkpoint merely because the local contract layer changed.
            completed_attempts = replay_attempt - 1
            prior_cycle["attempt_in_flight"] = replay_attempt
            persisted_wait = {}

        if (
            wait_matches_cycle
            and str(persisted_wait.get("phase") or "") == "FAILED"
            and (persisted_wait.get("decision") or {}).get("should_retry") is True
        ):
            await self._honor_persisted_retry_delay(persisted_wait)
            retry_event_exists = any(
                item.get("event") == "PROVIDER_RETRY"
                and item.get("key") == retry_key
                and (item.get("details") or {}).get("cycle_id") == cycle_id
                and int((item.get("details") or {}).get("completed_attempts") or 0)
                == completed_attempts
                for item in RepairLedger.events(state, key=retry_key)
            )
            if not retry_event_exists:
                RepairLedger.provider_retry(
                    state,
                    retry_key,
                    details={
                        "prompt_id": prompt_id,
                        "cycle_id": cycle_id,
                        "completed_attempts": completed_attempts,
                        "next_attempt": completed_attempts + 1,
                        "failure_kind": persisted_wait.get("failure_kind"),
                        "http_status": persisted_wait.get("http_status"),
                        "delay_seconds": persisted_wait.get("delay_seconds", 0.0),
                        "reason": (persisted_wait.get("decision") or {}).get(
                            "reason"
                        ),
                        "restored_from_checkpoint": True,
                    },
                )
                self._update(wf, state=state)

        successful_contract_repair_run_id = str(
            prior_cycle.get("successful_contract_repair_run_id") or ""
        ).strip()
        if successful_contract_repair_run_id:
            result = self._replay_producer_contract_repair(
                wf,
                prompt_id=prompt_id,
                envelope=envelope,
                source_run_id=str(
                    prior_cycle.get("contract_repair_source_run_id") or ""
                ),
                repair_run_id=successful_contract_repair_run_id,
                repair_application_artifact_id=(
                    str(
                        prior_cycle.get(
                            "successful_contract_repair_artifact_id"
                        )
                        or ""
                    ).strip()
                    or None
                ),
            )
            prior_cycle["last_success_replayed_at"] = utc_now()
            if wait_matches_cycle:
                state.pop("provider_wait", None)
            self._update(wf, state=state)
            return result

        successful_call_key = str(
            prior_cycle.get("successful_call_key") or ""
        ).strip()
        if successful_call_key:
            result = await self.executor.execute(
                prompt_id,
                envelope,
                project_id=wf["project_id"],
                workflow_id=wf["id"],
                original_environment=state.get("original_environment"),
                call_key=successful_call_key,
            )
            prior_cycle["last_success_replayed_at"] = utc_now()
            if wait_matches_cycle:
                state.pop("provider_wait", None)
            if recovery_run_id:
                state.pop("contract_migration_recovery", None)
            self._update(wf, state=state)
            return result

        if wait_matches_cycle and (persisted_wait.get("decision") or {}).get(
            "should_retry"
        ) is False:
            raise self._provider_exhaustion_from_checkpoint(persisted_wait)

        semantic_retry_issues = [
            copy.deepcopy(item)
            for item in prior_cycle.get("semantic_retry_issues") or []
            if isinstance(item, dict)
        ]
        while True:
            in_flight = int(prior_cycle.get("attempt_in_flight") or 0)
            attempt_number = (
                in_flight
                if in_flight > completed_attempts
                else completed_attempts + 1
            )
            attempt_call_key = (
                f"{base_call_key}-cycle-{cycle_id}-attempt-{attempt_number}"
            )
            prior_cycle["attempt_in_flight"] = attempt_number
            prior_cycle["updated_at"] = utc_now()
            state["provider_wait"] = {
                "retry_key": retry_key,
                "cycle_id": cycle_id,
                "input_hash": input_hash,
                "protocol_version": MODEL_RESPONSE_PROTOCOL_VERSION,
                "provider_request_spec_hash": provider_request_spec_hash,
                "completed_attempts": completed_attempts,
                "attempt_in_flight": attempt_number,
                "base_call_key": base_call_key,
                "attempt_call_key": attempt_call_key,
                "retry_limit": policy.max_retries,
                "max_attempts": policy.max_retries + 1,
                "prompt_id": prompt_id,
                "section_id": section_id or None,
                "section_phase": section_phase or None,
                "semantic_retry_issues": copy.deepcopy(semantic_retry_issues),
                "phase": "CALLING",
            }
            # Persist the attempt identity before invoking the provider.  If the
            # process exits while the request is in flight, the same call key is
            # replayed instead of allocating a fresh retry budget.
            self._update(wf, state=state)
            try:
                execute_kwargs = {
                    "project_id": wf["project_id"],
                    "workflow_id": wf["id"],
                    "original_environment": state.get("original_environment"),
                    "call_key": attempt_call_key,
                    "recovery_run_id": recovery_run_id,
                }
                if semantic_retry_issues:
                    execute_kwargs["semantic_retry_issues"] = semantic_retry_issues
                semantic_baseline_runs = state.get(
                    "semantic_producer_regeneration_baseline_runs"
                ) or {}
                semantic_baseline_run_id = str(
                    semantic_baseline_runs.get(prompt_id) or ""
                ).strip()
                if semantic_baseline_run_id:
                    execute_kwargs["semantic_regeneration_baseline_run_id"] = (
                        semantic_baseline_run_id
                    )
                result = await self.executor.execute(
                    prompt_id,
                    envelope,
                    **execute_kwargs,
                )
            except (PromptExecutionError, ValueError, KeyError) as exc:
                if bool(getattr(exc, "recoverable", False)):
                    # The executor may have committed a successful call and then
                    # raised a crash-injection/recovery signal.  Keep the exact
                    # attempt in flight so restart replays the same call key and
                    # lets the executor return its atomic committed result.
                    raise
                completed_attempts = attempt_number
                prior_cycle["completed_attempts"] = completed_attempts
                prior_cycle.pop("attempt_in_flight", None)
                prior_cycle["updated_at"] = utc_now()
                classification = classify_runtime_failure(exc)
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
                retry_allowed_here = (
                    classification.retryable
                    and (
                        retry_categories is None
                        or classification.category in retry_categories
                    )
                )
                next_semantic_retry_issues = self._semantic_retry_issues(
                    prompt_id, exc
                )
                if next_semantic_retry_issues:
                    semantic_retry_issues = next_semantic_retry_issues
                    prior_cycle["semantic_retry_issues"] = copy.deepcopy(
                        semantic_retry_issues
                    )
                if decision.should_retry and not retry_allowed_here:
                    decision = RetryDecision(
                        should_retry=False,
                        completed_attempts=decision.completed_attempts,
                        retry_number=decision.retry_number,
                        max_retries=decision.max_retries,
                        max_attempts=decision.max_attempts,
                        delay_seconds=0.0,
                        waiting_status=decision.waiting_status,
                        exhausted_status=classification.workflow_status,
                        reason=(
                            "provider failure is not retryable at this workflow boundary"
                        ),
                    )
                state["provider_wait"] = {
                    "retry_key": retry_key,
                    "cycle_id": cycle_id,
                    "input_hash": input_hash,
                    "protocol_version": MODEL_RESPONSE_PROTOCOL_VERSION,
                    "provider_request_spec_hash": provider_request_spec_hash,
                    "completed_attempts": completed_attempts,
                    "retry_number": decision.retry_number,
                    "base_call_key": base_call_key,
                    "attempt_call_key": attempt_call_key,
                    "retry_limit": policy.max_retries,
                    "max_retries": decision.max_retries,
                    "max_attempts": decision.max_attempts,
                    "delay_seconds": decision.delay_seconds,
                    "retry_not_before": self._retry_not_before(
                        decision.delay_seconds if decision.should_retry else 0.0
                    ),
                    "prompt_id": prompt_id,
                    "section_id": section_id or None,
                    "section_phase": section_phase or None,
                    "failure_kind": classification.failure_kind,
                    "category": classification.category.value,
                    "workflow_status": classification.workflow_status,
                    "retryable": classification.retryable,
                    "http_status": classification.http_status,
                    "retry_after_seconds": classification.retry_after_seconds,
                    "decision": decision.to_dict(),
                    "exhausted_status": decision.exhausted_status,
                    "last_error": redact_secret_text(str(exc)),
                    "failure_run_id": str(getattr(exc, "run_id", "") or "") or None,
                    "semantic_retry_issues": copy.deepcopy(semantic_retry_issues),
                    "phase": "FAILED",
                }

                # Persist every completed failure, including the terminal one,
                # before control returns to the workflow boundary.  A crash
                # between this point and status classification therefore cannot
                # grant another provider attempt.
                self._update(wf, state=state)

                failed_provider_wait = copy.deepcopy(state["provider_wait"])
                contract_repair = await self._repair_producer_contract_failure(
                    wf,
                    state,
                    prompt_id=prompt_id,
                    envelope=envelope,
                    exc=exc,
                    classification=classification,
                )
                if contract_repair.get("result") is not None:
                    result = contract_repair["result"]
                    # Applying the repair persists a deep-copied workflow state;
                    # continue bookkeeping on that current cycle object.
                    prior_cycle = state.setdefault(
                        "provider_call_cycles", {}
                    ).setdefault(retry_key, prior_cycle)
                else:
                    if contract_repair.get("attempted"):
                        # Restore the producer checkpoint after a failed repair,
                        # but do not strand a still-retryable producer cycle.
                        # The common bounded retry path below owns the remaining
                        # attempts and preserves the existing backoff/ledger.
                        state["provider_wait"] = failed_provider_wait
                        self._update(wf, state=state)
                    if not retry_allowed_here:
                        raise

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
                            "cycle_id": cycle_id,
                            "attempt_call_key": attempt_call_key,
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
                        await self._honor_persisted_retry_delay(
                            state["provider_wait"]
                        )
                    continue

            completed_attempts = attempt_number
            prior_cycle["completed_attempts"] = completed_attempts
            prior_cycle.pop("attempt_in_flight", None)
            prior_cycle.pop("semantic_retry_issues", None)
            prior_cycle["successful_attempt"] = attempt_number
            contract_repair_metadata = result.get("contract_repair")
            if isinstance(contract_repair_metadata, dict):
                prior_cycle.pop("successful_call_key", None)
                prior_cycle["successful_contract_repair_run_id"] = str(
                    contract_repair_metadata.get("repair_run_id") or ""
                )
                prior_cycle["successful_contract_repair_artifact_id"] = str(
                    contract_repair_metadata.get(
                        "repair_application_artifact_id"
                    )
                    or ""
                )
                prior_cycle["contract_repair_source_run_id"] = str(
                    contract_repair_metadata.get("source_run_id") or ""
                )
            else:
                prior_cycle.pop("successful_contract_repair_run_id", None)
                prior_cycle.pop("successful_contract_repair_artifact_id", None)
                prior_cycle.pop("contract_repair_source_run_id", None)
                prior_cycle["successful_call_key"] = str(
                    result.get("call_key") or attempt_call_key
                )
            prior_cycle["successful_run_id"] = str(
                result.get("run_id") or ""
            ) or None
            prior_cycle["completed_at"] = utc_now()
            if completed_attempts > 1:
                RepairLedger.provider_recovered(
                    state,
                    retry_key,
                    details={
                        "prompt_id": prompt_id,
                        "cycle_id": cycle_id,
                        "successful_call_key": prior_cycle.get("successful_call_key"),
                        "completed_attempts": completed_attempts,
                        "retries_used": completed_attempts - 1,
                    },
                )
            state.pop("provider_wait", None)
            if recovery_run_id:
                state.pop("contract_migration_recovery", None)
            self._update(wf, state=state)
            return result

    @staticmethod
    def _provider_exhaustion_from_checkpoint(
        wait: dict[str, Any],
    ) -> ProviderRetriesExhausted:
        """Recreate the typed exhaustion raised before a process restart."""

        decision_data = dict(wait.get("decision") or {})
        completed_attempts = max(
            1,
            int(
                decision_data.get("completed_attempts")
                or wait.get("completed_attempts")
                or 1
            ),
        )
        max_retries = max(
            0,
            int(
                decision_data.get("max_retries")
                if decision_data.get("max_retries") is not None
                else wait.get("retry_limit") or 0
            ),
        )
        category_raw = str(
            wait.get("category") or FailureCategory.PROVIDER_TRANSIENT.value
        )
        try:
            category = FailureCategory(category_raw)
        except ValueError:
            category = FailureCategory.PROVIDER_TRANSIENT
        exhausted_status = str(
            decision_data.get("exhausted_status")
            or wait.get("exhausted_status")
            or wait.get("workflow_status")
            or WorkflowStatus.BLOCKED_PROVIDER.value
        )
        classification = FailureClassification(
            category=category,
            workflow_status=str(
                wait.get("workflow_status") or exhausted_status
            ),
            retryable=bool(wait.get("retryable", True)),
            consumes_semantic_repair_budget=False,
            reason="provider retry exhaustion restored from workflow checkpoint",
            failure_kind=(str(wait.get("failure_kind")) if wait.get("failure_kind") else None),
            http_status=wait.get("http_status"),
            retry_after_seconds=wait.get("retry_after_seconds"),
            details={"restored_from_checkpoint": True},
        )
        decision = RetryDecision(
            should_retry=False,
            completed_attempts=completed_attempts,
            retry_number=int(
                decision_data.get("retry_number") or completed_attempts
            ),
            max_retries=max_retries,
            max_attempts=int(
                decision_data.get("max_attempts") or max_retries + 1
            ),
            delay_seconds=0.0,
            waiting_status=str(
                decision_data.get("waiting_status")
                or WorkflowStatus.WAITING_PROVIDER.value
            ),
            exhausted_status=exhausted_status,
            reason=str(
                decision_data.get("reason")
                or "provider retry budget is exhausted"
            ),
        )
        original = PromptExecutionError(
            str(wait.get("last_error") or "persisted provider retries exhausted")
        )
        failure = dict(wait.get("failure") or {})
        failure.setdefault("prompt_id", wait.get("prompt_id"))
        failure.setdefault("retryable", True)
        failure.setdefault("failure_kind", wait.get("failure_kind"))
        failure["restored_from_checkpoint"] = True
        return ProviderRetriesExhausted(
            original,
            classification=classification,
            decision=decision,
            failure_payload=failure,
        )

    def _seal_persisted_provider_exhaustion(
        self,
        wf: dict[str, Any],
        state: dict[str, Any],
    ) -> bool:
        """Close a terminal retry decision that survived a process crash."""

        wait = state.get("provider_wait")
        if not isinstance(wait, dict):
            return False
        if bool(wait.get("exhausted")):
            # The boundary already persisted the classified workflow status.
            # In particular, WAITING_CONFIGURATION must be allowed to reach the
            # dependency recheck below rather than being sealed on every advance.
            return False
        if (wait.get("decision") or {}).get("should_retry") is not False:
            return False
        exc = self._provider_exhaustion_from_checkpoint(wait)
        state["last_error"] = redact_secret_text(str(exc.original_exception))
        state["provider_wait"] = {
            **wait,
            "exhausted": True,
            "exhausted_status": exc.decision.exhausted_status,
            "failure": exc.failure_payload,
            "boundary": str(wait.get("boundary") or "CRASH_RECOVERY"),
        }
        self._update(
            wf,
            status=exc.decision.exhausted_status,
            state=state,
        )
        self.db.audit(
            "PROVIDER_RETRIES_EXHAUSTED_RECOVERED",
            project_id=wf["project_id"],
            object_id=wf["id"],
            metadata={
                "retry_key": wait.get("retry_key"),
                "cycle_id": wait.get("cycle_id"),
                "completed_attempts": exc.decision.completed_attempts,
                "workflow_status": exc.decision.exhausted_status,
            },
        )
        # A configuration wait is not a terminal provider block.  After the
        # checkpoint is sealed, continue into the dependency recheck in this
        # same advance call so repaired credentials/endpoints can invalidate the
        # obsolete failed call generation immediately.
        return (
            exc.decision.exhausted_status
            != WorkflowStatus.WAITING_CONFIGURATION.value
        )

    @staticmethod
    def _invalidate_provider_checkpoint_after_configuration_recovery(
        state: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Start a fresh provider generation after dependencies are repaired.

        A non-retryable configuration response (for example HTTP 401/403) is
        deliberately persisted and replayed by exact call key.  Once the
        dependency checker proves that configuration has changed, retaining the
        same call key would replay the obsolete failure forever.  Mark the old
        cycle for a new generation and archive a bounded checkpoint summary.
        """

        wait = state.get("provider_wait")
        if not isinstance(wait, dict):
            return None
        decision = wait.get("decision") or {}
        configuration_wait = (
            str(wait.get("category") or "") == FailureCategory.CONFIGURATION.value
            or str(wait.get("workflow_status") or "")
            == WorkflowStatus.WAITING_CONFIGURATION.value
            or str(wait.get("exhausted_status") or decision.get("exhausted_status") or "")
            == WorkflowStatus.WAITING_CONFIGURATION.value
        )
        if not configuration_wait:
            return None

        retry_key = str(wait.get("retry_key") or "").strip()
        cycle_id = str(wait.get("cycle_id") or "").strip() or None
        cycle = (state.get("provider_call_cycles") or {}).get(retry_key)
        invalidated_at = utc_now()
        if isinstance(cycle, dict):
            cycle["force_new_generation"] = True
            cycle["invalidated_at"] = invalidated_at
            cycle["invalidation_reason"] = "CONFIGURATION_RECOVERED"
            cycle.pop("attempt_in_flight", None)
            cycle.pop("successful_call_key", None)
            cycle.pop("successful_run_id", None)

        summary = {
            "retry_key": retry_key or None,
            "cycle_id": cycle_id,
            "completed_attempts": int(wait.get("completed_attempts") or 0),
            "failure_run_id": wait.get("failure_run_id"),
            "failure_kind": wait.get("failure_kind"),
            "http_status": wait.get("http_status"),
            "reason": "CONFIGURATION_RECOVERED",
            "invalidated_at": invalidated_at,
        }
        history = state.setdefault("provider_checkpoint_history", [])
        history.append(summary)
        del history[:-50]
        state.pop("provider_wait", None)
        return summary

    def _block_provider_retries_exhausted(
        self,
        wf: dict[str, Any],
        state: dict[str, Any],
        exc: ProviderRetriesExhausted,
        *,
        boundary: str,
    ) -> dict[str, Any]:
        """Persist one typed provider-exhaustion outcome at any node boundary."""

        state["last_error"] = redact_secret_text(str(exc.original_exception))
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

    def _recover_retryable_provider_checkpoint(
        self,
        wf: dict[str, Any],
        state: dict[str, Any],
    ) -> bool:
        """Reopen a blocked workflow when its persisted retry is still pending.

        A failed contract-repair attempt may leave the outer producer checkpoint
        with ``decision.should_retry=true`` and unused attempts.  The workflow
        status must not hide that already-persisted retry decision.  This helper
        changes only orchestration status; it preserves the exact provider cycle,
        attempt counters, call keys, and backoff timestamp.
        """

        if not is_recoverable_block(wf["status"]):
            return False
        wait = state.get("provider_wait")
        if not isinstance(wait, dict):
            return False
        if (wait.get("decision") or {}).get("should_retry") is not True:
            return False
        if not self._provider_wait_matches_checkpoint(wf, state, wait):
            return False

        recovery = {
            "reason": "PERSISTED_PROVIDER_RETRY_STILL_AVAILABLE",
            "from_status": wf["status"],
            "to_status": WorkflowStatus.RUNNING.value,
            "prompt_id": wait.get("prompt_id"),
            "completed_attempts": wait.get("completed_attempts"),
            "max_attempts": wait.get("max_attempts"),
            "recovered_at": utc_now(),
        }
        state.setdefault("checkpoint_recovery_history", []).append(recovery)
        del state["checkpoint_recovery_history"][:-50]
        state["recovered_from"] = wf["status"]
        state.pop("last_error", None)
        self._update(
            wf,
            status=WorkflowStatus.RUNNING.value,
            state=state,
        )
        self.db.audit(
            "PROVIDER_RETRY_CHECKPOINT_RECOVERED",
            project_id=wf["project_id"],
            object_id=wf["id"],
            metadata=recovery,
        )
        return True

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
        if prompt_id == "P-ARGUMENT-ARCHITECTURE":
            # v8 authoritative-state path: quality guard is observation-only.
            # Producer status/result come only from the projector.
            return None, raw_status, copy.deepcopy(output)
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
        if str(output.get("prompt_id") or "") == "P-ARGUMENT-ARCHITECTURE-CRITIC":
            # Decision records remain useful audit metadata, but they have no
            # write capability over the canonical Argument Critic container.
            return str(output.get("status") or result.get("status") or "ERROR"), output
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
        semantic_canonical = (
            str(output.get("prompt_id") or "") == "P-ARGUMENT-ARCHITECTURE-CRITIC"
            and isinstance((output.get("result") or {}).get("deterministic_receipts"), list)
        )
        actionable = (
            [copy.deepcopy(item) for item in output.get("findings") or [] if isinstance(item, dict)]
            if semantic_canonical
            else []
        )
        seen = {
            (
                str(item.get("defect_key") or ""),
                str(item.get("finding_instance_id") or ""),
                str(item.get("code") or ""),
                str(item.get("target_path_or_span") or ""),
            )
            for item in actionable
        }
        for entry in (decision.get("decision_basis") or {}).get("actionable_findings") or []:
            if isinstance(entry, dict) and isinstance(entry.get("finding"), dict):
                finding = copy.deepcopy(entry["finding"])
                # The decision source is audit metadata, not part of the common
                # Finding schema passed to repair prompts.
                finding.pop("rule_id", None)
                finding.pop("responsibility", None)
                finding.pop("source", None)
                key = (
                    str(finding.get("defect_key") or ""),
                    str(finding.get("finding_instance_id") or ""),
                    str(finding.get("code") or ""),
                    str(finding.get("target_path_or_span") or ""),
                )
                if key not in seen:
                    actionable.append(finding)
                    seen.add(key)
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

    def _prepare_semantic_producer_regeneration(
        self,
        wf: dict[str, Any],
        state: dict[str, Any],
        *,
        producer_prompt: str,
        output: dict[str, Any],
    ) -> str:
        """Retry a semantic producer for deterministic non-USER deficiencies.

        The Runtime does not invent research content here.  It only converts its
        already-derived evidence/readiness deficiencies into a bounded revision
        card for the same producer.  A remaining deficiency after the configured
        budget is content-blocking, never an empty or advisory human Gate.
        """
        entry = self.pack.entry(producer_prompt)
        if str(entry.get("model_contract_mode") or "").upper() != "SEMANTIC":
            return "NOT_APPLICABLE"
        if producer_prompt not in set(CRITIC_PRODUCER.values()):
            return "NOT_APPLICABLE"

        gap_report = [
            copy.deepcopy(item)
            for item in (output.get("result") or {}).get("evidence_gap_report") or []
            if isinstance(item, dict)
            and bool(item.get("blocking"))
            and str(item.get("suggested_route") or "ORIGINAL_PRODUCER").upper()
            == "ORIGINAL_PRODUCER"
        ]
        if not gap_report:
            return "NOT_APPLICABLE"

        options = state.get("options") or {}
        try:
            limit = int(options.get("semantic_producer_regeneration_limit", 1))
        except (TypeError, ValueError):
            limit = 1
        limit = max(0, min(limit, 3))

        rounds = state.get("semantic_producer_regeneration_rounds") or {}
        completed = int(rounds.get(producer_prompt) or 0)
        if completed >= limit:
            state["last_error"] = (
                f"{producer_prompt} 在 {completed} 轮非 USER 语义补全重生成后仍存在"
                "证据或研究链缺口；继续自动重试不会增加新的证据来源。"
                "该缺口保持内容阻断，不转为空问题人工 Gate。"
            )
            self._clear_workflow_repair_rereview(state, producer_prompt)
            self._update(
                wf,
                status=WorkflowStatus.BLOCKED_CONTENT.value,
                state=state,
            )
            return "EXHAUSTED"

        round_number = completed + 1
        feedback = [
            semantic_gap_revision_finding(
                gap,
                producer_prompt=producer_prompt,
                round_number=round_number,
                index=index,
            )
            for index, gap in enumerate(gap_report, 1)
        ]
        validation_errors: list[str] = []
        for index, finding in enumerate(feedback):
            validation_errors.extend(
                f"/{index}{error}"
                for error in self.pack.validate_common(
                    "finding.schema.json", finding
                )
            )
        if validation_errors:
            raise ValueError(
                "Runtime generated invalid semantic regeneration findings: "
                + "; ".join(validation_errors[:20])
            )

        # Prepare and validate the exact next-round state before committing the
        # scheduling transition. A malformed adapter output must never be
        # persisted and discovered only by the following advance call.
        next_state = copy.deepcopy(state)
        next_state.setdefault("semantic_producer_regeneration_rounds", {})[
            producer_prompt
        ] = round_number
        next_state.setdefault("producer_revision_findings", {})[
            producer_prompt
        ] = feedback
        current_step = int(wf.get("current_step") or 0)
        current_result = (state.get("step_results") or {}).get(str(current_step)) or {}
        baseline_run_id = str(current_result.get("run_id") or "").strip()
        if not baseline_run_id:
            raise ValueError(
                f"{producer_prompt} semantic regeneration requires the exact "
                "persisted baseline run id"
            )
        next_state.setdefault("semantic_producer_regeneration_baseline_runs", {}).setdefault(
            producer_prompt, baseline_run_id
        )
        next_state.setdefault("semantic_producer_regeneration_history", []).append(
            {
                "producer_prompt": producer_prompt,
                "round": round_number,
                "gap_ids": [
                    str(item.get("gap_id") or "")
                    for item in gap_report
                    if item.get("gap_id")
                ],
                "defect_keys": [
                    str(item.get("defect_key") or "")
                    for item in gap_report
                    if item.get("defect_key")
                ],
                "baseline_run_id": baseline_run_id,
                "created_at": utc_now(),
            }
        )
        del next_state["semantic_producer_regeneration_history"][:-50]

        next_state.setdefault("step_results", {}).pop(str(current_step), None)
        next_state.pop("provider_wait", None)
        self._clear_workflow_repair_rereview(next_state, producer_prompt)
        self.context_builder.build(
            producer_prompt,
            wf["project_id"],
            workflow_id=wf["id"],
            workflow_state=next_state,
        )
        state.clear()
        state.update(next_state)
        self._update(
            wf,
            status=WorkflowStatus.RUNNING.value,
            current_step=current_step,
            state=state,
        )
        self.db.audit(
            "SEMANTIC_PRODUCER_REGENERATION_SCHEDULED",
            project_id=wf["project_id"],
            object_id=wf["id"],
            metadata={
                "producer_prompt": producer_prompt,
                "round": round_number,
                "gap_count": len(gap_report),
                "step": current_step,
            },
        )
        return "SCHEDULED"

    def _prepare_original_producer_regeneration(
        self,
        wf: dict[str, Any],
        state: dict[str, Any],
        *,
        critic_prompt: str,
        output: dict[str, Any],
    ) -> str:
        """Route structural semantic findings back to their original producer."""
        entry = self.pack.entry(critic_prompt)
        if str(entry.get("model_contract_mode") or "").upper() != "SEMANTIC":
            return "NOT_APPLICABLE"
        producer = CRITIC_PRODUCER.get(critic_prompt)
        if not producer:
            return "NOT_APPLICABLE"
        findings = [
            copy.deepcopy(item)
            for item in output.get("findings") or []
            if isinstance(item, dict)
            and bool(item.get("blocking", True))
            and str(item.get("suggested_route") or "").upper() == "ORIGINAL_PRODUCER"
        ]
        if not findings:
            return "NOT_APPLICABLE"

        steps = self.get(wf["id"])["steps"]
        producer_steps = [
            index for index, step in enumerate(steps)
            if step.get("prompt_id") == producer
        ]
        if not producer_steps:
            state["last_error"] = (
                f"{critic_prompt} requested ORIGINAL_PRODUCER regeneration, "
                f"but producer {producer} is not present in this workflow."
            )
            self._update(wf, status=WorkflowStatus.BLOCKED_TECHNICAL.value, state=state)
            return "EXHAUSTED"
        target_step = producer_steps[0]
        if target_step >= int(wf.get("current_step") or 0):
            state["last_error"] = (
                f"{critic_prompt} requested regeneration of {producer}, but "
                "the producer is not an earlier workflow step."
            )
            self._update(wf, status=WorkflowStatus.BLOCKED_TECHNICAL.value, state=state)
            return "EXHAUSTED"

        options = state.get("options") or {}
        try:
            limit = int(options.get("original_producer_regeneration_limit", 2))
        except (TypeError, ValueError):
            limit = 2
        limit = max(0, min(limit, 5))
        rounds = state.setdefault("producer_regeneration_rounds", {})
        completed = int(rounds.get(critic_prompt) or 0)
        if completed >= limit:
            state["last_error"] = (
                f"{critic_prompt} 在 {completed} 轮原生产阶段重生成后仍存在结构性语义问题；"
                "继续自动重生成可能形成循环，需要补充事实、调整研究设计或人工决定。"
            )
            self._clear_workflow_repair_rereview(state, critic_prompt)
            self._update(wf, status=WorkflowStatus.BLOCKED_CONTENT.value, state=state)
            return "EXHAUSTED"

        round_number = completed + 1
        rounds[critic_prompt] = round_number
        state.setdefault("producer_revision_findings", {})[producer] = findings
        state.setdefault("producer_regeneration_history", []).append({
            "critic_prompt": critic_prompt,
            "producer_prompt": producer,
            "round": round_number,
            "finding_instance_ids": [str(item.get("finding_instance_id") or "") for item in findings if item.get("finding_instance_id")],
            "finding_codes": [str(item.get("code") or "") for item in findings if item.get("code")],
            "from_step": int(wf.get("current_step") or 0),
            "to_step": target_step,
            "created_at": utc_now(),
        })
        del state["producer_regeneration_history"][:-50]
        step_results = state.setdefault("step_results", {})
        for key in list(step_results):
            try:
                index = int(key)
            except (TypeError, ValueError):
                continue
            if index >= target_step:
                step_results.pop(key, None)
        state.pop("provider_wait", None)
        if producer == "P-ARGUMENT-ARCHITECTURE":
            # ORIGINAL_PRODUCER establishes a new semantic subject.  The old
            # targeted-repair override must stop being active at scheduling time,
            # not only after a replacement Producer run eventually succeeds.
            self._supersede_repair_subject(
                state,
                critic_prompt=critic_prompt,
                producer_prompt=producer,
                reason="ORIGINAL_PRODUCER_REGENERATION_SCHEDULED",
            )
        else:
            self._clear_workflow_repair_rereview(state, critic_prompt)
        if producer == "P-ARGUMENT-ARCHITECTURE":
            state["section_results"] = []
            state["planning_revision_findings"] = []
            state.pop("integration_repair_section_ids", None)
            state.pop("integration_repair_findings", None)
            state.pop("active_section_id", None)
            state.pop("active_section_index", None)

        wf["current_step"] = target_step
        self._update(wf, status=WorkflowStatus.RUNNING.value, current_step=target_step, state=state)
        self.db.audit(
            "ORIGINAL_PRODUCER_REGENERATION_SCHEDULED",
            project_id=wf["project_id"],
            object_id=wf["id"],
            metadata={
                "critic_prompt": critic_prompt,
                "producer_prompt": producer,
                "round": round_number,
                "target_step": target_step,
                "finding_codes": [str(item.get("code") or "") for item in findings if item.get("code")],
            },
        )
        return "SCHEDULED"

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

    def _migrate_legacy_blocked_status(
        self,
        wf: dict[str, Any],
    ) -> dict[str, Any]:
        """Classify one historical generic BLOCKED checkpoint fail-closed.

        The old runtime used ``BLOCKED`` for provider, contract, content, human
        input, and technical failures.  Consuming a generic technical retry
        before recovering that category can call the provider again for a
        content defect or bypass an existing human-input requirement.
        """

        if wf["status"] != WorkflowStatus.BLOCKED.value:
            return wf
        state = wf["state"]
        step_key = str(wf["current_step"])
        current_result = (state.get("step_results") or {}).get(step_key)
        output: dict[str, Any] = {}
        questions: list[Any] = []
        blocking_questions: list[Any] = []
        run_id = ""
        prompt_id = ""
        result_status = ""
        if isinstance(current_result, dict):
            run_id = str(current_result.get("run_id") or "")
            prompt_id = str(current_result.get("prompt_id") or "")
            result_status = str(current_result.get("status") or "")
            if run_id:
                run = self.db.fetchone(
                    "SELECT output_json FROM prompt_runs WHERE id=? AND workflow_id=?",
                    (run_id, wf["id"]),
                )
                try:
                    decoded = json.loads((run or {}).get("output_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    decoded = {}
                if isinstance(decoded, dict):
                    output = decoded
            questions = list(output.get("user_questions") or [])
            blocking_questions = [
                item
                for item in questions
                if isinstance(item, dict) and bool(item.get("blocking"))
            ]

        requires_human_input = bool(questions) and (
            result_status == "NEED_USER_INPUT"
            or (result_status == "BLOCK" and bool(blocking_questions))
        )
        if requires_human_input:
            current_result["status"] = "NEED_USER_INPUT"
            migration = {
                "from": WorkflowStatus.BLOCKED.value,
                "to": WorkflowStatus.WAITING_GATE.value,
                "step": int(wf["current_step"]),
                "prompt_id": prompt_id or None,
                "run_id": run_id or None,
                "reason": "persisted blocking user questions require an exact human Gate",
                "migrated_at": utc_now(),
            }
            state["legacy_blocked_status_migration"] = migration
            state.setdefault("status_migrations", []).append(dict(migration))
            del state["status_migrations"][:-50]
            state.pop("last_error", None)
            self._update(wf, status=WorkflowStatus.RUNNING.value, state=state)
            refreshed = self.get(wf["id"])
            prompt_entry: dict[str, Any] = {}
            if prompt_id:
                try:
                    prompt_entry = self.pack.entry(prompt_id)
                except (AttributeError, KeyError):
                    # Historical rows may reference a Prompt removed from the
                    # current registry.  The persisted questions still prove a
                    # human checkpoint, so fail closed with the generic Gate
                    # rather than crashing migration or advancing the workflow.
                    prompt_entry = {}
            gate_type = prompt_entry.get("next_human_gate") or "PROJECT_GAP_RESOLUTION"
            self._create_gate(
                refreshed,
                gate_type,
                target_id=run_id or wf["id"],
                questions=questions,
            )
            self._update(
                refreshed,
                status=WorkflowStatus.WAITING_GATE.value,
                state=state,
            )
            self.db.audit(
                "WORKFLOW_LEGACY_BLOCK_CLASSIFIED",
                project_id=wf["project_id"],
                object_id=wf["id"],
                metadata=migration,
            )
            return self.get(wf["id"])

        error = str(state.get("last_error") or state.get("recovered_from") or "")
        if result_status == "REVISE" or (
            "确定性质量校验" in error
            or any(
                str(item).startswith("QG_")
                for item in state.get("quality_blocker_ids") or []
            )
        ):
            target = WorkflowStatus.BLOCKED_CONTENT
            reason = "persisted semantic or deterministic quality evidence"
        else:
            target = classify_legacy_blocked_error(error)
            reason = "persisted legacy error text" if error else "no recoverable category evidence"
            if not error:
                state["last_error"] = (
                    "Historical generic BLOCKED checkpoint has no classifiable failure evidence; "
                    "manual technical review is required."
                )

        migration = {
            "from": WorkflowStatus.BLOCKED.value,
            "to": target.value,
            "step": int(wf["current_step"]),
            "prompt_id": prompt_id or None,
            "run_id": run_id or None,
            "result_status": result_status or None,
            "reason": reason,
            "migrated_at": utc_now(),
        }
        state["legacy_blocked_status_migration"] = migration
        state.setdefault("status_migrations", []).append(dict(migration))
        del state["status_migrations"][:-50]
        self._update(wf, status=target.value, state=state)
        self.db.audit(
            "WORKFLOW_LEGACY_BLOCK_CLASSIFIED",
            project_id=wf["project_id"],
            object_id=wf["id"],
            metadata=migration,
        )
        return self.get(wf["id"])

    def _pause_for_workflow_input(
        self,
        wf: dict[str, Any],
        state: dict[str, Any],
        exc: WorkflowInputRequired,
    ) -> dict[str, Any]:
        prompt_id = exc.prompt_id
        state["last_error"] = redact_secret_text(str(exc))
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
        canonical_status = coerce_workflow_status(wf["status"]).value
        if canonical_status != wf["status"]:
            legacy_status = str(wf["status"])
            state = wf["state"]
            state.setdefault("status_migrations", []).append(
                {
                    "from": legacy_status,
                    "to": canonical_status,
                    "migrated_at": utc_now(),
                }
            )
            del state["status_migrations"][:-50]
            self._update(wf, status=canonical_status, state=state)
            self.db.audit(
                "WORKFLOW_STATUS_MIGRATED",
                project_id=wf["project_id"],
                object_id=workflow_id,
                metadata={"from": legacy_status, "to": canonical_status},
            )
            wf = self.get(workflow_id)
        current_gate = self._reconcile_open_gates(wf)
        wf = self.get(workflow_id)
        if current_gate is not None:
            return wf
        if is_terminal(wf["status"]):
            return wf
        state = wf["state"]
        if self._seal_persisted_provider_exhaustion(wf, state):
            return self.get(workflow_id)
        if self._recover_provider_block_after_protocol_upgrade(wf, state):
            wf = self.get(workflow_id)
            state = wf["state"]
        if self._recover_contract_block_after_normalizer_upgrade(wf, state):
            wf = self.get(workflow_id)
            state = wf["state"]
        if self._recover_retryable_provider_checkpoint(wf, state):
            wf = self.get(workflow_id)
            state = wf["state"]
        if (
            is_recoverable_block(wf["status"])
            and wf["status"] != WorkflowStatus.BLOCKED.value
        ):
            return wf
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
            if (
                wf["status"] == WorkflowStatus.WAITING_CONFIGURATION.value
                and self.dependency_preflight is None
            ):
                # The dependency checker is the authority that can prove a
                # configuration wait has cleared.  Its absence is not evidence
                # of recovery and must not reopen the workflow.
                return wf
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
            provider_checkpoint = (
                self._invalidate_provider_checkpoint_after_configuration_recovery(
                    state
                )
            )
            if provider_checkpoint:
                state.setdefault("configuration_recovered", {})[
                    "provider_checkpoint"
                ] = provider_checkpoint
            state.pop("last_error", None)
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
        if wf["status"] == WorkflowStatus.BLOCKED.value:
            # Classification is a separate persisted checkpoint.  Provider,
            # contract, content, and human-input failures must not consume the
            # old generic technical retry budget before their category is known.
            return self._migrate_legacy_blocked_status(wf)
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
                    state["last_error"] = redact_secret_text(str(exc))
                    state["public_research_failure"] = {
                        "category": str(getattr(exc, "category", "RUNTIME") or "RUNTIME"),
                        "error_code": str(getattr(exc, "error_code", "PUBLIC_RESEARCH_RUNTIME_ERROR") or "PUBLIC_RESEARCH_RUNTIME_ERROR"),
                        "message": redact_secret_text(str(exc)),
                        "details": redact_secrets(dict(getattr(exc, "details", {}) or {})),
                        "step": int(wf["current_step"]),
                        "recorded_at": utc_now(),
                    }
                    category = str(getattr(exc, "category", "RUNTIME") or "RUNTIME").upper()
                    blocked_status = {
                        "PLAN_CONTRACT": WorkflowStatus.BLOCKED_CONTRACT.value,
                        "RETRIEVAL": WorkflowStatus.BLOCKED_PROVIDER.value,
                        "SECURITY": WorkflowStatus.BLOCKED_CONTENT.value,
                        "INTEGRITY": WorkflowStatus.BLOCKED_CONTRACT.value,
                    }.get(category, WorkflowStatus.BLOCKED_TECHNICAL.value)
                    self._update(wf, status=blocked_status, state=state)
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
                except ValueError as exc:
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
                    state["last_error"] = redact_secret_text(str(exc))
                    self._update(
                        wf,
                        status=WorkflowStatus.BLOCKED_CONTENT.value,
                        state=state,
                    )
                    return self.get(workflow_id)
                except KeyError as exc:
                    state["last_error"] = redact_secret_text(str(exc))
                    self._update(
                        wf,
                        status=WorkflowStatus.BLOCKED_TECHNICAL.value,
                        state=state,
                    )
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
            try:
                pending_rereview = self._workflow_repair_rereview_checkpoint(
                    state, prompt_id
                )
            except ValueError as exc:
                state["last_error"] = redact_secret_text(str(exc))
                self._update(
                    wf,
                    status=WorkflowStatus.BLOCKED_TECHNICAL.value,
                    state=state,
                )
                return self.get(workflow_id)
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
                state["last_error"] = redact_secret_text(str(exc))
                status = failure["workflow_status"]
                self._update(wf, status=status, state=state)
                return self.get(workflow_id)

            state["step_results"][str(wf["current_step"])] = {"prompt_id": prompt_id, "run_id": result["run_id"], "status": result["status"]}
            if prompt_id == "P-FINAL-CONFIDENTIALITY-REVIEW":
                payload = envelope.get("payload") or {}
                snapshot = visible_document_snapshot(payload.get("candidate_document") or {})
                if not snapshot.get("section_count"):
                    raise ValueError("Final confidentiality review did not receive a frozen candidate document.")
                state["final_review_candidate_snapshot"] = snapshot
                state["final_review_candidate_set_snapshot"] = (
                    self.context_builder.final_review_candidate_set_snapshot(
                        wf["project_id"],
                        state,
                        document_section_map=(payload.get("candidate_document") or {}).get("sections") or [],
                    )
                )
                state["final_review_run_id"] = str(result.get("run_id") or "")
                state["final_review_authoring_workflow_id"] = str(
                    (state.get("prerequisite_workflow_ids") or {}).get("WF-4_PROPOSAL_AUTHORING") or ""
                )
            state["original_environment"] = result["route"]["environment"]
            # Any newly executed Producer output is a new repair subject.  An
            # older semantic repair must never mask regeneration/human-input
            # reruns in ContextBuilder.  If this very result was recovered by
            # an output-contract repair, preserve only that newly committed
            # application because it is the canonical value for this run.
            contract_repair_artifact_id = str(
                ((result.get("contract_repair") or {}).get("repair_application_artifact_id"))
                or ""
            ).strip()
            for critic_id, producer_id in CRITIC_PRODUCER.items():
                if producer_id != prompt_id:
                    continue
                self._supersede_repair_subject(
                    state,
                    critic_prompt=critic_id,
                    producer_prompt=prompt_id,
                    reason="FRESH_PRODUCER_RESULT",
                    preserve_application_artifact_id=(
                        contract_repair_artifact_id or None
                    ),
                )
            output = result["output"]
            if prompt_id == "P-PUBLIC-RESEARCH-SYNTHESIS":
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
                    self._update(
                        wf,
                        status=WorkflowStatus.BLOCKED_CONTENT.value,
                        state=state,
                    )
                    return self.get(workflow_id)
                self._update(wf, state=state)
            decision, effective_status, effective_output = self._record_decision(
                wf, state, prompt_id, result
            )
            if isinstance(pending_rereview, dict):
                self._complete_repair_rereview(
                    state,
                    pending_rereview,
                    critic_prompt=prompt_id,
                    review_run_id=str(result.get("run_id") or "") or None,
                    status=effective_status,
                )
                # Keep the checkpoint until the effective result and its next
                # workflow transition are committed together.  A crash after
                # lifecycle completion must still resume as an independent
                # re-review rather than silently downgrading to an initial review.
                pending_rereview["review_run_id"] = (
                    str(result.get("run_id") or "") or None
                )
                pending_rereview["completed_status"] = effective_status
            observed_result = copy.deepcopy(result)
            observed_result["status"] = effective_status
            observed_result["output"] = copy.deepcopy(effective_output)
            self._observe_quality_result(
                wf, state, prompt_id, observed_result
            )
            if decision and decision.get("decision") == "CONTRACT_CONFLICT":
                self._clear_workflow_repair_rereview(state, prompt_id)
                state["last_error"] = (
                    f"Decision responsibility protocol is inconsistent for {prompt_id}; "
                    "the immutable critic output and guard report were preserved in DECISION_RECORD."
                )
                self._update(wf, status="BLOCKED_CONTRACT", state=state)
                return self.get(workflow_id)
            if (
                prompt_id == WF3_RESEARCH_CRITIC
                and effective_status in {"REVISE", "BLOCK"}
            ):
                routing = wf3_critic_routing_report(effective_output)
                state.setdefault("wf3_critic_routing_history", []).append(
                    {
                        **routing,
                        "run_id": str(result.get("run_id") or ""),
                        "recorded_at": utc_now(),
                    }
                )
                del state["wf3_critic_routing_history"][:-50]
                if routing["has_non_synthesis_route"]:
                    self._clear_workflow_repair_rereview(state, prompt_id)
                    route_counts = ", ".join(
                        f"{route}={count}"
                        for route, count in routing["route_counts"].items()
                        if count
                    )
                    state["last_error"] = (
                        "公开研究 Critic 返回了超出 Synthesis 写权限的阻断项（"
                        + route_counts
                        + "）。这些问题必须回到对应的检索或计划边界；"
                        "系统已保留精确 Finding，未把它们误送给 Synthesis 定向修复。"
                    )
                    self._update(
                        wf,
                        status=WorkflowStatus.BLOCKED_CONTENT.value,
                        state=state,
                    )
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
                        for item in effective_output.get("findings") or []
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
                    self._record_full_integration_review(
                        wf, state, observed_result
                    )
                except ValueError as exc:
                    state["last_error"] = redact_secret_text(str(exc))
                    self._update(
                        wf,
                        status=WorkflowStatus.BLOCKED_CONTRACT.value,
                        state=state,
                    )
                    return self.get(workflow_id)
            if effective_status == "BLOCK":
                self._clear_workflow_repair_rereview(state, prompt_id)
                self._update(
                    wf,
                    status=WorkflowStatus.BLOCKED_CONTENT.value,
                    state=state,
                )
                return self.get(workflow_id)
            if prompt_id == "P-INTEGRATION-CRITIC" and effective_status == "REVISE":
                if isinstance(pending_rereview, dict):
                    # _prepare_integration_repair persists its transition. Clear
                    # the completed checkpoint in the same state write rather
                    # than in a second crash window.
                    self._clear_workflow_repair_rereview(state, prompt_id)
                repair_state = self._prepare_integration_repair(wf, state, effective_output)
                if repair_state == "SCHEDULED":
                    wf = self.get(workflow_id)
                    state = wf["state"]
                    continue
                if repair_state == "EXHAUSTED":
                    return self.get(workflow_id)
            if effective_status == "REVISE":
                regeneration_state = self._prepare_original_producer_regeneration(
                    wf, state, critic_prompt=prompt_id, output=effective_output
                )
                if regeneration_state == "SCHEDULED":
                    wf = self.get(workflow_id)
                    state = wf["state"]
                    continue
                if regeneration_state == "EXHAUSTED":
                    return self.get(workflow_id)
            if effective_status == "REVISE" and self._can_auto_repair(prompt_id, state):
                repaired = await self._auto_repair(wf, prompt_id, envelope, effective_output, state)
                if repaired:
                    state.setdefault("pending_repair_rereviews", {})[prompt_id] = {
                        **self._repair_rereview_checkpoint(repaired),
                        "critic_prompt": prompt_id,
                    }
                    self._update(wf, state=state)
                    continue
                if isinstance(state.get("last_targeted_repair_failure"), dict):
                    repair_failure = state["last_targeted_repair_failure"]
                    state["last_error"] = self._targeted_repair_failure_message(
                        state,
                        prompt_id=prompt_id,
                        fallback=f"{prompt_id} targeted repair failed",
                    )
                    if (
                        str(repair_failure.get("category") or "")
                        == FailureCategory.CONFIGURATION.value
                    ):
                        report = self._runtime_configuration_report(
                            str(repair_failure.get("error") or state["last_error"]),
                            scope=f"TARGETED_REPAIR_RUNTIME:{prompt_id}",
                        )
                        if report is not None:
                            return self._pause_for_configuration(
                                wf,
                                state,
                                report,
                                source=f"TARGETED_REPAIR_RUNTIME:{prompt_id}",
                            )
                    self._clear_workflow_repair_rereview(state, prompt_id)
                    self._update(
                        wf,
                        status=self._targeted_repair_block_status(state),
                        state=state,
                    )
                    return self.get(workflow_id)
            if effective_status == "REVISE":
                producer_regeneration_state = (
                    self._prepare_semantic_producer_regeneration(
                        wf,
                        state,
                        producer_prompt=prompt_id,
                        output=effective_output,
                    )
                )
                if producer_regeneration_state == "SCHEDULED":
                    wf = self.get(workflow_id)
                    state = wf["state"]
                    continue
                if producer_regeneration_state == "EXHAUSTED":
                    return self.get(workflow_id)

            if effective_status == "REVISE" and isinstance(pending_rereview, dict):
                self._clear_workflow_repair_rereview(state, prompt_id)
                state["last_error"] = (
                    f"{prompt_id} targeted repair independent re-review returned "
                    "REVISE; a second automatic repair or human confirmation cannot "
                    "replace independent verification."
                )
                self._update(
                    wf,
                    status=WorkflowStatus.BLOCKED_CONTENT.value,
                    state=state,
                )
                return self.get(workflow_id)
            if effective_status == "REVISE" and self._has_nonconfirmable_quality_failure(effective_output):
                self._clear_workflow_repair_rereview(state, prompt_id)
                codes = [str(item.get("code")) for item in effective_output.get("findings", []) if str(item.get("code", "")).startswith("QG_")]
                state["last_error"] = (
                    f"{prompt_id} 未通过确定性质量校验：" + "、".join(codes[:8])
                    + "。该问题必须由对应生产/审查阶段重新生成或补充证据，不能通过人工空确认覆盖。"
                )
                self._update(
                    wf,
                    status=WorkflowStatus.BLOCKED_CONTENT.value,
                    state=state,
                )
                return self.get(workflow_id)
            if effective_status == "NEED_USER_INPUT":
                self._clear_workflow_repair_rereview(state, prompt_id)
                blocking_gate_questions = [
                    item
                    for item in effective_output.get("user_questions") or []
                    if isinstance(item, dict) and bool(item.get("blocking"))
                ]
                if not blocking_gate_questions:
                    state["last_error"] = (
                        f"{prompt_id} returned NEED_USER_INPUT without a concrete blocking "
                        "user question; an empty human Gate is forbidden by the semantic contract."
                    )
                    self._update(
                        wf,
                        status=WorkflowStatus.BLOCKED_CONTRACT.value,
                        state=state,
                    )
                    return self.get(workflow_id)
                gate_type = (
                    self.pack.entry(prompt_id).get("next_human_gate")
                    or "PROJECT_GAP_RESOLUTION"
                )
                self._create_gate(
                    wf,
                    gate_type,
                    target_id=result["run_id"],
                    questions=blocking_gate_questions,
                    checkpoint_status=WorkflowStatus.WAITING_GATE.value,
                    checkpoint_state=state,
                )
                return self.get(workflow_id)

            if effective_status == "REVISE":
                self._clear_workflow_repair_rereview(state, prompt_id)
                state["last_error"] = (
                    f"{prompt_id} remains REVISE after the available machine "
                    "repair/regeneration routes were attempted or found inapplicable. "
                    "Non-blocking/advisory questions do not authorize a human Gate; "
                    "the deficiency remains content-blocked until retrieval, evidence, "
                    "or a subsequent machine regeneration can resolve it."
                )
                self._update(
                    wf,
                    status=WorkflowStatus.BLOCKED_CONTENT.value,
                    state=state,
                )
                return self.get(workflow_id)

            next_gate = self.pack.entry(prompt_id).get("next_human_gate")
            self._clear_workflow_repair_rereview(state, prompt_id)
            producer_revision_findings = state.get("producer_revision_findings")
            if isinstance(producer_revision_findings, dict) and prompt_id in producer_revision_findings:
                producer_revision_findings.pop(prompt_id, None)
                if not producer_revision_findings:
                    state.pop("producer_revision_findings", None)
            semantic_baseline_runs = state.get(
                "semantic_producer_regeneration_baseline_runs"
            )
            if isinstance(semantic_baseline_runs, dict):
                semantic_baseline_runs.pop(prompt_id, None)
                if not semantic_baseline_runs:
                    state.pop("semantic_producer_regeneration_baseline_runs", None)
            wf["current_step"] += 1
            self._update(wf, current_step=wf["current_step"], state=state)
            if next_gate:
                self._create_gate(wf, next_gate, target_id=result["run_id"], questions=output.get("user_questions", []))
                self._update(wf, status="WAITING_GATE", state=state)
                return self.get(workflow_id)

        if state.get("pending_repair_rereviews"):
            state["last_error"] = (
                "Workflow reached completion with an unresolved targeted-repair "
                "re-review checkpoint."
            )
            self._update(
                wf,
                status=WorkflowStatus.BLOCKED_TECHNICAL.value,
                state=state,
            )
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
            state["last_error"] = redact_secret_text(str(exc)) + "。必须记录修复运行并由独立Critic复审，人工确认或直接改库均不能放行。"
            state["quality_blocker_ids"] = [item.get("finding_id") for item in exc.findings]
            self._update(
                wf,
                status=WorkflowStatus.BLOCKED_CONTENT.value,
                state=state,
            )
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
