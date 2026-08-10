from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from .prompt_contracts import finding_code_errors, protocol_semantic_errors
from .util import expand_env, read_json


class PromptPack:
    def __init__(self, root: Path):
        self.root = root
        self.registry_doc = read_json(root / "config/prompt_registry.json")
        self.registry = {p["prompt_id"]: p for p in self.registry_doc["prompts"]}
        self.endpoints = expand_env(yaml.safe_load((root / "config/model_endpoints.yaml").read_text(encoding="utf-8")))
        self.models = expand_env(yaml.safe_load((root / "config/models.yaml").read_text(encoding="utf-8")))
        self.profiles = expand_env(yaml.safe_load((root / "config/prompt_model_profiles.yaml").read_text(encoding="utf-8")))
        self.routing = expand_env(yaml.safe_load((root / "policies/model_routing.yaml").read_text(encoding="utf-8")))
        self.section_profiles = yaml.safe_load((root / "knowledge/section_profiles.yaml").read_text(encoding="utf-8"))
        self.relation_matrix = yaml.safe_load((root / "knowledge/relation_matrix.yaml").read_text(encoding="utf-8"))
        self.shared_prompt = self._load_shared_prompt()
        self._schema_registry = self._build_schema_registry()
        self._structure_validator_cache: dict[tuple[str, str], Draft202012Validator] = {}

    def _load_shared_prompt(self) -> str:
        parts = []
        for rel in [
            "prompts/shared/business_rules.md",
            "prompts/shared/security_rules.md",
            "prompts/shared/source_authority.md",
            "prompts/shared/knowledge_status_rules.md",
            "prompts/shared/skill_rules.md",
            "prompts/shared/output_protocol.md",
        ]:
            parts.append((self.root / rel).read_text(encoding="utf-8"))
        return "\n\n".join(parts)

    def _build_schema_registry(self) -> Registry:
        registry = Registry()
        for path in self.root.glob("schemas/**/*.json"):
            schema = read_json(path)
            schema["$id"] = path.resolve().as_uri()
            registry = registry.with_resource(path.resolve().as_uri(), Resource.from_contents(schema))
        return registry

    def prompt_ids(self) -> list[str]:
        return list(self.registry)

    def entry(self, prompt_id: str) -> dict[str, Any]:
        if prompt_id not in self.registry:
            raise KeyError(f"Unknown prompt_id: {prompt_id}")
        return self.registry[prompt_id]

    def prompt_text(self, prompt_id: str) -> str:
        entry = self.entry(prompt_id)
        return (self.root / entry["prompt_file"]).read_text(encoding="utf-8")

    def schema_path(self, prompt_id: str, kind: str) -> Path:
        entry = self.entry(prompt_id)
        key = "input_schema" if kind == "input" else "output_schema"
        return (self.root / entry[key]).resolve()

    def schema(self, prompt_id: str, kind: str) -> dict[str, Any]:
        return read_json(self.schema_path(prompt_id, kind))

    def validator(self, prompt_id: str, kind: str) -> Draft202012Validator:
        path = self.schema_path(prompt_id, kind)
        schema = self.schema(prompt_id, kind)
        schema["$id"] = path.as_uri()
        return Draft202012Validator(schema, registry=self._schema_registry, format_checker=Draft202012Validator.FORMAT_CHECKER)

    def validate(self, prompt_id: str, kind: str, value: Any) -> list[str]:
        errors = sorted(self.validator(prompt_id, kind).iter_errors(value), key=lambda e: list(e.absolute_path))
        result = []
        for err in errors:
            path = "/" + "/".join(str(x) for x in err.absolute_path)
            result.append(f"{path or '/'}: {err.message}")
        if isinstance(value, dict):
            result.extend(self._protocol_semantic_errors(prompt_id, kind, value))
            if kind == "output":
                result.extend(
                    finding_code_errors(
                        prompt_id=prompt_id,
                        output=value,
                        prompt_text=self.prompt_text(prompt_id),
                    )
                )
        return result

    def _protocol_semantic_errors(
        self,
        prompt_id: str,
        kind: str,
        value: dict[str, Any],
    ) -> list[str]:
        return protocol_semantic_errors(
            prompt_id=prompt_id,
            kind=kind,
            value=value,
            expected_output_schema=str(
                self.entry(prompt_id).get("output_schema") or ""
            ),
        )

    @staticmethod
    def _structure_only_schema(node: Any, *, root: bool = False) -> Any:
        """Return a schema that checks container/scalar shape only.

        Model responses are normalized before the final strict JSON Schema
        validation.  The normalizers intentionally repair enum aliases and a
        small number of deterministic protocol fields, but they must never run
        on a value whose container type is already incompatible with the
        declared schema.  This projection preserves only type-bearing schema
        keywords and converts ``oneOf`` to ``anyOf`` so structurally compatible
        branches do not fail merely because semantic constraints were removed.

        Missing required fields, enum drift, bounds, formats and additional
        properties remain the responsibility of the normal strict validator.
        """
        if isinstance(node, bool):
            return node
        if isinstance(node, list):
            return [PromptPack._structure_only_schema(item) for item in node]
        if not isinstance(node, dict):
            return node

        projected: dict[str, Any] = {}
        if "type" in node:
            declared = node["type"]
            declared_types = (
                [declared]
                if isinstance(declared, str)
                else [item for item in declared if isinstance(item, str)]
                if isinstance(declared, list)
                else []
            )
            # Preflight distinguishes containers from scalars, not one scalar
            # primitive from another.  This keeps deterministic repairs such as
            # integer -> trusted string/null reachable, while preventing a
            # model-authored object/array from reaching code that calls int(),
            # set membership, regex helpers, or string methods on a scalar.
            container_types = [
                item for item in declared_types if item in {"object", "array"}
            ]
            scalar_declared = any(
                item in {"string", "integer", "number", "boolean", "null"}
                for item in declared_types
            )
            scalar_shape = ["string", "integer", "number", "boolean", "null"]
            if root and "object" in declared_types:
                projected["type"] = "object"
            elif container_types and scalar_declared:
                projected["type"] = [*container_types, *scalar_shape]
            elif container_types:
                projected["type"] = [*container_types, "null"]
            elif scalar_declared:
                projected["type"] = scalar_shape
        if isinstance(node.get("properties"), dict):
            projected["properties"] = {
                key: PromptPack._structure_only_schema(value)
                for key, value in node["properties"].items()
            }
        if isinstance(node.get("patternProperties"), dict):
            projected["patternProperties"] = {
                key: PromptPack._structure_only_schema(value)
                for key, value in node["patternProperties"].items()
            }
        if isinstance(node.get("items"), (dict, bool)):
            projected["items"] = PromptPack._structure_only_schema(node["items"])
        if isinstance(node.get("prefixItems"), list):
            projected["prefixItems"] = [
                PromptPack._structure_only_schema(item)
                for item in node["prefixItems"]
            ]
        if isinstance(node.get("contains"), (dict, bool)):
            projected["contains"] = PromptPack._structure_only_schema(node["contains"])
        if isinstance(node.get("additionalProperties"), (dict, bool)):
            # Keep only schema-valued additional properties.  A plain false is
            # a semantic strictness rule rather than a container-shape rule.
            if isinstance(node["additionalProperties"], dict):
                projected["additionalProperties"] = PromptPack._structure_only_schema(
                    node["additionalProperties"]
                )
        if isinstance(node.get("allOf"), list):
            projected["allOf"] = [
                PromptPack._structure_only_schema(item)
                for item in node["allOf"]
            ]
        branches: list[Any] = []
        for keyword in ("anyOf", "oneOf"):
            if isinstance(node.get(keyword), list):
                branches.extend(
                    PromptPack._structure_only_schema(item)
                    for item in node[keyword]
                )
        if branches:
            projected["anyOf"] = branches
        return projected

    def structure_schema(self, prompt_id: str, kind: str) -> dict[str, Any]:
        """Return an inlined schema projection used before normalization."""
        return self._structure_only_schema(self.inlined_schema(prompt_id, kind), root=True)

    def validate_structure(self, prompt_id: str, kind: str, value: Any) -> list[str]:
        """Validate declared value/container types without semantic checks."""
        cache_key = (prompt_id, kind)
        validator = self._structure_validator_cache.get(cache_key)
        if validator is None:
            validator = Draft202012Validator(
                self.structure_schema(prompt_id, kind),
                format_checker=Draft202012Validator.FORMAT_CHECKER,
            )
            self._structure_validator_cache[cache_key] = validator
        errors = sorted(validator.iter_errors(value), key=lambda e: list(e.absolute_path))
        result: list[str] = []
        for err in errors:
            path = "/" + "/".join(str(x) for x in err.absolute_path)
            result.append(f"{path or '/'}: {err.message}")
        return result

    def inlined_schema(self, prompt_id: str, kind: str) -> dict[str, Any]:
        path = self.schema_path(prompt_id, kind)
        return self._inline_refs(read_json(path), path, set())

    def _inline_refs(self, node: Any, base_path: Path, stack: set[str]) -> Any:
        if isinstance(node, list):
            return [self._inline_refs(item, base_path, stack) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            ref = str(node["$ref"])
            if ref.startswith("#"):
                target = read_json(base_path)
                fragment = ref[1:]
                target = self._resolve_fragment(target, fragment)
                key = f"{base_path.as_uri()}{ref}"
                if key in stack:
                    raise ValueError(f"Recursive local schema reference is not supported for model output: {key}")
                merged = self._inline_refs(target, base_path, stack | {key})
            else:
                ref_file, _, fragment = ref.partition("#")
                target_path = (base_path.parent / ref_file).resolve()
                key = f"{target_path.as_uri()}#{fragment}"
                if key in stack:
                    raise ValueError(f"Recursive schema reference is not supported for model output: {key}")
                target = read_json(target_path)
                if fragment:
                    target = self._resolve_fragment(target, fragment)
                merged = self._inline_refs(target, target_path, stack | {key})
            siblings = {k: v for k, v in node.items() if k != "$ref"}
            if siblings:
                if not isinstance(merged, dict):
                    return self._inline_refs(siblings, base_path, stack)
                merged = {**merged, **self._inline_refs(siblings, base_path, stack)}
            return merged
        result = {k: self._inline_refs(v, base_path, stack) for k, v in node.items() if k not in {"$id", "$schema"}}
        return result

    @staticmethod
    def _resolve_fragment(document: Any, fragment: str) -> Any:
        if not fragment:
            return document
        if not fragment.startswith("/"):
            raise ValueError(f"Unsupported JSON pointer fragment: {fragment}")
        current = document
        for token in fragment.lstrip("/").split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            current = current[int(token)] if isinstance(current, list) else current[token]
        return copy.deepcopy(current)

    def replay_case(self, prompt_id: str, case_type: str = "normal") -> dict[str, Any]:
        dirname = prompt_id.removeprefix("P-").lower().replace("-", "_")
        path = self.root / "replay" / "cases" / dirname / f"{case_type}.json"
        if not path.exists():
            raise FileNotFoundError(path)
        return read_json(path)

    def replay_input(self, prompt_id: str) -> dict[str, Any]:
        return copy.deepcopy(self.replay_case(prompt_id, "normal")["input"])

    def replay_output(self, prompt_id: str, case_type: str = "normal") -> dict[str, Any]:
        return copy.deepcopy(self.replay_case(prompt_id, case_type)["expected_output"])


    def section_profile_for(self, title: str | None) -> dict[str, Any]:
        normalized = str(title or "").strip()
        # A title can match multiple patterns (for example ``总体技术路线``
        # contains both ``技术路线`` and the broad algorithm keyword ``路径``).
        # First-match routing silently assigned the wrong section contract.  Use
        # deterministic specificity scoring: exact title > longer substring >
        # earlier profile order.
        matches: list[tuple[int, int, int, dict[str, Any]]] = []
        for profile_index, profile in enumerate(self.section_profiles.get("profiles", [])):
            for pattern in (str(item).strip() for item in profile.get("title_patterns", [])):
                if not pattern or pattern not in normalized:
                    continue
                exact = int(pattern == normalized)
                matches.append((exact, len(pattern), -profile_index, profile))
        if matches:
            profile = max(matches, key=lambda item: item[:3])[3]
            return {
                "profile_id": profile["profile_id"],
                "version": str(profile.get("version", "3.0.0")),
                "required_inputs": list(profile.get("required_inputs", [])),
                "acceptance_rules": list(profile.get("acceptance_rules", [])),
            }
        profile = self.section_profiles["default_profile"]
        return {
            "profile_id": profile["profile_id"],
            "version": str(profile.get("version", "3.0.0")),
            "required_inputs": list(profile.get("required_inputs", [])),
            "acceptance_rules": list(profile.get("acceptance_rules", [])),
        }

    def model_profile(self, prompt_id: str) -> dict[str, Any]:
        profile_id = self.entry(prompt_id)["model_profile"]
        return self.profiles["profiles"][profile_id]
