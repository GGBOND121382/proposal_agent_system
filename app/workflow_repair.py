from __future__ import annotations

from typing import Any

from .executor import PromptExecutionError
from .util import new_id, sha256_json
from .workflow_defs import CRITIC_PRODUCER




PRODUCER_RESULT_KEY = {
    "P-SCHEME-EXTRACT": "scheme_profile",
    "P-PROJECT-DEFINITION-EXTRACT": "project_definition",
    "P-FACT-EXTRACT": "fact_candidates",
    "P-TEMPLATE-EXTRACT": "template",
    "P-ARGUMENT-ARCHITECTURE": "argument_architecture",
    "P-REVISION-PLAN": "revision_plan",
    "P-WRITE-BLUEPRINT": "blueprint",
}
PRODUCER_ROLE = {
    "P-SECURITY-CLASSIFY": "SECURITY_REVIEW_AGENT",
    "P-SAFE-ONLINE-PACKAGE": "SECURITY_REVIEW_AGENT",
    "P-SCHEME-EXTRACT": "PROJECT_KNOWLEDGE_AGENT",
    "P-PROJECT-DEFINITION-EXTRACT": "PROJECT_KNOWLEDGE_AGENT",
    "P-FACT-EXTRACT": "PROJECT_KNOWLEDGE_AGENT",
    "P-TEMPLATE-EXTRACT": "TEMPLATE_AGENT",
    "P-ARGUMENT-ARCHITECTURE": "ARGUMENT_ARCHITECTURE_AGENT",
    "P-REVISION-PLAN": "PLANNING_AGENT",
    "P-WRITE-BLUEPRINT": "WRITING_AGENT",
    "P-WRITE-CONTENT": "WRITING_AGENT",
    "P-EXPRESSION-POLISH": "EXPRESSION_EDITOR_AGENT",
    "P-PUBLIC-RESEARCH-SYNTHESIS": "PROJECT_KNOWLEDGE_AGENT",
}


class WorkflowRepairMixin:
    async def _run_public_search(self, wf: dict[str, Any], state: dict[str, Any]) -> None:
        mode = self.executor.gateway.settings.runtime_mode
        if mode in {"REPLAY", "MOCK"}:
            state["public_search_results"] = {"sources": [], "passages": [], "queries": [], "mode": mode}
            return
        plan = self.context_builder._result(wf["project_id"], "P-PUBLIC-RESEARCH-PLAN") or {}
        provider = self.executor.gateway.settings.public_search_provider
        if mode == "SIMULATED" and provider == "disabled":
            state["public_search_results"] = self.research_service.simulated_search(plan)
            return
        state["public_search_results"] = await self.research_service.search(
            plan,
            project_id=wf["project_id"],
            workflow_id=wf["id"],
            security_level="PUBLIC",
        )

    def _can_auto_repair(self, prompt_id: str, state: dict[str, Any]) -> bool:
        if prompt_id not in CRITIC_PRODUCER:
            return False
        return int(state["repair_attempts"].get(prompt_id, 0)) < 1

    async def _auto_repair(self, wf: dict[str, Any], critic_prompt: str, critic_input: dict[str, Any], critic_output: dict[str, Any], state: dict[str, Any]) -> bool:
        producer = CRITIC_PRODUCER[critic_prompt]
        findings = [item for item in critic_output.get("findings", []) if item.get("repairable", False)]
        if not findings:
            return False
        result_key = PRODUCER_RESULT_KEY.get(producer)
        original = self.context_builder._result(wf["project_id"], producer, result_key)
        if original is None:
            return False

        state["repair_attempts"][critic_prompt] = int(state["repair_attempts"].get(critic_prompt, 0)) + 1
        object_id = str(
            original.get("plan_id")
            or original.get("candidate_id")
            or original.get("blueprint_id")
            or original.get("package_id")
            or original.get("template_id")
            or new_id("repair-object")
        )
        original_object = {
            "object_type": producer.removeprefix("P-").replace("-", "_"),
            "object_id": object_id,
            "object_hash": sha256_json(original),
            "content": original,
        }
        original_ref = {
            "object_id": object_id,
            "object_type": original_object["object_type"],
            "version": 1,
            "object_hash": original_object["object_hash"],
            "security_level": self._project_level(wf["project_id"]),
            "display_name": f"{producer}原始输出",
        }
        allowed_paths = []
        for finding in findings:
            target = str(finding.get("target_path_or_span") or "result")
            allowed_paths.append(target if target.startswith("content") else f"content.{target}")

        overrides = {
            "payload.original_object": original_object,
            "payload.original_producer": PRODUCER_ROLE.get(producer, "WRITING_AGENT"),
            "payload.findings_to_repair": findings,
            "payload.allowed_paths": allowed_paths,
            "payload.protected_paths": [],
            "payload.protected_hashes": [],
            "payload.original_input_refs": [original_ref],
        }
        try:
            envelope = self.context_builder.build(
                "P-TARGETED-REPAIR",
                wf["project_id"],
                workflow_id=wf["id"],
                workflow_state=state,
                overrides=overrides,
            )
            repaired = await self.executor.execute(
                "P-TARGETED-REPAIR",
                envelope,
                project_id=wf["project_id"],
                workflow_id=wf["id"],
                original_environment=state.get("original_environment", "OFFLINE_LOCAL"),
            )
        except PromptExecutionError:
            return False
        if repaired["status"] != "PASS":
            return False
        state["repair_overrides"][producer] = repaired["output"]["result"]["repaired_object"]
        self._update(wf, state=state)
        return True
