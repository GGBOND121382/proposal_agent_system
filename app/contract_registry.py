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

from .status_ontology import (
    CANONICAL_CLAIM_TYPES,
    CANONICAL_KNOWLEDGE_STATUSES,
    CANONICAL_TEMPORAL_STATUSES,
    normalize_claim_type,
    normalize_knowledge_status,
    normalize_temporal_status,
)

CONTRACT_REGISTRY_VERSION = "3.0.0"

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
        return max(
            candidates,
            key=lambda branch: (
                len(keys & set((_resolve_local_ref(branch, root_schema).get("properties") or {}).keys())),
                len(set(_resolve_local_ref(branch, root_schema).get("required") or []) & keys),
            ),
        )
    if isinstance(value, list):
        for branch in candidates:
            if _resolve_local_ref(branch, root_schema).get("type") == "array":
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
        "swappable_field_pairs": [list(x) for x in SWAPPABLE_FIELD_PAIRS],
    }


def registry_json() -> str:
    return json.dumps(dump_registry(), ensure_ascii=False, indent=2, sort_keys=True)
