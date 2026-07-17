from __future__ import annotations

import copy
import json
from typing import Any

from .executor import PromptExecutionError, PromptExecutor as BasePromptExecutor
from .runtime_executor import RuntimePromptExecutor
from .util import sha256_json


class G3RuntimePromptExecutor(RuntimePromptExecutor):
    """Capability runtime extensions used by the formal G3 run.

    The complete validated context remains available to deterministic quality checks
    and is retained in Trace evidence. Only repeated prose and duplicated evidence
    objects are compacted for the provider request, and the compacted object must
    still satisfy the prompt input schema.
    """

    @staticmethod
    def _compact_live_value(
        value: Any,
        path: tuple[str, ...] = (),
        *,
        aggressive: bool = False,
    ) -> Any:
        if isinstance(value, dict):
            return {
                key: G3RuntimePromptExecutor._compact_live_value(
                    item, (*path, str(key)), aggressive=aggressive
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            key = path[-1] if path else ""
            limits = {
                "source_refs": 2,
                "trace_links": 2,
                "passages": 5,
                "sources": 8,
                "source_catalog": 8,
                "source_documents": 6,
                "template_examples": 3,
                "existing_comments": 6,
                "conflicts": 6,
                "limitations": 6,
            }
            protected = {
                "candidate_sections",
                "candidate_document",
                "document_section_map",
                "section_contracts",
                "mandatory_sections",
                "linked_sections",
                "sections",
                "tasks",
                "paragraphs",
                "items",
                "relations",
                "fact_candidates",
                "section_profiles",
                "research_question_ids",
            }
            array_limits = {
                "trace_links": 1,
                "term_usage": 1,
                "source_preservation_summary": 1,
                "edit_log": 2,
                "preserved_trace_ids": 2,
                "trace_link_ids": 1,
                "evidence_ids": 2,
                "source_refs": 1 if aggressive else 2,
            }
            default_limit = len(value) if key in protected else (4 if aggressive else 8)
            selected = value[: array_limits.get(key, limits.get(key, default_limit))]
            return [
                G3RuntimePromptExecutor._compact_live_value(
                    item, (*path, str(index)), aggressive=aggressive
                )
                for index, item in enumerate(selected)
            ]
        if not isinstance(value, str):
            return value
        key = path[-1] if path else ""
        if key.endswith("_id") or key.endswith("_ids") or key.endswith("_hash") or key in {
            "id",
            "code",
            "status",
            "type",
            "role",
            "sha256",
            "source_hash",
            "document_hash",
            "input_hash",
            "output_hash",
            "url",
            "doi",
            "canonical_term",
            "profile_id",
            "section_id",
            "candidate_id",
        }:
            return value
        factor = 0.55 if aggressive else 1.0
        limits = {
            "candidate_text": 700,
            "text": 260,
            "quoted_text": 200,
            "description": 220,
            "rationale": 180,
            "summary": 240,
            "coverage_summary": 240,
            "section_contribution": 240,
            "repair_instruction": 180,
            "definition": 180,
            "task_instruction": 260,
            "source_path_or_span": 96,
            "content": 360,
            "page_text": 360,
            "excerpt": 360,
            "objective": 180,
            "argument_function": 160,
            "acceptance_rule": 140,
        }
        base_limit = limits.get(key, 300 if len(value) > 500 else len(value))
        limit = max(48, int(base_limit * factor))
        if len(value) <= limit:
            return value
        return BasePromptExecutor._compact_paragraph_text(value, limit=max(40, limit))

    def _prune_live_to_required_schema(
        self,
        value: Any,
        schema: Any,
        path: tuple[str, ...] = (),
    ) -> Any:
        if not isinstance(schema, dict):
            return self._compact_live_value(value, path, aggressive=True)
        branches = schema.get("oneOf") or schema.get("anyOf") or []
        if branches:
            selected = None
            for branch in branches:
                try:
                    from jsonschema import Draft202012Validator

                    if not list(Draft202012Validator(branch).iter_errors(value)):
                        selected = branch
                        break
                except Exception:
                    continue
            return self._prune_live_to_required_schema(value, selected or branches[0], path)
        if schema.get("allOf"):
            merged: dict[str, Any] = {
                key: item for key, item in schema.items() if key != "allOf"
            }
            properties = dict(merged.get("properties") or {})
            required = list(merged.get("required") or [])
            for part in schema.get("allOf") or []:
                if isinstance(part, dict):
                    properties.update(part.get("properties") or {})
                    required.extend(part.get("required") or [])
            merged["properties"] = properties
            merged["required"] = list(dict.fromkeys(required))
            return self._prune_live_to_required_schema(value, merged, path)
        kind = schema.get("type")
        if (kind == "object" or "properties" in schema) and isinstance(value, dict):
            properties = schema.get("properties") or {}
            required = set(schema.get("required") or [])
            if (path[-1] if path else "") == "candidate" and properties:
                paragraph_values = [
                    str(item.get("text") or "")
                    for item in value.get("paragraphs") or []
                    if isinstance(item, dict) and str(item.get("text") or "").strip()
                ]
                digest = "\n".join(paragraph_values) or str(value.get("candidate_text") or "")
                result: dict[str, Any] = {}
                for key in required:
                    child_schema = properties.get(key) or {}
                    item = value.get(key)
                    if key == "candidate_text":
                        item = BasePromptExecutor._compact_paragraph_text(digest, limit=250)
                    elif key == "paragraphs" and isinstance(item, list):
                        item = item[:1]
                    elif key == "term_usage":
                        item = []
                    result[key] = self._prune_live_to_required_schema(
                        item, child_schema, (*path, str(key))
                    )
                for paragraph in result.get("paragraphs") or []:
                    if isinstance(paragraph, dict):
                        paragraph["text"] = BasePromptExecutor._compact_paragraph_text(
                            str(paragraph.get("text") or ""), limit=80
                        )
                        paragraph["trace_link_ids"] = list(
                            paragraph.get("trace_link_ids") or []
                        )[:1]
                        paragraph["evidence_ids"] = list(
                            paragraph.get("evidence_ids") or []
                        )[:1]
                for link in result.get("trace_links") or []:
                    if isinstance(link, dict):
                        if link.get("target_path"):
                            link["target_path"] = str(link["target_path"])[:80]
                        if link.get("source_path_or_span"):
                            link["source_path_or_span"] = str(
                                link["source_path_or_span"]
                            )[:80]
                advancement = result.get("claim_advancement") or {}
                if isinstance(advancement, dict):
                    for array_key in (
                        "advanced_claim_ids",
                        "new_information_keys",
                        "distinguished_from_section_ids",
                    ):
                        if array_key in advancement:
                            advancement[array_key] = list(
                                advancement.get(array_key) or []
                            )[:1]
                    if advancement.get("section_contribution"):
                        advancement["section_contribution"] = BasePromptExecutor._compact_paragraph_text(
                            str(advancement["section_contribution"]), limit=100
                        )
                return result
            if not properties:
                if (path[-1] if path else "") == "narrative_architecture":
                    identity_keys = (
                        "architecture_id",
                        "document_type",
                        "central_proposition_id",
                        "central_proposition",
                        "research_question_ids",
                        "closest_prior_work_ids",
                        "work_package_ids",
                        "main_body_page_budget",
                        "main_body_word_budget",
                    )
                    result = {
                        key: self._compact_live_value(
                            value[key], (*path, key), aggressive=True
                        )
                        for key in identity_keys
                        if key in value
                    }
                    contracts = []
                    for item in value.get("section_contracts") or []:
                        if not isinstance(item, dict):
                            continue
                        contract_keys = (
                            "section_contract_id",
                            "section_id",
                            "title",
                            "profile_id",
                            "argument_function",
                            "unique_information_keys",
                            "prerequisite_section_ids",
                            "acceptance_rules",
                            "page_budget",
                        )
                        contracts.append(
                            {
                                key: self._compact_live_value(
                                    item[key],
                                    (*path, "section_contracts", key),
                                    aggressive=True,
                                )
                                for key in contract_keys
                                if key in item
                            }
                        )
                    result["section_contracts"] = contracts
                    if "attachments" in value:
                        result["attachments"] = self._compact_live_value(
                            value["attachments"],
                            (*path, "attachments"),
                            aggressive=True,
                        )
                    return result
                preferred = {
                    "architecture_id",
                    "document_type",
                    "central_proposition_id",
                    "central_proposition",
                    "research_question_ids",
                    "work_package_ids",
                    "main_body_page_budget",
                    "main_body_word_budget",
                    "section_contracts",
                    "attachments",
                    "candidate_id",
                    "candidate_text",
                    "paragraphs",
                    "trace_links",
                    "claim_advancement",
                    "tasks",
                    "sections",
                    "items",
                    "relations",
                    "status",
                    "verdict",
                    "findings",
                    "unresolved_items",
                }
                minimum = max(1, int(schema.get("minProperties") or 0))
                ordered = [key for key in value if key in preferred]
                ordered.extend(key for key in value if key not in ordered)
                selected = ordered[: max(minimum, min(len(ordered), 14))]
                return {
                    key: self._compact_live_value(
                        value[key], (*path, str(key)), aggressive=True
                    )
                    for key in selected
                }
            keep_optional = {
                "warnings",
                "unresolved_items",
                "findings",
                "source_refs",
                "evidence_refs",
                "trace_links",
                "claim_advancement",
                "coverage",
                "limitations",
                "conflicts",
                "prior_section_digest",
                "revision_findings",
                "public_research",
                "research_output",
            }
            result: dict[str, Any] = {}
            for key, item in value.items():
                keep = len(path) < 2 or key in required or key in keep_optional
                if not keep:
                    continue
                child_schema = properties.get(key)
                if child_schema is None:
                    additional = schema.get("additionalProperties", True)
                    if isinstance(additional, dict):
                        child_schema = additional
                    elif additional is False:
                        continue
                    else:
                        child_schema = {}
                result[key] = self._prune_live_to_required_schema(
                    item, child_schema, (*path, str(key))
                )
            return result
        if kind == "array" and isinstance(value, list):
            key = path[-1] if path else ""
            protected = {
                "candidate_sections",
                "candidate_document",
                "document_section_map",
                "section_contracts",
                "mandatory_sections",
                "linked_sections",
                "sections",
                "tasks",
                "paragraphs",
                "items",
                "relations",
                "fact_candidates",
                "section_profiles",
                "research_question_ids",
            }
            minimum = int(schema.get("minItems") or 0)
            array_limits = {
                "trace_links": 1,
                "term_usage": max(minimum, 1),
                "source_preservation_summary": max(minimum, 1),
                "edit_log": max(minimum, 2),
                "preserved_trace_ids": max(minimum, 2),
                "trace_link_ids": max(minimum, 1),
                "evidence_ids": max(minimum, 2),
                "source_refs": max(minimum, 1),
                "items": max(minimum, 6),
                "relations": max(minimum, 8),
            }
            limit = (
                len(value)
                if key in protected
                else array_limits.get(key, max(minimum, 4))
            )
            return [
                self._prune_live_to_required_schema(
                    item,
                    schema.get("items") or {},
                    (*path, str(index)),
                )
                for index, item in enumerate(value[:limit])
            ]
        return self._compact_live_value(value, path, aggressive=True)

    def _prepare_model_envelope(
        self, prompt_id: str, envelope: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        model_envelope, existing = super()._prepare_model_envelope(prompt_id, envelope)
        if not self.policy.enabled:
            return model_envelope, existing
        limit = int(
            getattr(
                getattr(self.gateway, "settings", None),
                "model_max_input_characters",
                80000,
            )
            or 80000
        )
        before = len(json.dumps(model_envelope, ensure_ascii=False))
        if before <= limit:
            return model_envelope, existing
        compact = self._compact_live_value(copy.deepcopy(model_envelope))
        after = len(json.dumps(compact, ensure_ascii=False))
        if after > limit:
            compact = self._compact_live_value(compact, aggressive=True)
            after = len(json.dumps(compact, ensure_ascii=False))
        if after > limit:
            compact = self._prune_live_to_required_schema(
                model_envelope,
                self.pack.inlined_schema(prompt_id, "input"),
            )
        errors = self.pack.validate(prompt_id, "input", compact)
        if errors:
            raise PromptExecutionError(
                "LIVE input compaction produced an invalid envelope",
                validation_errors=errors,
            )
        after = len(json.dumps(compact, ensure_ascii=False))
        if after > limit:
            raise PromptExecutionError(
                "LIVE input remains above configured character target after "
                f"schema-preserving compaction: {after}>{limit}"
            )
        metadata = {
            **(existing or {}),
            "mode": "SCHEMA_PRESERVING_LIVE_CONTEXT_COMPACTION",
            "prompt_id": prompt_id,
            "original_characters": len(json.dumps(envelope, ensure_ascii=False)),
            "pre_generic_characters": before,
            "model_characters": after,
            "configured_character_target": limit,
            "full_quality_context_sha256": sha256_json(envelope),
            "model_context_sha256": sha256_json(compact),
        }
        return compact, metadata

    @staticmethod
    def _schema_outline(schema: Any) -> dict[str, Any]:
        def skeleton(node: Any, depth: int = 0) -> Any:
            if depth > 8 or not isinstance(node, dict):
                return "<value>"
            if "enum" in node:
                return "<" + "|".join(
                    str(item) for item in node.get("enum") or []
                ) + ">"
            kind = str(
                node.get("type")
                or ("object" if "properties" in node else "value")
            )
            if kind == "object" or "properties" in node:
                required = set(node.get("required") or [])
                return {
                    name: skeleton(child, depth + 1)
                    for name, child in (node.get("properties") or {}).items()
                    if name in required
                }
            if kind == "array":
                return [skeleton(node.get("items") or {}, depth + 1)]
            return f"<{kind}>"

        top = schema.get("properties") or {} if isinstance(schema, dict) else {}
        top_required = set(schema.get("required") or []) if isinstance(schema, dict) else set()
        return {
            "instruction": (
                "Return one JSON object matching this required-field skeleton. "
                "Replace angle-bracket placeholders with valid values. "
                "Do not omit required keys."
            ),
            "required_skeleton": skeleton(schema),
            "optional_top_level_fields": [
                name for name in top if name not in top_required
            ],
        }

    def _system_prompt(self, prompt_id: str, output_schema: dict[str, Any]) -> str:
        mode = str(
            getattr(
                getattr(self.gateway, "settings", None),
                "model_schema_prompt_mode",
                "full",
            )
            or "full"
        ).lower()
        schema_guidance = (
            self._schema_outline(output_schema) if mode == "compact" else output_schema
        )
        return (
            self.pack.shared_prompt
            + "\n\n"
            + self.pack.prompt_text(prompt_id)
            + "\n\n# 运行时强制输出结构\n"
            + json.dumps(schema_guidance, ensure_ascii=False)
        )
