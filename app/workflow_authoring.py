from __future__ import annotations

from typing import Any

from .executor import PromptExecutionError


class WorkflowAuthoringMixin:
    async def _write_sections(self, wf: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
        """Generate and review every requested proposal section before one human review gate.

        The V2 prompts are section-oriented.  Running them once for a full-document task
        silently produced only one candidate.  This orchestrator keeps the prompt contracts
        unchanged while iterating over the CURRENT_PROPOSAL section tree.
        """
        sections = self._target_sections(wf["project_id"], state.get("options") or {})
        completed = {item.get("section_id") for item in state.get("section_results", [])}
        state.setdefault("section_results", [])
        for section in sections:
            section_id = section["section_id"]
            if section_id in completed:
                continue
            state["active_section_id"] = section_id
            state["active_section_title"] = section.get("title")
            section_record = {"section_id": section_id, "title": section.get("title"), "runs": []}
            for prompt_id in ["P-WRITE-BLUEPRINT", "P-WRITE-BLUEPRINT-CRITIC", "P-WRITE-CONTENT", "P-WRITE-CRITIC"]:
                try:
                    envelope = self.context_builder.build(
                        prompt_id,
                        wf["project_id"],
                        workflow_id=wf["id"],
                        workflow_state=state,
                        overrides={"payload.source_section": section},
                    )
                    result = await self.executor.execute(
                        prompt_id,
                        envelope,
                        project_id=wf["project_id"],
                        workflow_id=wf["id"],
                        original_environment=state.get("original_environment"),
                    )
                except (PromptExecutionError, ValueError, KeyError) as exc:
                    state["last_error"] = f"{section.get('title')}: {exc}"
                    self._update(wf, status="BLOCKED", state=state)
                    return self.get(wf["id"])
                section_record["runs"].append({"prompt_id": prompt_id, "run_id": result["run_id"], "status": result["status"]})
                state["original_environment"] = result["route"]["environment"]
                if result["status"] != "PASS":
                    state["last_error"] = f"{section.get('title')} / {prompt_id} returned {result['status']}"
                    self._update(wf, status="BLOCKED", state=state)
                    return self.get(wf["id"])
            state["section_results"].append(section_record)
            self._update(wf, state=state)

        state.pop("active_section_id", None)
        state.pop("active_section_title", None)
        wf["current_step"] += 1
        self._update(wf, current_step=wf["current_step"], state=state)
        refreshed = self.get(wf["id"])
        self._create_gate(refreshed, "CANDIDATE_REVIEW", target_id=wf["id"], questions=[])
        self._update(refreshed, status="WAITING_GATE", state=state)
        return self.get(wf["id"])

    def _target_sections(self, project_id: str, options: dict[str, Any]) -> list[dict[str, Any]]:
        sections = [
            section
            for section in self.context_builder.sections(project_id, "CURRENT_PROPOSAL")
            if section.get("level", 0) >= 1 and section.get("title", "").strip() not in {"", "全文"}
        ]
        requested_ids = set(options.get("target_section_ids") or [])
        requested_titles = set(options.get("target_section_titles") or [])
        if requested_ids or requested_titles:
            sections = [
                section
                for section in sections
                if section.get("section_id") in requested_ids or section.get("title") in requested_titles
            ]
        if sections:
            return sections
        # Backward-compatible smoke-test fallback for projects without an uploaded draft.
        return [self.pack.replay_input("P-WRITE-CONTENT")["payload"]["source_section"]]

