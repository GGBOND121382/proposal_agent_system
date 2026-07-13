from __future__ import annotations

from typing import Any

from .executor import PromptExecutionError
from .workflow_defs import CRITIC_PRODUCER


class WorkflowRepairMixin:
    async def _run_public_search(self, wf: dict[str, Any], state: dict[str, Any]) -> None:
        if self.executor.gateway.settings.runtime_mode in {"REPLAY", "MOCK"}:
            state["public_search_results"] = {"sources": [], "passages": [], "queries": [], "mode": self.executor.gateway.settings.runtime_mode}
            return
        plan = self.context_builder._result(wf["project_id"], "P-PUBLIC-RESEARCH-PLAN") or {}
        state["public_search_results"] = await self.research_service.search(plan)

    def _can_auto_repair(self, prompt_id: str, state: dict[str, Any]) -> bool:
        if prompt_id not in CRITIC_PRODUCER:
            return False
        return int(state["repair_attempts"].get(prompt_id, 0)) < 1

    async def _auto_repair(self, wf: dict[str, Any], critic_prompt: str, critic_input: dict[str, Any], critic_output: dict[str, Any], state: dict[str, Any]) -> bool:
        producer = CRITIC_PRODUCER[critic_prompt]
        findings = critic_output.get("findings", [])
        if not findings:
            return False
        state["repair_attempts"][critic_prompt] = int(state["repair_attempts"].get(critic_prompt, 0)) + 1
        original = self.context_builder._result(wf["project_id"], producer)
        overrides = {
            "payload.original_object": original or {},
            "payload.original_producer": producer,
            "payload.findings_to_repair": findings,
            "payload.allowed_paths": [f.get("target_path", "result") for f in findings],
        }
        try:
            envelope = self.context_builder.build("P-TARGETED-REPAIR", wf["project_id"], workflow_id=wf["id"], workflow_state=state, overrides=overrides)
            repaired = await self.executor.execute("P-TARGETED-REPAIR", envelope, project_id=wf["project_id"], workflow_id=wf["id"], original_environment=state.get("original_environment", "OFFLINE_LOCAL"))
        except PromptExecutionError:
            return False
        if repaired["status"] != "PASS":
            return False
        state["repair_overrides"][producer] = repaired["output"]["result"]["repaired_object"]
        self._update(wf, state=state)
        return True

