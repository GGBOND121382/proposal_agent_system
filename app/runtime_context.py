from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass
from typing import Any

from .context import ContextBuilder as BaseContextBuilder
from .runtime_policy import CapabilityPolicy, LIVE_ENVELOPE_REGISTRY
from .util import sha256_json, utc_now


class LiveContextBlocked(ValueError):
    def __init__(self, prompt_id: str, unresolved_paths: list[str]):
        self.prompt_id = prompt_id
        self.unresolved_paths = unresolved_paths
        super().__init__(
            f"LIVE context for {prompt_id} contains unresolved schema scaffold fields: "
            + ", ".join(unresolved_paths[:20])
        )


@dataclass
class Scaffold:
    value: Any
    markers: dict[str, tuple[Any, bool, str | None]]


def _join(path: str, token: str | int) -> str:
    return f"{path}.{token}" if path else str(token)


def _valid_string(schema: dict[str, Any], path: str) -> str:
    pattern = str(schema.get("pattern") or "")
    min_length = int(schema.get("minLength", 1))
    if "[a-f0-9]{64}" in pattern or min_length >= 64:
        value = "0" * max(64, min_length)
    elif "A-Za-z0-9" in pattern:
        value = "scaffold-" + sha256_json(path)[:12]
    elif schema.get("format") == "date-time":
        value = utc_now()
    elif schema.get("format") == "date":
        value = utc_now()[:10]
    elif schema.get("format") == "uri":
        value = "https://invalid.example/scaffold"
    elif schema.get("format") == "email":
        value = "scaffold@example.invalid"
    else:
        value = "__UNRESOLVED__" + re.sub(r"[^A-Za-z0-9_]", "_", path)[-80:]
    if len(value) < min_length:
        value += "x" * (min_length - len(value))
    max_length = schema.get("maxLength")
    if isinstance(max_length, int):
        value = value[:max_length]
    return value


def build_schema_scaffold(
    schema: dict[str, Any],
    path: str = "",
    *,
    required: bool = True,
    optional_root: str | None = None,
) -> Scaffold:
    if "const" in schema:
        return Scaffold(copy.deepcopy(schema["const"]), {})
    if "default" in schema:
        return Scaffold(copy.deepcopy(schema["default"]), {})
    if "enum" in schema and schema["enum"]:
        value = copy.deepcopy(schema["enum"][0])
        return Scaffold(value, {path: (copy.deepcopy(value), required, optional_root)})
    for key in ("oneOf", "anyOf"):
        options = schema.get(key)
        if isinstance(options, list) and options:
            null_option = next((item for item in options if item.get("type") == "null"), None)
            if null_option is not None:
                return Scaffold(None, {})
            return build_schema_scaffold(options[0], path, required=required, optional_root=optional_root)
    if isinstance(schema.get("allOf"), list):
        merged: dict[str, Any] = {}
        markers: dict[str, Any] = {}
        scalar: Any = None
        for item in schema["allOf"]:
            built = build_schema_scaffold(item, path, required=required, optional_root=optional_root)
            if isinstance(built.value, dict):
                merged.update(built.value)
            else:
                scalar = built.value
            markers.update(built.markers)
        return Scaffold(merged if merged else scalar, markers)

    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        if "null" in schema_type:
            return Scaffold(None, {})
        schema_type = schema_type[0]
    if schema_type == "object" or "properties" in schema:
        value: dict[str, Any] = {}
        markers: dict[str, tuple[Any, bool, str | None]] = {}
        required_keys = set(schema.get("required") or [])
        for key, child_schema in (schema.get("properties") or {}).items():
            child_path = _join(path, key)
            child_required = required and key in required_keys
            child_optional_root = optional_root if optional_root is not None else (None if child_required else child_path)
            child = build_schema_scaffold(
                child_schema,
                child_path,
                required=child_required,
                optional_root=child_optional_root,
            )
            value[key] = child.value
            markers.update(child.markers)
        if not markers and path:
            markers[path] = (copy.deepcopy(value), required, optional_root)
        return Scaffold(value, markers)
    if schema_type == "array":
        count = int(schema.get("minItems", 0))
        values = []
        markers: dict[str, tuple[Any, bool, str | None]] = {}
        for index in range(count):
            child = build_schema_scaffold(
                schema.get("items") or {},
                _join(path, index),
                required=required,
                optional_root=optional_root,
            )
            values.append(child.value)
            markers.update(child.markers)
        if not markers and path:
            markers[path] = (copy.deepcopy(values), required, optional_root)
        return Scaffold(values, markers)
    if schema_type == "string" or schema_type is None:
        value = _valid_string(schema, path)
        return Scaffold(value, {path: (copy.deepcopy(value), required, optional_root)})
    if schema_type == "integer":
        value = int(schema.get("minimum", 0))
        return Scaffold(value, {path: (value, required, optional_root)})
    if schema_type == "number":
        value = float(schema.get("minimum", 0.0))
        return Scaffold(value, {path: (value, required, optional_root)})
    if schema_type == "boolean":
        return Scaffold(False, {path: (False, required, optional_root)})
    if schema_type == "null":
        return Scaffold(None, {})
    return Scaffold(None, {path: (None, required, optional_root)})


def _get_path(value: Any, dotted_path: str) -> tuple[bool, Any]:
    current = value
    if not dotted_path:
        return True, current
    for token in dotted_path.split("."):
        if isinstance(current, list):
            try:
                current = current[int(token)]
            except (ValueError, IndexError):
                return False, None
        elif isinstance(current, dict) and token in current:
            current = current[token]
        else:
            return False, None
    return True, current


def _delete_path(value: Any, dotted_path: str) -> None:
    parts = dotted_path.split(".") if dotted_path else []
    if not parts:
        return
    current = value
    for token in parts[:-1]:
        if isinstance(current, dict):
            current = current.get(token)
        elif isinstance(current, list):
            try:
                current = current[int(token)]
            except (ValueError, IndexError):
                return
        else:
            return
        if current is None:
            return
    last = parts[-1]
    if isinstance(current, dict):
        current.pop(last, None)
    elif isinstance(current, list):
        try:
            current.pop(int(last))
        except (ValueError, IndexError):
            return


class LiveContextBuilder(BaseContextBuilder):
    """Context builder that never reads Replay inputs in LIVE mode."""

    def __init__(self, db, pack):
        super().__init__(db, pack)
        self.runtime_mode = os.getenv("MODEL_RUNTIME_MODE", "REPLAY").upper()
        self.policy = CapabilityPolicy.from_environment()
        self.policy.assert_environment(self.runtime_mode)

    def _set_path_if_valid(
        self,
        prompt_id: str,
        envelope: dict[str, Any],
        dotted_path: str,
        value: Any,
        *,
        strict: bool = False,
    ) -> bool:
        changed = super()._set_path_if_valid(prompt_id, envelope, dotted_path, value, strict=strict)
        if changed and getattr(self, "runtime_mode", "REPLAY") == "LIVE":
            self._live_touched_paths.add(dotted_path)
        return changed

    def _path_was_touched(self, dotted_path: str) -> bool:
        return any(
            touched == dotted_path
            or dotted_path.startswith(touched + ".")
            or touched.startswith(dotted_path + ".")
            for touched in getattr(self, "_live_touched_paths", set())
        )

    def build(
        self,
        prompt_id: str,
        project_id: str,
        *,
        workflow_id: str | None = None,
        workflow_state: dict[str, Any] | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.runtime_mode != "LIVE":
            return super().build(
                prompt_id,
                project_id,
                workflow_id=workflow_id,
                workflow_state=workflow_state,
                overrides=overrides,
            )
        project = self.db.fetchone("SELECT * FROM projects WHERE id=?", (project_id,))
        if not project:
            raise KeyError(f"Project not found: {project_id}")
        config = json.loads(project["config_json"])
        docs = self._documents(project_id)
        state = workflow_state or {}
        context_hash = sha256_json(
            {
                "project": project,
                "documents": [item["document_hash"] for item in docs],
                "workflow_state": state,
            }
        )
        scaffold = build_schema_scaffold(self.pack.inlined_schema(prompt_id, "input"))
        self._live_touched_paths: set[str] = set()
        envelope = scaffold.value
        if not isinstance(envelope, dict):
            raise ValueError(f"Input schema for {prompt_id} did not produce an object scaffold")

        task = envelope.setdefault("task", {})
        active_section = str(state.get("active_section_id") or "")
        attempt = int((state.get("repair_attempts") or {}).get(prompt_id, 0)) + 1
        task["task_id"] = "task-" + sha256_json(
            {
                "prompt_id": prompt_id,
                "project_id": project_id,
                "workflow_id": workflow_id,
                "active_section_id": active_section,
                "attempt": attempt,
            }
        )[:16]
        task["current_step"] = prompt_id.removeprefix("P-").replace("-", "_")
        workflow_type = str(state.get("workflow_type") or "WF-1_PROJECT_INTAKE")
        task["workflow_type"] = (
            workflow_type.split("_", 1)[1]
            if workflow_type.startswith("WF-") and "_" in workflow_type
            else workflow_type
        )
        task["attempt"] = min(max(attempt, 1), 2)

        required_environment = self._required_environment(prompt_id, state)
        execution_level = "PUBLIC" if required_environment == "ONLINE_PUBLIC" else project["security_level"]
        envelope.setdefault("security_context", {}).update(
            {
                "project_security_level": execution_level,
                "input_max_security_level": execution_level,
                "required_environment": required_environment,
                "allowed_model_endpoint_ids": self._allowed_endpoints(project["security_level"], config, prompt_id),
                "prohibited_fields": config.get("prohibited_external_fields", []),
                "recipient_scope": config.get("recipient_scope", ["内部用户"]),
                "online_transfer_approval_status": self._online_approval_status(workflow_id),
                "policy_version": "2.0",
            }
        )
        envelope.setdefault("scope", {}).update(
            {
                "project_id": project_id,
                "target_object_ids": envelope.get("scope", {}).get("target_object_ids") or [],
                "read_only_object_ids": envelope.get("scope", {}).get("read_only_object_ids") or [],
                "protected_object_ids": envelope.get("scope", {}).get("protected_object_ids") or [],
            }
        )
        envelope["expected_output_schema"] = self.pack.entry(prompt_id)["output_schema"]
        self._apply_common_payload(envelope, prompt_id, project, config, docs, context_hash, state, workflow_id)

        fallback_values = {
            "payload.task_instruction": config.get("task_instruction") or project.get("description") or project.get("name"),
            "payload.project_name": project.get("name"),
            "payload.project_description": project.get("description"),
            "payload.intended_uses": [config.get("task_instruction") or project.get("description") or project.get("name")],
        }
        for path, value in fallback_values.items():
            if value:
                self._set_path_if_valid(prompt_id, envelope, path, value)
        if overrides:
            for path, value in overrides.items():
                self._set_path_if_valid(prompt_id, envelope, path, value, strict=True)

        prune_roots: set[str] = set()
        for path, marker_info in scaffold.markers.items():
            _marker, required_marker, optional_root = marker_info
            if not required_marker and optional_root and not self._path_was_touched(optional_root):
                prune_roots.add(optional_root)
        for root in sorted(prune_roots, key=lambda item: item.count("."), reverse=True):
            _delete_path(envelope, root)

        errors = self.pack.validate(prompt_id, "input", envelope)
        if errors:
            raise ValueError("LIVE context builder produced invalid input: " + "; ".join(errors[:20]))

        explicit_protocol_paths = {
            "schema_version",
            "prompt_id",
            "prompt_version",
            "task.task_id",
            "task.workflow_type",
            "task.current_step",
            "task.attempt",
            "security_context.project_security_level",
            "security_context.input_max_security_level",
            "security_context.required_environment",
            "security_context.online_transfer_approval_status",
            "security_context.allowed_model_endpoint_ids",
            "security_context.prohibited_fields",
            "security_context.recipient_scope",
            "security_context.policy_version",
            "scope.project_id",
            "scope.target_object_ids",
            "scope.read_only_object_ids",
            "scope.protected_object_ids",
            "freshness",
            "expected_output_schema",
        }
        unresolved = []
        for path, marker_info in scaffold.markers.items():
            marker, required_marker, _optional_root = marker_info
            if path in explicit_protocol_paths:
                continue
            exists, current = _get_path(envelope, path)
            if (
                exists
                and current == marker
                and required_marker
                and not self._path_was_touched(path)
            ):
                unresolved.append(path)
        if unresolved:
            raise LiveContextBlocked(prompt_id, sorted(unresolved))
        LIVE_ENVELOPE_REGISTRY.register(envelope)
        return envelope
