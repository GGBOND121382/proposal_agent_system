from __future__ import annotations

from typing import Any

from .executor import PromptExecutionError
from .llm import MODEL_RESPONSE_PROTOCOL_VERSION
from .runtime_failures import FailureCategory, classify_runtime_failure
from .util import sha256_json, sha256_text
from .workflow_input import CURRENT_PROPOSAL_INPUT, WorkflowInputRequired, material_input_questions
from .workflow_status import WorkflowStatus

THREE_SECTION_PROFILE_ORDER = (
    "BACKGROUND_AND_SIGNIFICANCE",
    "RESEARCH_CONTENT",
    "TECHNICAL_ROUTE",
)


class WorkflowAuthoringMixin:
    SECTION_PHASES = {
        "BLUEPRINT": ("P-WRITE-BLUEPRINT", "BLUEPRINT_CRITIC"),
        "BLUEPRINT_CRITIC": ("P-WRITE-BLUEPRINT-CRITIC", "CONTENT"),
        "CONTENT": ("P-WRITE-CONTENT", "CONTENT_CRITIC"),
        "CONTENT_CRITIC": ("P-WRITE-CRITIC", "POLISH"),
        "POLISH": ("P-EXPRESSION-POLISH", "EXPRESSION_CRITIC"),
        "EXPRESSION_CRITIC": ("P-EXPRESSION-CRITIC", "DONE"),
    }
    SECTION_REPAIR_CRITICS = {
        "P-WRITE-BLUEPRINT-CRITIC",
        "P-WRITE-CRITIC",
        "P-EXPRESSION-CRITIC",
    }
    SECTION_CRITIC_PRODUCERS = {
        "P-WRITE-BLUEPRINT-CRITIC": ("P-WRITE-BLUEPRINT", "BLUEPRINT"),
        "P-WRITE-CRITIC": ("P-WRITE-CONTENT", "CONTENT"),
        "P-EXPRESSION-CRITIC": ("P-EXPRESSION-POLISH", "POLISH"),
    }
    SECTION_PRODUCER_PHASES = {
        "P-WRITE-BLUEPRINT": "BLUEPRINT",
        "P-WRITE-CONTENT": "CONTENT",
        "P-EXPRESSION-POLISH": "POLISH",
    }

    @staticmethod
    def _append_section_run(progress: dict[str, Any], result: dict[str, Any], *, prompt_id: str | None = None, role: str | None = None) -> None:
        run_id = str(result.get("run_id") or "")
        if run_id and any(str(item.get("run_id") or "") == run_id for item in progress["runs"]):
            return
        record = {
            "prompt_id": prompt_id or result.get("prompt_id"),
            "run_id": run_id,
            "status": result.get("status"),
        }
        if role:
            record["role"] = role
        progress["runs"].append(record)

    def _apply_section_decision(
        self,
        wf: dict[str, Any],
        state: dict[str, Any],
        prompt_id: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Derive section control state through the workflow decision authority.

        Prompt runs and their model outputs are immutable evidence.  The section
        chain must nevertheless branch on the same critic/guard arbitration as
        ordinary workflow steps; otherwise a model-level ``REVISE`` containing
        no owned blocking finding can consume repair budget and block the
        workflow.  Keep the raw result untouched and return a derived control
        view carrying the effective status and actionable findings.

        The callable check preserves the standalone mixin contract used by
        lightweight authoring harnesses.  The production WorkflowEngine always
        supplies ``_record_decision``.
        """
        recorder = getattr(self, "_record_decision", None)
        derived = dict(result)
        if callable(recorder):
            decision, effective_status, effective_output = recorder(
                wf,
                state,
                prompt_id,
                result,
            )
            derived["model_status"] = str(
                result.get("status")
                or (result.get("output") or {}).get("status")
                or "ERROR"
            )
            derived["status"] = effective_status
            derived["output"] = effective_output
            if decision is not None:
                derived["decision"] = decision
        self._sync_section_revision_feedback(state, prompt_id, derived)
        return derived

    def _sync_section_revision_feedback(
        self,
        state: dict[str, Any],
        prompt_id: str,
        result: dict[str, Any],
    ) -> None:
        """Keep producer feedback aligned with the latest effective decision.

        Revision feedback is control state, not an append-only audit stream.  A
        new REVISE/BLOCK decision must replace the previous candidate's feedback
        even when the regeneration budget is already exhausted.  Conversely, a
        producer PASS proves that the feedback supplied to that producer has
        been consumed and must not leak into a later authoring phase.  Raw runs
        and decision artifacts remain the immutable history.
        """
        section_id = str(state.get("active_section_id") or "")
        if not section_id:
            return
        status = str(
            result.get("status")
            or (result.get("output") or {}).get("status")
            or ""
        ).upper()
        feedback_by_section = state.setdefault("section_revision_findings", {})
        if status in {"REVISE", "BLOCK"}:
            output = result.get("output") or {}
            feedback_by_section[section_id] = [
                dict(item)
                for item in output.get("findings") or []
                if isinstance(item, dict)
            ]
        elif status == "PASS" and prompt_id in self.SECTION_PRODUCER_PHASES:
            feedback_by_section.pop(section_id, None)

    def _block_section_chain(
        self,
        wf: dict[str, Any],
        state: dict[str, Any],
        section: dict[str, Any],
        message: str,
        *,
        configuration_error: Exception | str | None = None,
    ) -> dict[str, Any]:
        repair_failure = state.get("last_targeted_repair_failure")
        if isinstance(repair_failure, dict):
            message = self._targeted_repair_failure_message(
                state,
                prompt_id=str(
                    repair_failure.get("critic_prompt")
                    or "P-TARGETED-REPAIR"
                ),
                fallback=message,
            )
            if (
                configuration_error is None
                and str(repair_failure.get("category") or "")
                == FailureCategory.CONFIGURATION.value
            ):
                configuration_error = str(
                    repair_failure.get("error") or message
                )
        section_id = str(section.get("section_id") or "")
        progress = state.setdefault("section_progress", {}).setdefault(section_id, {})
        report = (
            self._runtime_configuration_report(
                configuration_error,
                scope=f"SECTION_CHAIN_RUNTIME:{section_id or 'UNKNOWN'}",
            )
            if configuration_error is not None
            else None
        )
        if report is not None:
            progress["status"] = "WAITING_CONFIGURATION"
            progress["last_error"] = message
            return self._pause_for_configuration(
                wf,
                state,
                report,
                source=f"SECTION_CHAIN_RUNTIME:{section_id or 'UNKNOWN'}",
            )
        if isinstance(repair_failure, dict):
            blocked_status = self._targeted_repair_block_status(state)
        elif configuration_error is not None:
            blocked_status = WorkflowStatus.BLOCKED_TECHNICAL.value
        else:
            blocked_status = WorkflowStatus.BLOCKED_CONTENT.value
        progress["status"] = blocked_status
        progress["last_error"] = message
        state["last_error"] = f"{section.get('title')}: {message}"
        self._update(wf, status=blocked_status, state=state)
        return self.get(wf["id"])

    @staticmethod
    def _targeted_repair_block_status(state: dict[str, Any]) -> str:
        """Preserve the actual targeted-repair failure category.

        A failed repair used to be flattened into the historical generic
        ``BLOCKED`` state.  That hid whether the repair provider, output
        contract, runtime, or semantic content was responsible and prevented
        the contract-migration path from selecting the saved repair output.
        """

        failure = state.get("last_targeted_repair_failure")
        if not isinstance(failure, dict):
            return WorkflowStatus.BLOCKED_TECHNICAL.value
        category = str(failure.get("category") or "")
        mapping = {
            FailureCategory.OUTPUT_CONTRACT.value: WorkflowStatus.BLOCKED_CONTRACT.value,
            FailureCategory.PROVIDER_TRANSIENT.value: WorkflowStatus.BLOCKED_PROVIDER.value,
            FailureCategory.TECHNICAL.value: WorkflowStatus.BLOCKED_TECHNICAL.value,
            FailureCategory.SEMANTIC_REVISE.value: WorkflowStatus.BLOCKED_CONTENT.value,
            # If dependency preflight can identify the missing configuration,
            # _block_section_chain converts it to WAITING_CONFIGURATION before
            # this fallback.  Without such a report, keep the workflow paused
            # instead of auto-resuming an unqualified waiting state.
            FailureCategory.CONFIGURATION.value: WorkflowStatus.BLOCKED_TECHNICAL.value,
            "SEMANTIC_REPAIR_REJECTED": WorkflowStatus.BLOCKED_CONTENT.value,
        }
        return mapping.get(category, WorkflowStatus.BLOCKED_TECHNICAL.value)

    _section_block_status = _targeted_repair_block_status

    def _block_section_prompt_failure(
        self,
        wf: dict[str, Any],
        state: dict[str, Any],
        section: dict[str, Any],
        exc: PromptExecutionError,
    ) -> dict[str, Any]:
        """Block or pause a section using the shared runtime failure taxonomy.

        Legacy section checkpoints can surface schema failures as a plain
        ``PromptExecutionError(validation_errors=...)`` instead of a typed
        provider exception.  Do not flatten those contract failures into the
        historical technical-error fallback.
        """
        classification = classify_runtime_failure(exc)
        if classification.category is FailureCategory.CONFIGURATION:
            return self._block_section_chain(
                wf,
                state,
                section,
                str(exc),
                configuration_error=exc,
            )

        section_id = str(section.get("section_id") or "")
        progress = state.setdefault("section_progress", {}).setdefault(
            section_id, {}
        )
        status = classification.workflow_status
        progress["status"] = status
        progress["last_error"] = str(exc)
        state["last_error"] = f"{section.get('title')}: {exc}"
        self._update(wf, status=status, state=state)
        return self.get(wf["id"])

    async def _execute_section_prompt(
        self,
        wf: dict[str, Any],
        state: dict[str, Any],
        section: dict[str, Any],
        progress: dict[str, Any],
        prompt_id: str,
        *,
        role: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        envelope = self.context_builder.build(
            prompt_id,
            wf["project_id"],
            workflow_id=wf["id"],
            workflow_state=state,
        )
        candidate_round_key = (
            f"section:{state.get('active_section_id') or ''}:{prompt_id}"
        )
        candidate_round = int(
            (state.get("acceptance_candidate_rounds") or {}).get(
                candidate_round_key,
                0,
            )
        )
        requested_call_key = None
        if candidate_round:
            requested_call_key = "call-acceptance-" + sha256_json(
                {
                    "workflow_id": wf["id"],
                    "section_id": state.get("active_section_id"),
                    "prompt_id": prompt_id,
                    "candidate_round": candidate_round,
                    # A round number is only loop position, not semantic call
                    # identity.  Recovery may intentionally reset/rebase a
                    # budget after a prompt or contract upgrade.  Binding the
                    # key to the complete envelope preserves restart
                    # idempotency for identical input while preventing reuse of
                    # an older candidate under changed prompts or feedback.
                    "input_hash": sha256_json(envelope),
                    "model_response_protocol_version": MODEL_RESPONSE_PROTOCOL_VERSION,
                }
            )[:24]
        provider_retry = getattr(self, "_execute_prompt_with_provider_retry", None)
        if callable(provider_retry):
            result = await provider_retry(
                wf,
                state,
                prompt_id=prompt_id,
                envelope=envelope,
                call_key=requested_call_key,
            )
        else:
            result = await self.executor.execute(
                prompt_id,
                envelope,
                project_id=wf["project_id"],
                workflow_id=wf["id"],
                original_environment=state.get("original_environment"),
                call_key=requested_call_key,
            )
        if prompt_id == "P-WRITE-CONTENT" and self.diagram_enrichment is not None and result["status"] == "PASS":
            result["output"] = await self.diagram_enrichment.enrich(
                project_id=wf["project_id"],
                workflow_id=wf["id"],
                run_id=result["run_id"],
                section=section,
                output=result["output"],
                security_level=(
                    result["output"].get("source_refs", [{}])[0].get("security_level", "INTERNAL")
                    if result["output"].get("source_refs") else "INTERNAL"
                ),
            )
        result = self._apply_section_decision(
            wf,
            state,
            prompt_id,
            result,
        )
        self._append_section_run(progress, result, prompt_id=prompt_id, role=role)
        state["original_environment"] = result["route"]["environment"]
        # A freshly generated producer object is a new repair subject. Repair
        # budgets and overrides belong to the previous candidate.
        if result["status"] == "PASS":
            critic_prompt = next(
                (
                    critic
                    for critic, (producer, _phase) in self.SECTION_CRITIC_PRODUCERS.items()
                    if producer == prompt_id
                ),
                None,
            )
            if critic_prompt:
                self._supersede_repair_subject(
                    state,
                    critic_prompt=critic_prompt,
                    producer_prompt=prompt_id,
                    reason="FRESH_PRODUCER_PASS",
                )
        self._observe_quality_result(wf, state, prompt_id, result)
        self._update(wf, state=state)
        return envelope, result

    def _schedule_acceptance_regeneration(
        self,
        state: dict[str, Any],
        progress: dict[str, Any],
        critic_prompt: str,
        critic_output: dict[str, Any],
    ) -> bool:
        """Schedule a bounded new producer candidate for an acceptance run."""
        options = state.get("options") or {}
        if not bool(options.get("acceptance_run")):
            return False
        producer_phase = self.SECTION_CRITIC_PRODUCERS.get(critic_prompt)
        if not producer_phase:
            return False
        findings = [
            item
            for item in critic_output.get("findings", [])
            if isinstance(item, dict)
        ]
        if not findings or any(not item.get("repairable", False) for item in findings):
            return False

        section_id = str(state.get("active_section_id") or "")
        round_key = f"section:{section_id}:{critic_prompt}"
        rounds = state.setdefault("acceptance_regeneration_rounds", {})
        limit = max(0, min(int(options.get("acceptance_regeneration_limit", 2)), 3))
        if int(rounds.get(round_key, 0)) >= limit:
            return False
        rounds[round_key] = int(rounds.get(round_key, 0)) + 1

        producer_prompt, producer_phase_name = producer_phase
        producer_round_key = f"section:{section_id}:{producer_prompt}"
        candidate_rounds = state.setdefault("acceptance_candidate_rounds", {})
        candidate_rounds[producer_round_key] = (
            int(candidate_rounds.get(producer_round_key, 0)) + 1
        )
        self._deactivate_repair_application(state, producer_prompt)
        progress["phase"] = producer_phase_name
        progress["status"] = "RUNNING"
        progress.pop("last_error", None)
        state.pop("last_error", None)
        return True

    @staticmethod
    def _acceptance_regenerable_review_status(
        state: dict[str, Any],
        status: str,
    ) -> bool:
        if status == "REVISE":
            return True
        options = state.get("options") or {}
        return bool(
            status == "BLOCK"
            and options.get("acceptance_run")
            and options.get("allow_repairable_block_regeneration")
        )

    def _schedule_acceptance_producer_regeneration(
        self,
        state: dict[str, Any],
        progress: dict[str, Any],
        producer_prompt: str,
        producer_output: dict[str, Any],
    ) -> bool:
        """Regenerate a producer object rejected by deterministic quality checks."""
        options = state.get("options") or {}
        producer_phase = self.SECTION_PRODUCER_PHASES.get(producer_prompt)
        if not producer_phase:
            return False
        findings = [
            item
            for item in producer_output.get("findings", [])
            if isinstance(item, dict)
        ]
        if not findings or any(not item.get("repairable", False) for item in findings):
            return False

        section_id = str(state.get("active_section_id") or "")
        round_key = f"section:{section_id}:{producer_prompt}"
        rounds = state.setdefault("acceptance_regeneration_rounds", {})
        default_limit = 2
        limit = max(
            0,
            min(
                int(options.get("acceptance_regeneration_limit", default_limit)),
                3,
            ),
        )
        if int(rounds.get(round_key, 0)) >= limit:
            return False
        rounds[round_key] = int(rounds.get(round_key, 0)) + 1
        candidate_rounds = state.setdefault("acceptance_candidate_rounds", {})
        candidate_rounds[round_key] = int(candidate_rounds.get(round_key, 0)) + 1
        self._deactivate_repair_application(state, producer_prompt)
        progress["phase"] = producer_phase
        progress["status"] = "RUNNING"
        progress.pop("last_error", None)
        state.pop("last_error", None)
        return True

    async def _write_sections(self, wf: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
        """Run an isolated, recoverable producer/critic/repair chain for each section.

        The chain is:
        Blueprint -> Blueprint Critic -> bounded Targeted Repair -> re-review
        -> Content -> Content Critic -> bounded Targeted Repair -> re-review
        -> Expression Polish -> Expression Critic -> bounded Targeted Repair
        -> re-review.

        Progress is persisted after every model run.  A restart re-enters the same
        phase; the Track-A deterministic call key then reuses an already committed
        response instead of duplicating model calls or artifacts.
        """
        options = state.get("options") or {}
        state["current_workflow_id"] = wf["id"]
        sections = self._target_sections(wf["project_id"], options, state)
        if bool(options.get("single_section_complete_chain")) and len(sections) != 1:
            state["last_error"] = (
                "单章节完整链要求精确选择一个章节；当前匹配 " + str(len(sections)) + " 个。"
            )
            self._update(wf, status=WorkflowStatus.BLOCKED_CONTENT.value, state=state)
            return self.get(wf["id"])

        completed = {str(item.get("section_id") or "") for item in state.get("section_results", [])}
        state.setdefault("section_results", [])
        progress_map = state.setdefault("section_progress", {})

        for section in sections:
            section_id = str(section["section_id"])
            if section_id in completed:
                continue
            state["active_section_id"] = section_id
            state["active_section_title"] = section.get("title")
            state["active_section"] = section
            progress = progress_map.setdefault(
                section_id,
                {
                    "section_id": section_id,
                    "title": section.get("title"),
                    "phase": "BLUEPRINT",
                    "status": "RUNNING",
                    "runs": [],
                },
            )
            progress.setdefault("runs", [])
            progress.setdefault("phase", "BLUEPRINT")
            progress["status"] = "RUNNING"
            self._update(wf, state=state)

            while progress["phase"] != "DONE":
                phase = str(progress["phase"])
                if phase not in self.SECTION_PHASES:
                    error = ValueError(f"未知章节阶段：{phase}")
                    return self._block_section_chain(
                        wf,
                        state,
                        section,
                        str(error),
                        configuration_error=error,
                    )
                prompt_id, next_phase = self.SECTION_PHASES[phase]
                pending_rereview = progress.get("pending_repair_rereview")
                is_pending_rereview = (
                    isinstance(pending_rereview, dict)
                    and str(pending_rereview.get("critic_prompt") or "") == prompt_id
                )
                if isinstance(pending_rereview, dict) and not is_pending_rereview:
                    error = ValueError(
                        "Persisted repair re-review checkpoint does not match "
                        f"the current section phase: expected "
                        f"{pending_rereview.get('critic_prompt')}, current {prompt_id}."
                    )
                    return self._block_section_chain(
                        wf,
                        state,
                        section,
                        str(error),
                        configuration_error=error,
                    )
                try:
                    if is_pending_rereview:
                        self._start_repair_rereview(
                            state,
                            pending_rereview,
                            critic_prompt=prompt_id,
                        )
                        self._update(wf, state=state)
                    envelope, result = await self._execute_section_prompt(
                        wf,
                        state,
                        section,
                        progress,
                        prompt_id,
                        role=(
                            "INDEPENDENT_REVIEW"
                            if is_pending_rereview
                            else ("INITIAL_REVIEW" if prompt_id.endswith("CRITIC") else "PRODUCER")
                        ),
                    )
                    if is_pending_rereview:
                        self._complete_repair_rereview(
                            state,
                            pending_rereview,
                            critic_prompt=prompt_id,
                            review_run_id=str(result.get("run_id") or "") or None,
                            status=str(result.get("status") or ""),
                        )
                        # Keep the checkpoint until the effective result and its
                        # phase transition are committed together.
                        pending_rereview["review_run_id"] = (
                            str(result.get("run_id") or "") or None
                        )
                        pending_rereview["completed_status"] = str(
                            result.get("status") or ""
                        )
                except WorkflowInputRequired:
                    raise
                except PromptExecutionError as exc:
                    return self._block_section_prompt_failure(
                        wf, state, section, exc
                    )
                except (ValueError, KeyError) as exc:
                    return self._block_section_chain(
                        wf, state, section, str(exc), configuration_error=exc
                    )

                if result["status"] == "PASS":
                    progress.pop("pending_repair_rereview", None)
                    progress["phase"] = next_phase
                    self._update(wf, state=state)
                    continue

                if result["status"] == "NEED_USER_INPUT":
                    progress.pop("pending_repair_rereview", None)
                    progress["status"] = "WAITING_GATE"
                    state["section_input_gate"] = {
                        "section_id": section_id,
                        "section_title": section.get("title"),
                        "phase": phase,
                        "next_phase": next_phase,
                        "prompt_id": prompt_id,
                        "run_id": result["run_id"],
                    }
                    self._update(wf, status="RUNNING", state=state)
                    refreshed = self.get(wf["id"])
                    self._create_gate(
                        refreshed,
                        self.pack.entry(prompt_id).get("next_human_gate")
                        or "PROJECT_GAP_RESOLUTION",
                        target_id=result["run_id"],
                        questions=result["output"].get("user_questions", []),
                    )
                    self._update(refreshed, status="WAITING_GATE", state=state)
                    return self.get(wf["id"])

                if (
                    result["status"] == "REVISE"
                    and self._schedule_acceptance_producer_regeneration(
                        state,
                        progress,
                        prompt_id,
                        result["output"],
                    )
                ):
                    progress.pop("pending_repair_rereview", None)
                    self._update(wf, status="RUNNING", state=state)
                    continue

                if (
                    prompt_id in self.SECTION_REPAIR_CRITICS
                    and result["status"] == "BLOCK"
                    and self._acceptance_regenerable_review_status(
                        state,
                        str(result["status"]),
                    )
                    and self._schedule_acceptance_regeneration(
                        state,
                        progress,
                        prompt_id,
                        result["output"],
                    )
                ):
                    progress.pop("pending_repair_rereview", None)
                    self._update(wf, status="RUNNING", state=state)
                    continue

                if result["status"] == "REVISE" and prompt_id in self.SECTION_REPAIR_CRITICS:
                    if not self._can_auto_repair(prompt_id, state):
                        if self._schedule_acceptance_regeneration(
                            state,
                            progress,
                            prompt_id,
                            result["output"],
                        ):
                            progress.pop("pending_repair_rereview", None)
                            self._update(wf, status="RUNNING", state=state)
                            continue
                        progress.pop("pending_repair_rereview", None)
                        message = (
                            f"{prompt_id} 定向修复后的独立复审返回 {result['status']}；"
                            "禁止二次自动修复或人工改正文放行。"
                            if is_pending_rereview
                            else f"{prompt_id} 在一次定向修复后仍需修改；章节修复额度已耗尽。"
                        )
                        return self._block_section_chain(
                            wf, state, section, message,
                        )
                    repaired = await self._auto_repair(wf, prompt_id, envelope, result["output"], state)
                    if not repaired:
                        progress.pop("pending_repair_rereview", None)
                        return self._block_section_chain(
                            wf, state, section, f"{prompt_id} 返回 REVISE，但没有可执行的局部修复或定向修复失败。",
                        )
                    self._append_section_run(
                        progress,
                        repaired,
                        prompt_id="P-TARGETED-REPAIR",
                        role="TARGETED_REPAIR",
                    )
                    progress["pending_repair_rereview"] = {
                        **self._repair_rereview_checkpoint(repaired),
                        "critic_prompt": prompt_id,
                    }
                    self._update(wf, state=state)
                    # Re-enter the same Critic phase.  The persisted checkpoint
                    # makes the next call an independent re-review and survives a
                    # crash before or after REREVIEW_STARTED is recorded.
                    continue

                progress.pop("pending_repair_rereview", None)
                return self._block_section_chain(
                    wf, state, section, f"{prompt_id} 返回 {result['status']}；该阶段不允许跳过或人工覆盖。",
                )

            progress["status"] = "COMPLETED"
            section_record = {
                "section_id": section_id,
                "title": section.get("title"),
                "status": "COMPLETED",
                "runs": list(progress["runs"]),
            }
            state["section_results"].append(section_record)
            state.setdefault("section_revision_findings", {}).pop(section_id, None)
            completed.add(section_id)
            self._update(wf, state=state)

        state.pop("active_section_id", None)
        state.pop("active_section_title", None)
        state.pop("active_section", None)
        state.pop("integration_repair_section_ids", None)
        state.pop("last_error", None)
        wf["current_step"] += 1
        skip_gate = bool(state.pop("skip_candidate_gate_once", False))
        self._update(wf, current_step=wf["current_step"], state=state)
        if skip_gate:
            return None
        refreshed = self.get(wf["id"])
        self._create_gate(refreshed, "CANDIDATE_REVIEW", target_id=wf["id"], questions=[])
        self._update(refreshed, status="WAITING_GATE", state=state)
        return self.get(wf["id"])

    def _three_section_mode(self, state: dict[str, Any]) -> bool:
        options = state.get("options") or {}
        return bool(
            options.get("three_section_cross_chapter")
            or options.get("integration_scope") == "THREE_SECTION_CROSS_CHAPTER"
        )

    def _resolve_three_section_contract(
        self,
        sections: list[dict[str, Any]],
        state: dict[str, Any],
    ) -> list[dict[str, Any]]:
        by_profile: dict[str, list[dict[str, Any]]] = {profile: [] for profile in THREE_SECTION_PROFILE_ORDER}
        for section in sections:
            profile = self.pack.section_profile_for(str(section.get("title") or ""))
            profile_id = str(profile.get("profile_id") or "")
            if profile_id in by_profile:
                by_profile[profile_id].append(section)
        missing = [profile for profile, values in by_profile.items() if not values]
        duplicate = [profile for profile, values in by_profile.items() if len(values) > 1]
        if missing or duplicate:
            details = []
            if missing:
                details.append("缺少章节角色：" + "、".join(missing))
            if duplicate:
                details.append("章节角色重复：" + "、".join(duplicate))
            raise ValueError(
                "三章节跨章链必须且只能包含背景、研究内容、技术路线三个唯一章节；" + "；".join(details)
            )
        resolved = [by_profile[profile][0] for profile in THREE_SECTION_PROFILE_ORDER]
        state["three_section_contract"] = {
            "contract_type": "THREE_SECTION_CROSS_CHAPTER",
            "ordered_profiles": list(THREE_SECTION_PROFILE_ORDER),
            "sections": [
                {
                    "section_id": str(section.get("section_id")),
                    "title": str(section.get("title") or ""),
                    "profile_id": profile,
                    "order": index + 1,
                }
                for index, (profile, section) in enumerate(zip(THREE_SECTION_PROFILE_ORDER, resolved))
            ],
        }
        return resolved

    def _validate_three_section_integration_envelope(
        self,
        state: dict[str, Any],
        envelope: dict[str, Any],
    ) -> None:
        if not self._three_section_mode(state):
            return
        contract = state.get("three_section_contract") or {}
        expected = [
            str(item.get("section_id")) for item in contract.get("sections") or []
            if isinstance(item, dict) and item.get("section_id")
        ]
        candidates = (envelope.get("payload") or {}).get("candidate_sections") or []
        actual = [
            str(item.get("section_id")) for item in candidates
            if isinstance(item, dict) and item.get("section_id")
        ]
        if len(expected) != 3 or len(actual) != 3 or set(actual) != set(expected):
            raise ValueError(
                "三章节跨章审查输入必须与已冻结的背景—研究内容—技术路线合同完全一致；"
                f"expected={expected}, actual={actual}"
            )

    @staticmethod
    def _section_ids_from_integration_output(
        output: dict[str, Any],
        known_section_ids: set[str],
    ) -> set[str]:
        result = output.get("result") or {}
        affected: set[str] = set()
        for key in ("redundancy_report", "document_type_drift", "page_budget_check"):
            report = result.get(key) or {}
            affected.update(str(x) for x in report.get("affected_section_ids", []) if x)
            affected.update(str(x) for x in report.get("overflow_section_ids", []) if x)
        for check in result.get("terminology_checks") or []:
            if isinstance(check, dict) and not check.get("consistent", True):
                affected.update(str(x) for x in check.get("sections", []) if x)
        evidence_strings: list[str] = []
        for check in result.get("numeric_checks") or []:
            if isinstance(check, dict) and not check.get("consistent", True):
                evidence_strings.extend(str(x) for x in check.get("occurrences", []) if x)
        for finding in output.get("findings") or []:
            if not isinstance(finding, dict):
                continue
            evidence_strings.append(str(finding.get("target_path_or_span") or ""))
            evidence_strings.extend(str(x) for x in finding.get("evidence_refs", []) if x)
        for value in evidence_strings:
            for section_id in known_section_ids:
                if section_id and section_id in value:
                    affected.add(section_id)
        return affected & known_section_ids

    def _prepare_integration_repair(self, wf: dict[str, Any], state: dict[str, Any], output: dict[str, Any]) -> str:
        """Route full-document findings to the earliest stage able to fix them.

        Argument defects require a new argument architecture; ownership and
        dependency defects require a new narrative plan; prose repetition can
        be repaired by rewriting only affected sections.  A later stage is
        never allowed to cosmetically mask an upstream structural defect.
        """
        findings = [
            item for item in output.get("findings", [])
            if isinstance(item, dict) and item.get("blocking", True)
        ]
        argument_routes = {"ARGUMENT_ARCHITECTURE_AGENT", "PROJECT_KNOWLEDGE_AGENT"}
        planning_codes = {
            "QG_DOCUMENT_DUPLICATE_INFORMATION_KEYS",
            "QG_DOCUMENT_CLAIM_OVERCONCENTRATION",
            "PAGE_BUDGET_EXCEEDED",
        }
        repairable_codes = {
            "QG_DOCUMENT_TEMPLATE_REPETITION", "DOCUMENT_TEMPLATE_REPETITION",
            "QG_DOCUMENT_DOMINATED_BY_AGENT_SYSTEM", "DOCUMENT_TYPE_DRIFT",
        }

        def effective_route(item: dict[str, Any]) -> str:
            explicit = str(item.get("suggested_route") or "").strip()
            if explicit:
                return explicit
            code = str(item.get("code") or "")
            if code in planning_codes:
                return "PLANNING_AGENT"
            if str(item.get("category") or "") == "ARGUMENT":
                return "ARGUMENT_ARCHITECTURE_AGENT"
            if code in repairable_codes:
                return "WRITING_AGENT"
            return ""

        routed_findings = [(item, effective_route(item)) for item in findings]
        argument_findings = [
            item for item, route in routed_findings if route in argument_routes
        ]
        planning_findings = [
            item for item, route in routed_findings if route == "PLANNING_AGENT"
        ]

        if argument_findings:
            rounds = int(state.get("integration_argument_rounds", 0))
            if rounds >= 1:
                state["last_error"] = "全篇审查在一次论证架构重构后仍发现上游论证缺陷，需要补充事实或由项目负责人调整中心命题。"
                self._update(wf, status=WorkflowStatus.BLOCKED_CONTENT.value, state=state)
                return "EXHAUSTED"
            state["integration_argument_rounds"] = rounds + 1
            state["argument_revision_findings"] = argument_findings
            state["planning_revision_findings"] = []
            state["section_results"] = []
            state.pop("integration_repair_section_ids", None)
            state.pop("integration_repair_findings", None)
            target_step = next(
                index for index, step in enumerate(self.get(wf["id"])["steps"])
                if step.get("prompt_id") == "P-ARGUMENT-ARCHITECTURE"
            )
            wf["current_step"] = target_step
            self._update(wf, status="RUNNING", current_step=target_step, state=state)
            return "SCHEDULED"

        if planning_findings:
            rounds = int(state.get("integration_planning_rounds", 0))
            if rounds >= 1:
                state["last_error"] = "全篇审查在一次章节合同重构后仍发现命题或信息归属冲突，需要人工调整论证架构。"
                self._update(wf, status=WorkflowStatus.BLOCKED_CONTENT.value, state=state)
                return "EXHAUSTED"
            state["integration_planning_rounds"] = rounds + 1
            state["planning_revision_findings"] = planning_findings
            state["section_results"] = []
            state.pop("integration_repair_section_ids", None)
            state.pop("integration_repair_findings", None)
            target_step = next(
                index for index, step in enumerate(self.get(wf["id"])["steps"])
                if step.get("prompt_id") == "P-REVISION-PLAN"
            )
            wf["current_step"] = target_step
            self._update(wf, status="RUNNING", current_step=target_step, state=state)
            return "SCHEDULED"

        contract_sections = (state.get("three_section_contract") or {}).get("sections") or []
        known_section_ids = {
            str(item.get("section_id")) for item in contract_sections
            if isinstance(item, dict) and item.get("section_id")
        }
        if not known_section_ids:
            known_section_ids = {
                str(item.get("section_id")) for item in state.get("section_results", [])
                if isinstance(item, dict) and item.get("section_id")
            }
        affected = self._section_ids_from_integration_output(output, known_section_ids)
        writing_findings = [
            item for item, route in routed_findings if route == "WRITING_AGENT"
        ]
        if writing_findings and not affected and self._three_section_mode(state):
            # A writing defect without a section locator cannot be silently accepted.
            # Regenerate the frozen three-section set rather than allowing manual edits.
            affected = set(known_section_ids)
        if not affected or not writing_findings:
            return "NOT_APPLICABLE"
        rounds = int(state.get("integration_repair_rounds", 0))
        if rounds >= 2:
            state["last_error"] = "全篇质量审查在两轮章节重写后仍未通过；需要修改论证架构或补充事实证据。"
            self._update(wf, status=WorkflowStatus.BLOCKED_CONTENT.value, state=state)
            return "EXHAUSTED"
        state["integration_repair_rounds"] = rounds + 1
        state["integration_repair_section_ids"] = sorted(affected)
        state["integration_repair_findings"] = writing_findings
        state.setdefault("cross_section_repair_history", []).append({
            "round": rounds + 1,
            "finding_codes": [str(item.get("code") or "") for item in writing_findings],
            "responsible_section_ids": sorted(affected),
            "route": "WRITING_AGENT",
        })
        state["section_results"] = [
            item for item in state.get("section_results", []) if str(item.get("section_id")) not in affected
        ]
        # S1 persists per-section phase checkpoints.  A cross-section repair must
        # invalidate only the responsible sections; otherwise a recovered workflow
        # would see phase=DONE and reuse the unchanged body without creating repair
        # evidence for the open integration finding.
        progress_map = state.setdefault("section_progress", {})
        for section_id in affected:
            progress_map.pop(str(section_id), None)
        state["skip_candidate_gate_once"] = True
        write_step = next(
            index for index, step in enumerate(self.get(wf["id"])["steps"])
            if step.get("type") == "WRITE_SECTIONS"
        )
        wf["current_step"] = write_step
        self._update(wf, status="RUNNING", current_step=write_step, state=state)
        return "SCHEDULED"

    def _target_sections(
        self,
        project_id: str,
        options: dict[str, Any],
        state: dict[str, Any] | None = None,
        *,
        workflow_id: str | None = None,
    ) -> list[dict[str, Any]]:
        source_sections = [
            section
            for section in self.context_builder.sections(project_id, "CURRENT_PROPOSAL")
            if section.get("level", 0) >= 1 and section.get("title", "").strip() not in {"", "全文"}
        ]
        by_id = {str(section.get("section_id")): section for section in source_sections if section.get("section_id")}
        by_title = {str(section.get("title")): section for section in source_sections if section.get("title")}

        # The approved narrative architecture, not the uploaded draft's raw
        # heading count, determines what belongs in the proposal.  This prevents
        # a long source outline from becoming dozens of same-type writing tasks.
        plan_workflow_id = str(
            (state or {}).get("parent_workflow_id")
            or workflow_id
            or (state or {}).get("current_workflow_id")
            or ""
        ) or None
        plan = self._context_result(
            project_id,
            "P-REVISION-PLAN",
            "revision_plan",
            workflow_id=plan_workflow_id,
            exact_workflow=bool(plan_workflow_id),
        ) or {}
        architecture = plan.get("narrative_architecture") or {}
        planned: list[dict[str, Any]] = []
        planned_ids: set[str] = set()
        project_row = self.db.fetchone("SELECT security_level FROM projects WHERE id=?", (project_id,)) or {}
        project_security_level = str(project_row.get("security_level") or "INTERNAL")
        for contract in architecture.get("section_contracts", []):
            if not isinstance(contract, dict) or contract.get("placement") == "OMIT":
                continue
            contract_title = str(contract.get("title") or "").strip()
            if contract_title in {"", "全文"}:
                continue
            section = by_id.get(str(contract.get("section_id"))) or by_title.get(contract_title)
            if section is None and contract.get("section_id") and contract.get("title"):
                # A confirmed revision plan may define a new target section even
                # when no CURRENT_PROPOSAL draft exists.  Represent that target
                # explicitly instead of falling back to a Replay sample.
                empty_text = ""
                section = {
                    "section_id": str(contract["section_id"]),
                    "section_key": str(contract.get("profile_id") or contract["section_id"]),
                    "title": str(contract["title"]),
                    "level": 1,
                    "text": empty_text,
                    "text_hash": sha256_text(empty_text),
                    "block_ids": [],
                    "contains_table": False,
                    "contains_formula": False,
                    "contains_image": False,
                    "contains_comment": False,
                    "contains_revision": False,
                    "security_level": project_security_level,
                }
            section_id = str((section or {}).get("section_id") or "")
            if section and section_id not in planned_ids:
                planned.append(section)
                planned_ids.add(section_id)
        sections = planned or source_sections
        effective_state = state if state is not None else {"options": options}
        if self._three_section_mode(effective_state):
            sections = self._resolve_three_section_contract(sections, effective_state)

        requested_ids = set(options.get("target_section_ids") or [])
        requested_titles = set(options.get("target_section_titles") or [])
        repair_ids = set((state or {}).get("integration_repair_section_ids") or [])
        if repair_ids:
            requested_ids = repair_ids
        if requested_ids or requested_titles:
            sections = [
                section
                for section in sections
                if section.get("section_id") in requested_ids or section.get("title") in requested_titles
            ]
        if sections:
            return sections
        raise WorkflowInputRequired(
            "P-WRITE-BLUEPRINT",
            gate_type=CURRENT_PROPOSAL_INPUT,
            missing_paths=["payload.source_section", "payload.revision_plan.narrative_architecture.section_contracts"],
            questions=material_input_questions(CURRENT_PROPOSAL_INPUT),
            message=(
                "没有 CURRENT_PROPOSAL 章节，也没有已确认修订计划中的明确目标章节。"
                "系统不会使用 Replay 章节代替真实写作对象。"
            ),
        )

