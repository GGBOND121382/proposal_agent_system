from __future__ import annotations

from typing import Any

from .executor import PromptExecutionError


class FullProposalSectionsMixin:
    async def _write_sections(self, wf: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
        if self._full_proposal_mode(state):
            return await self._write_full_proposal_concurrently(wf, state)
        return await self._write_sections_serial(wf, state)

    async def _write_sections_serial(self, wf: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
        """Run an isolated, recoverable producer/critic/repair chain for each section.

        The chain is:
        Blueprint -> Blueprint Critic -> at most one Targeted Repair -> re-review
        -> Content -> Content Critic -> at most one Targeted Repair -> re-review
        -> Expression Polish -> Expression Critic.

        Progress is persisted after every model run.  A restart re-enters the same
        phase; the Track-A deterministic call key then reuses an already committed
        response instead of duplicating model calls or artifacts.
        """
        options = state.get("options") or {}
        sections = self._target_sections(wf["project_id"], options, state)
        if bool(options.get("single_section_complete_chain")) and len(sections) != 1:
            state["last_error"] = (
                "单章节完整链要求精确选择一个章节；当前匹配 " + str(len(sections)) + " 个。"
            )
            self._update(wf, status="BLOCKED", state=state)
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
                try:
                    envelope, result = await self._execute_section_prompt(
                        wf, state, section, progress, prompt_id, role="INITIAL_REVIEW" if prompt_id.endswith("CRITIC") else "PRODUCER",
                    )
                except (PromptExecutionError, ValueError, KeyError) as exc:
                    return self._block_section_chain(wf, state, section, str(exc))

                if result["status"] == "PASS":
                    progress["phase"] = next_phase
                    self._update(wf, state=state)
                    continue

                if result["status"] == "REVISE" and prompt_id in self.SECTION_REPAIR_CRITICS:
                    if not self._can_auto_repair(prompt_id, state):
                        return self._block_section_chain(
                            wf, state, section, f"{prompt_id} 在一次定向修复后仍需修改；章节修复额度已耗尽。",
                        )
                    repaired = await self._auto_repair(wf, prompt_id, envelope, result["output"], state)
                    if not repaired:
                        return self._block_section_chain(
                            wf, state, section, f"{prompt_id} 返回 REVISE，但没有可执行的局部修复或定向修复失败。",
                        )
                    self._append_section_run(progress, repaired, prompt_id="P-TARGETED-REPAIR", role="TARGETED_REPAIR")
                    self._update(wf, state=state)
                    try:
                        _review_envelope, reviewed = await self._execute_section_prompt(
                            wf, state, section, progress, prompt_id, role="INDEPENDENT_REVIEW",
                        )
                    except (PromptExecutionError, ValueError, KeyError) as exc:
                        return self._block_section_chain(wf, state, section, f"定向修复后的独立复审失败：{exc}")
                    if reviewed["status"] != "PASS":
                        return self._block_section_chain(
                            wf, state, section,
                            f"{prompt_id} 定向修复后的独立复审返回 {reviewed['status']}；禁止二次自动修复或人工改正文放行。",
                        )
                    progress["phase"] = next_phase
                    self._update(wf, state=state)
                    continue

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
            completed.add(section_id)
            self._update(wf, state=state)

        state.pop("active_section_id", None)
        state.pop("active_section_title", None)
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
