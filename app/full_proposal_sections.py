from __future__ import annotations

from typing import Any

from .executor import PromptExecutionError
from .workflow_input import WorkflowInputRequired
from .workflow_status import WorkflowStatus


class FullProposalSectionsMixin:
    async def _write_sections(self, wf: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
        if self._full_proposal_mode(state):
            return await self._write_full_proposal_concurrently(wf, state)
        return await self._write_sections_serial(wf, state)

    async def _write_sections_serial(self, wf: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
        """Run an isolated, recoverable producer/critic/repair chain for each section.

        The chain is:
        Blueprint -> Blueprint Critic -> bounded Targeted Repair -> re-review
        -> Content -> Content Critic -> bounded Targeted Repair -> re-review
        -> Expression Polish -> Expression Critic.

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
                    return self._block_section_chain(wf, state, section, f"未知章节阶段：{phase}")
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
        suppress_gate = bool((state.get("options") or {}).get("suppress_candidate_gate"))
        self._update(wf, current_step=wf["current_step"], state=state)
        if skip_gate or suppress_gate:
            return None
        refreshed = self.get(wf["id"])
        self._create_gate(refreshed, "CANDIDATE_REVIEW", target_id=wf["id"], questions=[])
        self._update(refreshed, status="WAITING_GATE", state=state)
        return self.get(wf["id"])
