from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

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
        self.shared_prompt = self._load_shared_prompt()
        self._schema_registry = self._build_schema_registry()

    def _load_shared_prompt(self) -> str:
        parts = []
        for rel in [
            "prompts/shared/business_rules.md",
            "prompts/shared/security_rules.md",
            "prompts/shared/source_authority.md",
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

    def model_profile(self, prompt_id: str) -> dict[str, Any]:
        profile_id = self.entry(prompt_id)["model_profile"]
        return self.profiles["profiles"][profile_id]
