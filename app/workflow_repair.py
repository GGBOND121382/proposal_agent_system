from __future__ import annotations

import re
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
    "P-WRITE-CONTENT": None,
    "P-EXPRESSION-POLISH": None,
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

    @staticmethod
    def _repair_state_key(prompt_id: str, state: dict[str, Any]) -> str:
        """Return a section-scoped repair budget key when authoring a section.

        Section drafts are independent mutable objects.  A repair consumed for one
        section must not exhaust the budget or leak an override into another section.
        Non-section workflows retain the historic prompt-level key.
        """
        section_id = str(state.get("active_section_id") or "").strip()
        return f"section:{section_id}:{prompt_id}" if section_id else prompt_id

    @classmethod
    def _repair_override_key(cls, producer_prompt: str, state: dict[str, Any]) -> str:
        section_id = str(state.get("active_section_id") or "").strip()
        return f"section:{section_id}:{producer_prompt}" if section_id else producer_prompt

    def _can_auto_repair(self, prompt_id: str, state: dict[str, Any]) -> bool:
        if prompt_id not in CRITIC_PRODUCER:
            return False
        key = self._repair_state_key(prompt_id, state)
        return int(state.setdefault("repair_attempts", {}).get(key, 0)) < 1

    async def _auto_repair(self, wf: dict[str, Any], critic_prompt: str, critic_input: dict[str, Any], critic_output: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
        producer = CRITIC_PRODUCER[critic_prompt]
        findings = [item for item in critic_output.get("findings", []) if item.get("repairable", False)]
        if not findings:
            return None
        result_key = PRODUCER_RESULT_KEY.get(producer)
        if hasattr(self.context_builder, "_section_prompt_result"):
            original = self.context_builder._section_prompt_result(
                wf["project_id"],
                producer,
                workflow_id=wf.get("id"),
                section_id=str(state.get("active_section_id") or "") or None,
                key=result_key,
            )
        else:
            original = self.context_builder._result(wf["project_id"], producer, result_key)
        if original is None:
            return None

        attempt_key = self._repair_state_key(critic_prompt, state)
        state.setdefault("repair_attempts", {})[attempt_key] = int(
            state.setdefault("repair_attempts", {}).get(attempt_key, 0)
        ) + 1
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
        original_paragraph_ids = [
            str(item.get("paragraph_id"))
            for item in original.get("paragraphs") or []
            if isinstance(item, dict) and item.get("paragraph_id")
        ]
        def split_target_paths(value: str) -> list[str]:
            parts: list[str] = []
            current: list[str] = []
            bracket_depth = 0
            for char in value:
                if char == "[":
                    bracket_depth += 1
                elif char == "]" and bracket_depth:
                    bracket_depth -= 1
                if char in {",", ";"} and bracket_depth == 0:
                    part = "".join(current).strip()
                    if part:
                        parts.append(part)
                    current = []
                else:
                    current.append(char)
            part = "".join(current).strip()
            if part:
                parts.append(part)
            return parts

        def expand_numeric_bracket_ranges(value: str) -> list[str]:
            match = re.search(r"paragraphs\[([^\]]+)\]", value)
            if not match:
                return [value]
            identities: list[str] = []
            for token in match.group(1).split(","):
                token = token.strip()
                range_match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", token)
                if not range_match:
                    identities.append(token)
                    continue
                start, end = map(int, range_match.groups())
                step = 1 if end >= start else -1
                identities.extend(str(index) for index in range(start, end + step, step))
            return [
                value[: match.start(1)] + identity + value[match.end(1) :]
                for identity in identities
                if identity
            ]

        for finding in findings:
            target = str(finding.get("target_path_or_span") or "result")
            target_parts = [
                expanded
                for part in split_target_paths(target)
                for expanded in expand_numeric_bracket_ranges(part)
            ]
            bracket_ids = [
                item.strip()
                for part in target_parts
                for match in re.finditer(r"paragraphs\[([^\]]+)\]", part)
                for item in match.group(1).split(",")
                if item.strip() and not item.strip().isdigit()
            ]
            for part in target_parts:
                path = part.strip().replace("/", ".")
                if not path:
                    continue
                allowed_paths.append(
                    path if path.startswith("content.") else f"content.{path}"
                )
            if producer in {"P-WRITE-CONTENT", "P-WRITE-BLUEPRINT"}:
                paragraph_ids = [
                    *bracket_ids,
                    *re.findall(
                        r"(?<![A-Za-z0-9-])((?:P|para)-[A-Za-z0-9-]+)(?![A-Za-z0-9-])",
                        target + " " + str(finding.get("repair_instruction") or ""),
                        flags=re.I,
                    ),
                ]
                for paragraph_id in dict.fromkeys(paragraph_ids):
                    allowed_paths.append(f"content.{paragraph_id}")
            finding_code = str(finding.get("code") or "")
            finding_text = " ".join(
                str(finding.get(field) or "")
                for field in ("description", "repair_instruction", "target_path_or_span")
            )
            if producer == "P-WRITE-BLUEPRINT" and finding_code == "WORD_BUDGET_EXCEED":
                allowed_paths.extend(
                    f"content.{paragraph_id}.word_budget"
                    for paragraph_id in original_paragraph_ids
                )
            if producer == "P-WRITE-BLUEPRINT" and "CONTENT_KEY" in finding_code:
                allowed_paths.extend(
                    f"content.{paragraph_id}.novel_content_key"
                    for paragraph_id in original_paragraph_ids
                )
            if producer == "P-WRITE-CONTENT" and any(
                marker in finding_text
                for marker in ("PAGE_BUDGET", "篇幅", "字数", "word budget")
            ):
                allowed_paths.extend(
                    f"content.paragraphs[{index}].text"
                    for index, _paragraph_id in enumerate(original_paragraph_ids)
                )
            if producer == "P-WRITE-CONTENT" and "novel_content_key" in finding_text:
                allowed_paths.extend(
                    f"content.paragraphs[{index}].novel_content_key"
                    for index, _paragraph_id in enumerate(original_paragraph_ids)
                )
        if producer == "P-WRITE-CONTENT" and allowed_paths:
            allowed_paths.extend(
                [
                    "content.candidate_text",
                    "content.claim_advancement",
                ]
            )
        allowed_paths = list(dict.fromkeys(allowed_paths))

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
        except (PromptExecutionError, ValueError, KeyError):
            return None
        if repaired["status"] != "PASS":
            return None
        self.quality_manager.record_targeted_repair(
            project_id=wf["project_id"],
            workflow_id=str(state.get("quality_parent_workflow_id") or wf["id"]),
            repair_run_id=repaired["run_id"],
            finding_codes=[str(item.get("code")) for item in findings if item.get("code")],
            workflow_state=state,
        )
        override_key = self._repair_override_key(producer, state)
        state.setdefault("repair_overrides", {})[override_key] = repaired["output"]["result"]["repaired_object"]
        state["original_environment"] = repaired["route"]["environment"]
        self._update(wf, state=state)
        return repaired
