from __future__ import annotations

"""Unified model-output contract normalization.

This module is the single compatibility layer between model-created JSON and
strict JSON Schema validation.  It does not relax schemas.  It only performs
registered, deterministic conversions whose meaning is unambiguous for the
current field and target schema.  Unknown values remain unchanged and are
reported so the normal schema validator can reject them.
"""

import copy
import json
import re
from dataclasses import dataclass, asdict
from typing import Any, Iterable, Mapping

from jsonschema import Draft202012Validator

from .contracts.semantic_contract import annotate_schema_reference_semantics

from .status_ontology import (
    CANONICAL_CLAIM_TYPES,
    CANONICAL_KNOWLEDGE_STATUSES,
    CANONICAL_TEMPORAL_STATUSES,
    normalize_claim_type,
    normalize_knowledge_status,
    normalize_temporal_status,
)

CONTRACT_REGISTRY_VERSION = "5.4.0"


def prepare_schema_contract(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Apply the central reference-field registry before schema use."""

    return annotate_schema_reference_semantics(schema)

# Cross-workflow vocabularies live here rather than in prompt-specific
# normalizers.  JSON Schemas remain the strict authority for a concrete path;
# these domains define which vocabulary a field belongs to and which
# cross-domain spelling has the same unambiguous business meaning.
CANONICAL_PROJECT_ITEM_TYPES: tuple[str, ...] = (
    "PROJECT_BASIC",
    "STAKEHOLDER",
    "DEMAND",
    "SCENARIO",
    "CURRENT_STATE",
    "EXISTING_APPROACH",
    "GAP",
    "ROOT_CAUSE",
    "PROBLEM",
    "OBJECTIVE",
    "WORK_PACKAGE",
    "METHOD",
    "DATA_RESOURCE",
    "EXPERIMENT",
    "INNOVATION",
    "DELIVERABLE",
    "METRIC",
    "ACHIEVEMENT",
    "CAPABILITY",
    "TEAM_MEMBER",
    "SCHEDULE_PHASE",
    "RISK",
    "RESOURCE_REQUIREMENT",
    "BUDGET_ITEM",
    "COMPLIANCE_ITEM",
)

CANONICAL_ARGUMENT_NODE_TYPES: tuple[str, ...] = (
    "RESEARCH_GAP",
    "OBJECTIVE",
    "RESEARCH_CONTENT",
    "WORK_PACKAGE",
    "FORMAL_MODEL",
    "MECHANISM",
    "BASELINE",
    "EXPERIMENT_DESIGN",
    "EVALUATION_METRIC",
    "NOVEL_MECHANISM",
    "CLOSEST_PRIOR_WORK",
    "TEAM_EVIDENCE",
    "BOUNDARY_CONDITION",
    "RISK",
)

ENUM_DOMAINS: dict[str, tuple[str, ...]] = {
    "project_item_type": CANONICAL_PROJECT_ITEM_TYPES,
    "argument_node_type": CANONICAL_ARGUMENT_NODE_TYPES,
}

# Array items inherit their containing field name in ``visit``.  Consequently
# ``missing_item_types[*]`` is resolved through the same project-item domain as
# scalar item-type fields, independent of the prompt or JSON path.
FIELD_ENUM_DOMAINS: dict[str, str] = {
    "item_type": "project_item_type",
    "source_item_type": "project_item_type",
    "target_item_type": "project_item_type",
    "missing_item_types": "project_item_type",
    "node_type": "argument_node_type",
}

# These are domain adapters, not loose synonyms.  They translate a concept
# between the project-definition vocabulary and the argument-graph vocabulary.
# Ambiguous concepts (for example TEAM_EVIDENCE, which could mean an
# ACHIEVEMENT, CAPABILITY, or TEAM_MEMBER) are intentionally not guessed.
DOMAIN_ALIAS_CANDIDATES: dict[str, dict[str, tuple[str, ...]]] = {
    "project_item_type": {
        "CLOSEST_PRIOR_WORK": ("EXISTING_APPROACH",),
        "BASELINE": ("EXISTING_APPROACH",),
        "RESEARCH_GAP": ("GAP",),
        "RESEARCH_QUESTION": ("PROBLEM",),
        "RESEARCH_CONTENT": ("WORK_PACKAGE",),
        "FORMAL_MODEL": ("METHOD",),
        "EXPERIMENT_DESIGN": ("EXPERIMENT",),
        "EVALUATION_METRIC": ("METRIC",),
        "NOVEL_MECHANISM": ("INNOVATION",),
    },
    "argument_node_type": {
        "EXISTING_APPROACH": ("CLOSEST_PRIOR_WORK",),
        "GAP": ("RESEARCH_GAP",),
        "METHOD": ("FORMAL_MODEL",),
        "EXPERIMENT": ("EXPERIMENT_DESIGN",),
        "METRIC": ("EVALUATION_METRIC",),
        "INNOVATION": ("NOVEL_MECHANISM",),
        "ACHIEVEMENT": ("TEAM_EVIDENCE",),
        "CAPABILITY": ("TEAM_EVIDENCE",),
        "TEAM_MEMBER": ("TEAM_EVIDENCE",),
    },
}

# Canonical vocabularies that are shared across stages.  Stage-specific enums
# remain authoritative in their JSON Schemas; aliases below are selected only
# when their canonical target appears in the current schema's allowed values.
CANONICAL_ENUMS: dict[str, tuple[str, ...]] = {
    "knowledge_status": tuple(CANONICAL_KNOWLEDGE_STATUSES),
    "claim_type": tuple(CANONICAL_CLAIM_TYPES),
    "temporal_status": tuple(CANONICAL_TEMPORAL_STATUSES),
    "fact_role": ("FACT", "DESIGN", "TARGET", "ASSUMPTION", "UNKNOWN"),
    "stage3_claim_role": ("DESIGN_HYPOTHESIS",),
    "stage6_claim_status": (
        "PUBLIC_RESEARCH_SUMMARY",
        "CONFIRMED_DESIGN",
        "PROJECT_PLAN",
        "QUALIFIED_USER_ASSERTED",
        "BOUNDARY_STATEMENT",
    ),
    **ENUM_DOMAINS,
}

# Semantic equivalences whose target is chosen from the current schema.  A
# source token may map to different canonical values in different fields.
FIELD_ALIAS_CANDIDATES: dict[str, dict[str, tuple[str, ...]]] = {
    "fact_role": {
        "PROJECT_DESIGN": ("DESIGN",),
        "CONFIRMED_DESIGN": ("DESIGN",),
        "PROJECT_PLAN": ("DESIGN",),
        "PLAN": ("DESIGN",),
        "PLANNED": ("DESIGN",),
        "DESIGN_HYPOTHESIS": ("DESIGN",),
        "PROVISIONAL_TARGET": ("TARGET",),
        "EXPECTED_RESULT": ("TARGET",),
        "EXPECTED": ("TARGET",),
        "WORKING_ASSUMPTION": ("ASSUMPTION",),
        "MODEL_INFERENCE": ("ASSUMPTION",),
    },
    "claim_role": {
        "PROJECT_DESIGN": ("DESIGN_HYPOTHESIS",),
        "CONFIRMED_DESIGN": ("DESIGN_HYPOTHESIS",),
        "PROJECT_PLAN": ("DESIGN_HYPOTHESIS",),
        "PLAN": ("DESIGN_HYPOTHESIS",),
        "DESIGN": ("DESIGN_HYPOTHESIS",),
        "PLANNED": ("DESIGN_HYPOTHESIS",),
    },
    "claim_status": {
        "PROJECT_DESIGN": ("PROJECT_PLAN", "DESIGN_HYPOTHESIS_TO_BE_CHECKED_BY_PUBLIC_RESEARCH"),
        "PROJECT_PLAN": ("PROJECT_PLAN", "DESIGN_HYPOTHESIS_TO_BE_CHECKED_BY_PUBLIC_RESEARCH"),
        "PLAN": ("PROJECT_PLAN", "DESIGN_HYPOTHESIS_TO_BE_CHECKED_BY_PUBLIC_RESEARCH"),
        "PLANNED": ("PROJECT_PLAN", "DESIGN_HYPOTHESIS_TO_BE_CHECKED_BY_PUBLIC_RESEARCH"),
        "DESIGN": ("CONFIRMED_DESIGN", "DESIGN_HYPOTHESIS_TO_BE_CHECKED_BY_PUBLIC_RESEARCH"),
        "DESIGN_HYPOTHESIS": ("DESIGN_HYPOTHESIS_TO_BE_CHECKED_BY_PUBLIC_RESEARCH", "PROJECT_PLAN"),
        "PROVISIONAL_TARGET": ("PROJECT_PLAN",),
        "EXPECTED_RESULT": ("PROJECT_PLAN",),
        "TARGET": ("PROJECT_PLAN",),
        "USER_ASSERTED": ("QUALIFIED_USER_ASSERTED",),
        "PUBLIC_CLAIM": ("PUBLIC_RESEARCH_SUMMARY",),
        "BOUNDARY": ("BOUNDARY_STATEMENT",),
        "REQUIREMENT": ("BOUNDARY_STATEMENT",),
    },
    "status": {
        # Writer/critic response protocol.
        "ACCEPT": ("PASS", "ACCEPTED"),
        "ACCEPTED": ("PASS",),
        "REJECT": ("BLOCK", "FAIL"),
        "REJECTED": ("BLOCK", "FAIL"),
        "FAIL": ("BLOCK",),
        # Stage-4 node semantic aliases.
        "PROJECT_DESIGN": ("CONFIRMED_DESIGN",),
        "PROJECT_PLAN": ("CONFIRMED_DESIGN",),
        "PLAN": ("CONFIRMED_DESIGN",),
        "PLANNED": ("CONFIRMED_DESIGN",),
        "REQUIREMENT": ("CONFIRMED_REQUIREMENT",),
        "DESIGN_CONSTRAINT": ("CONFIRMED_DESIGN_CONSTRAINT",),
        "PROCESS_RULE": ("STAGE_PROCESS_RULE",),
        "GUIDE_UNKNOWN": ("UNRESOLVED_GUIDE_RULE",),
        "ASSUMPTION": ("WORKING_ASSUMPTION",),
        "METHOD_HYPOTHESIS": ("PROPOSED_METHOD_HYPOTHESIS",),
        "EXPECTED_RESULT": ("PROVISIONAL_TARGET",),
        "TARGET": ("PROVISIONAL_TARGET",),
        "VALIDATION_REQUIRED": ("TO_BE_VALIDATED",),
        "UNVALIDATED": ("TO_BE_VALIDATED",),
    },
    "verdict": {
        "PASS": ("ACCEPT", "ACCEPT_FOR_HUMAN_APPROVAL", "ACCEPT_FOR_IMPORT_REVIEW"),
        "ACCEPT": ("ACCEPT_FOR_HUMAN_APPROVAL", "ACCEPT_FOR_IMPORT_REVIEW"),
        "ACCEPTED": ("ACCEPT", "ACCEPT_FOR_HUMAN_APPROVAL", "ACCEPT_FOR_IMPORT_REVIEW"),
        "FAIL": ("BLOCK", "REJECT"),
        "BLOCK": ("REJECT",),
        "REJECTED": ("REJECT", "BLOCK"),
        "BLOCKED": ("BLOCK", "REJECT"),
    },
    "result": {
        "ACCEPT": ("PASS",),
        "ACCEPTED": ("PASS",),
        "REJECT": ("FAIL",),
        "REJECTED": ("FAIL",),
        "BLOCK": ("FAIL",),
        "BLOCKED": ("FAIL",),
    },
    "chain_type": {
        "GAP_TO_RQ": ("GAP_TO_QUESTION",),
        "RQ_TO_OBJECTIVE": ("QUESTION_TO_OBJECTIVE",),
        "OBJECTIVE_TO_CONTENT": ("OBJECTIVE_TO_WORK_PACKAGE",),
        "CONTENT_TO_METHOD": ("WORK_PACKAGE_TO_METHOD",),
        "METHOD_TO_EXPERIMENT": ("METHOD_TO_EVALUATION",),
        "GAP_TO_QUESTION": ("GAP_TO_RQ",),
        "QUESTION_TO_OBJECTIVE": ("RQ_TO_OBJECTIVE",),
        "OBJECTIVE_TO_WORK_PACKAGE": ("OBJECTIVE_TO_CONTENT",),
        "WORK_PACKAGE_TO_METHOD": ("CONTENT_TO_METHOD",),
        "METHOD_TO_EVALUATION": ("METHOD_TO_EXPERIMENT",),
    },
    "relation": {
        "ADDRESSED_BY": ("ADDRESSES",),
        "ADDRESSES": ("ADDRESSED_BY",),
        "VALIDATED_BY": ("VERIFIED_BY",),
        "VERIFIED_BY": ("VALIDATED_BY",),
        "SUPPORTED_BY": ("USES_MECHANISM",),
        "USES_MECHANISM": ("SUPPORTED_BY",),
        # IMPLEMENTS/REALIZED_BY also require endpoint reversal; handled below.
        "IMPLEMENTS": ("REALIZED_BY",),
        "REALIZED_BY": ("IMPLEMENTS",),
    },
    "relation_type": {
        "ADDRESSES": ("ADDRESSED_BY",),
        "VERIFIED_BY": ("VALIDATED_BY",),
        "IMPLEMENTS": ("REALIZED_BY",),
    },
    "suggested_route": {
        "USER_INPUT": ("USER_INPUT_OR_RESEARCH", "USER"),
        "USER_INPUT_OR_RESEARCH": ("USER_INPUT", "USER"),
        "PROJECT_OWNER": ("USER",),
    },
    "availability": {
        "PARTIAL": ("PARTIALLY_AVAILABLE",),
        "PARTIALLY_PRESENT": ("PARTIALLY_AVAILABLE",),
        "NOT_PROVIDED": ("MISSING", "NOT_AVAILABLE", "TO_BE_COLLECTED"),
        "NOT_AVAILABLE": ("MISSING", "TO_BE_COLLECTED"),
        "MISSING": ("NOT_AVAILABLE", "TO_BE_COLLECTED"),
        "TO_BE_COLLECTED": ("MISSING", "NOT_AVAILABLE"),
    },
    "document_type": {
        "HYBRID": ("HYBRID_RESEARCH_PROPOSAL",),
        "ENGINEERING": ("ENGINEERING_PROPOSAL",),
        "RESEARCH": ("RESEARCH_PROPOSAL",),
        "科研项目申请书": ("RESEARCH_PROPOSAL",),
        "RESEARCH_PROPOSAL": ("科研项目申请书",),
        "项目申请书": ("RESEARCH_PROPOSAL", "科研项目申请书"),
    },
    "severity": {
        "BLOCKER": ("BLOCKING", "P0"),
        "CRITICAL": ("BLOCKING", "P0"),
        "ERROR": ("MAJOR", "P1"),
        "WARNING": ("MINOR", "P2"),
    },
}

# Pairs for deterministic sibling-field swap repair.  This handles the common
# case in which two legal values were emitted under the wrong field names.
SWAPPABLE_FIELD_PAIRS: tuple[tuple[str, str], ...] = (
    ("knowledge_status", "claim_type"),
    ("knowledge_status", "temporal_status"),
    ("knowledge_status", "fact_role"),
    ("claim_type", "temporal_status"),
)


@dataclass
class ContractChange:
    path: str
    field: str
    original_value: Any
    canonical_value: Any
    rule: str
    reason: str


@dataclass
class ContractUnresolved:
    path: str
    field: str
    value: Any
    allowed_values: list[Any]
    reason: str


def _token(value: Any) -> str:
    value = str(value or "").strip()
    value = re.sub(r"[\s\-]+", "_", value)
    return value.upper()


def _enum_values(schema: Mapping[str, Any]) -> list[Any]:
    if "enum" in schema and isinstance(schema["enum"], list):
        return list(schema["enum"])
    if "const" in schema:
        return [schema["const"]]
    return []


def _domain_alias(field: str, value: Any, allowed: Iterable[Any]) -> tuple[Any | None, str | None]:
    domain = FIELD_ENUM_DOMAINS.get(field)
    if domain is None:
        return None, None
    targets = DOMAIN_ALIAS_CANDIDATES.get(domain, {}).get(_token(value), ())
    matches = [
        target
        for target in targets
        if any(
            isinstance(candidate, str) and _token(candidate) == _token(target)
            for candidate in allowed
        )
    ]
    if len(matches) != 1:
        return None, None
    canonical = next(
        candidate
        for candidate in allowed
        if isinstance(candidate, str) and _token(candidate) == _token(matches[0])
    )
    return canonical, f"registered {domain} cross-domain adapter"


def _resolve_local_ref(schema: Mapping[str, Any], root_schema: Mapping[str, Any]) -> Mapping[str, Any]:
    ref = schema.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return schema
    current: Any = root_schema
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        current = current[int(part)] if isinstance(current, list) else current[part]
    if not isinstance(current, Mapping):
        return schema
    siblings = {k: v for k, v in schema.items() if k != "$ref"}
    return {**current, **siblings}


def _choose_branch(value: Any, branches: Iterable[Any], root_schema: Mapping[str, Any]) -> Mapping[str, Any]:
    candidates = [b for b in branches if isinstance(b, Mapping)]
    if not candidates:
        return {}
    if isinstance(value, Mapping):
        keys = set(value)

        def branch_score(branch: Mapping[str, Any]) -> tuple[int, int, int, int]:
            resolved = _resolve_local_ref(branch, root_schema)
            properties = resolved.get("properties") or {}
            discriminator_score = 0
            discriminator_misses = 0
            for key in keys & set(properties):
                child = properties.get(key)
                if not isinstance(child, Mapping):
                    continue
                allowed = _enum_values(_resolve_local_ref(child, root_schema))
                if not allowed:
                    continue
                raw = value.get(key)
                exact = any(
                    raw == candidate
                    or (
                        isinstance(raw, str)
                        and isinstance(candidate, str)
                        and _token(raw) == _token(candidate)
                    )
                    for candidate in allowed
                )
                alias, _ = _domain_alias(str(key), raw, allowed)
                if exact:
                    discriminator_score += 4
                elif alias is not None:
                    discriminator_score += 3
                else:
                    discriminator_misses += 1
            return (
                discriminator_score,
                -discriminator_misses,
                len(keys & set(properties)),
                len(set(resolved.get("required") or []) & keys),
            )

        return max(
            candidates,
            key=branch_score,
        )
    if isinstance(value, list):
        for branch in candidates:
            if _resolve_local_ref(branch, root_schema).get("type") == "array":
                return branch
    if value is None:
        for branch in candidates:
            resolved = _resolve_local_ref(branch, root_schema)
            branch_type = resolved.get("type")
            types = [branch_type] if isinstance(branch_type, str) else list(branch_type or [])
            if "null" in types or None in _enum_values(resolved):
                return branch
    for branch in candidates:
        branch_type = _resolve_local_ref(branch, root_schema).get("type")
        if branch_type == "string" and isinstance(value, str):
            return branch
        if branch_type == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return branch
        if branch_type == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return branch
        if branch_type == "boolean" and isinstance(value, bool):
            return branch
    return candidates[0]


def _effective_schema(schema: Mapping[str, Any], value: Any, root_schema: Mapping[str, Any]) -> dict[str, Any]:
    current = dict(_resolve_local_ref(schema, root_schema))
    for key in ("oneOf", "anyOf"):
        if isinstance(current.get(key), list):
            branch = _choose_branch(value, current[key], root_schema)
            merged = dict(_resolve_local_ref(branch, root_schema))
            current = {**{k: v for k, v in current.items() if k != key}, **merged}
    if isinstance(current.get("allOf"), list):
        merged: dict[str, Any] = {k: v for k, v in current.items() if k != "allOf"}
        properties: dict[str, Any] = dict(merged.get("properties") or {})
        required = list(merged.get("required") or [])
        for branch in current["allOf"]:
            branch_effective = _effective_schema(branch, value, root_schema)
            properties.update(branch_effective.get("properties") or {})
            required.extend(branch_effective.get("required") or [])
            for k, v in branch_effective.items():
                if k not in {"properties", "required"}:
                    merged.setdefault(k, v)
        if properties:
            merged["properties"] = properties
        if required:
            merged["required"] = list(dict.fromkeys(required))
        current = merged
    return current


def _canonical_match(value: Any, allowed: list[Any]) -> Any | None:
    if value in allowed:
        return value
    if not isinstance(value, str):
        return None
    token = _token(value)
    matches = [candidate for candidate in allowed if isinstance(candidate, str) and _token(candidate) == token]
    return matches[0] if len(matches) == 1 else None


def _choose_alias(field: str, value: Any, allowed: list[Any], parent: Mapping[str, Any]) -> tuple[Any | None, str | None]:
    token = _token(value)
    allowed_strings = [x for x in allowed if isinstance(x, str)]

    if field == "knowledge_status":
        decision = normalize_knowledge_status(value, source_refs=parent.get("source_refs"))
        if decision.normalized and decision.canonical_status in allowed:
            return decision.canonical_status, decision.reason
    elif field == "claim_type":
        decision = normalize_claim_type(value)
        if decision.normalized and decision.canonical_value in allowed:
            return decision.canonical_value, decision.reason
    elif field == "temporal_status":
        decision = normalize_temporal_status(value)
        if decision.normalized and decision.canonical_value in allowed:
            return decision.canonical_value, decision.reason

    domain_value, domain_reason = _domain_alias(field, value, allowed)
    if domain_value is not None:
        return domain_value, domain_reason

    # Stage-4 node status needs node-type-aware conservative mapping.
    if field == "status" and "CONFIRMED_DESIGN" in allowed_strings:
        node_type = _token(parent.get("node_type"))
        if token in {"PROJECT_DESIGN", "PROJECT_PLAN", "PLAN", "PLANNED"}:
            if node_type == "EVALUATION_METRIC" and "PROVISIONAL_TARGET" in allowed_strings:
                return "PROVISIONAL_TARGET", "evaluation metrics remain provisional targets"
            if node_type in {"CLOSEST_PRIOR_WORK", "TEAM_EVIDENCE"} and "UNKNOWN" in allowed_strings:
                return "UNKNOWN", "unsupported evidence nodes must remain unknown"
            if node_type == "NOVEL_MECHANISM" and "TO_BE_VALIDATED" in allowed_strings:
                return "TO_BE_VALIDATED", "novel mechanisms require later validation"
            return "CONFIRMED_DESIGN", "selected project design maps to the Stage-4 design status"

    for candidate in FIELD_ALIAS_CANDIDATES.get(field, {}).get(token, ()):  # ordered, context-safe targets
        if candidate in allowed:
            return candidate, f"registered {field} alias"
    return None, None


def _property_schemas(schema: Mapping[str, Any], value: Mapping[str, Any], root_schema: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    effective = _effective_schema(schema, value, root_schema)
    return {
        str(k): _effective_schema(v, value.get(k), root_schema)
        for k, v in (effective.get("properties") or {}).items()
        if isinstance(v, Mapping)
    }


def required_null_container_errors(
    value: Any,
    schema: Mapping[str, Any],
) -> list[str]:
    """Report required object/array properties that providers returned as null.

    Optional containers may be deterministically defaulted to ``{}``/``[]`` by
    :func:`normalize_against_schema`.  A required container is different: null
    means the provider omitted a contractually required section.  Replacing it
    with an empty value can make semantically incomplete output pass schemas
    whose arrays allow zero items, so required nulls are rejected before any
    prompt-specific normalizer can dereference or mask them.
    """

    root_schema = prepare_schema_contract(schema)
    errors: list[str] = []

    def pointer(path: tuple[Any, ...]) -> str:
        if not path:
            return "/"
        escaped = [str(part).replace("~", "~0").replace("/", "~1") for part in path]
        return "/" + "/".join(escaped)

    def container_types(node_schema: Mapping[str, Any], node: Any) -> tuple[set[str], bool]:
        effective = _effective_schema(node_schema, node, root_schema)
        declared = effective.get("type")
        declared_types = {declared} if isinstance(declared, str) else set(declared or [])
        if not declared_types:
            if isinstance(effective.get("properties"), Mapping):
                declared_types.add("object")
            if isinstance(effective.get("items"), Mapping):
                declared_types.add("array")
        return {str(item) for item in declared_types}, "null" in declared_types

    def visit(node: Any, node_schema: Mapping[str, Any], path: tuple[Any, ...]) -> None:
        effective = _effective_schema(node_schema, node, root_schema)
        if isinstance(node, Mapping):
            properties = effective.get("properties") or {}
            required = {str(item) for item in effective.get("required") or []}
            for key, child in node.items():
                child_schema = properties.get(key)
                if child_schema is None:
                    additional = effective.get("additionalProperties")
                    child_schema = additional if isinstance(additional, Mapping) else {}
                if not isinstance(child_schema, Mapping):
                    continue
                child_path = (*path, key)
                if str(key) in required and child is None:
                    types, nullable = container_types(child_schema, child)
                    expected = sorted(types & {"object", "array"})
                    if expected and not nullable:
                        errors.append(
                            f"{pointer(child_path)}: required {'/'.join(expected)} container "
                            "is null; provider must return the required structured content"
                        )
                        continue
                if child is not None:
                    visit(child, child_schema, child_path)
            return
        if isinstance(node, list):
            item_schema = effective.get("items")
            if isinstance(item_schema, Mapping):
                for index, child in enumerate(node):
                    if child is not None:
                        visit(child, item_schema, (*path, index))

    visit(value, root_schema, ())
    return errors


def normalize_against_schema(
    value: Any,
    schema: Mapping[str, Any],
    *,
    contract_id: str,
) -> tuple[Any, dict[str, Any]]:
    """Return a normalized deep copy plus a complete conversion report.

    The function never invents missing business content. It only canonicalizes
    registered enum aliases, repairs unambiguous sibling-field swaps, and
    applies registered relation direction transforms. Unrecognized values stay
    untouched and are listed under ``unresolved``.
    """

    normalized = copy.deepcopy(value)
    root_schema = copy.deepcopy(dict(schema))
    changes: list[ContractChange] = []
    unresolved: list[ContractUnresolved] = []

    def record(path: str, field: str, before: Any, after: Any, rule: str, reason: str) -> None:
        changes.append(ContractChange(path, field, before, after, rule, reason))

    def visit(node: Any, node_schema: Mapping[str, Any], path: str, parent: Mapping[str, Any] | None = None, field: str = "") -> Any:
        effective = _effective_schema(node_schema, node, root_schema)

        # Providers frequently use JSON null for an omitted object/array even
        # when the schema requires an actual container.  Existing normalizers
        # already interpret these values as empty via ``or {}`` / ``or []``.
        # Make that behavior explicit and auditable before any business code
        # dereferences the value.  Schemas that explicitly allow null are left
        # unchanged.
        if node is None:
            declared = effective.get("type")
            declared_types = [declared] if isinstance(declared, str) else list(declared or [])
            if "null" not in declared_types:
                if "object" in declared_types and "array" not in declared_types:
                    record(path, field, None, {}, "NULL_CONTAINER_DEFAULT", "null object normalized to empty object")
                    node = {}
                elif "array" in declared_types and "object" not in declared_types:
                    record(path, field, None, [], "NULL_CONTAINER_DEFAULT", "null array normalized to empty array")
                    node = []

        if isinstance(node, dict):
            props = _property_schemas(effective, node, root_schema)

            # Repair unambiguous field swaps before individual alias handling.
            for left, right in SWAPPABLE_FIELD_PAIRS:
                if left not in node or right not in node or left not in props or right not in props:
                    continue
                left_allowed = _enum_values(props[left])
                right_allowed = _enum_values(props[right])
                if not left_allowed or not right_allowed:
                    continue
                left_own = _canonical_match(node[left], left_allowed)
                right_own = _canonical_match(node[right], right_allowed)
                left_as_right = _canonical_match(node[left], right_allowed)
                right_as_left = _canonical_match(node[right], left_allowed)
                if left_own is None and right_own is None and left_as_right is not None and right_as_left is not None:
                    before_left, before_right = node[left], node[right]
                    node[left], node[right] = right_as_left, left_as_right
                    record(f"{path}/{left}", left, before_left, node[left], "FIELD_SWAP", f"swapped with sibling {right}")
                    record(f"{path}/{right}", right, before_right, node[right], "FIELD_SWAP", f"swapped with sibling {left}")

            for key in list(node.keys()):
                child_schema = props.get(key)
                if child_schema is None:
                    additional = effective.get("additionalProperties")
                    child_schema = additional if isinstance(additional, Mapping) else {}
                node[key] = visit(node[key], child_schema, f"{path}/{key}", node, key)
            return node

        if isinstance(node, list):
            item_schema = effective.get("items") if isinstance(effective.get("items"), Mapping) else {}
            for index, item in enumerate(node):
                node[index] = visit(item, item_schema, f"{path}/{index}", parent, field)
            return node

        allowed = _enum_values(effective)
        if not allowed:
            return node
        canonical = _canonical_match(node, allowed)
        if canonical is not None:
            if canonical != node:
                record(path, field, node, canonical, "CANONICAL_FORMAT", "case/spacing/hyphen canonicalization")
            return canonical

        alias, reason = _choose_alias(field, node, allowed, parent or {})
        if alias is not None:
            before = node
            node = alias
            record(path, field, before, alias, "REGISTERED_ALIAS", reason or "registered alias")

            # Direction-aware cross-stage relation conversion.
            token = _token(before)
            if field in {"relation", "relation_type"} and isinstance(parent, dict):
                if (token, alias) in {("IMPLEMENTS", "REALIZED_BY"), ("REALIZED_BY", "IMPLEMENTS")}:
                    if "from_id" in parent and "to_id" in parent:
                        old_from, old_to = parent["from_id"], parent["to_id"]
                        parent["from_id"], parent["to_id"] = old_to, old_from
                        record(
                            path.rsplit("/", 1)[0],
                            "from_id/to_id",
                            {"from_id": old_from, "to_id": old_to},
                            {"from_id": parent["from_id"], "to_id": parent["to_id"]},
                            "RELATION_DIRECTION_ADAPTER",
                            f"{token}->{alias} reverses the Stage-3/Stage-4 edge direction",
                        )
            return node

        unresolved.append(
            ContractUnresolved(
                path=path,
                field=field,
                value=node,
                allowed_values=allowed,
                reason="unregistered enum drift; strict schema validation must decide",
            )
        )
        return node

    normalized = visit(normalized, root_schema, "$")
    report = {
        "schema_version": "1.0",
        "normalizer_version": CONTRACT_REGISTRY_VERSION,
        "contract_id": contract_id,
        "normalized_count": len(changes),
        "changes": [asdict(x) for x in changes],
        "unresolved_count": len(unresolved),
        "unresolved": [asdict(x) for x in unresolved],
    }
    return normalized, report


def normalize_registered_enum_aliases_against_schema(
    value: Any,
    schema: Mapping[str, Any],
    *,
    contract_id: str,
    protected_paths: Iterable[str] = (),
) -> tuple[Any, dict[str, Any]]:
    """Normalize only schema-bound enum representation aliases.

    Unlike :func:`normalize_against_schema`, this consumption-boundary helper
    does not default null containers, swap sibling fields, reverse relation
    endpoints, or synthesize any missing content.  It is deliberately limited
    to canonical case/spacing and unambiguous aliases registered for the
    semantic domain of the current field.  Unknown values remain untouched so
    strict schema validation can reject them.
    """

    normalized = copy.deepcopy(value)
    root_schema = copy.deepcopy(dict(schema))
    protected = {str(item).rstrip("/") for item in protected_paths if str(item).strip()}
    changes: list[ContractChange] = []
    unresolved: list[ContractUnresolved] = []

    def record(path: str, field: str, before: Any, after: Any, rule: str, reason: str) -> None:
        changes.append(ContractChange(path, field, before, after, rule, reason))

    immutable_decision_fields = frozenset({"status", "verdict", "findings"})

    def visit(
        node: Any,
        node_schema: Mapping[str, Any],
        path: str,
        parent: Mapping[str, Any] | None = None,
        field: str = "",
    ) -> Any:
        if any(path == root or path.startswith(root + "/") for root in protected):
            return node
        effective = _effective_schema(node_schema, node, root_schema)
        if isinstance(node, dict):
            props = _property_schemas(effective, node, root_schema)
            for key in list(node.keys()):
                if key in immutable_decision_fields:
                    continue
                child_schema = props.get(key)
                if child_schema is None:
                    additional = effective.get("additionalProperties")
                    child_schema = additional if isinstance(additional, Mapping) else {}
                node[key] = visit(node[key], child_schema, f"{path}/{key}", node, key)
            return node
        if isinstance(node, list):
            item_schema = effective.get("items") if isinstance(effective.get("items"), Mapping) else {}
            for index, item in enumerate(node):
                node[index] = visit(item, item_schema, f"{path}/{index}", parent, field)
            return node

        allowed = _enum_values(effective)
        if not allowed:
            return node
        canonical = _canonical_match(node, allowed)
        if canonical is not None:
            if canonical != node:
                record(
                    path,
                    field,
                    node,
                    canonical,
                    "CANONICAL_FORMAT",
                    "case/spacing/hyphen canonicalization",
                )
            return canonical

        alias, reason = _choose_alias(field, node, allowed, parent or {})
        if alias is not None:
            token = _token(node)
            if field in {"relation", "relation_type"} and (token, alias) in {
                ("IMPLEMENTS", "REALIZED_BY"),
                ("REALIZED_BY", "IMPLEMENTS"),
            }:
                unresolved.append(
                    ContractUnresolved(
                        path=path,
                        field=field,
                        value=node,
                        allowed_values=allowed,
                        reason=(
                            "registered relation alias requires endpoint reversal and is "
                            "not safe at the representation-only consumption boundary"
                        ),
                    )
                )
                return node
            record(path, field, node, alias, "REGISTERED_ALIAS", reason or "registered alias")
            return alias

        unresolved.append(
            ContractUnresolved(
                path=path,
                field=field,
                value=node,
                allowed_values=allowed,
                reason="unregistered enum drift; strict schema validation must decide",
            )
        )
        return node

    normalized = visit(normalized, root_schema, "$")
    return normalized, {
        "schema_version": "1.0",
        "normalizer_version": CONTRACT_REGISTRY_VERSION,
        "contract_id": contract_id,
        "normalization_policy": "SCHEMA_BOUND_REGISTERED_ENUMS_ONLY",
        "normalized_count": len(changes),
        "changes": [asdict(item) for item in changes],
        "unresolved_count": len(unresolved),
        "unresolved": [asdict(item) for item in unresolved],
    }


def _json_pointer(path: tuple[Any, ...]) -> str:
    if not path:
        return "/"
    return "/" + "/".join(
        str(token).replace("~", "~0").replace("/", "~1")
        for token in path
    )


def _path_related(left: tuple[Any, ...], right: tuple[Any, ...]) -> bool:
    """Return whether two object paths are on the same ancestor chain.

    Ownership repair deliberately does not move fields between sibling
    business objects.  A misplaced field may be lifted to an ancestor or moved
    into a descendant object, but crossing between siblings is semantically
    ambiguous and must remain a schema error.
    """

    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    return longer[: len(shorter)] == shorter


def _collect_runtime_objects(
    value: Any,
    schema: Mapping[str, Any],
    root_schema: Mapping[str, Any],
    *,
    path: tuple[Any, ...] = (),
) -> list[tuple[tuple[Any, ...], dict[str, Any], dict[str, Any]]]:
    """Collect actual object nodes paired with their effective schemas."""

    effective = _effective_schema(schema, value, root_schema)
    collected: list[tuple[tuple[Any, ...], dict[str, Any], dict[str, Any]]] = []
    if isinstance(value, dict):
        collected.append((path, value, effective))
        properties = effective.get("properties") or {}
        additional = effective.get("additionalProperties")
        for key, child in value.items():
            child_schema = properties.get(key)
            if child_schema is None and isinstance(additional, Mapping):
                child_schema = additional
            if isinstance(child_schema, Mapping):
                collected.extend(
                    _collect_runtime_objects(
                        child,
                        child_schema,
                        root_schema,
                        path=(*path, key),
                    )
                )
    elif isinstance(value, list):
        item_schema = effective.get("items")
        if isinstance(item_schema, Mapping):
            for index, child in enumerate(value):
                collected.extend(
                    _collect_runtime_objects(
                        child,
                        item_schema,
                        root_schema,
                        path=(*path, index),
                    )
                )
    return collected


def _schema_error_count(value: Any, schema: Mapping[str, Any]) -> int:
    validator = Draft202012Validator(
        prepare_schema_contract(schema),
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )
    return sum(1 for _ in validator.iter_errors(value))


def _stable_json_union(*collections: list[Any]) -> list[Any]:
    """Merge JSON-compatible arrays without inventing or rewriting items."""

    merged: list[Any] = []
    seen: set[str] = set()
    for collection in collections:
        for item in collection:
            marker = json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if marker in seen:
                continue
            seen.add(marker)
            merged.append(copy.deepcopy(item))
    return merged


def _schema_declares_object(schema: Mapping[str, Any]) -> bool:
    declared = schema.get("type")
    if declared == "object":
        return True
    if isinstance(declared, list) and "object" in declared:
        return True
    return isinstance(schema.get("properties"), Mapping)


def _field_target_is_safe(
    field: str,
    target_path: tuple[Any, ...],
    target_schema: Mapping[str, Any],
) -> bool:
    """Return whether an ownership target is deterministic enough to repair.

    Required fields are safe because the strict schema independently proves
    that the value is missing at that exact owner.  Optional fields are not
    moved generically: a same-named optional field on an ancestor (for example
    ``source_refs`` or ``status``) may carry different business semantics.
    Such exceptional mirrors are handled by explicit prompt contracts.
    """

    del target_path  # retained in the signature for future registered rules
    return field in set(target_schema.get("required") or [])


def _missing_required_child_move_candidates(
    normalized: dict[str, Any],
    runtime_objects: list[tuple[tuple[Any, ...], dict[str, Any], dict[str, Any]]],
    root_schema: Mapping[str, Any],
    baseline_errors: int,
) -> list[dict[str, Any]]:
    """Return safe one-level container creation candidates.

    Providers sometimes flatten the fields of a required object into its
    parent and omit the wrapper object itself.  Creating an arbitrary container
    would be unsafe, so this adapter requires all of the following:

    * the missing child object is required by its parent;
    * every moved field is currently forbidden at the parent;
    * each field validates against exactly one missing required child object;
    * at least one required child field is present;
    * the complete strict-schema error count decreases after the batch move.
    """

    candidates: list[dict[str, Any]] = []
    for source_path, source_object, source_schema in runtime_objects:
        if source_schema.get("additionalProperties") is not False:
            continue
        source_properties = source_schema.get("properties") or {}
        required_children: dict[str, Mapping[str, Any]] = {}
        for child_name in source_schema.get("required") or []:
            if child_name in source_object:
                continue
            child_schema = source_properties.get(child_name)
            if not isinstance(child_schema, Mapping):
                continue
            effective_child = _effective_schema(child_schema, None, root_schema)
            if _schema_declares_object(effective_child):
                required_children[str(child_name)] = effective_child
        if not required_children:
            continue

        extras = {
            field: source_object[field]
            for field in source_object
            if field not in source_properties
        }
        if not extras:
            continue

        ownership: dict[str, list[str]] = {}
        for field, field_value in extras.items():
            owners: list[str] = []
            for child_name, child_schema in required_children.items():
                field_schema = (child_schema.get("properties") or {}).get(field)
                if not isinstance(field_schema, Mapping):
                    continue
                validator = Draft202012Validator(
                    dict(field_schema),
                    format_checker=Draft202012Validator.FORMAT_CHECKER,
                )
                if not any(validator.iter_errors(field_value)):
                    owners.append(child_name)
            ownership[field] = owners

        for child_name, child_schema in required_children.items():
            child_fields = [
                field
                for field, owners in ownership.items()
                if owners == [child_name]
            ]
            child_required = set(child_schema.get("required") or [])
            if not child_fields or not child_required.intersection(child_fields):
                continue

            trial = copy.deepcopy(normalized)
            source_cursor: Any = trial
            for token in source_path:
                source_cursor = source_cursor[token]
            child_object: dict[str, Any] = {}
            for field in child_fields:
                child_object[field] = source_cursor.pop(field)
            source_cursor[child_name] = child_object
            trial_errors = _schema_error_count(trial, root_schema)
            if trial_errors >= baseline_errors:
                continue
            candidates.append(
                {
                    "field": child_name,
                    "source_path": source_path,
                    "target_path": (*source_path, child_name),
                    "trial": trial,
                    "before_errors": baseline_errors,
                    "after_errors": trial_errors,
                    "distance": 1,
                    "rule": "CREATE_REQUIRED_OBJECT_AND_MOVE_FIELDS",
                    "moved_fields": sorted(child_fields),
                }
            )
    return candidates


def _invalid_misplaced_field_candidates(
    value: Any,
    root_schema: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Report uniquely owned misplaced fields whose value has the wrong shape.

    Ownership repair may move a value only after the value independently
    validates against its unique target schema.  When a forbidden field has one
    deterministic required owner on the same ancestor chain but the value does
    not validate there, silently leaving it for a generic schema error obscures
    the actual protocol fault.  This analyzer reports that condition without
    mutating the provider output.
    """

    runtime_objects = _collect_runtime_objects(value, root_schema, root_schema)
    invalid: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for source_path, source_object, source_schema in runtime_objects:
        source_properties = source_schema.get("properties") or {}
        if source_schema.get("additionalProperties") is not False:
            continue
        for field, field_value in source_object.items():
            if field in source_properties:
                continue
            targets: list[tuple[tuple[Any, ...], Mapping[str, Any], list[str]]] = []
            for target_path, target_object, target_schema in runtime_objects:
                if target_path == source_path or not _path_related(source_path, target_path):
                    continue
                target_field_schema = (target_schema.get("properties") or {}).get(field)
                if not isinstance(target_field_schema, Mapping):
                    continue
                if field in target_object or not _field_target_is_safe(field, target_path, target_schema):
                    continue
                validator = Draft202012Validator(
                    dict(target_field_schema),
                    format_checker=Draft202012Validator.FORMAT_CHECKER,
                )
                field_errors = sorted(
                    (error.message for error in validator.iter_errors(field_value)),
                )
                if field_errors:
                    targets.append((target_path, target_field_schema, field_errors))
            unique_paths = {target_path for target_path, _, _ in targets}
            if len(unique_paths) != 1:
                continue
            target_path, _, field_errors = targets[0]
            key = (_json_pointer(source_path), field, _json_pointer(target_path))
            if key in seen:
                continue
            seen.add(key)
            invalid.append({
                "field": field,
                "source_path": _json_pointer((*source_path, field)),
                "target_path": _json_pointer((*target_path, field)),
                "reason": "field has one required schema owner but its value is invalid for that owner",
                "validation_errors": field_errors,
            })
    return invalid


def synchronize_required_mirrored_arrays(
    value: Any,
    schema: Mapping[str, Any],
    *,
    contract_id: str,
) -> tuple[Any, dict[str, Any]]:
    """Losslessly synchronize direct root/result array mirrors declared by schema.

    A mirror is derived from the schema rather than from a prompt ID: the same
    field must be required at both the root and ``result`` objects, both field
    schemas must be identical arrays, and both runtime values must already be
    arrays.  The adapter only unions existing items in stable order; it never
    creates or rewrites an item.
    """

    # The caller owns an isolated response object.  Avoid a second full-tree
    # copy here; large proposal envelopes otherwise incur quadratic copying
    # before any mirror is even detected.
    normalized = value
    report = {
        "schema_version": "1.0",
        "contract_id": contract_id,
        "synchronized_count": 0,
        "changes": [],
    }
    if not isinstance(normalized, dict):
        return normalized, report

    root_schema = prepare_schema_contract(schema)
    root_properties = root_schema.get("properties") or {}
    result_schema = root_properties.get("result")
    if not isinstance(result_schema, Mapping):
        return normalized, report
    result_schema = prepare_schema_contract(result_schema)
    result_properties = result_schema.get("properties") or {}
    result_value = normalized.get("result")
    if not isinstance(result_value, dict):
        return normalized, report

    root_required = set(root_schema.get("required") or [])
    result_required = set(result_schema.get("required") or [])
    candidates = sorted(root_required & result_required & set(root_properties) & set(result_properties))

    for field in candidates:
        root_field_schema = prepare_schema_contract(root_properties[field])
        result_field_schema = prepare_schema_contract(result_properties[field])
        if root_field_schema != result_field_schema:
            continue
        if root_field_schema.get("type") != "array":
            continue
        root_items = normalized.get(field)
        nested_items = result_value.get(field)
        if not isinstance(root_items, list) or not isinstance(nested_items, list):
            continue

        merged = _stable_json_union(root_items, nested_items)
        if root_items == merged and nested_items == merged:
            continue
        normalized[field] = copy.deepcopy(merged)
        result_value[field] = copy.deepcopy(merged)
        report["changes"].append({
            "field": field,
            "root_path": f"/{field}",
            "result_path": f"/result/{field}",
            "root_count": len(root_items),
            "result_count": len(nested_items),
            "merged_count": len(merged),
        })

    report["synchronized_count"] = len(report["changes"])
    return normalized, report


def repair_field_ownership_against_schema(
    value: Any,
    schema: Mapping[str, Any],
    *,
    contract_id: str,
    max_moves: int = 32,
) -> tuple[Any, dict[str, Any]]:
    """Repair deterministic object-level field ownership drift.

    Large language models commonly emit a valid field under an adjacent parent
    or child object.  This repair is intentionally conservative:

    * the field must be forbidden at its current object;
    * a same-named required target property must exist on the same ancestor chain;
    * the target property must currently be absent;
    * the value must independently validate against the target property schema;
    * exactly one best move must reduce the complete output-schema error count.

    It can also recreate one missing *required* child object when the provider
    flattened uniquely owned child fields into the parent.  The function never
    creates business content, overwrites an existing value, moves optional
    same-named fields generically, or crosses between sibling business objects.
    Ambiguous cases remain untouched and are reported for strict validation to
    block.
    """

    # Ownership analysis is non-mutating until a candidate move is selected,
    # and every selected move is applied to its own trial copy.  Re-copying the
    # complete response and the inlined schema on every prompt makes concurrent
    # full-proposal runs scale quadratically without adding isolation.
    normalized = value
    root_schema = schema
    changes: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    unresolved_keys: set[tuple[str, str, tuple[str, ...]]] = set()

    if not isinstance(normalized, dict):
        return normalized, {
            "schema_version": "1.1",
            "normalizer_version": CONTRACT_REGISTRY_VERSION,
            "contract_id": contract_id,
            "normalized_count": 0,
            "changes": [],
            "unresolved_count": 0,
            "unresolved": [],
            "invalid_misplacement_count": 0,
            "invalid_misplacements": [],
        }

    for _ in range(max_moves):
        baseline_errors = _schema_error_count(normalized, root_schema)
        runtime_objects = _collect_runtime_objects(normalized, root_schema, root_schema)
        candidate_moves: list[dict[str, Any]] = _missing_required_child_move_candidates(
            normalized,
            runtime_objects,
            root_schema,
            baseline_errors,
        )

        for source_path, source_object, source_schema in runtime_objects:
            source_properties = source_schema.get("properties") or {}
            if source_schema.get("additionalProperties") is not False:
                continue
            for field in list(source_object.keys()):
                if field in source_properties:
                    continue
                source_value = source_object[field]
                matching_targets: list[dict[str, Any]] = []
                for target_path, target_object, target_schema in runtime_objects:
                    if target_path == source_path or not _path_related(source_path, target_path):
                        continue
                    target_properties = target_schema.get("properties") or {}
                    target_field_schema = target_properties.get(field)
                    if not isinstance(target_field_schema, Mapping):
                        continue
                    if not _field_target_is_safe(field, target_path, target_schema):
                        continue
                    field_validator = Draft202012Validator(
                        dict(target_field_schema),
                        format_checker=Draft202012Validator.FORMAT_CHECKER,
                    )

                    target_has_field = field in target_object
                    if target_has_field:
                        target_value = target_object[field]
                        if isinstance(source_value, list) and isinstance(target_value, list):
                            replacement_value = _stable_json_union(target_value, source_value)
                            rule = "SCHEMA_FIELD_OWNERSHIP_ARRAY_MERGE"
                        elif source_value == target_value:
                            replacement_value = copy.deepcopy(target_value)
                            rule = "SCHEMA_FIELD_OWNERSHIP_DUPLICATE_REMOVAL"
                        else:
                            continue
                    else:
                        replacement_value = copy.deepcopy(source_value)
                        rule = "SCHEMA_FIELD_OWNERSHIP_MOVE"

                    if any(field_validator.iter_errors(replacement_value)):
                        continue

                    trial = copy.deepcopy(normalized)
                    source_cursor: Any = trial
                    for token in source_path:
                        source_cursor = source_cursor[token]
                    target_cursor: Any = trial
                    for token in target_path:
                        target_cursor = target_cursor[token]
                    source_cursor.pop(field)
                    target_cursor[field] = replacement_value
                    trial_errors = _schema_error_count(trial, root_schema)
                    if trial_errors >= baseline_errors:
                        continue
                    matching_targets.append(
                        {
                            "field": field,
                            "source_path": source_path,
                            "target_path": target_path,
                            "trial": trial,
                            "before_errors": baseline_errors,
                            "after_errors": trial_errors,
                            "distance": abs(len(source_path) - len(target_path)),
                            "rule": rule,
                            "moved_fields": [field],
                        }
                    )

                if not matching_targets:
                    continue
                matching_targets.sort(
                    key=lambda item: (
                        item["after_errors"],
                        item["distance"],
                        _json_pointer(item["target_path"]),
                    )
                )
                best_score = (
                    matching_targets[0]["after_errors"],
                    matching_targets[0]["distance"],
                )
                best = [
                    item
                    for item in matching_targets
                    if (item["after_errors"], item["distance"]) == best_score
                ]
                if len(best) == 1:
                    candidate_moves.append(best[0])
                else:
                    target_paths = tuple(
                        sorted(_json_pointer(item["target_path"]) for item in best)
                    )
                    unresolved_key = (_json_pointer(source_path), field, target_paths)
                    if unresolved_key not in unresolved_keys:
                        unresolved_keys.add(unresolved_key)
                        unresolved.append(
                            {
                                "field": field,
                                "source_path": _json_pointer(source_path),
                                "candidate_target_paths": list(target_paths),
                                "reason": "ambiguous field ownership; strict schema validation must decide",
                            }
                        )

        if not candidate_moves:
            break
        candidate_moves.sort(
            key=lambda item: (
                item["after_errors"],
                item["distance"],
                _json_pointer(item["source_path"]),
                item["field"],
            )
        )
        selected = candidate_moves[0]
        normalized = selected["trial"]
        changes.append(
            {
                "field": selected["field"],
                "source_path": _json_pointer(selected["source_path"]),
                "target_path": _json_pointer(selected["target_path"]),
                "moved_fields": list(selected.get("moved_fields") or [selected["field"]]),
                "rule": selected.get("rule") or "SCHEMA_FIELD_OWNERSHIP_MOVE",
                "reason": "unique schema-owned target reduced strict schema errors",
                "schema_errors_before": selected["before_errors"],
                "schema_errors_after": selected["after_errors"],
            }
        )

    invalid_misplacements = _invalid_misplaced_field_candidates(normalized, root_schema)
    return normalized, {
        "schema_version": "1.1",
        "normalizer_version": CONTRACT_REGISTRY_VERSION,
        "contract_id": contract_id,
        "normalized_count": len(changes),
        "changes": changes,
        "unresolved_count": len(unresolved),
        "unresolved": unresolved,
        "invalid_misplacement_count": len(invalid_misplacements),
        "invalid_misplacements": invalid_misplacements,
    }


def field_ownership_contract_lines(schema: Mapping[str, Any]) -> list[str]:
    """Return compact direct-child ownership rules generated from a schema."""

    root_schema = dict(schema)
    rows: list[str] = []

    def walk(node_schema: Mapping[str, Any], path: str, depth: int) -> None:
        effective = _effective_schema(node_schema, None, root_schema)
        properties = effective.get("properties") or {}
        if properties and effective.get("additionalProperties") is False and depth <= 3:
            required = set(effective.get("required") or [])
            ordered = [
                f"{name}{'*' if name in required else ''}"
                for name in properties
            ]
            rows.append(
                f"- `{path}` 的直接字段仅允许：" + " / ".join(ordered)
            )
        for key, child in properties.items():
            if isinstance(child, Mapping):
                walk(child, f"{path}.{key}", depth + 1)
        items = effective.get("items")
        if isinstance(items, Mapping):
            walk(items, f"{path}[*]", depth + 1)

    walk(root_schema, "$", 0)
    return rows


FIELD_OWNERSHIP_CONTRACT_START = "<!-- FIELD_OWNERSHIP_CONTRACT:START -->"
FIELD_OWNERSHIP_CONTRACT_END = "<!-- FIELD_OWNERSHIP_CONTRACT:END -->"


def augment_prompt_with_field_ownership_contract(
    prompt: str,
    schema: Mapping[str, Any],
    *,
    contract_id: str,
) -> str:
    """Append an idempotent, schema-generated object ownership contract."""

    rows = field_ownership_contract_lines(schema)
    if not rows:
        return prompt
    base = prompt
    if FIELD_OWNERSHIP_CONTRACT_START in base:
        base = base.split(FIELD_OWNERSHIP_CONTRACT_START, 1)[0].rstrip()
    block = (
        FIELD_OWNERSHIP_CONTRACT_START
        + "\n# 自动生成的字段归属契约\n"
        + f"契约ID：`{contract_id}`；注册表版本：`{CONTRACT_REGISTRY_VERSION}`。\n"
        + "星号表示必填字段。字段只能出现在列出的直接父对象下，不得上移、下沉或放入相邻对象。\n"
        + "\n".join(rows)
        + "\n"
        + FIELD_OWNERSHIP_CONTRACT_END
    )
    return base + "\n\n" + block + "\n"


def reference_integrity_contract_lines(schema: Mapping[str, Any]) -> list[str]:
    """Return schema paths grouped by their registered reference semantics."""

    root_schema = prepare_schema_contract(schema)
    definitions: set[str] = set()
    references: set[str] = set()
    entity_or_field_paths: set[str] = set()

    def walk(node_schema: Mapping[str, Any], path: str) -> None:
        effective = _effective_schema(node_schema, None, root_schema)
        semantic = str(effective.get("x-reference-semantic") or "")
        if semantic == "NEW_ENTITY_ID":
            definitions.add(path)
        elif semantic in {"ENTITY_REF", "SOURCE_REF", "FINDING_REF"}:
            references.add(path)
        elif semantic == "ENTITY_OR_FIELD_PATH":
            entity_or_field_paths.add(path)
        for key, child in (effective.get("properties") or {}).items():
            if isinstance(child, Mapping):
                walk(child, f"{path}.{key}")
        items = effective.get("items")
        if isinstance(items, Mapping):
            walk(items, f"{path}[*]")

    walk(root_schema, "$")
    rows: list[str] = []
    if definitions:
        rows.append("- 本次输出中可定义实体ID的路径：" + " / ".join(f"`{item}`" for item in sorted(definitions)))
    if references:
        rows.append("- 必须解析为真实实体的引用路径：" + " / ".join(f"`{item}`" for item in sorted(references)))
    if entity_or_field_paths:
        rows.append(
            "- 只允许实体ID或实体锚定字段路径的引用路径："
            + " / ".join(f"`{item}`" for item in sorted(entity_or_field_paths))
        )
    return rows


REFERENCE_INTEGRITY_CONTRACT_START = "<!-- REFERENCE_INTEGRITY_CONTRACT:START -->"
REFERENCE_INTEGRITY_CONTRACT_END = "<!-- REFERENCE_INTEGRITY_CONTRACT:END -->"


def augment_prompt_with_reference_integrity_contract(
    prompt: str,
    schema: Mapping[str, Any],
    *,
    contract_id: str,
) -> str:
    """Append the model-facing form of the runtime reference validator."""

    rows = reference_integrity_contract_lines(schema)
    if not rows:
        return prompt
    base = prompt
    if REFERENCE_INTEGRITY_CONTRACT_START in base:
        base = base.split(REFERENCE_INTEGRITY_CONTRACT_START, 1)[0].rstrip()
    block = (
        REFERENCE_INTEGRITY_CONTRACT_START
        + "\n# 自动生成的引用完整性契约\n"
        + f"契约ID：`{contract_id}`；注册表版本：`{CONTRACT_REGISTRY_VERSION}`。\n"
        + "每一个引用ID必须逐字等于输入中可见的真实实体ID，或等于本次输出中实际定义的实体ID；"
          "仅声明一个引用不能定义实体。禁止悬空引用、占位ID、只生成引用而漏掉实体，也不得通过删除引用掩盖应有的来源关系。\n"
        + "引用数组的每个字符串只能包含纯ID或契约允许的实体锚定字段路径；禁止在同一字符串中追加括号说明、引号、冒号说明、来源类型、数组位置、Hash或解释文字。"
          "需要说明证据含义时写入Finding的description或repair_instruction，不得污染evidence_refs等引用字段。\n"
        + "命名空间必须分离：根级trusted_source_catalog中的source_id只服务于source_refs来源绑定；"
          "尤其禁止把input-*合成目录ID写入evidence_refs或其他实体引用字段。"
          "ENTITY_OR_FIELD_PATH应引用输入对象内部真实定义的candidate_id、paragraph_id等自然实体ID，"
          "或引用语义契约登记的顶层输入对象名（例如polished_candidate）；不得引用目录容器ID。\n"
        + "\n".join(rows)
        + "\n"
        + REFERENCE_INTEGRITY_CONTRACT_END
    )
    return base + "\n\n" + block + "\n"


def enum_contract_lines(schema: Mapping[str, Any]) -> list[str]:
    """Return compact JSON-path enum instructions generated from a schema."""
    root_schema = dict(schema)
    rows: list[str] = []

    def walk(node_schema: Mapping[str, Any], path: str) -> None:
        effective = _effective_schema(node_schema, None, root_schema)
        allowed = _enum_values(effective)
        if allowed:
            rows.append(f"- `{path}` 仅允许：" + " / ".join(str(x) for x in allowed))
        for key, child in (effective.get("properties") or {}).items():
            if isinstance(child, Mapping):
                walk(child, f"{path}.{key}")
        items = effective.get("items")
        if isinstance(items, Mapping):
            walk(items, f"{path}[*]")

    walk(root_schema, "$")
    return rows


ENUM_CONTRACT_START = "<!-- UNIFIED_ENUM_CONTRACT:START -->"
ENUM_CONTRACT_END = "<!-- UNIFIED_ENUM_CONTRACT:END -->"


def augment_prompt_with_enum_contract(prompt: str, schema: Mapping[str, Any], *, contract_id: str) -> str:
    """Append one generated enum contract, replacing any older generated copy.

    Request artifacts may be resumed and rewritten.  Marker-based replacement
    keeps prompt injection idempotent and avoids accumulating stale contracts.
    """
    rows = enum_contract_lines(schema)
    if not rows:
        return prompt
    base = prompt
    if ENUM_CONTRACT_START in base:
        base = base.split(ENUM_CONTRACT_START, 1)[0].rstrip()
    block = (
        ENUM_CONTRACT_START
        + "\n# 自动生成的枚举契约（唯一合法词表）\n"
        + f"契约ID：`{contract_id}`；注册表版本：`{CONTRACT_REGISTRY_VERSION}`。\n"
        + "必须逐路径使用下列值，不得创造近义枚举，也不得把一个字段的值放入另一字段：\n"
        + "\n".join(rows)
        + "\n"
        + ENUM_CONTRACT_END
    )
    return base + "\n\n" + block + "\n"


def report_warning(report: Mapping[str, Any], *, limit: int = 12) -> str | None:
    changes = list(report.get("changes") or [])
    if not changes:
        return None
    pieces = [
        f"{item.get('path')}: {item.get('original_value')}->{item.get('canonical_value')}"
        for item in changes[:limit]
    ]
    if len(changes) > limit:
        pieces.append(f"另有{len(changes)-limit}项详见Trace")
    return "SYSTEM_CONTRACT_NORMALIZATION[v%s]: %s" % (
        report.get("normalizer_version", CONTRACT_REGISTRY_VERSION),
        "; ".join(pieces),
    )


def dump_registry() -> dict[str, Any]:
    return {
        "registry_version": CONTRACT_REGISTRY_VERSION,
        "canonical_enums": {k: list(v) for k, v in CANONICAL_ENUMS.items()},
        "field_alias_candidates": {
            field: {alias: list(targets) for alias, targets in aliases.items()}
            for field, aliases in FIELD_ALIAS_CANDIDATES.items()
        },
        "enum_domains": {name: list(values) for name, values in ENUM_DOMAINS.items()},
        "field_enum_domains": dict(FIELD_ENUM_DOMAINS),
        "domain_alias_candidates": {
            domain: {alias: list(targets) for alias, targets in aliases.items()}
            for domain, aliases in DOMAIN_ALIAS_CANDIDATES.items()
        },
        "swappable_field_pairs": [list(x) for x in SWAPPABLE_FIELD_PAIRS],
    }


def registry_json() -> str:
    return json.dumps(dump_registry(), ensure_ascii=False, indent=2, sort_keys=True)
