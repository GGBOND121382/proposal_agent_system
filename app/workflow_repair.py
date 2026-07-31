from __future__ import annotations

import copy
import inspect
import re
from typing import Any

from .executor import PromptExecutionError
from .util import sha256_json
from .workflow_defs import CRITIC_PRODUCER




PRODUCER_RESULT_KEY = {
    "P-SCHEME-EXTRACT": "scheme_profile",
    "P-PROJECT-DEFINITION-EXTRACT": "project_definition",
    "P-FACT-EXTRACT": "fact_candidates",
    "P-TEMPLATE-EXTRACT": "template",
    # The critic evaluates both the graph and the research-design matrix, so a
    # targeted repair must receive the full producer result rather than only
    # the nested argument_architecture graph.
    "P-ARGUMENT-ARCHITECTURE": None,
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
    def _context_result(
        self,
        project_id: str,
        prompt_id: str,
        key: str | None = None,
        *,
        workflow_id: str | None = None,
        exact_workflow: bool = False,
    ) -> Any:
        """Read a context result with workflow scoping when the builder supports it.

        Lightweight test/dry-run builders may implement the legacy three-argument
        interface. Signature inspection preserves that compatibility without
        swallowing TypeError raised inside a real builder implementation.
        """
        reader = self.context_builder._result
        parameters = inspect.signature(reader).parameters
        if "workflow_id" in parameters:
            return reader(
                project_id,
                prompt_id,
                key,
                workflow_id=workflow_id,
                exact_workflow=exact_workflow,
            )
        return reader(project_id, prompt_id, key)

    async def _run_public_search(self, wf: dict[str, Any], state: dict[str, Any]) -> None:
        mode = self.executor.gateway.settings.runtime_mode
        if mode in {"REPLAY", "MOCK"}:
            state["public_search_results"] = {"sources": [], "passages": [], "queries": [], "mode": mode}
            return
        plan = self._context_result(
            wf["project_id"],
            "P-PUBLIC-RESEARCH-PLAN",
            workflow_id=wf["id"],
            exact_workflow=True,
        ) or {}
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
        options = state.get("options") or {}
        try:
            configured_limit = int(options.get("targeted_repair_limit", 1))
        except (TypeError, ValueError):
            configured_limit = 1
        # One pass remains the production-safe default. Acceptance/test runs may
        # explicitly permit bounded convergence when an independent re-review
        # exposes a second set of repairable findings.
        repair_limit = max(1, min(configured_limit, 3))
        return (
            int(state.setdefault("repair_attempts", {}).get(key, 0))
            < repair_limit
        )

    _REPAIR_ID_FIELDS = (
        "claim_id",
        "item_id",
        "node_id",
        "edge_id",
        "paragraph_id",
        "section_id",
        "plan_id",
        "candidate_id",
        "blueprint_id",
        "package_id",
        "template_id",
        "object_id",
        "id",
    )

    @classmethod
    def _repair_content_adapter(
        cls,
        original: Any,
        result_key: str | None,
    ) -> tuple[dict[str, Any], str | None]:
        """Adapt producer results to the object-only targeted-repair contract.

        ``P-TARGETED-REPAIR`` intentionally accepts an object as
        ``original_object.content``.  Most producer results are objects, while
        ``P-FACT-EXTRACT`` exposes the ``fact_candidates`` result as a list.
        Wrap collection-shaped results under their canonical result key and
        remember that key so the repaired value can be restored to its original
        shape before it is placed in ``repair_overrides``.
        """
        if isinstance(original, dict):
            return copy.deepcopy(original), None
        if isinstance(original, list):
            collection_key = str(result_key or "items").strip() or "items"
            return {collection_key: copy.deepcopy(original)}, collection_key
        raise TypeError(
            "Targeted repair only supports object or list producer results; "
            f"received {type(original).__name__}"
        )

    @classmethod
    def _repair_object_id(
        cls,
        content: dict[str, Any],
        producer: str,
    ) -> str:
        for field in cls._REPAIR_ID_FIELDS:
            value = content.get(field)
            if isinstance(value, (str, int)) and str(value).strip():
                return str(value).strip()
        producer_slug = producer.removeprefix("P-").lower().replace("_", "-")
        return f"repair:{producer_slug}:{sha256_json(content)[:16]}"

    @classmethod
    def _collection_selector(
        cls,
        items: list[Any],
        identity: str,
    ) -> str | None:
        identity = str(identity or "").strip()
        if not identity:
            return None
        for field in cls._REPAIR_ID_FIELDS:
            for item in items:
                if isinstance(item, dict) and str(item.get(field) or "") == identity:
                    return f"{field}={identity}"
        return None

    @classmethod
    def _canonical_repair_path(
        cls,
        raw_path: str,
        *,
        content: dict[str, Any],
        collection_key: str | None,
    ) -> str:
        path = str(raw_path or "").strip().replace("/", ".")
        path = re.sub(r"\.+", ".", path).strip(".")
        path = re.sub(r"^\$\.?", "", path)
        path = re.sub(r"^(?:result|payload)\.", "", path)
        if path.startswith("content."):
            path = path[len("content.") :]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*_candidate", path):
            path = ""
        path = re.sub(r"^[A-Za-z_][A-Za-z0-9_]*_candidate\.", "", path)
        if not path or path in {"result", "payload", "content"}:
            return f"content.{collection_key}" if collection_key else "content"
        if not collection_key:
            return f"content.{path}"

        items = content.get(collection_key)
        if not isinstance(items, list):
            return f"content.{path}"

        collection_pattern = re.fullmatch(
            rf"{re.escape(collection_key)}\[([^\]]+)\](?:\.(.+))?",
            path,
        )
        if collection_pattern:
            selector = collection_pattern.group(1).strip()
            suffix = collection_pattern.group(2)
            if not selector.isdigit() and "=" not in selector:
                selector = cls._collection_selector(items, selector) or selector
            canonical = f"content.{collection_key}[{selector}]"
            return canonical + (f".{suffix}" if suffix else "")

        if path == collection_key:
            return f"content.{collection_key}"

        dotted_collection_pattern = re.fullmatch(
            rf"{re.escape(collection_key)}\.([^.]*)?(?:\.(.+))?",
            path,
        )
        if dotted_collection_pattern:
            selector = str(dotted_collection_pattern.group(1) or "").strip()
            suffix = dotted_collection_pattern.group(2)
            if selector:
                if not selector.isdigit() and "=" not in selector:
                    selector = cls._collection_selector(items, selector) or selector
                canonical = f"content.{collection_key}[{selector}]"
                return canonical + (f".{suffix}" if suffix else "")
            return f"content.{collection_key}"

        identity, separator, suffix = path.partition(".")
        selector = cls._collection_selector(items, identity)
        if selector:
            canonical = f"content.{collection_key}[{selector}]"
            return canonical + (f".{suffix}" if separator and suffix else "")

        if len(items) == 1 and isinstance(items[0], dict) and identity in items[0]:
            return f"content.{collection_key}[0].{path}"
        return f"content.{collection_key}.{path}"

    @staticmethod
    def _restore_repaired_shape(
        repaired_object: Any,
        collection_key: str | None,
    ) -> tuple[bool, Any]:
        value = repaired_object
        if isinstance(value, dict) and isinstance(value.get("content"), dict):
            is_metadata_wrapper = any(
                key in value for key in ("object_id", "object_type", "object_hash")
            )
            is_plain_wrapper = set(value) == {"content"}
            contains_wrapped_collection = bool(
                collection_key and collection_key in value["content"]
            )
            if is_metadata_wrapper or is_plain_wrapper or contains_wrapped_collection:
                value = value["content"]
        if collection_key:
            if not isinstance(value, dict) or not isinstance(value.get(collection_key), list):
                return False, None
            return True, copy.deepcopy(value[collection_key])
        if not isinstance(value, dict):
            return False, None
        return True, copy.deepcopy(value)

    async def _auto_repair(self, wf: dict[str, Any], critic_prompt: str, critic_input: dict[str, Any], critic_output: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
        producer = CRITIC_PRODUCER[critic_prompt]
        findings = [
            item
            for item in critic_output.get("findings", [])
            if item.get("repairable", False)
            and not (
                str(item.get("code") or "").startswith("QG_")
                and str(item.get("target_type") or "").endswith("CRITIC")
            )
        ]
        if not findings:
            return None
        result_key = PRODUCER_RESULT_KEY.get(producer)
        repair_override_reader = getattr(
            self.context_builder,
            "_repair_override",
            None,
        )
        if callable(repair_override_reader):
            repair_override = repair_override_reader(state, producer)
        else:
            repair_override = (state.get("repair_overrides") or {}).get(
                self._repair_override_key(producer, state)
            )
        if repair_override is not None:
            original = repair_override
        elif hasattr(self.context_builder, "_section_prompt_result"):
            original = self.context_builder._section_prompt_result(
                wf["project_id"],
                producer,
                workflow_id=wf.get("id"),
                section_id=str(state.get("active_section_id") or "") or None,
                key=result_key,
            )
        else:
            original = self._context_result(
                wf["project_id"],
                producer,
                result_key,
                workflow_id=wf["id"],
                exact_workflow=True,
            )
        if original is None:
            return None
        try:
            repair_content, collection_key = self._repair_content_adapter(
                original,
                result_key,
            )
        except TypeError:
            return None

        attempt_key = self._repair_state_key(critic_prompt, state)
        previous_attempts = int(
            state.setdefault("repair_attempts", {}).get(attempt_key, 0)
        )
        state.setdefault("repair_attempts", {})[attempt_key] = previous_attempts + 1
        object_id = self._repair_object_id(repair_content, producer)
        original_object = {
            "object_type": producer.removeprefix("P-").replace("-", "_"),
            "object_id": object_id,
            "object_hash": sha256_json(repair_content),
            "content": repair_content,
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
            for item in repair_content.get("paragraphs") or []
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
                if not str(part or "").strip():
                    continue
                allowed_paths.append(
                    self._canonical_repair_path(
                        part,
                        content=repair_content,
                        collection_key=collection_key,
                    )
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
            if (
                producer == "P-WRITE-BLUEPRINT"
                and "WORD_BUDGET" in finding_code
            ):
                allowed_paths.extend(
                    f"content.{paragraph_id}.word_budget"
                    for paragraph_id in original_paragraph_ids
                )
            if (
                producer == "P-WRITE-BLUEPRINT"
                and (
                    "CONTENT_KEY" in finding_code
                    or "novel_content_key" in finding_text
                )
            ):
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
            # Contract construction and technical execution failures did not
            # produce a repair candidate and must not consume semantic budget.
            state.setdefault("repair_attempts", {})[attempt_key] = previous_attempts
            return None
        if repaired["status"] != "PASS":
            return None
        repaired_object = repaired["output"]["result"]["repaired_object"]
        restored, repaired_value = self._restore_repaired_shape(
            repaired_object,
            collection_key,
        )
        if not restored:
            return None
        self.quality_manager.record_targeted_repair(
            project_id=wf["project_id"],
            workflow_id=str(state.get("quality_parent_workflow_id") or wf["id"]),
            repair_run_id=repaired["run_id"],
            finding_codes=[str(item.get("code")) for item in findings if item.get("code")],
            workflow_state=state,
        )
        override_key = self._repair_override_key(producer, state)
        state.setdefault("repair_overrides", {})[override_key] = repaired_value
        if collection_key:
            state.setdefault("repair_shape_adaptations", []).append({
                "producer_prompt": producer,
                "critic_prompt": critic_prompt,
                "collection_key": collection_key,
                "object_id": object_id,
                "repair_run_id": repaired["run_id"],
            })
        state["original_environment"] = repaired["route"]["environment"]
        self._update(wf, state=state)
        return repaired
