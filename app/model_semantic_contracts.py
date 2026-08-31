from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Iterable

from jsonschema import Draft202012Validator

from .contracts.semantic_contract import get_semantic_contract
from .gate_answer_contract import semantic_question_answer_schema
from .json_pointer import (
    JsonPointerError,
    format_pointer,
    is_ancestor_or_same,
    parse_pointer,
    resolve_pointer,
)


SEMANTIC_MODEL_CONTRACT_VERSION = "2026-08-31.v14-wf4-reference-graph"
SEMANTIC_PROMPTS = frozenset({
    "P-ARGUMENT-ARCHITECTURE",
    "P-ARGUMENT-ARCHITECTURE-CRITIC",
    "P-TARGETED-REPAIR",
    "P-SAFE-ONLINE-PACKAGE",
    "P-SAFE-ONLINE-PACKAGE-CRITIC",
    "P-PUBLIC-RESEARCH-PLAN",
    "P-PUBLIC-RESEARCH-PLAN-SCOPE-CRITIC",
    "P-PUBLIC-RESEARCH-SYNTHESIS",
    "P-PUBLIC-RESEARCH-CRITIC",
    "P-ONLINE-RESULT-IMPORT-CRITIC",
})

_ARGUMENT_CHAIN_RULE_ID = "SC-ARGUMENT-DETERMINISTIC-CHAINS"
_ARGUMENT_MATRIX_RULE_ID = "SC-ARGUMENT-DESIGN-MATRIX-COMPLETENESS"
_ARGUMENT_EVIDENCE_RULE_ID = "SC-ARGUMENT-EVIDENCE-REQUIREMENTS"
_ARGUMENT_STRUCTURAL_RULE_ID = "SC-ARGUMENT-STRUCTURAL-REQUIREMENTS"
_ARGUMENT_CRITIC_TAXONOMY_RULE_ID = "SC-ARGUMENT-CRITIC-ISSUE-TAXONOMY"
_ARGUMENT_DEFECT_RULE_ID = "SC-ARGUMENT-DETERMINISTIC-DEFECTS"
_ARGUMENT_STATE_OWNERSHIP_RULE_ID = "SC-ARGUMENT-STATE-OWNERSHIP"
_ARGUMENT_REPAIR_POLICY_RULE_ID = "SC-ARGUMENT-TARGETED-REPAIR-POLICY"


def _argument_rule_config(rule_id: str) -> dict[str, Any]:
    return dict(get_semantic_contract().rule(rule_id).config)


def _argument_chain_specs() -> tuple[dict[str, Any], ...]:
    raw = _argument_rule_config(_ARGUMENT_CHAIN_RULE_ID).get("chains") or ()
    return tuple(dict(item) for item in raw if isinstance(item, Mapping))


def _argument_matrix_required_fields() -> tuple[str, ...]:
    raw = _argument_rule_config(_ARGUMENT_MATRIX_RULE_ID).get("required_fields") or ()
    return tuple(str(item) for item in raw if str(item).strip())


def argument_evidence_binding_specs(stage: str | None = None) -> tuple[dict[str, Any], ...]:
    """Return the authoritative Stage-wire -> canonical evidence binding registry.

    Evidence reference validation, deterministic invalid-reference removal and
    wire/canonical closure tests all consume this one registry.  A model selects
    evidence semantically; Runtime owns reference-domain identity and validity.
    """
    raw = _argument_rule_config(_ARGUMENT_EVIDENCE_RULE_ID).get("bindings") or ()
    specs = tuple(dict(item) for item in raw if isinstance(item, Mapping))
    if stage is None:
        return specs
    stage_name = str(stage or "").upper()
    return tuple(
        item for item in specs
        if str(item.get("stage") or "").upper() == stage_name
    )


def _argument_matrix_optional_fields() -> tuple[str, ...]:
    raw = _argument_rule_config(_ARGUMENT_MATRIX_RULE_ID).get("optional_fields") or ()
    return tuple(str(item) for item in raw if str(item).strip())


def _argument_matrix_covered_fields() -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            (*_argument_matrix_required_fields(), *_argument_matrix_optional_fields())
        )
    )


def _argument_evidence_config() -> dict[str, Any]:
    return _argument_rule_config(_ARGUMENT_EVIDENCE_RULE_ID)


def _argument_evidence_requirements() -> tuple[dict[str, Any], ...]:
    raw = _argument_evidence_config().get("requirements") or ()
    return tuple(dict(item) for item in raw if isinstance(item, Mapping))


def _argument_structural_requirements() -> tuple[dict[str, Any], ...]:
    raw = _argument_rule_config(_ARGUMENT_STRUCTURAL_RULE_ID).get("requirements") or ()
    return tuple(dict(item) for item in raw if isinstance(item, Mapping))


def _argument_graph_ownership_config() -> dict[str, Any]:
    raw = _argument_rule_config(_ARGUMENT_STRUCTURAL_RULE_ID).get("graph_ownership") or {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def _argument_critic_taxonomy() -> dict[str, Any]:
    return _argument_rule_config(_ARGUMENT_CRITIC_TAXONOMY_RULE_ID)


def _critic_dimension_issue_codes() -> dict[str, set[str]]:
    dimensions = dict(_argument_critic_taxonomy().get("dimensions") or {})
    return {
        str(dimension): {
            str(code)
            for code in dict(config).get("issue_codes") or ()
            if str(code).strip()
        }
        for dimension, config in dimensions.items()
        if isinstance(config, Mapping)
    }


def _critic_component_by_node_type() -> dict[str, str]:
    return {
        str(node_type): str(component)
        for node_type, component in dict(
            _argument_critic_taxonomy().get("component_by_node_type") or {}
        ).items()
        if str(node_type).strip() and str(component).strip()
    }


def _critic_matrix_field_by_component() -> dict[str, str]:
    return {
        str(component): str(field)
        for component, field in dict(
            _argument_critic_taxonomy().get("matrix_field_by_component") or {}
        ).items()
        if str(component).strip() and str(field).strip()
    }


def _critic_component_for_matrix_field(field: str) -> str | None:
    target = str(field)
    return next(
        (component for component, matrix_field in _critic_matrix_field_by_component().items() if matrix_field == target),
        None,
    )


def _required_node_type_for_component(component: str) -> str:
    field = _critic_matrix_field_by_component().get(str(component))
    return str(_repair_reference_node_types().get(str(field or "")) or "EVIDENCE")


def _reference_type_members(reference_type: str) -> set[str]:
    configured = dict(_argument_critic_taxonomy().get("reference_type_members") or {})
    members = configured.get(str(reference_type))
    if members:
        return {str(value) for value in members if str(value).strip()}
    return {str(reference_type)} if str(reference_type).strip() else set()


def _reference_type_for_matrix_field(field: str) -> str | None:
    if str(field) == "research_question_id":
        return "RESEARCH_QUESTION"
    value = _repair_reference_node_types().get(str(field))
    return str(value) if value else None


def _node_matches_reference_type(actual_node_type: str, expected_node_type: str) -> bool:
    return str(actual_node_type) in _reference_type_members(str(expected_node_type))


def _critic_precise_target_components() -> set[str]:
    return {
        str(value)
        for value in _argument_critic_taxonomy().get("precise_target_components") or ()
        if str(value).strip()
    }


def _critic_allowed_target_components() -> dict[str, set[str]]:
    return {
        str(code): {str(value) for value in values or () if str(value).strip()}
        for code, values in dict(
            _argument_critic_taxonomy().get("allowed_target_components_by_code") or {}
        ).items()
    }


def _critic_deterministic_failure_score() -> int:
    return int(_argument_critic_taxonomy().get("deterministic_failure_score") or 1)


def _critic_semantic_failure_max_score() -> float:
    try:
        return float(_argument_critic_taxonomy().get("semantic_failure_max_score") or 2)
    except (TypeError, ValueError):
        return 2.0


def _critic_semantic_issue_policy(code: str) -> dict[str, Any]:
    policies = dict(_argument_critic_taxonomy().get("semantic_issue_policy_by_code") or {})
    raw = policies.get(str(code)) or {}
    if not isinstance(raw, Mapping):
        raw = {}
    route = str(raw.get("route") or "ORIGINAL_PRODUCER").upper()
    if route not in {"ARGUMENT_ARCHITECTURE_AGENT", "ORIGINAL_PRODUCER", "USER", "BLOCK"}:
        route = "ORIGINAL_PRODUCER"
    severity = str(raw.get("severity") or "P1").upper()
    if severity not in {"P0", "P1", "P2", "P3"}:
        severity = "P1"
    return {
        "severity": severity,
        "route": route,
        "blocking": bool(raw.get("blocking", True)),
        "repairable": bool(raw.get("repairable", route == "ARGUMENT_ARCHITECTURE_AGENT")),
    }


def _critic_dimension_for_issue(code: str, component: str | None) -> str:
    candidates = [
        dimension
        for dimension, codes in _critic_dimension_issue_codes().items()
        if str(code) in codes
    ]
    if not candidates:
        return "ARGUMENT_CHAIN"
    if len(candidates) == 1:
        return candidates[0]
    semantic_component = str(component or "").upper()
    if (
        semantic_component in {"CENTRAL_PROPOSITION", "SCOPE"}
        and "CENTRAL_THESIS" in candidates
    ):
        return "CENTRAL_THESIS"
    if (
        semantic_component in {"METHOD", "ASSUMPTION", "THEORETICAL_PROPERTY"}
        and "METHOD_SUBSTANCE" in candidates
    ):
        return "METHOD_SUBSTANCE"
    if "ARGUMENT_CHAIN" in candidates:
        return "ARGUMENT_CHAIN"
    return candidates[0]


def _critic_issue_needs_user_input(issue: Mapping[str, Any]) -> bool:
    # `resolution=USER_INPUT` is retained only as a backwards-compatible model
    # hint.  Canonical routing is Runtime-owned.
    return bool(issue.get("needs_user_input")) or str(issue.get("resolution") or "").upper() == "USER_INPUT"


def _critic_issue_requires_structure_change(issue: Mapping[str, Any]) -> bool:
    # `resolution=REGENERATE` is retained only for old replay/model outputs.
    return bool(issue.get("requires_structure_change")) or str(issue.get("resolution") or "").upper() == "REGENERATE"


def _producer_gap_kind_policy(kind: str) -> dict[str, Any]:
    policies = dict(_argument_critic_taxonomy().get("producer_gap_kind_policy") or {})
    raw = policies.get(str(kind)) or policies.get("OTHER") or {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def _question_target_paths() -> dict[str, str]:
    return {
        str(area): str(path)
        for area, path in dict(
            _argument_critic_taxonomy().get("question_target_path_by_area") or {}
        ).items()
        if str(area).strip() and str(path).strip()
    }


def _revision_component_for_code(code: str) -> str | None:
    mapping = dict(
        _argument_critic_taxonomy().get("revision_component_by_code") or {}
    )
    value = mapping.get(str(code))
    return str(value) if value is not None else None


def _argument_defect_families() -> dict[str, dict[str, Any]]:
    raw = _argument_rule_config(_ARGUMENT_DEFECT_RULE_ID).get("families") or {}
    return {
        str(family_id): dict(config)
        for family_id, config in dict(raw).items()
        if isinstance(config, Mapping)
    }


def _argument_defect_policy(family_id: str) -> dict[str, Any]:
    families = _argument_defect_families()
    try:
        return families[str(family_id)]
    except KeyError as exc:
        raise ValueError(f"unknown deterministic Argument defect family: {family_id}") from exc


def _argument_state_ownership_policy() -> dict[str, Any]:
    return _argument_rule_config(_ARGUMENT_STATE_OWNERSHIP_RULE_ID)


def _argument_authoritative_root() -> str:
    root = str(
        _argument_state_ownership_policy().get("authoritative_root")
        or "result/authored_state"
    ).strip("/")
    return "/" + root


def _argument_projection_version() -> str:
    return str(
        _argument_state_ownership_policy().get("projection_version")
        or "ARGUMENT_PROJECTOR_V2"
    )


def _argument_repair_policy() -> dict[str, Any]:
    return _argument_rule_config(_ARGUMENT_REPAIR_POLICY_RULE_ID)


def _argument_repair_authoritative_root() -> str:
    root = str(
        _argument_repair_policy().get("authoritative_root") or "/authored_state"
    ).strip()
    if not root.startswith("/"):
        root = "/" + root
    return root.rstrip("/") or "/authored_state"


def _repair_reference_node_types() -> dict[str, str]:
    return {
        str(field): str(node_type)
        for field, node_type in dict(
            _argument_repair_policy().get("reference_node_types") or {}
        ).items()
    }


def _iter_semantic_pattern_values(
    root: Any,
    pattern: str,
) -> Iterable[tuple[Any, tuple[int, ...], Any]]:
    parts = tuple(part for part in str(pattern).split("/") if part)

    def visit(value: Any, index: int, captures: tuple[int, ...], parent: Any):
        if index >= len(parts):
            yield value, captures, parent
            return
        part = parts[index]
        if part == "*":
            if isinstance(value, list):
                for item_index, child in enumerate(value):
                    yield from visit(child, index + 1, captures + (item_index,), value)
            return
        if isinstance(value, dict) and part in value:
            yield from visit(value[part], index + 1, captures, value)

    yield from visit(root, 0, (), None)


def _semantic_pattern_value_for_captures(
    root: Any, pattern: str, captures: tuple[int, ...]
) -> Any:
    parts = tuple(part for part in str(pattern).split("/") if part)
    capture_index = 0
    value = root
    for part in parts:
        if part == "*":
            if capture_index >= len(captures) or not isinstance(value, list):
                return None
            item_index = captures[capture_index]
            capture_index += 1
            if not 0 <= item_index < len(value):
                return None
            value = value[item_index]
            continue
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _semantic_thread_from_pattern(pattern: str, captures: tuple[int, ...]) -> int | None:
    parts = tuple(part for part in str(pattern).split("/") if part)
    if len(parts) >= 2 and parts[0] == "research_threads" and parts[1] == "*" and captures:
        return captures[0]
    return None


def _semantic_thread_from_tree(
    root: dict[str, Any], pattern: str, captures: tuple[int, ...]
) -> int | None:
    captured = _semantic_thread_from_pattern(pattern, captures)
    if captured is None:
        return None
    thread = _semantic_pattern_value_for_captures(root, "research_threads/*", captures)
    if isinstance(thread, dict) and isinstance(thread.get("_thread_index"), int):
        return int(thread["_thread_index"])
    return captured


def _argument_defect_key(
    rule_key: str,
    thread_index: int | None,
    semantic_object_key: str,
    *,
    defect_family: str | None = None,
) -> str:
    thread_key = str(thread_index) if isinstance(thread_index, int) else "GLOBAL"
    object_key = str(semantic_object_key or "UNSCOPED")
    family_key = str(defect_family or "MODEL")
    return f"ARGUMENT:{str(rule_key)}:{family_key}:{thread_key}:{object_key}"


def _stable_receipt_id(*parts: Any) -> str:
    material = "\x1f".join(str(part or "") for part in parts)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:20].upper()
    return f"DR-ARG-{digest}"

def supports_semantic_model_contract(prompt_id: str) -> bool:
    return prompt_id in SEMANTIC_PROMPTS


def _compact_strings(value: Any, *, limit: int = 12) -> list[str]:
    result: list[str] = []
    def visit(node: Any) -> None:
        if len(result) >= limit:
            return
        if isinstance(node, str):
            text = node.strip()
            if text and text not in result:
                result.append(text)
            return
        if isinstance(node, list):
            for item in node:
                visit(item)
                if len(result) >= limit:
                    return
            return
        if isinstance(node, dict):
            for key, item in node.items():
                if key in {"item_id", "claim_id", "owner_ref", "item_hash", "source_hash", "document_version_id", "security_level", "confidence"}:
                    continue
                visit(item)
                if len(result) >= limit:
                    return
    visit(value)
    return result


def _project_item_statement(item: dict[str, Any]) -> str:
    content = item.get("content")
    parts = _compact_strings(content, limit=10)
    if not parts:
        return str(item.get("item_type") or "项目知识项")
    return "；".join(parts)


def _dedupe_source_refs(refs: Iterable[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in refs:
        if not isinstance(raw, dict):
            continue
        key = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if key in seen:
            continue
        seen.add(key)
        result.append(copy.deepcopy(raw))
    return result


def _semantic_text_key(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^[A-Za-z]+[-_ ]?\d+[：:]\s*", "", text)
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[，。；：！？,:;!?“”\"'（）()\[\]{}]", "", text)
    return text.lower()



def _design_seed(canonical_envelope: dict[str, Any]) -> dict[str, Any] | None:
    payload = canonical_envelope.get("payload") or {}
    seed = payload.get("argument_graph_seed") or {}
    subgraph = payload.get("project_subgraph") or {}
    if not isinstance(seed, dict):
        seed = {}
    if not isinstance(subgraph, dict):
        subgraph = {}

    proposition = seed.get("central_proposition") or {}
    questions = [x for x in seed.get("research_questions") or [] if isinstance(x, dict)]
    scope = seed.get("scope_boundaries") or {}
    nodes = {
        str(x.get("node_id")): x
        for x in seed.get("nodes") or []
        if isinstance(x, dict) and x.get("node_id")
    }
    edges = [x for x in seed.get("edges") or [] if isinstance(x, dict)]
    objectives_by_question: dict[str, str] = {}
    for edge in edges:
        if str(edge.get("relation") or "") != "ADDRESSED_BY":
            continue
        source, target = str(edge.get("source_id") or ""), str(edge.get("target_id") or "")
        target_node = nodes.get(target)
        if source and target_node and str(target_node.get("statement") or "").strip():
            objectives_by_question[source] = str(target_node["statement"]).strip()

    chains: list[dict[str, Any]] = []
    for question in questions:
        question_id = str(question.get("node_id") or "")
        linked_gap_ids = [str(x) for x in question.get("linked_gap_ids") or []]
        gap = next(
            (
                str(nodes[g].get("statement") or "").strip()
                for g in linked_gap_ids
                if g in nodes and str(nodes[g].get("statement") or "").strip()
            ),
            None,
        )
        statement = str(question.get("statement") or "").strip()
        if statement:
            chains.append(
                {
                    "gap": gap,
                    "question": statement,
                    "objective": objectives_by_question.get(question_id),
                }
            )

    useful_types = {
        "PROBLEM",
        "GAP",
        "ROOT_CAUSE",
        "OBJECTIVE",
        "WORK_PACKAGE",
        "METHOD",
        "EXPERIMENT",
        "INNOVATION",
        "CAPABILITY",
        "METRIC",
        "DELIVERABLE",
    }
    components: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for node in seed.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        node_type = str(node.get("node_type") or "")
        if node_type in {"GAP", "RESEARCH_GAP", "OBJECTIVE"}:
            continue
        statement = str(node.get("statement") or "").strip()
        if not statement:
            continue
        key = (node_type, _semantic_text_key(statement))
        if key in seen:
            continue
        seen.add(key)
        components.append(
            {
                "component_type": node_type or "DESIGN_COMPONENT",
                "statement": statement,
                "knowledge_status": str(node.get("status") or "UNKNOWN"),
            }
        )

    subgraph_items: dict[str, dict[str, Any]] = {}
    semantic_item: dict[str, tuple[str, str]] = {}
    for item in subgraph.get("items") or []:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("item_id") or "")
        item_type = str(item.get("item_type") or "")
        if item_id:
            subgraph_items[item_id] = item
        if item_type not in useful_types:
            continue
        statement = _project_item_statement(item).strip()
        if not statement:
            continue
        semantic_item[item_id] = (item_type, statement)
        key = (item_type, _semantic_text_key(statement))
        if key in seen:
            continue
        seen.add(key)
        components.append(
            {
                "component_type": item_type,
                "statement": statement,
                "knowledge_status": str(item.get("knowledge_status") or "UNKNOWN"),
            }
        )

    relations: list[dict[str, str]] = []
    seen_relations: set[tuple[str, str, str]] = set()
    for relation in subgraph.get("relations") or []:
        if not isinstance(relation, dict):
            continue
        if str(relation.get("status") or "").upper() in {"REJECTED", "SUPERSEDED"}:
            continue
        source = semantic_item.get(str(relation.get("source_item_id") or ""))
        target = semantic_item.get(str(relation.get("target_item_id") or ""))
        relation_type = str(relation.get("relation_type") or "").strip()
        if not source or not target or not relation_type:
            continue
        key = (_semantic_text_key(source[1]), relation_type, _semantic_text_key(target[1]))
        if key in seen_relations:
            continue
        seen_relations.add(key)
        relations.append(
            {
                "source_type": source[0],
                "source_statement": source[1],
                "relation": relation_type,
                "target_type": target[0],
                "target_statement": target[1],
            }
        )

    result = {
        "central_proposition": str(proposition.get("statement") or "").strip() or None,
        "boundary_conditions": [
            str(x) for x in proposition.get("boundary_conditions") or [] if str(x).strip()
        ],
        "scope_in": [str(x) for x in scope.get("in_scope") or [] if str(x).strip()],
        "scope_out": [str(x) for x in scope.get("out_of_scope") or [] if str(x).strip()],
        "existing_chains": chains,
        "existing_components": components,
        "existing_relations": relations,
    }
    return result if any(
        [
            result["central_proposition"],
            result["boundary_conditions"],
            result["scope_in"],
            result["scope_out"],
            chains,
            components,
            relations,
        ]
    ) else None



def _design_seed_statements(canonical_envelope: dict[str, Any]) -> list[str]:
    seed = _design_seed(canonical_envelope)
    if not isinstance(seed, dict):
        return []
    result: list[str] = []
    if seed.get("central_proposition"):
        result.append(str(seed["central_proposition"]))
    result.extend(str(x) for x in seed.get("boundary_conditions") or [])
    result.extend(str(x) for x in seed.get("scope_in") or [])
    result.extend(str(x) for x in seed.get("scope_out") or [])
    for chain in seed.get("existing_chains") or []:
        if isinstance(chain, dict):
            for key in ("gap", "question", "objective"):
                value = str(chain.get(key) or "").strip()
                if value:
                    result.append(value)
    for component in seed.get("existing_components") or []:
        if isinstance(component, dict):
            value = str(component.get("statement") or "").strip()
            if value:
                result.append(value)
    for relation in seed.get("existing_relations") or []:
        if isinstance(relation, dict):
            for key in ("source_statement", "target_statement"):
                value = str(relation.get(key) or "").strip()
                if value:
                    result.append(value)
    return result


def _duplicates_design_seed(statement: str, design_statements: Iterable[str]) -> bool:
    key = _semantic_text_key(statement)
    if len(key) < 16: return False
    for known in design_statements:
        kk = _semantic_text_key(known)
        if len(kk) < 16: continue
        if key == kk: return True
        shorter, longer = (key, kk) if len(key) <= len(kk) else (kk, key)
        if len(shorter) >= 24 and shorter in longer: return True
    return False


def _evidence_records(canonical_envelope: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    payload = canonical_envelope.get("payload") or {}
    cards: list[dict[str, Any]] = []
    records: dict[str, dict[str, Any]] = {}
    seen_ids: set[str] = set()
    design_statements = _design_seed_statements(canonical_envelope)
    for claim in payload.get("confirmed_facts") or []:
        if not isinstance(claim, dict): continue
        evidence_id = str(claim.get("claim_id") or "").strip()
        statement = str(claim.get("claim_text") or "").strip()
        if not evidence_id or not statement: continue
        knowledge_status = str(claim.get("knowledge_status") or "UNKNOWN")
        if knowledge_status == "USER_ASSERTED" and _duplicates_design_seed(statement, design_statements):
            continue
        if evidence_id in seen_ids: continue
        seen_ids.add(evidence_id)
        card = {"evidence_id": evidence_id, "evidence_type": str(claim.get("claim_type") or "FACT"), "knowledge_status": knowledge_status, "statement": statement,
                "qualifiers": [str(x) for x in claim.get("qualifiers") or [] if str(x).strip()]}
        cards.append(card)
        records[evidence_id] = {"card": card, "source_refs": _dedupe_source_refs(claim.get("source_refs") or [])}
    return cards, records



def _revision_issues(canonical_envelope: dict[str, Any]) -> list[dict[str, Any]]:
    payload = canonical_envelope.get("payload") or {}
    result: list[dict[str, Any]] = []
    for finding in payload.get("revision_findings") or []:
        if not isinstance(finding, dict):
            continue
        problem = str(
            finding.get("problem")
            or finding.get("description")
            or ""
        ).strip()
        if not problem:
            continue
        required_action = str(
            finding.get("required_action")
            or finding.get("repair_instruction")
            or "根据审查意见修正该语义部件，并保持已有事实与范围约束。"
        ).strip()
        code = str(finding.get("code") or "")
        component = str(
            finding.get("component")
            or finding.get("semantic_component")
            or _revision_component_for_code(code)
            or finding.get("target_type")
            or "RESEARCH_DESIGN"
        ).strip()
        evidence_ids = [
            str(x)
            for x in (
                finding.get("evidence_ids")
                or finding.get("evidence_refs")
                or []
            )
            if str(x).strip()
        ]
        result.append(
            {
                "problem": problem,
                "severity": str(finding.get("severity") or "P2"),
                "code": code or None,
                "defect_key": str(finding.get("defect_key") or "").strip()
                or None,
                "component": component,
                "thread": finding.get("semantic_thread")
                if isinstance(finding.get("semantic_thread"), int)
                else None,
                "review_unit_key": str(finding.get("semantic_review_unit_key") or "").strip()
                or None,
                "required_action": required_action,
                "evidence_ids": list(dict.fromkeys(evidence_ids)),
                "blocking": bool(finding.get("blocking", True)),
                "route": str(
                    finding.get("suggested_route") or "ORIGINAL_PRODUCER"
                ).upper(),
            }
        )
    return result


def _human_resolutions(canonical_envelope: dict[str, Any]) -> list[dict[str, Any]]:
    payload = canonical_envelope.get("payload") or {}
    result = []
    for resolution in payload.get("human_resolutions") or []:
        if not isinstance(resolution, dict):
            continue
        raw_targets = (
            resolution.get("target_paths")
            or resolution.get("resolved_target_paths")
            or []
        )
        targets = [raw_targets] if isinstance(raw_targets, str) else raw_targets
        if "answer" in resolution:
            answer = resolution.get("answer")
        elif "resolved_value" in resolution:
            answer = resolution.get("resolved_value")
        else:
            answer = resolution.get("value")
        question_id = str(resolution.get("question_id") or "").strip()
        question = str(resolution.get("question") or "").strip()
        if not targets and not question_id:
            continue
        item = {
            "target": str(targets[0]) if targets else f"question:{question_id}",
            "answer": copy.deepcopy(answer),
        }
        if question_id:
            item["question_id"] = question_id
        if question:
            item["question"] = question
        if len(targets) > 1:
            item["target_paths"] = [str(target) for target in targets]
        result.append(item)
    return result




_ARGUMENT_STAGE_PRESENTATION_ONLY_TERMS = (
    "字体", "字号", "行距", "页边距", "页码", "版式", "排版", "封面", "目录格式",
    "附件编号", "导出格式", "导出为", "pdf格式", "word格式",
    "font size", "font family", "line spacing", "page margin", "pagination",
    "cover page", "table of contents format", "export format",
)

def _argument_stage_requirements(values: Iterable[Any]) -> list[str]:
    """Exclude only unmistakable presentation/export requirements."""
    result: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if not item:
            continue
        lowered = item.lower()
        if any(term in lowered for term in _ARGUMENT_STAGE_PRESENTATION_ONLY_TERMS):
            continue
        result.append(item)
    return result


def build_argument_architecture_model_input(canonical_envelope: dict[str, Any]) -> dict[str, Any]:
    payload = canonical_envelope.get("payload") or {}
    contract = payload.get("proposal_contract") or {}
    instruction = payload.get("task_instruction") or {}
    cards, _ = _evidence_records(canonical_envelope)
    return {
        "project_task": {
            "objective": str(instruction.get("objective") or "").strip(),
            "document_type": str(contract.get("document_type") or "UNKNOWN"),
            "evaluation_logic": str(contract.get("primary_evaluation_logic") or "UNKNOWN"),
            "max_research_questions": int(contract.get("max_core_research_questions") or 4),
            "specific_requirements": _argument_stage_requirements(
                instruction.get("specific_requirements") or []
            ),
            "priority_order": _argument_stage_requirements(
                instruction.get("priority_order") or []
            ),
            "acceptance_preferences": _argument_stage_requirements(
                instruction.get("acceptance_preferences") or []
            ),
        },
        "constraints": {
            "must_preserve": [
                str(x) for x in instruction.get("must_preserve") or [] if str(x).strip()
            ],
            "forbidden_changes": [
                str(x) for x in instruction.get("forbidden_changes") or [] if str(x).strip()
            ],
            "main_body_exclusions": [
                str(x)
                for x in contract.get("forbidden_main_body_topics") or []
                if str(x).strip()
            ],
            "appendix_topics": [
                str(x)
                for x in contract.get("appendix_only_topics") or []
                if str(x).strip()
            ],
        },
        "evidence_cards": cards,
        "design_seed": _design_seed(canonical_envelope),
        "revision_issues": _revision_issues(canonical_envelope),
        "human_resolutions": _human_resolutions(canonical_envelope),
    }




def _argument_skeleton_seed(canonical_envelope: dict[str, Any]) -> dict[str, Any] | None:
    """Project only problem-definition semantics needed by the Skeleton stage."""
    seed = _design_seed(canonical_envelope)
    if not isinstance(seed, dict):
        return None
    return {
        "central_proposition": copy.deepcopy(seed.get("central_proposition")),
        "boundary_conditions": copy.deepcopy(seed.get("boundary_conditions") or []),
        "scope_in": copy.deepcopy(seed.get("scope_in") or []),
        "scope_out": copy.deepcopy(seed.get("scope_out") or []),
        "existing_chains": copy.deepcopy(seed.get("existing_chains") or []),
    }


def build_argument_skeleton_model_input(
    canonical_envelope: dict[str, Any],
) -> dict[str, Any]:
    """Build the unregistered flat Stage-A input without design-stage payload."""
    base = build_argument_architecture_model_input(canonical_envelope)
    return {
        "project_task": copy.deepcopy(base["project_task"]),
        "constraints": copy.deepcopy(base["constraints"]),
        "evidence_cards": copy.deepcopy(base["evidence_cards"]),
        "skeleton_seed": _argument_skeleton_seed(canonical_envelope),
        "revision_issues": copy.deepcopy(base["revision_issues"]),
        "human_resolutions": copy.deepcopy(base["human_resolutions"]),
    }


def _argument_skeleton_schema_validator() -> Draft202012Validator:
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "prompt_pack"
        / "schemas"
        / "model"
        / "argument_skeleton_model_output.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


def _stable_jsonschema_error_message(error: Any) -> str:
    """Render JSON-Schema errors independently of mapping insertion order.

    ``jsonschema`` embeds ``repr(error.instance)`` in some messages.  Provider
    objects preserve wire key order, while audited JSON is persisted with
    sorted keys.  The two objects are semantically identical but their reprs
    differ, which used to change retry request identities after evidence
    replay.  Replace only that embedded representation with canonical JSON.
    """
    message = str(error.message)
    instance = getattr(error, "instance", None)
    if not isinstance(instance, (dict, list)):
        return message
    unstable = repr(instance)
    if unstable not in message:
        return message
    try:
        stable = json.dumps(
            instance,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return message
    return message.replace(unstable, stable)


def argument_skeleton_model_output_errors(value: Any) -> list[str]:
    """Validate only the flat Stage-A wire shape; no Runtime registration yet."""
    errors = sorted(
        _argument_skeleton_schema_validator().iter_errors(value),
        key=lambda error: list(error.absolute_path),
    )
    rendered: list[str] = []
    for error in errors:
        suffix = "/".join(str(token) for token in error.absolute_path)
        message = _stable_jsonschema_error_message(error)
        rendered.append(f"/{suffix}: {message}" if suffix else f"/: {message}")
    return rendered




_ARGUMENT_SKELETON_OWNED_SEED_COMPONENT_TYPES = {
    "CENTRAL_PROPOSITION",
    "SCOPE",
    "GAP",
    "RESEARCH_GAP",
    "ROOT_CAUSE",
    "PROBLEM",
    "RESEARCH_QUESTION",
    "QUESTION",
    "OBJECTIVE",
    "ASSUMPTION",
    "BOUNDARY_CONDITION",
    "FALSIFICATION_RULE",
}


def _argument_design_seed(canonical_envelope: dict[str, Any]) -> dict[str, Any] | None:
    """Project compact Stage-B hints without repeating Skeleton-owned semantics.

    ``seed_key`` is a request-local semantic handle, not a canonical object ID.  It
    lets relation hints refer to a component once instead of copying long source
    and target statements into every relation.  If an endpoint was deliberately
    filtered because Stage A owns it (notably OBJECTIVE), its semantic statement
    remains inline so no information is lost.
    """
    seed = _design_seed(canonical_envelope)
    if not isinstance(seed, dict):
        return None

    def is_design_side(component_type: Any) -> bool:
        return str(component_type or "").upper() not in _ARGUMENT_SKELETON_OWNED_SEED_COMPONENT_TYPES

    raw_components = [
        copy.deepcopy(item)
        for item in seed.get("existing_components") or []
        if isinstance(item, dict) and is_design_side(item.get("component_type"))
    ]
    components: list[dict[str, Any]] = []
    endpoint_to_key: dict[tuple[str, str], str] = {}
    for index, item in enumerate(raw_components, start=1):
        seed_key = f"S{index:03d}"
        component_type = str(item.get("component_type") or "")
        statement = str(item.get("statement") or "")
        compact = copy.deepcopy(item)
        compact["seed_key"] = seed_key
        components.append(compact)
        if component_type and statement:
            endpoint_to_key[(component_type, statement)] = seed_key

    def keep_relation(item: dict[str, Any]) -> bool:
        source_type = str(item.get("source_type") or "").upper()
        target_type = str(item.get("target_type") or "").upper()
        if is_design_side(source_type) and is_design_side(target_type):
            return True
        return source_type == "OBJECTIVE" and target_type == "WORK_PACKAGE"

    relations: list[dict[str, Any]] = []
    for item in seed.get("existing_relations") or []:
        if not isinstance(item, dict) or not keep_relation(item):
            continue
        source_type = str(item.get("source_type") or "")
        target_type = str(item.get("target_type") or "")
        source_statement = str(item.get("source_statement") or "")
        target_statement = str(item.get("target_statement") or "")
        relation = {
            "source_type": source_type,
            "relation": str(item.get("relation") or ""),
            "target_type": target_type,
        }
        source_key = endpoint_to_key.get((source_type, source_statement))
        target_key = endpoint_to_key.get((target_type, target_statement))
        if source_key:
            relation["source_key"] = source_key
        elif source_statement:
            relation["source_statement"] = source_statement
        if target_key:
            relation["target_key"] = target_key
        elif target_statement:
            relation["target_statement"] = target_statement
        relations.append(relation)
    return {
        "existing_components": components,
        "existing_relations": relations,
    }


def _argument_design_frozen_skeleton(skeleton_output: dict[str, Any]) -> dict[str, Any]:
    """Return the smallest read-only Stage-A semantic view needed by DESIGN.

    Runtime retains the complete Stage-A wire object for validation and assembly.
    DESIGN only needs the proposition/scope/thread semantics plus already-declared
    gaps/questions so it can avoid rewriting or duplicating them.  Per-thread
    provenance arrays remain grouped by semantic role in a compact inherited
    evidence object because DESIGN receives the full evidence-card pool separately.
    """
    proposition = skeleton_output.get("central_proposition") if isinstance(skeleton_output.get("central_proposition"), dict) else {}
    scope = skeleton_output.get("scope") if isinstance(skeleton_output.get("scope"), dict) else {}

    compact_threads: list[dict[str, Any]] = []
    for thread_index, thread in enumerate(skeleton_output.get("research_threads") or []):
        if not isinstance(thread, dict):
            continue

        def evidence_values(field: str) -> list[str]:
            values: list[str] = []
            for evidence_id in thread.get(field) or []:
                value = str(evidence_id or "").strip()
                if value and value not in values:
                    values.append(value)
            return values

        inherited_evidence = {
            "gap": evidence_values("gap_evidence_ids"),
            "limitation_mechanism": evidence_values(
                "limitation_mechanism_evidence_ids"
            ),
            "objective": evidence_values("objective_evidence_ids"),
        }
        compact_threads.append({
            "thread_index": thread_index,
            "gap_statement": str(thread.get("gap_statement") or ""),
            "limitation_mechanism_statement": str(thread.get("limitation_mechanism_statement") or ""),
            "question_statement": str(thread.get("question_statement") or ""),
            "question_type": str(thread.get("question_type") or ""),
            "answerability": str(thread.get("answerability") or ""),
            "success_evidence": copy.deepcopy(thread.get("success_evidence") or []),
            "objective_statement": str(thread.get("objective_statement") or ""),
            "assumptions": copy.deepcopy(thread.get("assumptions") or []),
            "falsification_or_comparison_rule": str(thread.get("falsification_or_comparison_rule") or ""),
            "inherited_evidence": inherited_evidence,
        })

    return {
        "central_proposition": {
            "statement": str(proposition.get("statement") or ""),
            "proposition_type": str(proposition.get("proposition_type") or ""),
            "falsifiable_or_comparable": bool(proposition.get("falsifiable_or_comparable")),
            "boundary_conditions": copy.deepcopy(proposition.get("boundary_conditions") or []),
        },
        "scope": copy.deepcopy(scope),
        "research_threads": compact_threads,
        "evidence_gaps": copy.deepcopy(skeleton_output.get("evidence_gaps") or []),
        "user_questions": copy.deepcopy(skeleton_output.get("user_questions") or []),
        "cannot_proceed_reason": copy.deepcopy(skeleton_output.get("cannot_proceed_reason")),
    }

def build_argument_design_model_input(
    canonical_envelope: dict[str, Any],
    skeleton_output: dict[str, Any],
) -> dict[str, Any]:
    """Build the unregistered flat Stage-B input with an immutable Stage-A Skeleton."""
    skeleton_errors = argument_skeleton_model_output_errors(skeleton_output)
    if skeleton_errors:
        raise ValueError("Invalid Argument Skeleton: " + "; ".join(skeleton_errors[:6]))
    base = build_argument_architecture_model_input(canonical_envelope)
    return {
        "project_task": copy.deepcopy(base["project_task"]),
        "constraints": copy.deepcopy(base["constraints"]),
        "evidence_cards": copy.deepcopy(base["evidence_cards"]),
        "frozen_skeleton": _argument_design_frozen_skeleton(skeleton_output),
        "design_seed": _argument_design_seed(canonical_envelope),
        "revision_issues": copy.deepcopy(base["revision_issues"]),
        "human_resolutions": copy.deepcopy(base["human_resolutions"]),
    }


def _argument_design_schema_validator() -> Draft202012Validator:
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "prompt_pack"
        / "schemas"
        / "model"
        / "argument_design_model_output.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


def argument_design_model_output_errors(value: Any) -> list[str]:
    """Validate the flat Stage-B wire shape; no Runtime registration yet."""
    errors = sorted(
        _argument_design_schema_validator().iter_errors(value),
        key=lambda error: list(error.absolute_path),
    )
    rendered: list[str] = []
    for error in errors:
        suffix = "/".join(str(token) for token in error.absolute_path)
        message = _stable_jsonschema_error_message(error)
        rendered.append(f"/{suffix}: {message}" if suffix else f"/: {message}")
    return rendered


def argument_design_model_reference_errors(
    value: dict[str, Any],
    skeleton_output: dict[str, Any],
) -> list[str]:
    """Validate every locally resolvable flat-record reference.

    The check is deliberately best-effort when unrelated records still have wire-shape
    defects: malformed index tuples are left to JSON-Schema validation, while references
    whose index fields are already well-typed are validated in the same model attempt.
    This prevents a bounded retry from serially discovering shape and reference defects.
    """
    if argument_skeleton_model_output_errors(skeleton_output):
        return ["/frozen_skeleton: invalid Argument Skeleton"]
    if not isinstance(value, dict):
        return []

    thread_count = len(skeleton_output.get("research_threads") or [])
    errors: list[str] = []

    def index_tuple(item: Any, keys: tuple[str, ...]) -> tuple[int, ...] | None:
        if not isinstance(item, dict):
            return None
        values: list[int] = []
        for name in keys:
            value_at_key = item.get(name)
            if not isinstance(value_at_key, int) or isinstance(value_at_key, bool):
                return None
            values.append(value_at_key)
        return tuple(values)

    def rows(collection: str) -> list[Any]:
        value_rows = value.get(collection)
        return value_rows if isinstance(value_rows, list) else []

    def keyset(collection: str, keys: tuple[str, ...]) -> set[tuple[int, ...]]:
        result: set[tuple[int, ...]] = set()
        for pos, item in enumerate(rows(collection)):
            key = index_tuple(item, keys)
            if key is None:
                continue
            if key in result:
                errors.append(f"/{collection}/{pos}: duplicate local index {key}")
            result.add(key)
        return result

    wp = keyset("work_packages", ("thread_index", "work_package_index"))
    methods = keyset("methods", ("thread_index", "work_package_index", "method_index"))
    evals = keyset("evaluations", ("thread_index", "work_package_index", "method_index", "evaluation_index"))
    innovations = keyset("innovations", ("thread_index", "innovation_index"))
    foundation = keyset("foundation", ("thread_index", "foundation_index"))
    keyset("theoretical_properties", ("thread_index", "work_package_index", "method_index", "property_index"))
    keyset("baselines", ("thread_index", "work_package_index", "method_index", "evaluation_index", "baseline_index"))
    keyset("ablations", ("thread_index", "work_package_index", "method_index", "evaluation_index", "ablation_index"))
    keyset("innovation_prior_work", ("thread_index", "innovation_index", "prior_work_index"))

    def exact_relation_duplicates(
        collection: str,
        keys: tuple[str, ...],
        *,
        nullable_fields: frozenset[str] = frozenset(),
    ) -> None:
        seen: set[tuple[Any, ...]] = set()
        for pos, item in enumerate(rows(collection)):
            if not isinstance(item, dict):
                continue
            values: list[Any] = []
            valid = True
            for key_name in keys:
                raw = item.get(key_name)
                if raw is None and key_name in nullable_fields:
                    values.append(None)
                elif isinstance(raw, int) and not isinstance(raw, bool):
                    values.append(raw)
                else:
                    valid = False
                    break
            if not valid:
                continue
            relation_key = tuple(values)
            if relation_key in seen:
                errors.append(
                    f"/{collection}/{pos}: duplicate exact relation {relation_key}"
                )
            else:
                seen.add(relation_key)

    exact_relation_duplicates(
        "innovation_evaluation_refs",
        ("thread_index", "innovation_index", "work_package_index", "method_index", "evaluation_index"),
    )
    exact_relation_duplicates(
        "foundation_supports",
        ("thread_index", "foundation_index", "work_package_index", "method_index"),
        nullable_fields=frozenset({"method_index"}),
    )

    for collection in (
        "work_packages", "methods", "theoretical_properties", "evaluations", "baselines",
        "ablations", "innovations", "innovation_prior_work", "innovation_evaluation_refs",
        "foundation", "foundation_supports", "evidence_gaps",
    ):
        for pos, item in enumerate(rows(collection)):
            if not isinstance(item, dict):
                continue
            thread_index = item.get("thread_index")
            if isinstance(thread_index, int) and not isinstance(thread_index, bool):
                if not (0 <= thread_index < thread_count):
                    errors.append(f"/{collection}/{pos}/thread_index: out of range")

    def require_parent(collection: str, parent_fields: tuple[str, ...], parents: set[tuple[int, ...]]) -> None:
        for pos, item in enumerate(rows(collection)):
            parent = index_tuple(item, parent_fields)
            if parent is not None and parent not in parents:
                errors.append(f"/{collection}/{pos}: unresolved parent index {parent}")

    require_parent("methods", ("thread_index", "work_package_index"), wp)
    for collection in ("theoretical_properties", "evaluations"):
        require_parent(collection, ("thread_index", "work_package_index", "method_index"), methods)
    for collection in ("baselines", "ablations"):
        require_parent(collection, ("thread_index", "work_package_index", "method_index", "evaluation_index"), evals)
    require_parent("innovation_prior_work", ("thread_index", "innovation_index"), innovations)
    require_parent("innovation_evaluation_refs", ("thread_index", "innovation_index"), innovations)
    for pos, item in enumerate(rows("innovation_evaluation_refs")):
        target = index_tuple(item, ("thread_index", "work_package_index", "method_index", "evaluation_index"))
        if target is not None and target not in evals:
            errors.append(f"/innovation_evaluation_refs/{pos}: unresolved evaluation index {target}")
    require_parent("foundation_supports", ("thread_index", "foundation_index"), foundation)
    for pos, item in enumerate(rows("foundation_supports")):
        if not isinstance(item, dict):
            continue
        if item.get("method_index") is None:
            target = index_tuple(item, ("thread_index", "work_package_index"))
            if target is not None and target not in wp:
                errors.append(f"/foundation_supports/{pos}: unresolved work-package index {target}")
        else:
            target = index_tuple(item, ("thread_index", "work_package_index", "method_index"))
            if target is not None and target not in methods:
                errors.append(f"/foundation_supports/{pos}: unresolved method index {target}")
    return errors

def assemble_argument_authored_thread(
    skeleton_output: dict[str, Any],
    design_output: dict[str, Any],
    thread_index: int,
) -> dict[str, Any]:
    """Deterministically assemble one legacy authored research thread from flat Stage A/B records.

    The function is deliberately pure and unregistered: it validates both wire
    contracts and all local references, never invents semantic content, and
    derives only nesting/order from explicit local indexes.
    """
    skeleton_errors = argument_skeleton_model_output_errors(skeleton_output)
    if skeleton_errors:
        raise ValueError("Invalid Argument Skeleton: " + "; ".join(skeleton_errors[:6]))
    design_errors = argument_design_model_output_errors(design_output)
    if design_errors:
        raise ValueError("Invalid Argument Design: " + "; ".join(design_errors[:6]))
    reference_errors = argument_design_model_reference_errors(design_output, skeleton_output)
    if reference_errors:
        raise ValueError("Invalid Argument Design references: " + "; ".join(reference_errors[:6]))

    threads = skeleton_output.get("research_threads") or []
    if not isinstance(thread_index, int) or isinstance(thread_index, bool) or not (0 <= thread_index < len(threads)):
        raise ValueError("thread_index out of range")
    skeleton_thread = threads[thread_index]

    def rows(collection: str, *index_fields: str) -> list[dict[str, Any]]:
        selected = [
            copy.deepcopy(item)
            for item in design_output.get(collection) or []
            if int(item.get("thread_index", -1)) == thread_index
        ]
        return sorted(
            selected,
            key=lambda item: tuple(
                -1 if item.get(field) is None else int(item[field])
                for field in index_fields
            ),
        )

    # Flat Stage-B indexes are record keys, not positions in the nested
    # authored-state arrays.  Providers may legally use sparse keys or reuse a
    # global sequence across threads.  Record the deterministic sorted position
    # of every referenced object while assembling so outgoing nested references
    # never depend on the provider's choice of key values.
    work_package_positions: dict[int, int] = {}
    method_positions: dict[tuple[int, int], int] = {}
    evaluation_positions: dict[tuple[int, int, int], int] = {}

    work_packages: list[dict[str, Any]] = []
    for work_package_position, wp in enumerate(
        rows("work_packages", "work_package_index")
    ):
        wp_index = int(wp["work_package_index"])
        work_package_positions[wp_index] = work_package_position
        assembled_methods: list[dict[str, Any]] = []
        matching_methods = [
            method
            for method in rows("methods", "work_package_index", "method_index")
            if int(method["work_package_index"]) == wp_index
        ]
        for method_position, method in enumerate(matching_methods):
            method_index = int(method["method_index"])
            method_positions[(wp_index, method_index)] = method_position
            theoretical_properties = [
                {"statement": item["statement"], "evidence_ids": copy.deepcopy(item["evidence_ids"])}
                for item in rows("theoretical_properties", "work_package_index", "method_index", "property_index")
                if int(item["work_package_index"]) == wp_index and int(item["method_index"]) == method_index
            ]
            evaluations: list[dict[str, Any]] = []
            matching_evaluations = [
                evaluation
                for evaluation in rows(
                    "evaluations",
                    "work_package_index",
                    "method_index",
                    "evaluation_index",
                )
                if int(evaluation["work_package_index"]) == wp_index
                and int(evaluation["method_index"]) == method_index
            ]
            for evaluation_position, evaluation in enumerate(matching_evaluations):
                evaluation_index = int(evaluation["evaluation_index"])
                evaluation_positions[
                    (wp_index, method_index, evaluation_index)
                ] = evaluation_position
                baselines = [
                    {"statement": item["statement"], "evidence_ids": copy.deepcopy(item["evidence_ids"])}
                    for item in rows("baselines", "work_package_index", "method_index", "evaluation_index", "baseline_index")
                    if int(item["work_package_index"]) == wp_index
                    and int(item["method_index"]) == method_index
                    and int(item["evaluation_index"]) == evaluation_index
                ]
                ablations = [
                    item["statement"]
                    for item in rows("ablations", "work_package_index", "method_index", "evaluation_index", "ablation_index")
                    if int(item["work_package_index"]) == wp_index
                    and int(item["method_index"]) == method_index
                    and int(item["evaluation_index"]) == evaluation_index
                ]
                evaluations.append({
                    "statement": evaluation["statement"],
                    "evidence_ids": copy.deepcopy(evaluation["evidence_ids"]),
                    "baselines": baselines,
                    "ablations": ablations,
                    "success_criteria": copy.deepcopy(evaluation["success_criteria"]),
                })
            assembled_methods.append({
                "statement": method["statement"],
                "evidence_ids": copy.deepcopy(method["evidence_ids"]),
                "method_type": method["method_type"],
                "assumptions": copy.deepcopy(method["assumptions"]),
                "theoretical_properties": theoretical_properties,
                "evaluations": evaluations,
            })
        work_packages.append({
            "statement": wp["statement"],
            "evidence_ids": copy.deepcopy(wp["evidence_ids"]),
            "methods": assembled_methods,
        })

    innovations: list[dict[str, Any]] = []
    for innovation in rows("innovations", "innovation_index"):
        innovation_index = int(innovation["innovation_index"])
        prior_work = [
            {"statement": item["statement"], "evidence_ids": copy.deepcopy(item["evidence_ids"])}
            for item in rows("innovation_prior_work", "innovation_index", "prior_work_index")
            if int(item["innovation_index"]) == innovation_index
        ]
        evaluation_refs: list[dict[str, int]] = []
        for item in rows(
            "innovation_evaluation_refs",
            "innovation_index",
            "work_package_index",
            "method_index",
            "evaluation_index",
        ):
            if int(item["innovation_index"]) != innovation_index:
                continue
            wp_index = int(item["work_package_index"])
            method_index = int(item["method_index"])
            evaluation_index = int(item["evaluation_index"])
            evaluation_refs.append(
                {
                    "work_package_index": work_package_positions[wp_index],
                    "method_index": method_positions[(wp_index, method_index)],
                    "evaluation_index": evaluation_positions[
                        (wp_index, method_index, evaluation_index)
                    ],
                }
            )
        innovations.append({
            "statement": innovation["statement"],
            "evidence_ids": copy.deepcopy(innovation["evidence_ids"]),
            "contribution": innovation["contribution"],
            "closest_prior_work": prior_work,
            "evaluation_refs": evaluation_refs,
        })

    foundation: list[dict[str, Any]] = []
    for item in rows("foundation", "foundation_index"):
        foundation_index = int(item["foundation_index"])
        supports: list[dict[str, int | None]] = []
        for link in rows(
            "foundation_supports",
            "foundation_index",
            "work_package_index",
            "method_index",
        ):
            if int(link["foundation_index"]) != foundation_index:
                continue
            wp_index = int(link["work_package_index"])
            method_index = link.get("method_index")
            supports.append(
                {
                    "work_package_index": work_package_positions[wp_index],
                    "method_index": (
                        None
                        if method_index is None
                        else method_positions[(wp_index, int(method_index))]
                    ),
                }
            )
        foundation.append({
            "statement": item["statement"],
            "evidence_ids": copy.deepcopy(item["evidence_ids"]),
            "supports": supports,
        })

    return {
        "gap": {
            "statement": skeleton_thread["gap_statement"],
            "limitation_mechanism": {
                "statement": skeleton_thread["limitation_mechanism_statement"],
                "evidence_ids": copy.deepcopy(skeleton_thread["limitation_mechanism_evidence_ids"]),
            },
            "evidence_ids": copy.deepcopy(skeleton_thread["gap_evidence_ids"]),
        },
        "question": {
            "statement": skeleton_thread["question_statement"],
            "question_type": skeleton_thread["question_type"],
            "answerability": skeleton_thread["answerability"],
            "success_evidence": copy.deepcopy(skeleton_thread["success_evidence"]),
        },
        "objective": {
            "statement": skeleton_thread["objective_statement"],
            "evidence_ids": copy.deepcopy(skeleton_thread["objective_evidence_ids"]),
        },
        "thread_assumptions": copy.deepcopy(skeleton_thread["assumptions"]),
        "work_packages": work_packages,
        "innovations": innovations,
        "foundation": foundation,
        "falsification_or_comparison_rule": skeleton_thread["falsification_or_comparison_rule"],
    }


def _dedupe_argument_user_questions(*collections: Iterable[Any]) -> list[dict[str, Any]]:
    """Keep the first owner of the same user-facing question text.

    Skeleton is passed first by the assembler, so Design cannot silently override
    the same question with different routing metadata. This is exact deterministic
    ownership normalization, not fuzzy semantic merging.
    """
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for collection in collections:
        for raw in collection or []:
            if not isinstance(raw, dict):
                continue
            key = _semantic_text_key(raw.get("question"))
            if not key:
                key = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if key in seen:
                continue
            seen.add(key)
            result.append(copy.deepcopy(raw))
    return result


def _dedupe_argument_evidence_gaps(*collections: Iterable[Any]) -> list[dict[str, Any]]:
    """Collapse only deterministically identical gap identities.

    The key deliberately requires kind, thread ownership, and the same normalized
    suggested question (or reason fallback). Near-duplicate prose is preserved for
    the responsible model stage to resolve rather than guessed away by Python.
    """
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for collection in collections:
        for raw in collection or []:
            if not isinstance(raw, dict):
                continue
            semantic = _semantic_text_key(raw.get("suggested_question") or raw.get("reason"))
            key = (
                str(raw.get("kind") or ""),
                str(raw.get("thread_index")),
                semantic,
            )
            if semantic and key in seen:
                continue
            if semantic:
                seen.add(key)
            result.append(copy.deepcopy(raw))
    return result


def assemble_argument_authored_state(
    skeleton_output: dict[str, Any],
    design_output: dict[str, Any],
) -> dict[str, Any]:
    """Deterministically assemble the complete legacy authored semantic state.

    Stage A exclusively owns the problem definition and thread skeleton. Stage B
    exclusively owns research-design records. This adapter preserves both wire
    contracts verbatim, derives only legacy nesting/order, and deliberately does
    not generate Runtime identities or projection metadata.
    """
    skeleton_errors = argument_skeleton_model_output_errors(skeleton_output)
    if skeleton_errors:
        raise ValueError("Invalid Argument Skeleton: " + "; ".join(skeleton_errors[:6]))
    design_errors = argument_design_model_output_errors(design_output)
    if design_errors:
        raise ValueError("Invalid Argument Design: " + "; ".join(design_errors[:6]))
    reference_errors = argument_design_model_reference_errors(design_output, skeleton_output)
    if reference_errors:
        raise ValueError("Invalid Argument Design references: " + "; ".join(reference_errors[:6]))

    skeleton_reason = skeleton_output.get("cannot_proceed_reason")
    design_reason = design_output.get("cannot_proceed_reason")
    if skeleton_reason and design_reason and skeleton_reason != design_reason:
        raise ValueError("Conflicting cannot_proceed_reason between Skeleton and Design")

    threads = [
        assemble_argument_authored_thread(skeleton_output, design_output, thread_index)
        for thread_index in range(len(skeleton_output.get("research_threads") or []))
    ]
    return {
        "central_proposition": copy.deepcopy(skeleton_output["central_proposition"]),
        "scope": copy.deepcopy(skeleton_output["scope"]),
        "research_threads": threads,
        "evidence_gaps": _dedupe_argument_evidence_gaps(
            skeleton_output.get("evidence_gaps") or [],
            design_output.get("evidence_gaps") or [],
        ),
        "user_questions": _dedupe_argument_user_questions(
            skeleton_output.get("user_questions") or [],
            design_output.get("user_questions") or [],
        ),
        "cannot_proceed_reason": copy.deepcopy(design_reason or skeleton_reason),
    }


def split_argument_authored_state(
    authored_state: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Invert deterministic Stage-A/B assembly for regeneration baselines.

    This is an internal representation adapter, not a schema migration.  It
    lets a later Design-only repair reuse the exact accepted Skeleton and a
    compact thread-local slice of the previous Design instead of asking the
    model to recreate the whole Stage-0 object.
    """

    if not isinstance(authored_state, dict):
        raise ValueError("Argument authored_state baseline must be an object")

    skeleton_questions: list[dict[str, Any]] = []
    design_questions: list[dict[str, Any]] = []
    for question in authored_state.get("user_questions") or []:
        if not isinstance(question, dict):
            continue
        target = str(question.get("target_area") or "")
        destination = (
            skeleton_questions if target == "PROJECT_SCOPE" else design_questions
        )
        destination.append(copy.deepcopy(question))

    cannot_reason = copy.deepcopy(authored_state.get("cannot_proceed_reason"))
    skeleton: dict[str, Any] = {
        "central_proposition": copy.deepcopy(authored_state.get("central_proposition")),
        "scope": copy.deepcopy(authored_state.get("scope")),
        "research_threads": [],
        "evidence_gaps": [],
        "user_questions": skeleton_questions,
        "cannot_proceed_reason": cannot_reason if skeleton_questions else None,
    }
    design: dict[str, Any] = {
        "work_packages": [],
        "methods": [],
        "theoretical_properties": [],
        "evaluations": [],
        "baselines": [],
        "ablations": [],
        "innovations": [],
        "innovation_prior_work": [],
        "innovation_evaluation_refs": [],
        "foundation": [],
        "foundation_supports": [],
        "evidence_gaps": copy.deepcopy(authored_state.get("evidence_gaps") or []),
        "user_questions": design_questions,
        "cannot_proceed_reason": None if skeleton_questions else cannot_reason,
    }

    for thread_index, thread in enumerate(authored_state.get("research_threads") or []):
        if not isinstance(thread, dict):
            raise ValueError(f"Argument authored_state thread {thread_index} is invalid")
        gap = thread.get("gap") or {}
        limitation = gap.get("limitation_mechanism") or {}
        question = thread.get("question") or {}
        objective = thread.get("objective") or {}
        skeleton["research_threads"].append(
            {
                "gap_statement": copy.deepcopy(gap.get("statement")),
                "gap_evidence_ids": copy.deepcopy(gap.get("evidence_ids") or []),
                "limitation_mechanism_statement": copy.deepcopy(
                    limitation.get("statement")
                ),
                "limitation_mechanism_evidence_ids": copy.deepcopy(
                    limitation.get("evidence_ids") or []
                ),
                "question_statement": copy.deepcopy(question.get("statement")),
                "question_type": copy.deepcopy(question.get("question_type")),
                "answerability": copy.deepcopy(question.get("answerability")),
                "success_evidence": copy.deepcopy(
                    question.get("success_evidence") or []
                ),
                "objective_statement": copy.deepcopy(objective.get("statement")),
                "objective_evidence_ids": copy.deepcopy(
                    objective.get("evidence_ids") or []
                ),
                "assumptions": copy.deepcopy(thread.get("thread_assumptions") or []),
                "falsification_or_comparison_rule": copy.deepcopy(
                    thread.get("falsification_or_comparison_rule")
                ),
            }
        )

        for work_package_index, work_package in enumerate(
            thread.get("work_packages") or []
        ):
            design["work_packages"].append(
                {
                    "thread_index": thread_index,
                    "work_package_index": work_package_index,
                    "statement": copy.deepcopy(work_package.get("statement")),
                    "evidence_ids": copy.deepcopy(
                        work_package.get("evidence_ids") or []
                    ),
                }
            )
            for method_index, method in enumerate(work_package.get("methods") or []):
                design["methods"].append(
                    {
                        "thread_index": thread_index,
                        "work_package_index": work_package_index,
                        "method_index": method_index,
                        "statement": copy.deepcopy(method.get("statement")),
                        "evidence_ids": copy.deepcopy(method.get("evidence_ids") or []),
                        "method_type": copy.deepcopy(method.get("method_type")),
                        "assumptions": copy.deepcopy(method.get("assumptions") or []),
                    }
                )
                for property_index, prop in enumerate(
                    method.get("theoretical_properties") or []
                ):
                    design["theoretical_properties"].append(
                        {
                            "thread_index": thread_index,
                            "work_package_index": work_package_index,
                            "method_index": method_index,
                            "property_index": property_index,
                            "statement": copy.deepcopy(prop.get("statement")),
                            "evidence_ids": copy.deepcopy(prop.get("evidence_ids") or []),
                        }
                    )
                for evaluation_index, evaluation in enumerate(
                    method.get("evaluations") or []
                ):
                    design["evaluations"].append(
                        {
                            "thread_index": thread_index,
                            "work_package_index": work_package_index,
                            "method_index": method_index,
                            "evaluation_index": evaluation_index,
                            "statement": copy.deepcopy(evaluation.get("statement")),
                            "evidence_ids": copy.deepcopy(
                                evaluation.get("evidence_ids") or []
                            ),
                            "success_criteria": copy.deepcopy(
                                evaluation.get("success_criteria") or []
                            ),
                        }
                    )
                    for baseline_index, baseline in enumerate(
                        evaluation.get("baselines") or []
                    ):
                        design["baselines"].append(
                            {
                                "thread_index": thread_index,
                                "work_package_index": work_package_index,
                                "method_index": method_index,
                                "evaluation_index": evaluation_index,
                                "baseline_index": baseline_index,
                                "statement": copy.deepcopy(baseline.get("statement")),
                                "evidence_ids": copy.deepcopy(
                                    baseline.get("evidence_ids") or []
                                ),
                            }
                        )
                    for ablation_index, ablation in enumerate(
                        evaluation.get("ablations") or []
                    ):
                        design["ablations"].append(
                            {
                                "thread_index": thread_index,
                                "work_package_index": work_package_index,
                                "method_index": method_index,
                                "evaluation_index": evaluation_index,
                                "ablation_index": ablation_index,
                                "statement": copy.deepcopy(ablation),
                            }
                        )

        for innovation_index, innovation in enumerate(thread.get("innovations") or []):
            design["innovations"].append(
                {
                    "thread_index": thread_index,
                    "innovation_index": innovation_index,
                    "statement": copy.deepcopy(innovation.get("statement")),
                    "evidence_ids": copy.deepcopy(innovation.get("evidence_ids") or []),
                    "contribution": copy.deepcopy(innovation.get("contribution")),
                }
            )
            for prior_work_index, prior_work in enumerate(
                innovation.get("closest_prior_work") or []
            ):
                design["innovation_prior_work"].append(
                    {
                        "thread_index": thread_index,
                        "innovation_index": innovation_index,
                        "prior_work_index": prior_work_index,
                        "statement": copy.deepcopy(prior_work.get("statement")),
                        "evidence_ids": copy.deepcopy(
                            prior_work.get("evidence_ids") or []
                        ),
                    }
                )
            for evaluation_ref in innovation.get("evaluation_refs") or []:
                design["innovation_evaluation_refs"].append(
                    {
                        "thread_index": thread_index,
                        "innovation_index": innovation_index,
                        "work_package_index": int(
                            evaluation_ref["work_package_index"]
                        ),
                        "method_index": int(evaluation_ref["method_index"]),
                        "evaluation_index": int(
                            evaluation_ref["evaluation_index"]
                        ),
                    }
                )

        for foundation_index, foundation in enumerate(thread.get("foundation") or []):
            design["foundation"].append(
                {
                    "thread_index": thread_index,
                    "foundation_index": foundation_index,
                    "statement": copy.deepcopy(foundation.get("statement")),
                    "evidence_ids": copy.deepcopy(foundation.get("evidence_ids") or []),
                }
            )
            for support in foundation.get("supports") or []:
                design["foundation_supports"].append(
                    {
                        "thread_index": thread_index,
                        "foundation_index": foundation_index,
                        "work_package_index": int(support["work_package_index"]),
                        "method_index": (
                            None
                            if support.get("method_index") is None
                            else int(support["method_index"])
                        ),
                    }
                )

    skeleton_errors = argument_skeleton_model_output_errors(skeleton)
    design_errors = argument_design_model_output_errors(design)
    reference_errors = argument_design_model_reference_errors(design, skeleton)
    if skeleton_errors or design_errors or reference_errors:
        raise ValueError(
            "Argument authored_state baseline cannot be split: "
            + "; ".join(
                [*skeleton_errors, *design_errors, *reference_errors][:12]
            )
        )
    if assemble_argument_authored_state(skeleton, design) != authored_state:
        raise ValueError("Argument authored_state baseline split is not lossless")
    return skeleton, design


def _evidence_ids_for_source_refs(refs: Iterable[Any], records: dict[str, dict[str, Any]]) -> list[str]:
    """Conservative legacy fallback.

    New argument candidates carry an authored-evidence sidecar.  This helper is
    retained only for old stored candidates that predate that sidecar.  It never
    expands a binding merely because two Evidence Cards share one ``source_id``:
    only an exact source-ref set with a unique matching Evidence Card is accepted.
    """
    normalized_refs = _dedupe_source_refs(refs)
    if not normalized_refs:
        return []
    target = {
        json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for r in normalized_refs
    }
    matches: list[str] = []
    for eid, record in records.items():
        record_refs = {
            json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for r in _dedupe_source_refs(record.get("source_refs") or [])
        }
        if record_refs == target:
            matches.append(str(eid))
    return matches if len(matches) == 1 else []



def _authored_evidence_binding_map(candidate: dict[str, Any]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for item in candidate.get("authored_evidence_bindings") or []:
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("node_id") or "").strip()
        if not node_id:
            continue
        result[node_id] = list(
            dict.fromkeys(
                str(x)
                for x in item.get("evidence_ids") or []
                if str(x).strip()
            )
        )
    return result


def _semantic_statement_from_node(
    node: dict[str, Any] | None,
    records: dict[str, dict[str, Any]],
    authored_bindings: dict[str, list[str]] | None = None,
    *,
    include_node_id: bool = False,
) -> dict[str, Any]:
    node = node or {}
    node_id = str(node.get("node_id") or "")
    if authored_bindings is not None and node_id in authored_bindings:
        evidence_ids = list(authored_bindings[node_id])
    else:
        evidence_ids = _evidence_ids_for_source_refs(node.get("source_refs") or [], records)
    result = {
        "statement": str(node.get("statement") or ""),
        "evidence_ids": evidence_ids,
    }
    if include_node_id and node_id:
        result["_node_id"] = node_id
    return result


def _critic_review_units(
    candidate: dict[str, Any],
) -> tuple[list[dict[str, str]], dict[str, str]]:
    graph = candidate.get("argument_architecture") or {}
    units: list[dict[str, str]] = []
    mapping: dict[str, str] = {}

    def add(unit_key: str, component: str, statement: Any, node_id: Any) -> None:
        text = str(statement or "").strip()
        nid = str(node_id or "").strip()
        if not unit_key or not text or not nid:
            return
        units.append(
            {
                "unit_key": unit_key,
                "component": component,
                "statement": text,
            }
        )
        mapping[unit_key] = nid

    proposition = graph.get("central_proposition") or {}
    add(
        "CENTRAL_PROPOSITION",
        "CENTRAL_PROPOSITION",
        proposition.get("statement"),
        proposition.get("node_id"),
    )
    for index, question in enumerate(graph.get("research_questions") or [], 1):
        if isinstance(question, dict):
            add(
                f"RESEARCH_QUESTION:{index}",
                "RESEARCH_QUESTION",
                question.get("statement"),
                question.get("node_id"),
            )

    counts: dict[str, int] = {}
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        node_type = str(node.get("node_type") or "DESIGN_COMPONENT")
        counts[node_type] = counts.get(node_type, 0) + 1
        add(
            f"{node_type}:{counts[node_type]}",
            node_type,
            node.get("statement"),
            node.get("node_id"),
        )
    return units, mapping


def _critic_candidate_semantics(
    canonical_envelope: dict[str, Any],
    *,
    include_machine_ids: bool = False,
) -> dict[str, Any]:
    payload = canonical_envelope.get("payload") or {}
    candidate = _canonical_argument_candidate(
        canonical_envelope, payload.get("architecture_candidate") or {}
    )
    graph = candidate.get("argument_architecture") or {}
    matrix = candidate.get("research_design_matrix") or []
    nodes = [n for n in graph.get("nodes") or [] if isinstance(n, dict)]
    nodes_by_id = {
        str(n.get("node_id")): n
        for n in nodes
        if n.get("node_id")
    }
    thread_assumption_id_map = {
        int(item.get("thread_index")): [
            str(value)
            for value in item.get("assumption_node_ids") or []
            if str(value).strip()
        ]
        for item in candidate.get("authored_thread_assumptions") or []
        if isinstance(item, dict) and isinstance(item.get("thread_index"), int)
    }
    thread_assumption_map = {
        thread_index: [
            str((nodes_by_id.get(node_id) or {}).get("statement") or "").strip()
            for node_id in node_ids
            if str((nodes_by_id.get(node_id) or {}).get("node_type") or "") == "ASSUMPTION"
            and str((nodes_by_id.get(node_id) or {}).get("statement") or "").strip()
        ]
        for thread_index, node_ids in thread_assumption_id_map.items()
    }
    questions_by_id = {
        str(q.get("node_id")): q
        for q in graph.get("research_questions") or []
        if isinstance(q, dict) and q.get("node_id")
    }
    question_thread_index = {
        str(q.get("node_id")): index
        for index, q in enumerate(graph.get("research_questions") or [])
        if isinstance(q, dict) and q.get("node_id")
    }
    _, records = _evidence_records(canonical_envelope)
    authored_bindings = _authored_evidence_binding_map(candidate)
    edges = [e for e in graph.get("edges") or [] if isinstance(e, dict)]

    outgoing: dict[tuple[str, str], list[str]] = {}
    incoming: dict[tuple[str, str], list[str]] = {}
    for edge in edges:
        source = str(edge.get("source_id") or "")
        target = str(edge.get("target_id") or "")
        relation = str(edge.get("relation") or "")
        if source and target and relation:
            outgoing.setdefault((source, relation), []).append(target)
            incoming.setdefault((target, relation), []).append(source)

    def semantic(node_id: str) -> dict[str, Any]:
        return _semantic_statement_from_node(
            nodes_by_id.get(str(node_id)),
            records,
            authored_bindings,
            include_node_id=include_machine_ids,
        )

    proposition = graph.get("central_proposition") or {}
    prop_id = str(proposition.get("node_id") or "")
    if prop_id in authored_bindings:
        prop_evidence = list(authored_bindings[prop_id])
    else:
        prop_evidence = _evidence_ids_for_source_refs(
            proposition.get("source_refs") or [],
            records,
        )
    central = {
        "statement": str(proposition.get("statement") or ""),
        "proposition_type": str(proposition.get("proposition_type") or "UNKNOWN"),
        "falsifiable_or_comparable": bool(
            proposition.get("falsifiable_or_comparable")
        ),
        "boundary_conditions": [
            str(x) for x in proposition.get("boundary_conditions") or []
        ],
        "evidence_ids": prop_evidence,
    }
    if include_machine_ids and prop_id:
        central["_node_id"] = prop_id

    threads: list[dict[str, Any]] = []
    for row_position, row in enumerate(matrix):
        if not isinstance(row, dict):
            continue

        research_question_id = str(row.get("research_question_id") or "")
        thread_index = question_thread_index.get(research_question_id, row_position)
        rq = questions_by_id.get(research_question_id) or {}
        row_ids = {
            field: [str(x) for x in row.get(field) or []]
            for field in (
                "gap_ids",
                "objective_ids",
                "work_package_ids",
                "method_ids",
                "evaluation_ids",
                "innovation_ids",
                "closest_prior_work_ids",
                "foundation_evidence_ids",
            )
        }
        gap_id = row_ids["gap_ids"][0] if row_ids["gap_ids"] else ""
        objective_id = (
            row_ids["objective_ids"][0] if row_ids["objective_ids"] else ""
        )

        limitation_sources = incoming.get((gap_id, "EXPLAINS"), [])
        limitation = next(
            (
                semantic(nid)
                for nid in limitation_sources
                if str((nodes_by_id.get(nid) or {}).get("node_type") or "")
                == "LIMITATION_MECHANISM"
            ),
            {"statement": "", "evidence_ids": []},
        )

        thread_assumptions = list(thread_assumption_map.get(thread_index, []))

        wp_ids = row_ids["work_package_ids"]
        method_row_ids = set(row_ids["method_ids"])
        eval_row_ids = set(row_ids["evaluation_ids"])
        prior_row_ids = set(row_ids["closest_prior_work_ids"])
        innovation_row_ids = row_ids["innovation_ids"]
        foundation_row_ids = row_ids["foundation_evidence_ids"]

        work_packages: list[dict[str, Any]] = []
        method_positions: dict[str, tuple[int, int]] = {}
        eval_positions: dict[str, tuple[int, int, int]] = {}
        nested_method_ids: set[str] = set()
        nested_eval_ids: set[str] = set()
        nested_baseline_ids: set[str] = set()
        nested_prior_ids: set[str] = set()

        for wi, wp_id in enumerate(wp_ids):
            wp_sem = semantic(wp_id)
            methods: list[dict[str, Any]] = []
            linked_methods = [
                mid
                for mid in outgoing.get((wp_id, "USES"), [])
                if mid in method_row_ids
            ]
            for mi, mid in enumerate(linked_methods):
                nested_method_ids.add(mid)
                method_positions[mid] = (wi, mi)
                method_node = nodes_by_id.get(mid) or {}
                method_type = str(method_node.get("node_type") or "")
                if method_type not in _reference_type_members("METHOD"):
                    method_type = "ANALYTICAL_METHOD"
                method_sem = semantic(mid)

                assumptions: list[str] = []
                for aid in outgoing.get((mid, "ASSUMES"), []):
                    node = nodes_by_id.get(aid) or {}
                    if str(node.get("node_type") or "") == "ASSUMPTION":
                        text = str(node.get("statement") or "").strip()
                        if text:
                            assumptions.append(text)

                theoretical_properties: list[dict[str, Any]] = []
                for tid in outgoing.get((mid, "HAS_PROPERTY"), []):
                    node = nodes_by_id.get(tid) or {}
                    if str(node.get("node_type") or "") == "THEORETICAL_PROPERTY":
                        theoretical_properties.append(semantic(tid))

                evaluations: list[dict[str, Any]] = []
                linked_evals = [
                    eid
                    for eid in outgoing.get((mid, "VALIDATED_BY"), [])
                    if eid in eval_row_ids
                ]
                for ei, eid in enumerate(linked_evals):
                    nested_eval_ids.add(eid)
                    eval_positions[eid] = (wi, mi, ei)
                    ev_sem = semantic(eid)
                    baselines: list[dict[str, Any]] = []
                    for bid in outgoing.get((eid, "COMPARES_WITH"), []):
                        node = nodes_by_id.get(bid) or {}
                        if str(node.get("node_type") or "") == "BASELINE":
                            nested_baseline_ids.add(bid)
                            baselines.append(semantic(bid))
                    ablations = [
                        str((nodes_by_id.get(aid) or {}).get("statement") or "")
                        for aid in outgoing.get((eid, "INCLUDES_ABLATION"), [])
                        if str((nodes_by_id.get(aid) or {}).get("node_type") or "")
                        == "ABLATION"
                    ]
                    success_criteria = [
                        str((nodes_by_id.get(sid) or {}).get("statement") or "")
                        for sid in outgoing.get((eid, "MEASURED_BY"), [])
                        if str((nodes_by_id.get(sid) or {}).get("node_type") or "")
                        == "SUCCESS_CRITERION"
                    ]
                    evaluations.append(
                        {
                            **ev_sem,
                            "baselines": baselines,
                            "ablations": [x for x in ablations if x],
                            "success_criteria": [x for x in success_criteria if x],
                        }
                    )

                methods.append(
                    {
                        **method_sem,
                        "method_type": method_type,
                        "assumptions": assumptions,
                        "theoretical_properties": theoretical_properties,
                        "evaluations": evaluations,
                    }
                )
            work_packages.append({**wp_sem, "methods": methods})

        innovations: list[dict[str, Any]] = []
        for iid in innovation_row_ids:
            inn_sem = semantic(iid)
            prior_ids = [
                pid
                for pid in incoming.get((iid, "CONTRASTS_WITH"), [])
                if pid in prior_row_ids
            ]
            prior_work = []
            for pid in prior_ids:
                nested_prior_ids.add(pid)
                prior_work.append(semantic(pid))
            contribution = next(
                (
                    str((nodes_by_id.get(cid) or {}).get("statement") or "").strip()
                    for cid in outgoing.get((iid, "YIELDS"), [])
                    if str((nodes_by_id.get(cid) or {}).get("node_type") or "")
                    == "CONTRIBUTION"
                ),
                None,
            )
            evaluation_refs: list[dict[str, int]] = []
            for eid in incoming.get((iid, "EVIDENCES"), []):
                pos = eval_positions.get(eid)
                if pos is not None:
                    evaluation_refs.append(
                        {
                            "work_package_index": pos[0],
                            "method_index": pos[1],
                            "evaluation_index": pos[2],
                        }
                    )
            innovations.append(
                {
                    **inn_sem,
                    "contribution": contribution,
                    "closest_prior_work": prior_work,
                    "evaluation_refs": evaluation_refs,
                }
            )

        foundations: list[dict[str, Any]] = []
        wp_pos = {wid: index for index, wid in enumerate(wp_ids)}
        for fid in foundation_row_ids:
            f_sem = semantic(fid)
            supports: list[dict[str, Any]] = []
            for target in outgoing.get((fid, "SUPPORTS"), []):
                if target in wp_pos:
                    supports.append(
                        {
                            "work_package_index": wp_pos[target],
                            "method_index": None,
                        }
                    )
                elif target in method_positions:
                    wi, mi = method_positions[target]
                    supports.append(
                        {
                            "work_package_index": wi,
                            "method_index": mi,
                        }
                    )
            foundations.append({**f_sem, "supports": supports})

        represented = (
            set(wp_ids)
            | nested_method_ids
            | nested_eval_ids
            | set(innovation_row_ids)
            | nested_prior_ids
            | set(foundation_row_ids)
        )
        unlinked_components: list[dict[str, Any]] = []
        for field, component_type in (
            ("method_ids", "METHOD"),
            ("evaluation_ids", "EVALUATION"),
            ("closest_prior_work_ids", "PRIOR_WORK"),
        ):
            for nid in row_ids[field]:
                if nid in represented:
                    continue
                node = nodes_by_id.get(nid)
                if not node:
                    continue
                unlinked_components.append(
                    {
                        "component_type": component_type,
                        **_semantic_statement_from_node(
                            node,
                            records,
                            authored_bindings,
                            include_node_id=include_machine_ids,
                        ),
                    }
                )

        thread_payload = {
            "gap": {
                **semantic(gap_id),
                "limitation_mechanism": limitation,
            },
            "question": {
                "statement": str(rq.get("statement") or ""),
                "question_type": str(rq.get("question_type") or "TECHNICAL"),
                "answerability": str(rq.get("answerability") or "UNCLEAR"),
                "success_evidence": [
                    str(x) for x in rq.get("success_evidence") or []
                ],
            },
            "objective": semantic(objective_id),
            "thread_assumptions": thread_assumptions,
            "work_packages": work_packages,
            "innovations": innovations,
            "foundation": foundations,
            "falsification_or_comparison_rule": str(
                row.get("falsification_or_comparison_rule") or ""
            ),
            "unlinked_components": unlinked_components,
        }
        if include_machine_ids:
            thread_payload["_thread_index"] = thread_index
        threads.append(thread_payload)

    scope = graph.get("scope_boundaries") or {}
    review_units, _ = _critic_review_units(candidate)
    return {
        "central_proposition": central,
        "scope": {
            "in_scope": [str(x) for x in scope.get("in_scope") or []],
            "out_of_scope": [str(x) for x in scope.get("out_of_scope") or []],
        },
        "research_threads": threads,
        "evidence_gaps": copy.deepcopy(candidate.get("evidence_gap_report") or []),
        "readiness_summary": str(
            (candidate.get("readiness") or {}).get("summary") or ""
        ),
        "review_units": review_units,
    }


def build_argument_architecture_critic_model_input(canonical_envelope: dict[str, Any]) -> dict[str, Any]:
    producer=build_argument_architecture_model_input(canonical_envelope)
    return {"project_task":producer["project_task"],"constraints":producer["constraints"],"evidence_cards":producer["evidence_cards"],"candidate":_critic_candidate_semantics(canonical_envelope),"human_resolutions":producer["human_resolutions"]}


def _repair_model_path(canonical_path: str) -> str:
    tokens=parse_pointer(str(canonical_path))
    if tokens and tokens[0]=="content": tokens=tokens[1:]
    if not tokens: raise JsonPointerError("Targeted Repair cannot authorize the whole object root")
    return format_pointer(tokens)


def _repair_canonical_path(model_path: str) -> str:
    return format_pointer(("content", *parse_pointer(str(model_path))))


def _bounded_repair_context(value: Any, *, depth: int=0, max_depth: int=2) -> Any:
    if depth>=max_depth:
        if isinstance(value,dict): return {"_summary":f"{len(value)} fields"}
        if isinstance(value,list): return [f"... {len(value)} items ..."]
        if isinstance(value,str) and len(value)>320: return value[:320]+"…"
        return copy.deepcopy(value)
    if isinstance(value,dict):
        result={}
        for index,(key,child) in enumerate(value.items()):
            if index>=12: result["_more_fields"]=len(value)-index; break
            if key in {"source_hash","item_hash","object_hash","instruction_hash","document_version_id","span_start","span_end"}: continue
            result[str(key)]=_bounded_repair_context(child,depth=depth+1,max_depth=max_depth)
        return result
    if isinstance(value,list):
        result=[_bounded_repair_context(x,depth=depth+1,max_depth=max_depth) for x in value[:8]]
        if len(value)>8: result.append(f"... {len(value)-8} more items ...")
        return result
    if isinstance(value,str) and len(value)>600: return value[:600]+"…"
    return copy.deepcopy(value)


def _repair_parent_context(original_content: dict[str, Any], model_path: str) -> Any:
    tokens=parse_pointer(model_path)
    if len(tokens)<=1: return _bounded_repair_context(original_content)
    try: parent=resolve_pointer(original_content,format_pointer(tokens[:-1]))
    except JsonPointerError: return None
    return _bounded_repair_context(parent)



def _repair_result_view(original_content: dict[str, Any]) -> dict[str, Any]:
    """Return the business consumer value regardless of legacy envelope shape.

    Argument v9 Targeted Repair receives ProducerResult directly. Generic/legacy
    repair paths may still pass a full producer protocol envelope. This adapter
    is read-only and never changes the authoritative repair root.
    """
    if isinstance(original_content.get("authored_state"), dict):
        return original_content
    result = original_content.get("result")
    if isinstance(result, dict):
        return result
    return original_content


def _repair_candidate_entities(
    original_content: dict[str, Any],
    model_paths: Iterable[str],
) -> list[dict[str, Any]]:
    required_types: set[str] = set()
    include_questions = False
    reference_node_types = _repair_reference_node_types()
    for path in model_paths:
        tokens = parse_pointer(path)
        for field, node_type in reference_node_types.items():
            if field in tokens:
                required_types.add(node_type)
        if "research_question_id" in tokens:
            include_questions = True

    result = _repair_result_view(original_content)
    graph = (result.get("argument_architecture") or {})
    entities: list[dict[str, Any]] = []
    method_node_types = _reference_type_members("METHOD")
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        actual_type = str(node.get("node_type") or "")
        semantic_type = "METHOD" if actual_type in method_node_types else actual_type
        if semantic_type not in required_types:
            continue
        entities.append(
            {
                "entity_id": str(node.get("node_id") or ""),
                "entity_type": semantic_type,
                "statement": str(node.get("statement") or ""),
                "status": str(node.get("status") or "UNKNOWN"),
            }
        )
    if include_questions:
        for q in graph.get("research_questions") or []:
            if isinstance(q, dict):
                entities.append(
                    {
                        "entity_id": str(q.get("node_id") or ""),
                        "entity_type": "RESEARCH_QUESTION",
                        "statement": str(q.get("statement") or ""),
                        "status": "PLANNED",
                    }
                )
    return [e for e in entities if e["entity_id"] and e["statement"]]


def _repair_matrix_context(original_content: dict[str, Any], model_path: str) -> tuple[str, dict[str, Any]] | None:
    tokens=parse_pointer(model_path)
    try: pos=tokens.index("research_design_matrix")
    except ValueError: return None
    if pos+1>=len(tokens) or not str(tokens[pos+1]).isdigit(): return None
    idx=int(tokens[pos+1]); result=_repair_result_view(original_content); matrix=result.get("research_design_matrix") or []
    if not (0<=idx<len(matrix)) or not isinstance(matrix[idx],dict): return None
    row=matrix[idx]; graph=result.get("argument_architecture") or {}
    nodes_by_id={str(n.get("node_id")):n for n in graph.get("nodes") or [] if isinstance(n,dict) and n.get("node_id")}
    qs={str(q.get("node_id")):q for q in graph.get("research_questions") or [] if isinstance(q,dict) and q.get("node_id")}
    def summaries(field):
        values=row.get(field) or []; values=[values] if isinstance(values,str) else values; items=[]
        for eid in values:
            node=nodes_by_id.get(str(eid))
            if node: items.append({"entity_id":str(node.get("node_id") or ""),"entity_type":str(node.get("node_type") or ""),"statement":str(node.get("statement") or ""),"status":str(node.get("status") or "UNKNOWN")})
        return items
    rq_id=str(row.get("research_question_id") or ""); rq=qs.get(rq_id)
    qsum={"entity_id":rq_id,"statement":str(rq.get("statement") or ""),"question_type":str(rq.get("question_type") or ""),"answerability":str(rq.get("answerability") or "")} if isinstance(rq,dict) else None
    cid=f"research-thread-{idx+1:03d}"
    return cid,{"context_id":cid,"research_question":qsum,"gap":summaries("gap_ids"),"objective":summaries("objective_ids"),"work_packages":summaries("work_package_ids")}


def _repair_semantic_neighborhoods(original_content: dict[str, Any], model_paths: Iterable[str]) -> list[dict[str, Any]]:
    result=[]; seen=set()
    for path in model_paths:
        context=_repair_matrix_context(original_content,path)
        if context and context[0] not in seen: seen.add(context[0]); result.append(context[1])
    return result


def _repair_local_context(original_content: dict[str, Any], model_path: str) -> Any:
    mc=_repair_matrix_context(original_content,model_path)
    if mc:
        tokens=parse_pointer(model_path); field=str(tokens[-2]) if len(tokens)>=2 and str(tokens[-1]).isdigit() else str(tokens[-1])
        return {"context_id":mc[0],"field":field}
    return _repair_parent_context(original_content,model_path)


def _argument_repair_semantic_context(value: Any, *, depth: int = 0) -> Any:
    policy = _argument_repair_policy()
    machine_fields = {str(field) for field in policy.get("machine_fields") or ()}
    machine_suffixes = tuple(str(suffix) for suffix in policy.get("machine_suffixes") or ())
    max_depth = int(policy.get("local_context_max_depth") or 3)
    max_items = int(policy.get("local_context_max_items") or 8)

    if depth >= max_depth:
        if isinstance(value, dict):
            return {"_summary": f"{len(value)} semantic fields"}
        if isinstance(value, list):
            return [f"... {len(value)} semantic items ..."]
        return copy.deepcopy(value)
    if isinstance(value, dict):
        projected: dict[str, Any] = {}
        for key, child in value.items():
            skey = str(key)
            if skey in machine_fields or any(
                skey.endswith(suffix) for suffix in machine_suffixes
            ):
                continue
            projected[skey] = _argument_repair_semantic_context(
                child,
                depth=depth + 1,
            )
        return projected
    if isinstance(value, list):
        return [
            _argument_repair_semantic_context(child, depth=depth + 1)
            for child in value[:max_items]
        ]
    return copy.deepcopy(value)


def _argument_authored_state_schema_validator() -> Draft202012Validator:
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "prompt_pack"
        / "schemas"
        / "model"
        / "argument_architecture_model_output.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


def _validate_argument_authoritative_state_or_raise(authored_state: dict[str, Any]) -> None:
    if not isinstance(authored_state, dict):
        raise ValueError("Argument authoritative state must be an object")
    errors = sorted(
        _argument_authored_state_schema_validator().iter_errors(authored_state),
        key=lambda error: list(error.absolute_path),
    )
    if not errors:
        return
    rendered: list[str] = []
    for error in errors:
        suffix = "/".join(str(token) for token in error.absolute_path)
        path = "/result/authored_state" + (f"/{suffix}" if suffix else "")
        rendered.append(f"{path}: {error.message}")
    raise ValueError("invalid Argument authoritative state: " + "; ".join(rendered))


def _argument_authoritative_state_semantic_errors(
    canonical_envelope: dict[str, Any],
    authored_state: dict[str, Any],
    *,
    path_prefix: str = "",
) -> list[str]:
    """Validate references/indices owned by the authoritative state itself.

    Generic output-integrity heuristics deliberately do not enter authored_state;
    this validator is therefore the single source of truth for its evidence and
    local structured references at Producer, Critic re-projection and Repair commit.
    """
    cards, _ = _evidence_records(canonical_envelope)
    known_evidence = {str(card["evidence_id"]) for card in cards}
    errors: list[str] = []

    def path(suffix: str) -> str:
        suffix = suffix if suffix.startswith("/") else "/" + suffix
        return f"{path_prefix}{suffix}" if path_prefix else suffix

    def visit(node: Any, tokens: tuple[str, ...] = ()) -> None:
        if isinstance(node, list):
            for index, item in enumerate(node):
                visit(item, (*tokens, str(index)))
            return
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            child = (*tokens, str(key))
            if key == "evidence_ids" and isinstance(value, list):
                for index, evidence_id in enumerate(value):
                    if str(evidence_id) not in known_evidence:
                        errors.append(
                            path("/" + "/".join((*child, str(index))))
                            + f": evidence_id {evidence_id!r} is not present in evidence_cards"
                        )
            else:
                visit(value, child)

    visit(authored_state)

    for index, question in enumerate(authored_state.get("user_questions") or []):
        if (
            isinstance(question, dict)
            and question.get("question_type") == "CHOICE"
            and not question.get("allowed_values")
        ):
            errors.append(
                path(f"/user_questions/{index}/allowed_values")
                + ": CHOICE requires at least one allowed value"
            )

    for thread_index, thread in enumerate(authored_state.get("research_threads") or []):
        if not isinstance(thread, dict):
            continue
        work_packages = [
            item for item in thread.get("work_packages") or [] if isinstance(item, dict)
        ]

        def evaluation_ref_valid(ref: dict[str, Any]) -> bool:
            wi = ref.get("work_package_index")
            mi = ref.get("method_index")
            ei = ref.get("evaluation_index")
            if not all(isinstance(value, int) for value in (wi, mi, ei)):
                return False
            if not (0 <= wi < len(work_packages)):
                return False
            methods = [
                item for item in work_packages[wi].get("methods") or [] if isinstance(item, dict)
            ]
            if not (0 <= mi < len(methods)):
                return False
            evaluations = [
                item for item in methods[mi].get("evaluations") or [] if isinstance(item, dict)
            ]
            return 0 <= ei < len(evaluations)

        for innovation_index, innovation in enumerate(thread.get("innovations") or []):
            if not isinstance(innovation, dict):
                continue
            for ref_index, ref in enumerate(innovation.get("evaluation_refs") or []):
                if not isinstance(ref, dict) or not evaluation_ref_valid(ref):
                    errors.append(
                        path(
                            f"/research_threads/{thread_index}/innovations/{innovation_index}"
                            f"/evaluation_refs/{ref_index}"
                        )
                        + ": reference must point to an existing evaluation in this research thread"
                    )

        for foundation_index, foundation in enumerate(thread.get("foundation") or []):
            if not isinstance(foundation, dict):
                continue
            for ref_index, ref in enumerate(foundation.get("supports") or []):
                valid = isinstance(ref, dict)
                wi = ref.get("work_package_index") if valid else None
                mi = ref.get("method_index") if valid else None
                valid = valid and isinstance(wi, int) and 0 <= wi < len(work_packages)
                if valid and mi is not None:
                    methods = [
                        item for item in work_packages[wi].get("methods") or [] if isinstance(item, dict)
                    ]
                    valid = isinstance(mi, int) and 0 <= mi < len(methods)
                if not valid:
                    errors.append(
                        path(
                            f"/research_threads/{thread_index}/foundation/{foundation_index}"
                            f"/supports/{ref_index}"
                        )
                        + ": reference must point to an existing work package or method in this research thread"
                    )
    return errors


def _argument_authored_path_map(authored_state: dict[str, Any]) -> dict[str, str]:
    """Map projector-owned object IDs to their only writable authored-state path."""
    root = _argument_repair_authoritative_root()
    mapping: dict[str, str] = {
        "arg-proposition-001": f"{root}/central_proposition"
    }
    for ti, thread in enumerate(authored_state.get("research_threads") or [], 1):
        if not isinstance(thread, dict):
            continue
        prefix = f"{ti:03d}"
        base = f"{root}/research_threads/{ti - 1}"
        mapping[f"arg-gap-{prefix}"] = f"{base}/gap"
        mapping[f"arg-limitation-{prefix}"] = f"{base}/gap/limitation_mechanism"
        mapping[f"arg-rq-{prefix}"] = f"{base}/question"
        mapping[f"arg-objective-{prefix}"] = f"{base}/objective"
        for ai, _ in enumerate(thread.get("thread_assumptions") or [], 1):
            mapping[f"arg-assumption-{prefix}-{ai:02d}"] = f"{base}/thread_assumptions/{ai - 1}"
        for wi, wp in enumerate(thread.get("work_packages") or [], 1):
            if not isinstance(wp, dict):
                continue
            wp_base = f"{base}/work_packages/{wi - 1}"
            mapping[f"arg-wp-{prefix}-{wi:02d}"] = wp_base
            for mi, method in enumerate(wp.get("methods") or [], 1):
                if not isinstance(method, dict):
                    continue
                method_base = f"{wp_base}/methods/{mi - 1}"
                mapping[f"arg-method-{prefix}-{wi:02d}-{mi:02d}"] = method_base
                for ai, _ in enumerate(method.get("assumptions") or [], 1):
                    mapping[f"arg-method-assumption-{prefix}-{wi:02d}-{mi:02d}-{ai:02d}"] = f"{method_base}/assumptions/{ai - 1}"
                for pi, _ in enumerate(method.get("theoretical_properties") or [], 1):
                    mapping[f"arg-theory-{prefix}-{wi:02d}-{mi:02d}-{pi:02d}"] = f"{method_base}/theoretical_properties/{pi - 1}"
                for ei, evaluation in enumerate(method.get("evaluations") or [], 1):
                    if not isinstance(evaluation, dict):
                        continue
                    eval_base = f"{method_base}/evaluations/{ei - 1}"
                    mapping[f"arg-eval-{prefix}-{wi:02d}-{mi:02d}-{ei:02d}"] = eval_base
                    for bi, _ in enumerate(evaluation.get("baselines") or [], 1):
                        mapping[f"arg-baseline-{prefix}-{wi:02d}-{mi:02d}-{ei:02d}-{bi:02d}"] = f"{eval_base}/baselines/{bi - 1}"
                    for ai, _ in enumerate(evaluation.get("ablations") or [], 1):
                        mapping[f"arg-ablation-{prefix}-{wi:02d}-{mi:02d}-{ei:02d}-{ai:02d}"] = f"{eval_base}/ablations/{ai - 1}"
                    for si, _ in enumerate(evaluation.get("success_criteria") or [], 1):
                        mapping[f"arg-success-{prefix}-{wi:02d}-{mi:02d}-{ei:02d}-{si:02d}"] = f"{eval_base}/success_criteria/{si - 1}"
        for ii, innovation in enumerate(thread.get("innovations") or [], 1):
            if not isinstance(innovation, dict):
                continue
            innovation_base = f"{base}/innovations/{ii - 1}"
            mapping[f"arg-innovation-{prefix}-{ii:02d}"] = innovation_base
            mapping[f"arg-contribution-{prefix}-{ii:02d}"] = f"{innovation_base}/contribution"
            for pi, _ in enumerate(innovation.get("closest_prior_work") or [], 1):
                mapping[f"arg-prior-{prefix}-{ii:02d}-{pi:02d}"] = f"{innovation_base}/closest_prior_work/{pi - 1}"
        for fi, _ in enumerate(thread.get("foundation") or [], 1):
            mapping[f"arg-foundation-{prefix}-{fi:02d}"] = f"{base}/foundation/{fi - 1}"
    return mapping


def argument_authoritative_repair_paths(
    original_content: dict[str, Any], findings: Iterable[dict[str, Any]]
) -> list[str]:
    """Resolve Critic findings to authored-state paths; derived projections are never writable."""
    result = original_content if isinstance(original_content, dict) else None
    if not isinstance(result, dict):
        return []
    authored_state = result.get("authored_state")
    if not isinstance(authored_state, dict):
        return []
    node_paths = _argument_authored_path_map(authored_state)
    _, review_mapping = _critic_review_units(result)
    paths: list[str] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        code = str(finding.get("code") or "")
        if code == "ARGUMENT_SCOPE_VIOLATION":
            paths.append(f"{_argument_repair_authoritative_root()}/scope")
        review_key = str(finding.get("semantic_review_unit_key") or "").strip()
        node_id = str(review_mapping.get(review_key) or "") if review_key else ""
        authored_path = node_paths.get(node_id)
        if authored_path:
            paths.append(authored_path)
            continue
        thread = finding.get("semantic_thread")
        if isinstance(thread, int) and thread >= 0:
            thread_path = f"{_argument_repair_authoritative_root()}/research_threads/{thread}"
            try:
                resolve_pointer(original_content, thread_path)
            except JsonPointerError:
                continue
            paths.append(thread_path)
    return list(dict.fromkeys(paths))


def _argument_repair_writable_paths(
    original_content: dict[str, Any],
    authorized_roots: Iterable[str],
) -> list[str]:
    policy = _argument_repair_policy()
    editable_fields = {str(field) for field in policy.get("editable_fields") or ()}
    machine_fields = {str(field) for field in policy.get("machine_fields") or ()}
    machine_suffixes = tuple(str(suffix) for suffix in policy.get("machine_suffixes") or ())
    writable: list[str] = []
    authoritative_root = _argument_repair_authoritative_root()
    authored_state = (original_content.get("authored_state") or {})
    semantic_object_paths = (
        set(_argument_authored_path_map(authored_state).values())
        if isinstance(authored_state, dict)
        else set()
    )

    def field_is_machine_managed(field: str) -> bool:
        return field in machine_fields or any(field.endswith(suffix) for suffix in machine_suffixes)

    def collect(node: Any, tokens: tuple[Any, ...], root_path: str) -> None:
        path = format_pointer(tokens)
        if path != root_path and path in semantic_object_paths:
            # A child semantic object has its own review-unit identity and must be
            # repaired only when a finding explicitly targets that child.
            return
        if tokens:
            field = str(tokens[-1])
            if field in editable_fields and not field_is_machine_managed(field):
                if path not in writable:
                    writable.append(path)
                return
        if isinstance(node, dict):
            for key, value in node.items():
                collect(value, (*tokens, key), root_path)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                collect(value, (*tokens, index), root_path)

    for root in authorized_roots:
        # Argument repair is a transaction over authored state only.  Derived
        # graph/matrix/status fields are immutable projections and cannot be
        # used as repair roots even when a Critic finding points there.
        if not is_ancestor_or_same(authoritative_root, root):
            continue
        try:
            value = resolve_pointer(original_content, root)
        except JsonPointerError:
            continue
        collect(value, tuple(parse_pointer(root)), root)
    return writable


def _effective_repair_allowed_paths(
    payload: dict[str, Any],
    original_content: dict[str, Any],
) -> list[str]:
    raw = [
        _repair_model_path(str(path))
        for path in payload.get("allowed_paths") or []
    ]
    object_type = str(
        (payload.get("original_object") or {}).get("object_type") or ""
    ).upper()
    if object_type != "ARGUMENT_ARCHITECTURE":
        return raw
    return _argument_repair_writable_paths(original_content, raw)


def build_targeted_repair_model_input(canonical_envelope: dict[str, Any]) -> dict[str, Any]:
    payload=canonical_envelope.get("payload") or {}; original_object=payload.get("original_object") or {}; original_content=original_object.get("content") or {}
    if not isinstance(original_content,dict): original_content={}
    allowed=_effective_repair_allowed_paths(payload, original_content)
    findings=[x for x in payload.get("findings_to_repair") or [] if isinstance(x,dict)]
    feedback=((payload.get("contract_feedback") or {}).get("validation_errors") or [])
    direct=[]
    argument_repair = str(original_object.get("object_type") or "").upper() == "ARGUMENT_ARCHITECTURE"
    for finding in findings:
        raw=str(finding.get("target_path_or_span") or "").strip()
        if not raw.startswith("/"): continue
        try: cp=_repair_model_path(raw)
        except JsonPointerError: continue
        overlapping = [
            root for root in allowed
            if is_ancestor_or_same(root, cp) or is_ancestor_or_same(cp, root)
        ]
        if argument_repair:
            direct.extend(overlapping)
        elif overlapping:
            direct.append(cp)
    targets=list(dict.fromkeys(direct)) or allowed
    repair_targets=[]
    for index,path in enumerate(targets,1):
        canonical_target=_repair_canonical_path(path); problems=[]
        for finding in findings:
            raw=str(finding.get("target_path_or_span") or "")
            try: fp=_repair_model_path(raw)
            except (JsonPointerError,ValueError): fp=raw
            if fp==path or (fp.startswith("/") and (is_ancestor_or_same(path,fp) or is_ancestor_or_same(fp,path))):
                desc=str(finding.get("description") or "").strip(); instr=str(finding.get("repair_instruction") or "").strip()
                if desc and desc not in problems: problems.append(desc)
                if instr and instr not in problems: problems.append(instr)
        for error in feedback:
            et=str(error).strip()
            if et and (path in et or canonical_target in et) and et not in problems: problems.append(et)
        if not problems: problems=[str(f.get("description") or "修复当前局部问题") for f in findings[:3]]
        try: current=resolve_pointer(original_content,path); exists=True
        except JsonPointerError: current=None; exists=False
        local_context = _repair_local_context(original_content, path)
        if argument_repair:
            local_context = _argument_repair_semantic_context(local_context)
        repair_targets.append({"target_id":f"repair-target-{index:03d}","path":path,"path_exists":exists,
                               "current_value":_bounded_repair_context(current) if isinstance(current,(dict,list)) else copy.deepcopy(current),
                               "problem":problems[:8],"local_context":local_context})
    related=[]
    for item in findings:
        raw=str(item.get("target_path_or_span") or "").strip(); matched=False
        if raw.startswith("/"):
            try:
                fp=_repair_model_path(raw); matched=any(is_ancestor_or_same(t,fp) or is_ancestor_or_same(fp,t) for t in targets)
            except JsonPointerError: pass
        if not matched: related.append({"code":str(item.get("code") or ""),"description":str(item.get("description") or ""),"repair_instruction":str(item.get("repair_instruction")) if item.get("repair_instruction") is not None else None})
    bq=sum(1 for x in original_content.get("user_questions") or [] if isinstance(x,dict) and bool(x.get("blocking")))
    bf=sum(1 for x in original_content.get("findings") or [] if isinstance(x,dict) and bool(x.get("blocking")) and str(x.get("suggested_route") or "")=="USER")
    human=[]
    for res in payload.get("human_resolutions") or []:
        if not isinstance(res,dict): continue
        ts=res.get("target_paths") or res.get("resolved_target_paths") or []; ts=[ts] if isinstance(ts,str) else ts
        answer=res.get("answer") if "answer" in res else res.get("resolved_value") if "resolved_value" in res else res.get("value")
        human.extend({"target":str(t),"answer":copy.deepcopy(answer)} for t in ts)
    return {"task_context":{"producer_role":str(payload.get("original_producer") or "UNKNOWN"),"object_type":str(original_object.get("object_type") or "UNKNOWN")},
            "repair_targets":repair_targets,
            "reference_context":{"related_findings":related,"candidate_entities":_repair_candidate_entities(original_content,targets),"semantic_neighborhoods":_repair_semantic_neighborhoods(original_content,targets),
                                 "status_context":{"current_status":str(original_content.get("status")) if original_content.get("status") is not None else None,"blocking_user_questions":bq,"blocking_user_findings":bf},"human_resolutions":human},
            "previous_attempt_feedback":[str(x) for x in feedback if str(x).strip()][:20]}


_WF3_SEMANTIC_PROMPTS = frozenset({
    "P-SAFE-ONLINE-PACKAGE",
    "P-SAFE-ONLINE-PACKAGE-CRITIC",
    "P-PUBLIC-RESEARCH-PLAN",
    "P-PUBLIC-RESEARCH-PLAN-SCOPE-CRITIC",
    "P-PUBLIC-RESEARCH-SYNTHESIS",
    "P-PUBLIC-RESEARCH-CRITIC",
    "P-ONLINE-RESULT-IMPORT-CRITIC",
})


def _wf3_payload(canonical_envelope: dict[str, Any]) -> dict[str, Any]:
    payload = canonical_envelope.get("payload") or {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _wf3_strings(values: Any) -> list[str]:
    result: list[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _wf3_semantic_safe_package(value: Any) -> dict[str, Any]:
    package = value if isinstance(value, Mapping) else {}
    return {
        "task_description": str(package.get("task_description") or "").strip(),
        "queries": _wf3_strings(package.get("queries")),
        "allowed_context": _wf3_strings(package.get("allowed_context")),
        "prohibited_inferences": _wf3_strings(package.get("prohibited_inferences")),
        "prohibited_outputs": _wf3_strings(package.get("prohibited_outputs")),
    }


def _wf3_forbidden_categories(payload: Mapping[str, Any]) -> list[str]:
    policy = payload.get("security_policy") if isinstance(payload.get("security_policy"), Mapping) else {}
    values = [
        *list(payload.get("prohibited_fields") or []),
        *list(policy.get("prohibited_external_fields") or []),
    ]
    # These are semantic categories, not internal source values.  Keep the list
    # stable and bounded so the provider never receives internal identifiers.
    return _wf3_strings(values)[:64]


def build_safe_online_package_model_input(canonical_envelope: dict[str, Any]) -> dict[str, Any]:
    payload = _wf3_payload(canonical_envelope)
    need = payload.get("research_need") if isinstance(payload.get("research_need"), Mapping) else {}
    return {
        "research_need": {
            "question": str(need.get("question") or "").strip(),
            "reason_online_needed": str(need.get("reason_online_needed") or "").strip(),
            "desired_output": str(need.get("desired_output") or "").strip(),
        },
        "target_task_type": str(payload.get("target_task_type") or "PUBLIC_RESEARCH"),
        "approved_boundary": {
            "allowed_topics": _wf3_strings(payload.get("allowed_topics")),
            "forbidden_semantic_categories": _wf3_forbidden_categories(payload),
        },
    }


def build_safe_online_package_critic_model_input(canonical_envelope: dict[str, Any]) -> dict[str, Any]:
    payload = _wf3_payload(canonical_envelope)
    policy = payload.get("security_policy") if isinstance(payload.get("security_policy"), Mapping) else {}
    package = _wf3_semantic_safe_package(payload.get("package_candidate"))
    return {
        "outbound_candidate": package,
        "approved_boundary": {
            "allowed_topics": _wf3_strings(payload.get("allowed_topics")),
            "forbidden_semantic_categories": _wf3_strings(policy.get("prohibited_external_fields")),
        },
    }


def build_public_research_plan_model_input(canonical_envelope: dict[str, Any]) -> dict[str, Any]:
    payload = _wf3_payload(canonical_envelope)
    package = payload.get("safe_online_package_content")
    if not isinstance(package, Mapping):
        package = {}
    constraints = payload.get("time_constraints") if isinstance(payload.get("time_constraints"), Mapping) else {}
    known: list[str] = []
    for source in payload.get("known_public_sources") or []:
        if not isinstance(source, Mapping):
            continue
        summary = str(source.get("quoted_text") or "").strip()
        if summary and summary not in known:
            known.append(summary)
    return {
        "approved_task": {
            "task_type": str(package.get("task_type") or payload.get("task_type") or "PUBLIC_RESEARCH"),
            "task_description": str(package.get("task_description") or "").strip(),
            "seed_queries": _wf3_strings(package.get("queries")),
            "allowed_context": _wf3_strings(package.get("allowed_context")),
            "prohibited_inferences": _wf3_strings(package.get("prohibited_inferences")),
            "prohibited_outputs": _wf3_strings(package.get("prohibited_outputs")),
        },
        "time_constraints": {
            "start_date": constraints.get("start_date"),
            "end_date": constraints.get("end_date"),
            "freshness_required": bool(constraints.get("freshness_required")),
        },
        "evidence_requirements": _wf3_strings(payload.get("evidence_requirements")),
        "known_public_source_summaries": known[:40],
        "scope_revision_notes": [
            {
                "description": str(item.get("description") or "").strip(),
                "repair_instruction": str(item.get("repair_instruction") or "").strip(),
            }
            for item in payload.get("revision_findings") or []
            if isinstance(item, Mapping)
            and str(item.get("description") or "").strip()
            and str(item.get("repair_instruction") or "").strip()
        ][:20],
    }


def build_public_research_plan_scope_critic_model_input(canonical_envelope: dict[str, Any]) -> dict[str, Any]:
    payload = _wf3_payload(canonical_envelope)
    boundary = payload.get("approved_boundary") if isinstance(payload.get("approved_boundary"), Mapping) else {}
    questions = _wf3_strings(payload.get("research_questions"))
    executable: list[dict[str, Any]] = []
    for index, item in enumerate(payload.get("executable_queries") or []):
        if not isinstance(item, Mapping):
            continue
        query = str(item.get("query") or "").strip()
        linked = [
            int(value) for value in item.get("linked_question_indexes") or []
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        ]
        if query:
            executable.append({
                "query_index": index,
                "query": query,
                "linked_question_indexes": list(dict.fromkeys(linked)),
            })
    return {
        "approved_boundary": {
            "task_description": str(boundary.get("task_description") or "").strip(),
            "allowed_topics": _wf3_strings(boundary.get("allowed_topics")),
            "allowed_context": _wf3_strings(boundary.get("allowed_context")),
            "prohibited_inferences": _wf3_strings(boundary.get("prohibited_inferences")),
            "prohibited_outputs": _wf3_strings(boundary.get("prohibited_outputs")),
        },
        "research_questions": questions,
        "executable_queries": executable,
    }


def _wf3_model_queries(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in plan.get("queries") or []:
        if isinstance(item, Mapping):
            query = str(item.get("query") or item.get("query_text") or item.get("text") or "").strip()
            linked = [int(v) for v in item.get("linked_question_indexes") or [] if isinstance(v, int) and not isinstance(v, bool)]
        else:
            query = str(item or "").strip()
            linked = []
        if query:
            result.append({"query": query, "linked_question_indexes": list(dict.fromkeys(linked))})
    return result


def _wf3_source_alias_maps(canonical_envelope: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    payload = _wf3_payload(canonical_envelope)
    source_ids: set[str] = set()
    for field in ("retrieved_sources", "public_sources"):
        for ref in payload.get(field) or []:
            if isinstance(ref, Mapping) and ref.get("source_id"):
                source_ids.add(str(ref.get("source_id")))
    for field in ("extracted_passages", "public_source_passages"):
        for passage in payload.get(field) or []:
            if not isinstance(passage, Mapping):
                continue
            ref = passage.get("source_ref") if isinstance(passage.get("source_ref"), Mapping) else {}
            source_id = str(ref.get("source_id") or passage.get("source_id") or "").strip()
            if source_id:
                source_ids.add(source_id)
    real_to_alias = {source_id: f"S{index:03d}" for index, source_id in enumerate(sorted(source_ids), 1)}
    return real_to_alias, {alias: source_id for source_id, alias in real_to_alias.items()}


def _wf3_model_passages(
    values: Any,
    *,
    limit: int = 80,
    source_aliases: Mapping[str, str] | None = None,
) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for passage in values or []:
        if not isinstance(passage, Mapping):
            continue
        source_ref = passage.get("source_ref") if isinstance(passage.get("source_ref"), Mapping) else {}
        source_id = str(source_ref.get("source_id") or passage.get("source_id") or "").strip()
        text = str(passage.get("text") or "").strip()
        relevance = str(passage.get("relevance") or "公开研究证据").strip()
        if source_id and text:
            visible_id = str((source_aliases or {}).get(source_id) or source_id)
            result.append({"source_id": visible_id, "text": text, "relevance": relevance or "公开研究证据"})
        if len(result) >= limit:
            break
    return result


def _wf3_compact_research_sufficiency(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    status = str(source.get("status") or "SUFFICIENT")
    if status not in {"SUFFICIENT", "DEGRADED", "BLOCKING_FAILURE"}:
        status = "SUFFICIENT"
    gaps: list[dict[str, Any]] = []
    for item in source.get("research_gaps") or []:
        if not isinstance(item, Mapping):
            continue
        gaps.append({
            "gap_id": str(item.get("gap_id") or f"gap-{len(gaps)+1}"),
            "scope": str(item.get("scope") or "GLOBAL") if str(item.get("scope") or "GLOBAL") in {"QUERY", "GLOBAL"} else "GLOBAL",
            "linked_question_indexes": [int(v) for v in item.get("linked_question_indexes") or [] if isinstance(v, int) and not isinstance(v, bool)],
            "gap_types": _wf3_strings(item.get("gap_types")) or ["UNSPECIFIED"],
            "source_count": item.get("source_count") if isinstance(item.get("source_count"), int) and not isinstance(item.get("source_count"), bool) else None,
            "required_source_count": item.get("required_source_count") if isinstance(item.get("required_source_count"), int) and not isinstance(item.get("required_source_count"), bool) else None,
            "authoritative_source_count": item.get("authoritative_source_count") if isinstance(item.get("authoritative_source_count"), int) and not isinstance(item.get("authoritative_source_count"), bool) else None,
            "required_authoritative_source_count": item.get("required_authoritative_source_count") if isinstance(item.get("required_authoritative_source_count"), int) and not isinstance(item.get("required_authoritative_source_count"), bool) else None,
            "description": str(item.get("description") or "公开研究证据存在已知缺口。"),
        })
    return {"status": status, "research_gaps": gaps[:32]}


def _wf3_claim_source_ids(claim: Mapping[str, Any]) -> list[str]:
    return _wf3_strings(
        (ref or {}).get("source_id")
        for ref in claim.get("source_refs") or []
        if isinstance(ref, Mapping)
    )


def build_public_research_synthesis_model_input(canonical_envelope: dict[str, Any]) -> dict[str, Any]:
    payload = _wf3_payload(canonical_envelope)
    plan = payload.get("research_plan") if isinstance(payload.get("research_plan"), Mapping) else {}
    real_to_alias, _ = _wf3_source_alias_maps(canonical_envelope)
    return {
        "research_plan": {
            "research_questions": _wf3_strings(plan.get("research_questions")),
            "queries": _wf3_model_queries(plan),
            "evidence_requirements": _wf3_strings(plan.get("evidence_requirements")),
            "prohibited_inferences": _wf3_strings(plan.get("prohibited_inferences")),
        },
        "research_sufficiency": _wf3_compact_research_sufficiency(payload.get("research_sufficiency")),
        "evidence_passages": _wf3_model_passages(payload.get("extracted_passages"), source_aliases=real_to_alias),
    }


def build_public_research_critic_model_input(canonical_envelope: dict[str, Any]) -> dict[str, Any]:
    payload = _wf3_payload(canonical_envelope)
    plan = payload.get("research_plan") if isinstance(payload.get("research_plan"), Mapping) else {}
    synthesis = payload.get("synthesis_candidate") if isinstance(payload.get("synthesis_candidate"), Mapping) else {}
    real_to_alias, _ = _wf3_source_alias_maps(canonical_envelope)
    claims: list[dict[str, Any]] = []
    for claim in synthesis.get("claims") or []:
        if not isinstance(claim, Mapping):
            continue
        claim_id = str(claim.get("claim_id") or "").strip()
        text = str(claim.get("claim_text") or "").strip()
        if claim_id and text:
            claims.append({
                "claim_id": claim_id,
                "claim_text": text,
                "source_ids": [real_to_alias.get(source_id, source_id) for source_id in _wf3_claim_source_ids(claim)],
            })
    comparisons: list[dict[str, Any]] = []
    for item in synthesis.get("source_comparisons") or []:
        if not isinstance(item, Mapping):
            continue
        topic = str(item.get("topic") or "").strip()
        source_ids = [real_to_alias.get(source_id, source_id) for source_id in _wf3_strings(item.get("source_ids"))]
        agreement = str(item.get("agreement") or "").strip()
        summary = str(item.get("summary") or "").strip()
        if topic and len(source_ids) >= 2 and agreement in {"AGREE", "PARTIAL", "CONFLICT"} and summary:
            comparisons.append({
                "topic": topic,
                "source_ids": source_ids,
                "agreement": agreement,
                "summary": summary,
            })
    return {
        "research_questions": _wf3_strings(plan.get("research_questions")),
        "claims": claims,
        "research_sufficiency": _wf3_compact_research_sufficiency(payload.get("research_sufficiency")),
        "evidence_passages": _wf3_model_passages(payload.get("extracted_passages"), source_aliases=real_to_alias),
        "source_comparisons": comparisons,
        "declared_conflicts": _wf3_strings(synthesis.get("conflicts")),
        "declared_limitations": _wf3_strings(synthesis.get("limitations")),
    }


def build_online_result_import_critic_model_input(canonical_envelope: dict[str, Any]) -> dict[str, Any]:
    payload = _wf3_payload(canonical_envelope)
    package = payload.get("approved_safe_package_content") if isinstance(payload.get("approved_safe_package_content"), Mapping) else {}
    result_package = payload.get("result_package") if isinstance(payload.get("result_package"), Mapping) else {}
    claims: list[dict[str, Any]] = []
    for claim in result_package.get("claims") or []:
        if not isinstance(claim, Mapping):
            continue
        claim_id = str(claim.get("claim_id") or "").strip()
        text = str(claim.get("claim_text") or "").strip()
        if claim_id and text:
            claims.append({"claim_id": claim_id, "claim_text": text, "source_ids": _wf3_claim_source_ids(claim)})
    return {
        "approved_task": _wf3_semantic_safe_package(package),
        "claims": claims,
        "public_source_snippets": _wf3_model_passages(payload.get("public_source_passages")),
    }


def _wf3_known_source_refs(canonical_envelope: dict[str, Any]) -> dict[str, dict[str, Any]]:
    payload = _wf3_payload(canonical_envelope)
    refs: dict[str, dict[str, Any]] = {}
    for field in ("retrieved_sources", "public_sources"):
        for ref in payload.get(field) or []:
            if isinstance(ref, Mapping) and ref.get("source_id"):
                refs[str(ref["source_id"])] = copy.deepcopy(dict(ref))
    for field in ("extracted_passages", "public_source_passages"):
        for passage in payload.get(field) or []:
            if not isinstance(passage, Mapping):
                continue
            ref = passage.get("source_ref")
            if isinstance(ref, Mapping) and ref.get("source_id"):
                refs[str(ref["source_id"])] = copy.deepcopy(dict(ref))
    package = payload.get("result_package") if isinstance(payload.get("result_package"), Mapping) else {}
    for claim in package.get("claims") or []:
        if not isinstance(claim, Mapping):
            continue
        for ref in claim.get("source_refs") or []:
            if isinstance(ref, Mapping) and ref.get("source_id"):
                refs[str(ref["source_id"])] = copy.deepcopy(dict(ref))
    return refs


def _wf3_semantic_reference_errors(prompt_id: str, canonical_envelope: dict[str, Any], semantic_output: dict[str, Any]) -> list[str]:
    model_input = build_semantic_model_input(prompt_id, canonical_envelope)
    errors: list[str] = []
    if prompt_id == "P-PUBLIC-RESEARCH-PLAN":
        count = len(semantic_output.get("research_questions") or [])
        for qindex, query in enumerate(semantic_output.get("queries") or []):
            if not isinstance(query, Mapping):
                continue
            for lindex, linked in enumerate(query.get("linked_question_indexes") or []):
                if not isinstance(linked, int) or isinstance(linked, bool) or linked < 0 or linked >= count:
                    errors.append(f"/queries/{qindex}/linked_question_indexes/{lindex}: index {linked!r} is outside research_questions")
    elif prompt_id == "P-PUBLIC-RESEARCH-PLAN-SCOPE-CRITIC":
        queries = [item for item in model_input.get("executable_queries") or [] if isinstance(item, Mapping)]
        query_indexes = {int(item.get("query_index")) for item in queries if isinstance(item.get("query_index"), int)}
        for index, issue in enumerate(semantic_output.get("issues") or []):
            if not isinstance(issue, Mapping):
                continue
            query_index = issue.get("query_index")
            if not isinstance(query_index, int) or isinstance(query_index, bool) or query_index not in query_indexes:
                errors.append(f"/issues/{index}/query_index: unknown query_index {query_index!r}")
    elif prompt_id == "P-PUBLIC-RESEARCH-SYNTHESIS":
        known = {str(item.get("source_id")) for item in model_input.get("evidence_passages") or [] if isinstance(item, Mapping)}
        for cindex, claim in enumerate(semantic_output.get("claims") or []):
            if not isinstance(claim, Mapping):
                continue
            for sindex, source_id in enumerate(claim.get("source_ids") or []):
                if str(source_id) not in known:
                    errors.append(f"/claims/{cindex}/source_ids/{sindex}: source_id {source_id!r} is not present in evidence_passages")
        for cindex, comparison in enumerate(semantic_output.get("source_comparisons") or []):
            if not isinstance(comparison, Mapping):
                continue
            for sindex, source_id in enumerate(comparison.get("source_ids") or []):
                if str(source_id) not in known:
                    errors.append(f"/source_comparisons/{cindex}/source_ids/{sindex}: source_id {source_id!r} is not present in evidence_passages")
    elif prompt_id == "P-PUBLIC-RESEARCH-CRITIC":
        known_claims = {str(item.get("claim_id")) for item in model_input.get("claims") or [] if isinstance(item, Mapping)}
        known_sources = {str(item.get("source_id")) for item in model_input.get("evidence_passages") or [] if isinstance(item, Mapping)}
        question_count = len(model_input.get("research_questions") or [])
        for index, issue in enumerate(semantic_output.get("issues") or []):
            if not isinstance(issue, Mapping):
                continue
            issue_type = str(issue.get("issue_type") or "")
            claim_id = issue.get("claim_id")
            question_index = issue.get("question_index")
            if claim_id is not None and str(claim_id) not in known_claims:
                errors.append(f"/issues/{index}/claim_id: unknown claim_id {claim_id!r}")
            if question_index is not None and (not isinstance(question_index, int) or isinstance(question_index, bool) or question_index < 0 or question_index >= question_count):
                errors.append(f"/issues/{index}/question_index: index {question_index!r} is outside research_questions")
            if issue_type in {"UNSUPPORTED_CLAIM", "OVERGENERALIZED_CLAIM"} and claim_id is None:
                errors.append(f"/issues/{index}/claim_id: {issue_type} requires a claim_id")
            if issue_type == "UNANSWERED_RESEARCH_QUESTION" and question_index is None:
                errors.append(f"/issues/{index}/question_index: UNANSWERED_RESEARCH_QUESTION requires question_index")
            for sindex, source_id in enumerate(issue.get("evidence_source_ids") or []):
                if str(source_id) not in known_sources:
                    errors.append(f"/issues/{index}/evidence_source_ids/{sindex}: unknown source_id {source_id!r}")
    elif prompt_id == "P-ONLINE-RESULT-IMPORT-CRITIC":
        known_claims = {str(item.get("claim_id")) for item in model_input.get("claims") or [] if isinstance(item, Mapping)}
        decisions = [item for item in semantic_output.get("claim_decisions") or [] if isinstance(item, Mapping)]
        seen: set[str] = set()
        for index, item in enumerate(decisions):
            claim_id = str(item.get("claim_id") or "")
            if claim_id not in known_claims:
                errors.append(f"/claim_decisions/{index}/claim_id: unknown claim_id {claim_id!r}")
            if claim_id in seen:
                errors.append(f"/claim_decisions/{index}/claim_id: duplicate claim_id {claim_id!r}")
            seen.add(claim_id)
        missing = sorted(known_claims - seen)
        if missing:
            errors.append("/claim_decisions: every input claim must be classified exactly once; missing=" + ",".join(missing))
        for index, issue in enumerate(semantic_output.get("security_issues") or []):
            if not isinstance(issue, Mapping):
                continue
            claim_id = issue.get("claim_id")
            if claim_id is not None and str(claim_id) not in known_claims:
                errors.append(f"/security_issues/{index}/claim_id: unknown claim_id {claim_id!r}")
    return errors


def _wf3_canonical_base(canonical_envelope: dict[str, Any], prompt_id: str) -> dict[str, Any]:
    return {
        "schema_version": str(canonical_envelope.get("schema_version") or "2.0"),
        "prompt_id": prompt_id,
        "prompt_version": str(canonical_envelope.get("prompt_version") or "2.0.0"),
        "status": "PASS",
        "result": {},
        "findings": [],
        "unresolved_items": [],
        "user_questions": [],
        "source_refs": [],
        "warnings": [],
    }


def expand_safe_online_package_model_output(canonical_envelope: dict[str, Any], semantic_output: dict[str, Any]) -> dict[str, Any]:
    payload = _wf3_payload(canonical_envelope)
    output = _wf3_canonical_base(canonical_envelope, "P-SAFE-ONLINE-PACKAGE")
    output["result"] = {
        "package_id": "runtime",
        "task_type": str(payload.get("target_task_type") or "PUBLIC_RESEARCH"),
        "task_description": str(semantic_output.get("task_description") or ""),
        "queries": copy.deepcopy(semantic_output.get("queries") or []),
        "allowed_context": copy.deepcopy(semantic_output.get("allowed_context") or []),
        "entity_placeholders": [],
        "removed_fields": _wf3_forbidden_categories(payload),
        "prohibited_inferences": copy.deepcopy(semantic_output.get("prohibited_inferences") or []),
        "prohibited_outputs": copy.deepcopy(semantic_output.get("prohibited_outputs") or []),
        "valid_until": None,
        "security_level": "PUBLIC",
    }
    return output


def _wf3_finding(*, code: str, category: str, target_type: str, target_path: str | None, description: str, repair_instruction: str | None, route: str, blocking: bool, severity: str = "P1", evidence_refs: list[str] | None = None, repairable: bool = True) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "category": category,
        "target_type": target_type,
        "target_path_or_span": target_path,
        "description": description,
        "evidence_refs": list(evidence_refs or []),
        "repairable": repairable,
        "repair_instruction": repair_instruction,
        "suggested_route": route,
        "blocking": blocking,
    }


def expand_safe_online_package_critic_model_output(canonical_envelope: dict[str, Any], semantic_output: dict[str, Any]) -> dict[str, Any]:
    output = _wf3_canonical_base(canonical_envelope, "P-SAFE-ONLINE-PACKAGE-CRITIC")
    findings: list[dict[str, Any]] = []
    redactions: list[str] = []
    for issue in semantic_output.get("issues") or []:
        if not isinstance(issue, Mapping):
            continue
        risk_type = str(issue.get("risk_type") or "")
        model_action = str(issue.get("required_action") or "")
        field = str(issue.get("outbound_field") or "")
        code = {
            "IDENTIFIABLE_PROJECT": "SAFE_PACKAGE_REIDENTIFICATION",
            "COMBINATION_REIDENTIFICATION": "SAFE_PACKAGE_REIDENTIFICATION",
            "SCOPE_EXCESS": "SAFE_PACKAGE_SCOPE_EXCESS",
            "MISSING_PROHIBITION": "SAFE_PACKAGE_MISSING_PROHIBITION",
        }.get(risk_type, "SAFE_PACKAGE_SEMANTIC_RISK")
        runtime_action = {
            "IDENTIFIABLE_PROJECT": "REDACT",
            "COMBINATION_REIDENTIFICATION": "REDACT",
            "SCOPE_EXCESS": "NARROW_SCOPE",
            "MISSING_PROHIBITION": "ADD_PROHIBITION",
        }.get(risk_type, "REDACT")
        redaction = str(issue.get("required_redaction") or "").strip()
        if redaction and redaction not in redactions:
            redactions.append(redaction)
        description = str(issue.get("description") or "发现外发语义风险。")
        if model_action == "BLOCK":
            description += " 模型建议 BLOCK 仅作为语义观察；最终控制策略由运行时决定。"
        findings.append(_wf3_finding(
            code=code,
            category="SECURITY",
            target_type="SAFE_ONLINE_PACKAGE",
            target_path=f"/payload/package_candidate/{field}" if field else "/payload/package_candidate",
            description=description,
            repair_instruction=(redaction or f"按 {runtime_action} 策略修订准备外发的文本。"),
            route="ORIGINAL_PRODUCER",
            blocking=True,
            severity="P1",
            evidence_refs=[],
            repairable=True,
        ))
    output["findings"] = findings
    output["result"] = {
        "verdict": "REVISE" if findings else "ACCEPT_FOR_HUMAN_APPROVAL",
        "reidentification_risk": str(semantic_output.get("risk_level") or "LOW"),
        "checked_prohibited_fields": [],
        "required_redactions": redactions,
    }
    output["status"] = "REVISE" if findings else "PASS"
    return output


def expand_public_research_plan_model_output(canonical_envelope: dict[str, Any], semantic_output: dict[str, Any]) -> dict[str, Any]:
    payload = _wf3_payload(canonical_envelope)
    package = payload.get("safe_online_package_content") if isinstance(payload.get("safe_online_package_content"), Mapping) else {}
    authoritative_evidence_requirements = _wf3_strings(payload.get("evidence_requirements"))
    authoritative_prohibited_inferences = _wf3_strings(package.get("prohibited_inferences"))
    output = _wf3_canonical_base(canonical_envelope, "P-PUBLIC-RESEARCH-PLAN")
    queries = []
    for item in semantic_output.get("queries") or []:
        if isinstance(item, Mapping):
            queries.append({
                "query_id": "runtime",
                "query": str(item.get("query") or ""),
                "linked_question_indexes": copy.deepcopy(item.get("linked_question_indexes") or []),
            })
    output["result"] = {
        "plan_id": "runtime",
        "task_type": str(payload.get("task_type") or "PUBLIC_RESEARCH"),
        "research_questions": copy.deepcopy(semantic_output.get("research_questions") or []),
        "binding_contract_version": "1.0",
        "queries": queries,
        "source_priorities": copy.deepcopy(semantic_output.get("source_priorities") or []),
        "time_scope": None,
        "evidence_requirements": authoritative_evidence_requirements,
        "prohibited_inferences": authoritative_prohibited_inferences,
    }
    return output


def expand_public_research_plan_scope_critic_model_output(canonical_envelope: dict[str, Any], semantic_output: dict[str, Any]) -> dict[str, Any]:
    output = _wf3_canonical_base(canonical_envelope, "P-PUBLIC-RESEARCH-PLAN-SCOPE-CRITIC")
    model_input = build_public_research_plan_scope_critic_model_input(canonical_envelope)
    query_count = len(model_input.get("executable_queries") or [])
    rejected: list[int] = []
    findings: list[dict[str, Any]] = []
    for issue in semantic_output.get("issues") or []:
        if not isinstance(issue, Mapping):
            continue
        query_index = issue.get("query_index")
        if not isinstance(query_index, int) or isinstance(query_index, bool) or not 0 <= query_index < query_count:
            continue
        if query_index not in rejected:
            rejected.append(query_index)
        issue_type = str(issue.get("issue_type") or "OUTSIDE_APPROVED_SCOPE")
        code = (
            "PUBLIC_RESEARCH_QUERY_SENSITIVE_INFERENCE"
            if issue_type == "SENSITIVE_INFERENCE_RISK"
            else "PUBLIC_RESEARCH_QUERY_SCOPE_EXCESS"
        )
        findings.append(_wf3_finding(
            code=code,
            category="SECURITY",
            target_type="PUBLIC_RESEARCH_QUERY",
            target_path=f"/payload/executable_queries/{query_index}/query",
            description=str(issue.get("description") or "最终公开检索查询超出批准研究边界。"),
            repair_instruction="重新生成该查询，使其仅覆盖已经批准的公开研究主题且不要求受禁止的敏感推断。",
            route="ORIGINAL_PRODUCER",
            blocking=True,
            severity="P1",
            evidence_refs=[],
            repairable=False,
        ))
    approved = [index for index in range(query_count) if index not in set(rejected)]
    output["findings"] = findings
    output["result"] = {
        "verdict": "REVISE" if rejected else "ACCEPT",
        "approved_query_indexes": approved,
        "rejected_query_indexes": rejected,
    }
    output["status"] = "REVISE" if rejected else "PASS"
    return output


def expand_public_research_synthesis_model_output(canonical_envelope: dict[str, Any], semantic_output: dict[str, Any]) -> dict[str, Any]:
    output = _wf3_canonical_base(canonical_envelope, "P-PUBLIC-RESEARCH-SYNTHESIS")
    payload = _wf3_payload(canonical_envelope)
    refs = _wf3_known_source_refs(canonical_envelope)
    _, alias_to_real = _wf3_source_alias_maps(canonical_envelope)
    claims: list[dict[str, Any]] = []
    for item in semantic_output.get("claims") or []:
        if not isinstance(item, Mapping):
            continue
        qualifiers = _wf3_strings([*(item.get("qualifiers") or []), "MODEL_SYNTHESIS"])
        real_source_ids = [alias_to_real.get(str(source_id), str(source_id)) for source_id in item.get("source_ids") or []]
        source_refs = [copy.deepcopy(refs[source_id]) for source_id in real_source_ids if source_id in refs]
        claims.append({
            "claim_id": "runtime",
            "claim_text": str(item.get("claim_text") or ""),
            "claim_type": "PUBLIC_CLAIM",
            "subject_id": None,
            "temporal_status": "UNKNOWN",
            "qualifiers": qualifiers,
            "numeric_values": [],
            "source_refs": source_refs,
            "knowledge_status": "DOCUMENT_EXTRACTED",
            "security_level": "PUBLIC",
        })
    comparisons: list[dict[str, Any]] = []
    for item in semantic_output.get("source_comparisons") or []:
        if not isinstance(item, Mapping):
            continue
        normalized = copy.deepcopy(dict(item))
        normalized["source_ids"] = [alias_to_real.get(str(source_id), str(source_id)) for source_id in item.get("source_ids") or []]
        comparisons.append(normalized)

    limitations = _wf3_strings(semantic_output.get("limitations"))
    sufficiency = payload.get("research_sufficiency") if isinstance(payload.get("research_sufficiency"), Mapping) else {}
    if str(sufficiency.get("status") or "") == "DEGRADED":
        for gap in sufficiency.get("research_gaps") or []:
            if not isinstance(gap, Mapping):
                continue
            description = str(gap.get("description") or "公开研究证据存在已知缺口。").strip()
            marker = f"RESEARCH_GAP[{gap.get('gap_id') or 'unknown'}]: {description}"
            if marker not in limitations:
                limitations.append(marker)
    coverage_summary = str(semantic_output.get("coverage_summary") or "").strip()
    if str(sufficiency.get("status") or "") == "DEGRADED" and "DEGRADED" not in coverage_summary.upper():
        coverage_summary = (coverage_summary + " Research sufficiency is DEGRADED; known evidence gaps are preserved in limitations.").strip()
    output["result"] = {
        "claims": claims,
        "source_comparisons": comparisons,
        "conflicts": copy.deepcopy(semantic_output.get("conflicts") or []),
        "limitations": limitations,
        "coverage_summary": coverage_summary or "Public research synthesis completed with the available evidence.",
    }
    return output


def expand_public_research_critic_model_output(canonical_envelope: dict[str, Any], semantic_output: dict[str, Any]) -> dict[str, Any]:
    output = _wf3_canonical_base(canonical_envelope, "P-PUBLIC-RESEARCH-CRITIC")
    payload = _wf3_payload(canonical_envelope)
    sufficiency = payload.get("research_sufficiency") if isinstance(payload.get("research_sufficiency"), Mapping) else {}
    acknowledged_question_indexes: set[int] = set()
    for gap in sufficiency.get("research_gaps") or []:
        if not isinstance(gap, Mapping):
            continue
        for value in gap.get("linked_question_indexes") or []:
            if isinstance(value, int) and not isinstance(value, bool):
                acknowledged_question_indexes.add(value)
    _, alias_to_real = _wf3_source_alias_maps(canonical_envelope)

    findings: list[dict[str, Any]] = []
    unsupported: list[str] = []
    for issue in semantic_output.get("issues") or []:
        if not isinstance(issue, Mapping):
            continue
        issue_type = str(issue.get("issue_type") or "")
        claim_id = str(issue.get("claim_id") or "").strip()
        question_index = issue.get("question_index")
        evidence_refs = [alias_to_real.get(str(source_id), str(source_id)) for source_id in issue.get("evidence_source_ids") or []]

        if issue_type == "UNANSWERED_RESEARCH_QUESTION" and isinstance(question_index, int) and question_index in acknowledged_question_indexes:
            findings.append(_wf3_finding(
                code="PUBLIC_CRITIC_ACKNOWLEDGED_RESEARCH_GAP",
                category="EVIDENCE",
                target_type="PUBLIC_RESEARCH_SYNTHESIS",
                target_path=f"/payload/research_plan/research_questions/{question_index}",
                description=str(issue.get("description") or "该研究问题对应确定性 ResearchGap，综合结果已按证据不足处理。"),
                repair_instruction=None,
                route="ORIGINAL_PRODUCER",
                blocking=False,
                severity="P2",
                evidence_refs=evidence_refs,
                repairable=False,
            ))
            continue

        if issue_type in {"UNSUPPORTED_CLAIM", "OVERGENERALIZED_CLAIM"} and claim_id and claim_id not in unsupported:
            unsupported.append(claim_id)
        if issue_type in {"UNSUPPORTED_CLAIM", "OVERGENERALIZED_CLAIM"}:
            code = "PUBLIC_CRITIC_UNSUPPORTED_CLAIM"
            path = f"/payload/synthesis_candidate/claims/{claim_id}" if claim_id else "/payload/synthesis_candidate/claims"
        elif issue_type == "MISSING_COUNTEREVIDENCE":
            code = "PUBLIC_CRITIC_MISSING_COUNTEREVIDENCE"
            path = "/payload/synthesis_candidate/limitations"
        else:
            code = "PUBLIC_CRITIC_UNANSWERED_RESEARCH_QUESTION"
            path = f"/payload/research_plan/research_questions/{question_index}" if question_index is not None else "/payload/research_plan/research_questions"
        findings.append(_wf3_finding(
            code=code,
            category="EVIDENCE",
            target_type="PUBLIC_RESEARCH_SYNTHESIS",
            target_path=path,
            description=str(issue.get("description") or "公开研究综合存在语义证据缺口。"),
            repair_instruction=str(issue.get("repair_instruction") or "依据已有公开证据修订综合结果。"),
            route="ORIGINAL_PRODUCER",
            blocking=True,
            severity="P1",
            evidence_refs=evidence_refs,
            repairable=True,
        ))
    blocking_findings = [item for item in findings if bool(item.get("blocking"))]
    output["findings"] = findings
    output["result"] = {
        "verdict": "REVISE" if blocking_findings else "ACCEPT_FOR_IMPORT_REVIEW",
        "source_quality_summary": [],
        "unsupported_claim_ids": unsupported,
        "missing_counterevidence_topics": copy.deepcopy(semantic_output.get("missing_counterevidence_topics") or []),
    }
    output["status"] = "REVISE" if blocking_findings else "PASS"
    return output


_WF3_PROMPT_INJECTION_TARGETS = {
    "CRITIC_AGENT",
    "MODEL_ROLE",
    "SYSTEM_RULES",
    "TOOL_BEHAVIOR",
    "OUTPUT_CONSTRAINT",
    "HIDDEN_CONTEXT",
}
_WF3_PROMPT_INJECTION_CONTROL_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE | re.DOTALL)
    for pattern in (
        r"\b(?:ignore|disregard|forget|override|bypass)\b.{0,100}\b(?:instruction|rule|prompt|policy|system|developer)\b",
        r"\b(?:system|developer)\s+(?:prompt|message|instruction)s?\b",
        r"\b(?:act as|pretend to be|you are now)\b",
        r"\b(?:respond|reply|output|return)\b.{0,80}\b(?:only|json|schema|format)\b",
        r"\b(?:call|invoke|use)\b.{0,80}\b(?:tool|function|api)\b",
        r"\b(?:execute|run)\b.{0,80}\b(?:command|code|script|shell)\b",
        r"\b(?:reveal|show|print|leak|expose)\b.{0,100}\b(?:system prompt|hidden instruction|developer message|secret)\b",
        r"(?:忽略|无视|覆盖|绕过).{0,40}(?:指令|规则|提示词|系统消息|开发者消息|安全策略)",
        r"(?:你现在是|扮演|假装成为).{0,40}(?:助手|模型|智能体|系统)",
        r"(?:调用|使用).{0,40}(?:工具|函数|接口|API)",
        r"(?:执行|运行).{0,40}(?:命令|代码|脚本|Shell)",
        r"(?:输出|返回|回复).{0,40}(?:JSON|格式|模式|仅仅|只能)",
        r"(?:泄露|显示|打印|暴露).{0,40}(?:系统提示词|隐藏指令|开发者消息|秘密)",
    )
)


def _wf3_prompt_injection_corroborated(issue: Mapping[str, Any]) -> bool:
    """Return True only for a structurally supported control-plane instruction.

    The semantic critic may *suspect* prompt injection, but it does not own the
    P0/blocking decision.  Runtime requires a declared control target, a concrete
    requested behavior, and lexical evidence of an instruction that attempts to
    alter model/agent/tool/output control.  Rhetorical academic prose therefore
    remains an auditable observation rather than a workflow-wide security block.
    """

    target = str(issue.get("instruction_target") or "").strip().upper()
    requested_behavior = str(issue.get("requested_behavior") or "").strip()
    evidence_excerpt = str(issue.get("evidence_excerpt") or "").strip()
    if target not in _WF3_PROMPT_INJECTION_TARGETS:
        return False
    if not requested_behavior or not evidence_excerpt:
        return False
    evidence = f"{evidence_excerpt}\n{requested_behavior}"
    return any(pattern.search(evidence) for pattern in _WF3_PROMPT_INJECTION_CONTROL_PATTERNS)


def expand_online_result_import_critic_model_output(canonical_envelope: dict[str, Any], semantic_output: dict[str, Any]) -> dict[str, Any]:
    output = _wf3_canonical_base(canonical_envelope, "P-ONLINE-RESULT-IMPORT-CRITIC")
    decisions = {
        str(item.get("claim_id")): str(item.get("decision"))
        for item in semantic_output.get("claim_decisions") or []
        if isinstance(item, Mapping)
    }
    payload = _wf3_payload(canonical_envelope)
    result_package = payload.get("result_package") if isinstance(payload.get("result_package"), Mapping) else {}
    claim_source_bindings = {
        str(claim.get("claim_id")): _wf3_claim_source_ids(claim)
        for claim in result_package.get("claims") or []
        if isinstance(claim, Mapping) and claim.get("claim_id")
    }
    findings: list[dict[str, Any]] = []
    forced_reject: set[str] = set()
    hard_blocked = False
    package_review_required = False

    for issue in semantic_output.get("security_issues") or []:
        if not isinstance(issue, Mapping):
            continue
        issue_type = str(issue.get("issue_type") or "")
        claim_id = str(issue.get("claim_id") or "").strip()
        target_path = (
            f"/payload/result_package/claims/{claim_id}"
            if claim_id
            else "/payload/result_package/claims"
        )

        if issue_type == "PROMPT_INJECTION":
            if _wf3_prompt_injection_corroborated(issue):
                hard_blocked = True
                code, blocking, severity = "IMPORT_PROMPT_INJECTION", True, "P0"
                route, repairable = "BLOCK", False
                repair_instruction = "阻止导入并移除含控制型指令的公开来源后重新审查。"
            else:
                # Model-only suspicion is retained for audit but cannot create a
                # P0/global reject.  This is deliberately distinct from a
                # runtime-corroborated injection finding.
                code, blocking, severity = "IMPORT_PROMPT_INJECTION_SUSPECTED", False, "P2"
                route, repairable = "ORIGINAL_PRODUCER", False
                repair_instruction = "保留为语义安全观察；未获运行时控制指令证据，不升级为阻断。"
        elif issue_type in {"SCOPE_VIOLATION", "SENSITIVE_INFERENCE"}:
            code = "IMPORT_SCOPE_VIOLATION" if issue_type == "SCOPE_VIOLATION" else "IMPORT_SENSITIVE_INFERENCE"
            if claim_id:
                # A claim-local semantic boundary issue rejects only that claim.
                forced_reject.add(claim_id)
                blocking, severity = False, "P2"
                route, repairable = "ORIGINAL_PRODUCER", False
                repair_instruction = "仅排除该越界/敏感推断 claim；其余 claim 继续独立审查。"
            else:
                # A package-level semantic boundary issue must be regenerated,
                # but is not a model-owned P0 hard security block.
                package_review_required = True
                blocking, severity = True, "P1"
                route, repairable = "ORIGINAL_PRODUCER", True
                repair_instruction = "修订公开研究综合以恢复批准范围边界后重新审查。"
        elif issue_type == "UNSOURCED_CLAIM":
            if claim_id and not claim_source_bindings.get(claim_id):
                # Source-reference existence is deterministic.  Only an actual
                # missing binding may force rejection here; semantic support
                # adequacy has already been reviewed by Research Critic.
                forced_reject.add(claim_id)
                code, blocking, severity = "IMPORT_UNSOURCED_CLAIM", False, "P2"
                route, repairable = "ORIGINAL_PRODUCER", False
                repair_instruction = "不导入缺少确定性公开来源绑定的 claim。"
            else:
                code, blocking, severity = "IMPORT_UNSOURCED_CLAIM_SUSPECTED", False, "P2"
                route, repairable = "ORIGINAL_PRODUCER", False
                repair_instruction = (
                    "保留为语义观察；运行时已确认来源绑定存在，不重复覆盖 Research Critic 的证据支持裁决。"
                )
        else:
            code, blocking, severity = "IMPORT_SEMANTIC_REVIEW", False, "P2"
            route, repairable = "ORIGINAL_PRODUCER", False
            repair_instruction = "保留为非阻断语义观察。"

        findings.append(_wf3_finding(
            code=code,
            category="SECURITY" if issue_type != "UNSOURCED_CLAIM" else "EVIDENCE",
            target_type="PUBLIC_CLAIM_IMPORT",
            target_path=target_path,
            description=str(issue.get("description") or "导入候选存在语义风险。"),
            repair_instruction=repair_instruction,
            route=route,
            blocking=blocking,
            severity=severity,
            evidence_refs=[],
            repairable=repairable,
        ))

    accepted = [
        claim_id for claim_id, decision in decisions.items()
        if decision == "IMPORT_PUBLIC_CLAIM" and claim_id not in forced_reject
    ]
    reference_only = [
        claim_id for claim_id, decision in decisions.items()
        if decision == "REFERENCE_ONLY" and claim_id not in forced_reject
    ]
    rejected = [
        claim_id for claim_id, decision in decisions.items()
        if decision == "REJECT" or claim_id in forced_reject
    ]

    if hard_blocked:
        accepted = []
        reference_only = []
        rejected = list(decisions)
        recommendation = "REJECT"
    elif package_review_required:
        recommendation = "RETURN_FOR_REVIEW"
    elif accepted:
        recommendation = "IMPORT_PUBLIC_CLAIM_CANDIDATES"
    elif reference_only:
        recommendation = "IMPORT_REFERENCE_ONLY"
    else:
        recommendation = "RETURN_FOR_REVIEW" if decisions else "REJECT"

    output["findings"] = findings
    output["status"] = "BLOCK" if hard_blocked else ("REVISE" if package_review_required else "PASS")
    output["result"] = {
        "import_recommendation": recommendation,
        "accepted_claim_ids": accepted,
        "reference_only_claim_ids": reference_only,
        "rejected_claim_ids": rejected,
        "prompt_injection_detected": hard_blocked,
        "scope_violation_detected": any(
            str(item.get("issue_type")) in {"SCOPE_VIOLATION", "SENSITIVE_INFERENCE"}
            for item in semantic_output.get("security_issues") or []
            if isinstance(item, Mapping)
        ),
        "required_user_confirmations": [],
    }
    return output

def build_semantic_model_input(prompt_id: str, canonical_envelope: dict[str, Any]) -> dict[str, Any]:
    if prompt_id=="P-ARGUMENT-ARCHITECTURE": return build_argument_architecture_model_input(canonical_envelope)
    if prompt_id=="P-ARGUMENT-ARCHITECTURE-CRITIC": return build_argument_architecture_critic_model_input(canonical_envelope)
    if prompt_id=="P-TARGETED-REPAIR": return build_targeted_repair_model_input(canonical_envelope)
    if prompt_id=="P-SAFE-ONLINE-PACKAGE": return build_safe_online_package_model_input(canonical_envelope)
    if prompt_id=="P-SAFE-ONLINE-PACKAGE-CRITIC": return build_safe_online_package_critic_model_input(canonical_envelope)
    if prompt_id=="P-PUBLIC-RESEARCH-PLAN": return build_public_research_plan_model_input(canonical_envelope)
    if prompt_id=="P-PUBLIC-RESEARCH-PLAN-SCOPE-CRITIC": return build_public_research_plan_scope_critic_model_input(canonical_envelope)
    if prompt_id=="P-PUBLIC-RESEARCH-SYNTHESIS": return build_public_research_synthesis_model_input(canonical_envelope)
    if prompt_id=="P-PUBLIC-RESEARCH-CRITIC": return build_public_research_critic_model_input(canonical_envelope)
    if prompt_id=="P-ONLINE-RESULT-IMPORT-CRITIC": return build_online_result_import_critic_model_input(canonical_envelope)
    raise KeyError(f"No semantic model input builder registered for {prompt_id}")



def targeted_repair_structural_blockers(
    canonical_envelope: dict[str, Any],
) -> list[str]:
    model_input = build_targeted_repair_model_input(canonical_envelope)
    blockers: list[str] = []
    object_type = str(
        ((canonical_envelope.get("payload") or {}).get("original_object") or {}).get("object_type") or ""
    ).upper()
    if object_type == "ARGUMENT_ARCHITECTURE" and not model_input.get("repair_targets"):
        blockers.append(
            "Argument Architecture repair has no authoritative authored-state target; regeneration is required"
        )
    types = {
        str(e.get("entity_type") or "")
        for e in model_input.get("reference_context", {}).get("candidate_entities", [])
        if isinstance(e, dict)
    }
    reference_node_types = _repair_reference_node_types()
    for target in model_input.get("repair_targets") or []:
        if not isinstance(target, dict):
            continue
        path = str(target.get("path") or "")
        if not bool(target.get("path_exists")):
            blockers.append(
                f"{path}: target does not exist; local repair cannot create structure"
            )
            continue
        tokens = parse_pointer(path)
        required = next(
            (
                node_type
                for field, node_type in reference_node_types.items()
                if field in tokens
            ),
            None,
        )
        if required and required not in types:
            blockers.append(
                f"{path}: no existing {required} entity is available; repair would require creating a business entity"
            )
    return list(dict.fromkeys(blockers))



def _same_container_shape(before: Any, after: Any) -> bool:
    if isinstance(before, dict):
        return (
            isinstance(after, dict)
            and set(before) == set(after)
            and all(_same_container_shape(before[k], after[k]) for k in before)
        )
    if isinstance(before, list):
        return (
            isinstance(after, list)
            and len(before) == len(after)
            and all(_same_container_shape(a, b) for a, b in zip(before, after))
        )
    return not isinstance(after, (dict, list))


def _repair_reference_field(path: str) -> str | None:
    tokens = parse_pointer(path)
    for field in _repair_reference_node_types():
        if field in tokens:
            return field
    if "research_question_id" in tokens:
        return "research_question_id"
    return None


def _existing_reference_ids(
    original_content: dict[str, Any],
    field: str,
) -> set[str]:
    result_view = _repair_result_view(original_content)
    graph = (result_view.get("argument_architecture") or {})
    if field == "research_question_id":
        return {
            str(q.get("node_id"))
            for q in graph.get("research_questions") or []
            if isinstance(q, dict) and q.get("node_id")
        }
    required_type = _repair_reference_node_types().get(field)
    method_types = _reference_type_members("METHOD")
    result: set[str] = set()
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict) or not node.get("node_id"):
            continue
        actual_type = str(node.get("node_type") or "")
        matches = (
            actual_type in method_types
            if required_type == "METHOD"
            else actual_type == required_type
        )
        if matches:
            result.add(str(node["node_id"]))
    return result


def _reference_repair_is_local(
    original_content: dict[str, Any],
    path: str,
    after: Any,
) -> bool:
    field = _repair_reference_field(path)
    if not field:
        return False
    existing = _existing_reference_ids(original_content, field)
    if not existing:
        return False
    tokens = parse_pointer(path)
    if tokens and tokens[-1] == field:
        if field == "research_question_id":
            return isinstance(after, str) and after in existing
        return (
            isinstance(after, list)
            and all(isinstance(x, str) and x in existing for x in after)
        )
    if field in tokens:
        if isinstance(after, str):
            return after in existing
        if isinstance(after, list):
            return all(isinstance(x, str) and x in existing for x in after)
    return False


def _set_existing_pointer(document: Any, pointer: str, value: Any) -> None:
    tokens=parse_pointer(pointer)
    if not tokens: raise JsonPointerError("cannot replace root")
    parent=resolve_pointer(document,format_pointer(tokens[:-1])) if tokens[:-1] else document; leaf=tokens[-1]
    if isinstance(parent,dict):
        if leaf not in parent: raise JsonPointerError(f"object key does not exist: {leaf!r}")
        parent[leaf]=copy.deepcopy(value); return
    if isinstance(parent,list):
        if not leaf.isdigit(): raise JsonPointerError(f"invalid array index token: {leaf!r}")
        idx=int(leaf)
        if idx>=len(parent): raise JsonPointerError(f"array index out of range: {idx}")
        parent[idx]=copy.deepcopy(value); return
    raise JsonPointerError("cannot replace value below scalar parent")


def _semantic_diff_paths(before: Any, after: Any, path_tokens: tuple[Any,...]=()) -> list[str]:
    if type(before) is not type(after): return [format_pointer(path_tokens)]
    if isinstance(before,dict):
        result=[]
        for key in sorted(set(before)|set(after),key=str):
            child=(*path_tokens,key)
            result.append(format_pointer(child)) if key not in before or key not in after else result.extend(_semantic_diff_paths(before[key],after[key],child))
        return result
    if isinstance(before,list):
        if len(before)!=len(after): return [format_pointer(path_tokens)]
        result=[]
        for i,(a,b) in enumerate(zip(before,after)): result.extend(_semantic_diff_paths(a,b,(*path_tokens,i)))
        return result
    return [] if before==after else [format_pointer(path_tokens)]



def targeted_repair_semantic_errors(
    canonical_envelope: dict[str, Any],
    semantic_output: dict[str, Any],
) -> list[str]:
    payload = canonical_envelope.get("payload") or {}
    original = ((payload.get("original_object") or {}).get("content") or {})
    allowed = _effective_repair_allowed_paths(payload, original)
    protected = [
        _repair_model_path(str(p))
        for p in payload.get("protected_paths") or []
    ]
    decision = str(semantic_output.get("decision") or "")
    changes = [
        x for x in semantic_output.get("changes") or [] if isinstance(x, dict)
    ]
    errors: list[str] = []

    if decision == "APPLY":
        if not changes:
            errors.append("/changes: APPLY requires at least one change")
        if semantic_output.get("escalation_reason"):
            errors.append(
                "/escalation_reason: APPLY must not include an escalation reason"
            )
    elif decision == "ESCALATE":
        if changes:
            errors.append("/changes: ESCALATE must not include changes")
        if not str(semantic_output.get("escalation_reason") or "").strip():
            errors.append(
                "/escalation_reason: ESCALATE requires a concrete reason"
            )

    seen: set[str] = set()
    for i, change in enumerate(changes):
        path = str(change.get("path") or "")
        if path.startswith("/content/") or path == "/content":
            errors.append(
                f"/changes/{i}/path: model repair paths are relative to original content and must not start with /content"
            )
            continue
        try:
            parse_pointer(path)
        except JsonPointerError as exc:
            errors.append(f"/changes/{i}/path: {exc}")
            continue
        if path in seen:
            errors.append(f"/changes/{i}/path: duplicate path {path!r}")
            continue
        seen.add(path)
        if not any(is_ancestor_or_same(root, path) for root in allowed):
            errors.append(
                f"/changes/{i}/path: {path!r} is outside authorized repair targets"
            )
            continue
        if any(
            is_ancestor_or_same(root, path) or is_ancestor_or_same(path, root)
            for root in protected
        ):
            errors.append(
                f"/changes/{i}/path: {path!r} overlaps protected content"
            )
            continue
        try:
            before = resolve_pointer(original, path)
        except JsonPointerError:
            errors.append(
                f"/changes/{i}/path: structural insertion is not a local repair; {path!r} does not exist"
            )
            continue

        after = change.get("value")
        if before == after:
            errors.append(
                f"/changes/{i}/value: change at {path!r} is a no-op"
            )
            continue

        reference_field = _repair_reference_field(path)
        if reference_field:
            if not _reference_repair_is_local(original, path, after):
                errors.append(
                    f"/changes/{i}/value: reference update at {path!r} must use only existing "
                    "business entities; creating or inventing a referenced entity requires regeneration"
                )
            continue

        if not _same_container_shape(before, after):
            errors.append(
                f"/changes/{i}/value: changing business-object structure is not a local repair and requires escalation"
            )

    object_type = str(
        ((payload.get("original_object") or {}).get("object_type") or "")
    ).upper()
    if decision == "APPLY" and object_type == "ARGUMENT_ARCHITECTURE" and not errors:
        repaired = copy.deepcopy(original)
        for change in changes:
            _set_existing_pointer(repaired, str(change.get("path") or ""), change.get("value"))
        authored_state = repaired.get("authored_state")
        if not isinstance(authored_state, dict):
            errors.append(
                "/authored_state: Argument Architecture repair requires persisted authoritative state"
            )
        else:
            validator = _argument_authored_state_schema_validator()
            for schema_error in sorted(validator.iter_errors(authored_state), key=lambda e: list(e.absolute_path)):
                suffix = "/".join(str(token) for token in schema_error.absolute_path)
                path = "/authored_state" + (f"/{suffix}" if suffix else "")
                errors.append(f"{path}: {schema_error.message}")
    return errors


def _critic_review_component_group(component: str) -> str:
    value = str(component or "")
    return _critic_component_by_node_type().get(value, value)


def semantic_model_reference_errors(
    prompt_id: str,
    canonical_envelope: dict[str, Any],
    semantic_output: dict[str, Any],
) -> list[str]:
    if prompt_id in _WF3_SEMANTIC_PROMPTS:
        return _wf3_semantic_reference_errors(prompt_id, canonical_envelope, semantic_output)
    if prompt_id == "P-TARGETED-REPAIR":
        return targeted_repair_semantic_errors(canonical_envelope, semantic_output)
    if prompt_id not in {
        "P-ARGUMENT-ARCHITECTURE",
        "P-ARGUMENT-ARCHITECTURE-CRITIC",
    }:
        return []
    if prompt_id == "P-ARGUMENT-ARCHITECTURE":
        return _argument_authoritative_state_semantic_errors(
            canonical_envelope, semantic_output
        )

    cards, _ = _evidence_records(canonical_envelope)
    known = {str(c["evidence_id"]) for c in cards}
    errors: list[str] = []

    def visit(node: Any, path: tuple[str, ...] = ()) -> None:
        if isinstance(node, list):
            for i, item in enumerate(node):
                visit(item, path + (str(i),))
        elif isinstance(node, dict):
            for key, value in node.items():
                child = path + (key,)
                if key == "evidence_ids" and isinstance(value, list):
                    for i, eid in enumerate(value):
                        if str(eid) not in known:
                            errors.append(
                                "/"
                                + "/".join(child + (str(i),))
                                + f": evidence_id {eid!r} is not present in evidence_cards"
                            )
                else:
                    visit(value, child)

    visit(semantic_output)

    for i, q in enumerate(semantic_output.get("user_questions") or []):
        if (
            isinstance(q, dict)
            and q.get("question_type") == "CHOICE"
            and not q.get("allowed_values")
        ):
            errors.append(
                f"/user_questions/{i}/allowed_values: CHOICE requires at least one allowed value"
            )

    required_dimensions = set(_critic_dimension_issue_codes())
    dimensions = [
        str(x.get("dimension"))
        for x in semantic_output.get("quality_dimensions") or []
        if isinstance(x, dict)
    ]
    if set(dimensions) != required_dimensions or len(dimensions) != len(
        required_dimensions
    ):
        errors.append(
            "/quality_dimensions: must contain each of the seven argument quality dimensions exactly once"
        )

    # Model scores/evidence are advisory semantic observations.  Runtime derives
    # pass/fail and required_action from concrete semantic issues plus
    # deterministic receipts; a model cannot create or erase a workflow failure
    # by toggling quality_dimensions[].passed.
    issues = [
        x for x in semantic_output.get("issues") or [] if isinstance(x, dict)
    ]

    candidate = (canonical_envelope.get("payload") or {}).get(
        "architecture_candidate"
    ) or {}
    if not isinstance(candidate.get("authored_state"), dict):
        errors.append(
            "/payload/architecture_candidate/authored_state: authoritative state is required"
        )
        return errors
    candidate_state_errors = _argument_authoritative_state_semantic_errors(
        canonical_envelope,
        candidate["authored_state"],
        path_prefix="/payload/architecture_candidate/authored_state",
    )
    if candidate_state_errors:
        errors.extend(candidate_state_errors)
        return errors
    candidate = _canonical_argument_candidate(canonical_envelope, candidate)
    review_units, review_mapping = _critic_review_units(candidate)
    unit_components = {
        str(item["unit_key"]): _critic_review_component_group(
            str(item.get("component") or "")
        )
        for item in review_units
    }
    expected_keys = [str(x["unit_key"]) for x in review_units]

    for issue_index, issue in enumerate(issues):
        code = str(issue.get("code") or "")
        target = issue.get("target") or {}
        component = str(target.get("component") or "")
        raw_thread_index = target.get("thread_index")
        graph = candidate.get("argument_architecture") or {}
        thread_count = len(
            [q for q in graph.get("research_questions") or [] if isinstance(q, dict)]
        )
        if raw_thread_index is not None and not (
            isinstance(raw_thread_index, int)
            and 0 <= raw_thread_index < thread_count
        ):
            errors.append(
                f"/issues/{issue_index}/target/thread_index: target thread must identify an existing research thread"
            )
        if component in {"CENTRAL_PROPOSITION", "SCOPE"} and raw_thread_index is not None:
            errors.append(
                f"/issues/{issue_index}/target/thread_index: global component {component!r} must not claim a research thread"
            )
        if component == "THREAD" and not isinstance(raw_thread_index, int):
            errors.append(
                f"/issues/{issue_index}/target/thread_index: THREAD target requires an existing research thread index"
            )
        allowed_components = _critic_allowed_target_components().get(code, set())
        if component not in allowed_components:
            errors.append(
                f"/issues/{issue_index}/target/component: issue code {code!r} cannot target semantic component {component!r}"
            )
        review_key = str(target.get("review_unit_key") or "").strip()
        if component in _critic_precise_target_components():
            if not review_key:
                errors.append(
                    f"/issues/{issue_index}/target/review_unit_key: "
                    f"component {component!r} requires a precise semantic "
                    "review-unit target"
                )
            elif review_key not in review_mapping:
                errors.append(
                    f"/issues/{issue_index}/target/review_unit_key: "
                    f"unknown review unit {review_key!r}"
                )
            else:
                actual_component = unit_components.get(review_key, "")
                if actual_component != component:
                    errors.append(
                        f"/issues/{issue_index}/target: component "
                        f"{component!r} does not match review unit "
                        f"{review_key!r} ({actual_component!r})"
                    )
                canonical_thread = _critic_target_canonical_thread(candidate, target)
                if (
                    canonical_thread is not None
                    and raw_thread_index != canonical_thread
                ):
                    errors.append(
                        f"/issues/{issue_index}/target/thread_index: review unit {review_key!r} belongs to thread {canonical_thread}, not {raw_thread_index!r}"
                    )
    reviewed_keys = [
        str(x)
        for x in semantic_output.get("reviewed_unit_keys") or []
        if str(x).strip()
    ]
    missing = [x for x in expected_keys if x not in reviewed_keys]
    unknown = [x for x in reviewed_keys if x not in set(expected_keys)]
    if missing:
        errors.append(
            "/reviewed_unit_keys: critic did not explicitly cover review unit(s): "
            + ", ".join(missing[:12])
        )
    if unknown:
        errors.append(
            "/reviewed_unit_keys: contains unknown review unit(s): "
            + ", ".join(unknown[:12])
        )

    user_questions = [
        q
        for q in semantic_output.get("user_questions") or []
        if isinstance(q, dict)
    ]
    blocking_questions = [q for q in user_questions if bool(q.get("blocking"))]
    user_issues = [
        issue
        for issue in issues
        if _critic_issue_needs_user_input(issue)
    ]
    if user_issues and not blocking_questions:
        errors.append(
            "/issues: needs_user_input requires at least one concrete blocking user_question"
        )
    if blocking_questions and not user_issues:
        errors.append(
            "/user_questions: a blocking user question must correspond to a needs_user_input issue"
        )
    return errors


def _refs_for_evidence_ids(evidence_ids: Iterable[Any], records: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    refs=[]
    for eid in evidence_ids:
        record=records.get(str(eid))
        if record: refs.extend(record.get("source_refs") or [])
    return _dedupe_source_refs(refs)


def _evidence_record_is_supported(
    record: dict[str, Any] | None,
    *,
    foundation: bool = False,
    source_policy: str | None = None,
) -> bool:
    if not isinstance(record, dict):
        return False
    config = _argument_evidence_config()
    supported_statuses = {
        str(value) for value in config.get("supported_knowledge_statuses") or ()
    }
    card = record.get("card") or {}
    if str(card.get("knowledge_status") or "") not in supported_statuses:
        return False

    policy_name = str(source_policy or ("FOUNDATION" if foundation else "STANDARD"))
    policies = dict(config.get("source_policies") or {})
    policy = dict(policies.get(policy_name) or {})
    refs = [
        ref for ref in record.get("source_refs") or []
        if isinstance(ref, dict)
    ]
    if bool(policy.get("require_source_ref", True)) and not refs:
        return False
    allowed_types = {
        str(value) for value in policy.get("allowed_source_types") or ()
    }
    require_quote = bool(policy.get("require_quoted_text"))
    if not allowed_types and not require_quote:
        return bool(refs) if bool(policy.get("require_source_ref", True)) else True
    return any(
        (not allowed_types or str(ref.get("source_type") or "") in allowed_types)
        and (not require_quote or bool(str(ref.get("quoted_text") or "").strip()))
        for ref in refs
    )


def argument_foundation_eligible_evidence_ids(
    canonical_envelope: dict[str, Any],
) -> list[str]:
    """Return authoritative evidence IDs that may support declared team foundation."""
    _cards, records = _evidence_records(canonical_envelope)
    return [
        evidence_id
        for evidence_id, record in records.items()
        if _evidence_record_is_supported(record, source_policy="FOUNDATION")
    ]


def _has_supported_evidence(
    evidence_ids: Iterable[Any],
    records: dict[str, dict[str, Any]],
    *,
    foundation: bool = False,
    source_policy: str | None = None,
) -> bool:
    return any(
        _evidence_record_is_supported(
            records.get(str(eid)),
            foundation=foundation,
            source_policy=source_policy,
        )
        for eid in evidence_ids
        if str(eid).strip()
    )


def _argument_node_status_source_policies() -> dict[str, str]:
    policies: dict[str, str] = {}
    for requirement in _argument_evidence_requirements():
        node_type = str(requirement.get("status_node_type") or "")
        source_policy = str(requirement.get("source_policy") or "")
        if node_type and source_policy:
            policies[node_type] = source_policy
    return policies


def _semantic_node_status(
    node_type: str,
    evidence_ids: Iterable[Any],
    records: dict[str, dict[str, Any]],
) -> str:
    ids = [str(x) for x in evidence_ids]
    source_policy = _argument_node_status_source_policies().get(str(node_type))
    if not source_policy:
        return "PLANNED"
    supported = _has_supported_evidence(
        ids,
        records,
        source_policy=source_policy,
    )
    return "SUPPORTED" if supported else "UNKNOWN"


def _foundation_has_valid_support(
    foundation: dict[str, Any],
    work_packages: list[dict[str, Any]],
) -> bool:
    for ref in foundation.get("supports") or []:
        if not isinstance(ref, dict):
            continue
        wi = ref.get("work_package_index")
        mi = ref.get("method_index")
        if not isinstance(wi, int) or not 0 <= wi < len(work_packages):
            continue
        if mi is None:
            return True
        methods = [
            item for item in work_packages[wi].get("methods") or []
            if isinstance(item, dict)
        ]
        if isinstance(mi, int) and 0 <= mi < len(methods):
            return True
    return False


def _argument_quantified_support(
    support_flags: Iterable[bool],
    *,
    presence: str,
    coverage: str,
) -> bool:
    flags = tuple(bool(value) for value in support_flags)
    normalized_presence = str(presence).upper()
    normalized_coverage = str(coverage).upper()
    if not flags:
        return normalized_presence == "IF_PRESENT"
    if normalized_coverage == "ALL":
        return all(flags)
    if normalized_coverage == "ANY":
        return any(flags)
    raise ValueError(f"unknown argument evidence coverage {coverage!r}")


def _argument_evidence_requirement_checks(
    semantic_tree: dict[str, Any],
    records: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Evaluate evidence obligations with explicit quantifiers and canonical owners."""

    checks: list[dict[str, Any]] = []

    def evidence_ids(item: Any) -> list[str]:
        if not isinstance(item, dict):
            return []
        return list(
            dict.fromkeys(
                str(value)
                for value in item.get("evidence_ids") or []
                if str(value).strip()
            )
        )

    for requirement in _argument_evidence_requirements():
        requirement_id = str(requirement.get("requirement_id") or "")
        selector = str(requirement.get("selector") or "")
        owner_selector = str(requirement.get("owner_selector") or "")
        subject = str(requirement.get("subject") or "").upper()
        presence = str(requirement.get("presence") or "").upper()
        coverage = str(requirement.get("coverage") or "").upper()
        source_policy = str(requirement.get("source_policy") or "STANDARD")

        for value, captures, parent in _iter_semantic_pattern_values(semantic_tree, selector):
            thread_index = _semantic_thread_from_tree(semantic_tree, selector, captures)
            owner_value = (
                _semantic_pattern_value_for_captures(semantic_tree, owner_selector, captures)
                if owner_selector
                else None
            )
            obligations: list[tuple[Any, list[dict[str, Any]], str]] = []

            if subject == "OBJECT":
                present = isinstance(value, dict) and (
                    presence == "REQUIRED"
                    or bool(str(value.get("statement") or "").strip())
                )
                if present:
                    obligations.append((value, [value], requirement_id))
            elif subject == "COLLECTION":
                items = (
                    [item for item in value if isinstance(item, dict)]
                    if isinstance(value, list)
                    else []
                )
                if not items:
                    if presence == "REQUIRED":
                        target = parent if isinstance(parent, dict) else {}
                        obligations.append((target, [], requirement_id))
                elif coverage == "ALL":
                    for item_index, item in enumerate(items):
                        obligations.append((item, [item], f"{requirement_id}:{item_index}"))
                elif coverage == "ANY":
                    target = parent if isinstance(parent, dict) else {}
                    obligations.append((target, items, requirement_id))
                else:
                    raise ValueError(
                        f"unknown argument evidence coverage {coverage!r} for {requirement_id}"
                    )
            else:
                raise ValueError(
                    f"unknown argument evidence subject {subject!r} for {requirement_id}"
                )

            for target, items, obligation_key in obligations:
                item_evidence_ids = [evidence_ids(item) for item in items]
                item_support = [
                    _has_supported_evidence(ids, records, source_policy=source_policy)
                    for ids in item_evidence_ids
                ]
                supported = _argument_quantified_support(
                    item_support, presence=presence, coverage=coverage
                )
                all_evidence_ids = list(
                    dict.fromkeys(
                        eid for ids in item_evidence_ids for eid in ids
                    )
                )
                semantic_object_id = (
                    str(target.get("_node_id") or "")
                    if isinstance(target, dict)
                    else ""
                )
                semantic_object_key = semantic_object_id or (
                    f"{obligation_key}@" + ".".join(str(index) for index in captures)
                )
                owner = owner_value if isinstance(owner_value, dict) else target
                owner_semantic_object_id = (
                    str(owner.get("_node_id") or "") if isinstance(owner, dict) else ""
                )
                owner_semantic_object_key = owner_semantic_object_id or (
                    f"OWNER:{requirement_id}@" + ".".join(str(index) for index in captures)
                )
                checks.append(
                    {
                        "requirement_id": requirement_id,
                        "deterministic_defect_family": str(
                            requirement.get("deterministic_defect_family") or ""
                        ),
                        "thread_index": thread_index,
                        "supported": supported,
                        "evidence_ids": all_evidence_ids,
                        "semantic_object_id": semantic_object_id or None,
                        "semantic_object_key": semantic_object_key,
                        "owner_semantic_object_id": owner_semantic_object_id or None,
                        "owner_semantic_object_key": owner_semantic_object_key,
                        "finding_code": str(
                            requirement.get("finding_code")
                            or "ARGUMENT_EVIDENCE_UNSUPPORTED"
                        ),
                        "semantic_component": str(
                            requirement.get("semantic_component") or "EVIDENCE"
                        ),
                        "quality_dimension": str(
                            requirement.get("quality_dimension") or "EVIDENCE_SUPPORT"
                        ),
                        "deficiency_kind": str(
                            requirement.get("deficiency_kind") or "OTHER"
                        ),
                        "required_node_type": str(
                            requirement.get("required_node_type") or "EVIDENCE"
                        ),
                        "reason": str(
                            requirement.get("reason")
                            or "确定性证据要求未满足。"
                        ),
                        "suggested_question": str(
                            requirement.get("suggested_question")
                            or "补充可核验证据。"
                        ),
                    }
                )
    return checks


def _argument_structural_requirement_checks(
    semantic_tree: dict[str, Any],
) -> list[dict[str, Any]]:
    """Evaluate non-evidence semantic obligations from the structural registry."""
    checks: list[dict[str, Any]] = []
    for requirement in _argument_structural_requirements():
        requirement_id = str(requirement.get("requirement_id") or "")
        selector = str(requirement.get("selector") or "")
        owner_selector = str(requirement.get("owner_selector") or "")
        presence = str(requirement.get("presence") or "").upper()
        for value, captures, parent in _iter_semantic_pattern_values(semantic_tree, selector):
            thread_index = _semantic_thread_from_tree(semantic_tree, selector, captures)
            owner_value = _semantic_pattern_value_for_captures(
                semantic_tree, owner_selector, captures
            )
            if isinstance(value, list):
                present = any(
                    bool(str(item.get("statement") or "").strip())
                    if isinstance(item, dict)
                    else bool(str(item).strip())
                    for item in value
                )
            elif isinstance(value, dict):
                present = bool(str(value.get("statement") or "").strip())
            else:
                present = bool(str(value or "").strip())
            satisfied = present or presence == "IF_PRESENT"
            owner = owner_value if isinstance(owner_value, dict) else (parent if isinstance(parent, dict) else {})
            owner_id = str(owner.get("_node_id") or "")
            owner_key = owner_id or (
                f"OWNER:{requirement_id}@" + ".".join(str(index) for index in captures)
            )
            checks.append({
                "requirement_id": requirement_id,
                "deterministic_defect_family": str(requirement.get("deterministic_defect_family") or ""),
                "thread_index": thread_index,
                "satisfied": satisfied,
                "semantic_object_id": owner_id or None,
                "semantic_object_key": owner_key,
                "owner_semantic_object_id": owner_id or None,
                "owner_semantic_object_key": owner_key,
                "finding_code": str(requirement.get("finding_code") or ""),
                "semantic_component": str(requirement.get("semantic_component") or "RESEARCH_DESIGN"),
                "quality_dimension": str(requirement.get("quality_dimension") or "ARGUMENT_CHAIN"),
                "required_node_type": str(requirement.get("required_node_type") or "EVIDENCE"),
                "reason": str(requirement.get("reason") or "确定性结构要求未满足。"),
                "repair_instruction": str(requirement.get("repair_instruction") or "由原 Argument Producer 补齐缺失的结构语义。"),
            })
    return checks


def _producer_gap_from_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        "gap_id": str(receipt.get("receipt_id") or receipt.get("defect_key") or "ARG-GAP"),
        "defect_key": str(receipt.get("defect_key") or ""),
        "defect_family": str(receipt.get("defect_family") or ""),
        "finding_code": str(receipt.get("finding_code") or "RESEARCH_DESIGN_INCOMPLETE"),
        "semantic_component": str(receipt.get("owner_semantic_component") or receipt.get("semantic_component") or "RESEARCH_DESIGN"),
        "semantic_object_id": str(receipt.get("owner_semantic_object_id") or receipt.get("semantic_object_id") or "") or None,
        "semantic_review_unit_key": str(receipt.get("owner_semantic_review_unit_key") or receipt.get("semantic_review_unit_key") or "") or None,
        "quality_dimension": str(receipt.get("quality_dimension") or "ARGUMENT_CHAIN"),
        "required_node_type": str(receipt.get("required_node_type") or "EVIDENCE"),
        "thread_index": receipt.get("thread_index") if isinstance(receipt.get("thread_index"), int) else None,
        "reason": str(receipt.get("description") or "存在确定性语义缺口。"),
        # Deterministic receipt policy is the authority for whether a defect
        # blocks the producer.  Dropping it here used to turn hard chain/matrix
        # defects into advisory gaps and made the workflow regenerate the wrong
        # items.
        "blocking": bool(receipt.get("blocking", True)),
        "suggested_source_or_question": str(receipt.get("repair_instruction") or "由原 Argument Producer 基于现有事实重新生成该语义部件。"),
        "suggested_route": str(receipt.get("suggested_route") or "ORIGINAL_PRODUCER"),
    }


def _producer_declared_gap(item: dict[str, Any], index: int) -> dict[str, Any]:
    kind = str(item.get("kind") or "OTHER")
    policy = _producer_gap_kind_policy(kind)
    thread_index = item.get("thread_index") if isinstance(item.get("thread_index"), int) else None
    defect_key = _argument_defect_key(
        f"MODEL_GAP:{kind}:{index}", thread_index, f"DECLARED_GAP:{index}"
    )
    return {
        "gap_id": f"arg-model-gap-{index:03d}",
        "defect_key": defect_key,
        "defect_family": "MODEL_DECLARED_GAP",
        "finding_code": str(policy.get("finding_code") or "RESEARCH_DESIGN_INCOMPLETE"),
        "semantic_component": str(policy.get("semantic_component") or "RESEARCH_DESIGN"),
        "semantic_object_id": None,
        "semantic_review_unit_key": None,
        "quality_dimension": "ARGUMENT_CHAIN",
        "required_node_type": str(policy.get("required_node_type") or "EVIDENCE"),
        "thread_index": thread_index,
        "reason": str(item.get("reason") or "存在模型识别的语义或证据缺口。"),
        "blocking": bool(item.get("blocking")),
        "suggested_source_or_question": str(item.get("suggested_question") or "补充可核验材料。"),
        "suggested_route": "ORIGINAL_PRODUCER",
    }


def _node(*,node_id,node_type,statement,evidence_ids,records):
    ids=[str(x) for x in evidence_ids]
    return {"node_id":node_id,"node_type":node_type,"statement":statement,"status":_semantic_node_status(node_type,ids,records),"source_refs":_refs_for_evidence_ids(ids,records)}


def _edge(edge_id,source_id,relation,target_id,rationale): return {"edge_id":edge_id,"source_id":source_id,"relation":relation,"target_id":target_id,"rationale":rationale}

def _question_target_path(target_area: str) -> str:
    mapping = _question_target_paths()
    return mapping.get(str(target_area), mapping.get("OTHER", "/payload/confirmed_facts"))


def _answer_schema(question: dict[str, Any]) -> dict[str, Any]:
    return semantic_question_answer_schema(question)



def _argument_state_hash(value: dict[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def argument_projection_input_sha256(
    canonical_envelope: dict[str, Any], authored_state: dict[str, Any]
) -> str:
    """Hash every external input that can change an Argument projection.

    The projector owns all derived graph/matrix/evidence/status fields.  A cache
    freshness token therefore cannot hash authored_state alone: evidence records,
    proposal scope and the design seed also affect the deterministic projection.
    """
    payload = canonical_envelope.get("payload") or {}
    projection_input = {
        "projection_version": _argument_projection_version(),
        "authored_state": authored_state,
        "proposal_contract": payload.get("proposal_contract") or {},
        "confirmed_facts": payload.get("confirmed_facts") or [],
        "argument_graph_seed": payload.get("argument_graph_seed") or {},
        "project_subgraph": payload.get("project_subgraph") or {},
    }
    return _argument_state_hash(projection_input)


def _argument_authoritative_state(semantic_output: dict[str, Any]) -> dict[str, Any]:
    """Return the only model-authored state from which all runtime projections derive."""
    return copy.deepcopy(semantic_output)


def _project_argument_architecture_semantic_state(
    canonical_envelope: dict[str, Any],
    semantic_output: dict[str, Any],
) -> dict[str, Any]:
    _validate_argument_authoritative_state_or_raise(semantic_output)
    state_errors = _argument_authoritative_state_semantic_errors(
        canonical_envelope, semantic_output, path_prefix="/result/authored_state"
    )
    if state_errors:
        raise ValueError("invalid Argument authoritative state: " + "; ".join(state_errors))
    payload = canonical_envelope.get("payload") or {}
    authored_state = _argument_authoritative_state(semantic_output)
    contract = payload.get("proposal_contract") or {}
    _, records = _evidence_records(canonical_envelope)

    ps = semantic_output["central_proposition"]
    prop_id = "arg-proposition-001"
    proposition = {
        "node_id": prop_id,
        "statement": str(ps["statement"]),
        "proposition_type": str(ps["proposition_type"]),
        "falsifiable_or_comparable": bool(ps["falsifiable_or_comparable"]),
        "boundary_conditions": [str(x) for x in ps.get("boundary_conditions") or []],
        "source_refs": _refs_for_evidence_ids(ps.get("evidence_ids") or [], records),
    }

    rqs: list[dict[str, Any]] = []
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    matrix: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = [
        {
            "node_id": prop_id,
            "evidence_ids": list(
                dict.fromkeys(str(x) for x in ps.get("evidence_ids") or [] if str(x).strip())
            ),
        }
    ]
    thread_nodes: dict[int, dict[str, Any]] = {}
    thread_assumption_bindings: list[dict[str, Any]] = []
    edge_counter = 0

    def add_edge(source: str, relation: str, target: str, rationale: str) -> None:
        nonlocal edge_counter
        edge_counter += 1
        edges.append(
            _edge(
                f"arg-edge-{edge_counter:03d}",
                source,
                relation,
                target,
                rationale,
            )
        )

    def add_node(
        *,
        node_id: str,
        node_type: str,
        statement: str,
        evidence_ids: Iterable[Any],
    ) -> str:
        ids = list(
            dict.fromkeys(str(x) for x in evidence_ids if str(x).strip())
        )
        nodes.append(
            _node(
                node_id=node_id,
                node_type=node_type,
                statement=str(statement),
                evidence_ids=ids,
                records=records,
            )
        )
        bindings.append({"node_id": node_id, "evidence_ids": ids})
        return node_id

    for t_index, thread in enumerate(semantic_output.get("research_threads") or [], 1):
        prefix = f"{t_index:03d}"
        gap_id = f"arg-gap-{prefix}"
        limitation_id = f"arg-limitation-{prefix}"
        rq_id = f"arg-rq-{prefix}"
        obj_id = f"arg-objective-{prefix}"
        gap = thread["gap"]
        objective = thread["objective"]
        question = thread["question"]

        add_node(
            node_id=gap_id,
            node_type="RESEARCH_GAP",
            statement=str(gap["statement"]),
            evidence_ids=gap.get("evidence_ids") or [],
        )
        limitation = gap["limitation_mechanism"]
        add_node(
            node_id=limitation_id,
            node_type="LIMITATION_MECHANISM",
            statement=str(limitation["statement"]),
            evidence_ids=limitation.get("evidence_ids") or [],
        )
        add_edge(
            limitation_id,
            "EXPLAINS",
            gap_id,
            "模型明确给出的限制机制解释该研究差距。",
        )

        add_node(
            node_id=obj_id,
            node_type="OBJECTIVE",
            statement=str(objective["statement"]),
            evidence_ids=objective.get("evidence_ids") or [],
        )
        rqs.append(
            {
                "node_id": rq_id,
                "statement": str(question["statement"]),
                "question_type": str(question["question_type"]),
                "linked_gap_ids": [gap_id],
                "answerability": str(question["answerability"]),
                "success_evidence": [str(x) for x in question.get("success_evidence") or []],
            }
        )
        bindings.append({"node_id": rq_id, "evidence_ids": []})
        add_edge(gap_id, "MOTIVATES", rq_id, "研究差距触发该研究问题")
        add_edge(rq_id, "ADDRESSED_BY", obj_id, "研究目标直接回答该研究问题")

        assumption_ids: list[str] = []
        authored_thread_assumptions = [
            str(assumption)
            for assumption in thread.get("thread_assumptions") or []
            if str(assumption).strip()
        ]
        for ai, assumption in enumerate(authored_thread_assumptions, 1):
            aid = f"arg-assumption-{prefix}-{ai:02d}"
            assumption_ids.append(aid)
            add_node(
                node_id=aid,
                node_type="ASSUMPTION",
                statement=str(assumption),
                evidence_ids=[],
            )
        thread_assumption_bindings.append(
            {
                "thread_index": t_index - 1,
                "assumption_node_ids": list(assumption_ids),
            }
        )

        wp_ids: list[str] = []
        method_ids: list[str] = []
        eval_ids: list[str] = []
        baseline_ids: list[str] = []
        ablation_ids: list[str] = []
        success_criterion_ids: list[str] = []
        theory_ids: list[str] = []
        method_assumption_ids: list[str] = []
        inn_ids: list[str] = []
        contribution_ids: list[str] = []
        prior_ids: list[str] = []
        foundation_ids: list[str] = []
        wp_lookup: dict[int, str] = {}
        method_lookup: dict[tuple[int, int], str] = {}
        eval_lookup: dict[tuple[int, int, int], str] = {}

        for wi, wp in enumerate(thread.get("work_packages") or [], 1):
            wp_id = f"arg-wp-{prefix}-{wi:02d}"
            wp_ids.append(wp_id)
            wp_lookup[wi - 1] = wp_id
            add_node(
                node_id=wp_id,
                node_type="WORK_PACKAGE",
                statement=str(wp["statement"]),
                evidence_ids=wp.get("evidence_ids") or [],
            )
            add_edge(obj_id, "DECOMPOSES_TO", wp_id, "研究目标分解为该工作包")

            for mi, method in enumerate(wp.get("methods") or [], 1):
                mid = f"arg-method-{prefix}-{wi:02d}-{mi:02d}"
                method_ids.append(mid)
                method_lookup[(wi - 1, mi - 1)] = mid
                method_type = str(method.get("method_type") or "ANALYTICAL_METHOD")
                add_node(
                    node_id=mid,
                    node_type=method_type if method_type in _reference_type_members("METHOD") else "ANALYTICAL_METHOD",
                    statement=str(method["statement"]),
                    evidence_ids=method.get("evidence_ids") or [],
                )
                add_edge(wp_id, "USES", mid, "工作包明确使用该方法")

                for ai, assumption in enumerate(method.get("assumptions") or [], 1):
                    aid = f"arg-method-assumption-{prefix}-{wi:02d}-{mi:02d}-{ai:02d}"
                    method_assumption_ids.append(aid)
                    add_node(
                        node_id=aid,
                        node_type="ASSUMPTION",
                        statement=str(assumption),
                        evidence_ids=[],
                    )
                    add_edge(mid, "ASSUMES", aid, "方法明确声明该适用假设")

                for ti, prop in enumerate(method.get("theoretical_properties") or [], 1):
                    tid = f"arg-theory-{prefix}-{wi:02d}-{mi:02d}-{ti:02d}"
                    theory_ids.append(tid)
                    add_node(
                        node_id=tid,
                        node_type="THEORETICAL_PROPERTY",
                        statement=str(prop["statement"]),
                        evidence_ids=prop.get("evidence_ids") or [],
                    )
                    add_edge(mid, "HAS_PROPERTY", tid, "方法明确提出该理论性质")

                for ei, ev in enumerate(method.get("evaluations") or [], 1):
                    eid = f"arg-eval-{prefix}-{wi:02d}-{mi:02d}-{ei:02d}"
                    eval_ids.append(eid)
                    eval_lookup[(wi - 1, mi - 1, ei - 1)] = eid
                    add_node(
                        node_id=eid,
                        node_type="EXPERIMENT_DESIGN",
                        statement=str(ev["statement"]),
                        evidence_ids=ev.get("evidence_ids") or [],
                    )
                    add_edge(mid, "VALIDATED_BY", eid, "评价方案用于验证该方法")

                    for bi, baseline in enumerate(ev.get("baselines") or [], 1):
                        bid = f"arg-baseline-{prefix}-{wi:02d}-{mi:02d}-{ei:02d}-{bi:02d}"
                        baseline_ids.append(bid)
                        add_node(
                            node_id=bid,
                            node_type="BASELINE",
                            statement=str(baseline["statement"]),
                            evidence_ids=baseline.get("evidence_ids") or [],
                        )
                        add_edge(eid, "COMPARES_WITH", bid, "验证方案明确选择该比较基线")

                    for ai, ablation in enumerate(ev.get("ablations") or [], 1):
                        aid = f"arg-ablation-{prefix}-{wi:02d}-{mi:02d}-{ei:02d}-{ai:02d}"
                        ablation_ids.append(aid)
                        add_node(
                            node_id=aid,
                            node_type="ABLATION",
                            statement=str(ablation),
                            evidence_ids=[],
                        )
                        add_edge(eid, "INCLUDES_ABLATION", aid, "验证方案明确包含该消融检查")

                    for si, criterion in enumerate(ev.get("success_criteria") or [], 1):
                        sid = f"arg-success-{prefix}-{wi:02d}-{mi:02d}-{ei:02d}-{si:02d}"
                        success_criterion_ids.append(sid)
                        add_node(
                            node_id=sid,
                            node_type="SUCCESS_CRITERION",
                            statement=str(criterion),
                            evidence_ids=[],
                        )
                        add_edge(eid, "MEASURED_BY", sid, "验证方案明确使用该成功判据")

        for ii, inn in enumerate(thread.get("innovations") or [], 1):
            iid = f"arg-innovation-{prefix}-{ii:02d}"
            inn_ids.append(iid)
            add_node(
                node_id=iid,
                node_type="NOVEL_MECHANISM",
                statement=str(inn["statement"]),
                evidence_ids=inn.get("evidence_ids") or [],
            )
            cid = f"arg-contribution-{prefix}-{ii:02d}"
            contribution_ids.append(cid)
            add_node(
                node_id=cid,
                node_type="CONTRIBUTION",
                statement=str(inn["contribution"]),
                evidence_ids=[],
            )
            add_edge(iid, "YIELDS", cid, "创新点明确形成该研究贡献")

            for pi, prior in enumerate(inn.get("closest_prior_work") or [], 1):
                pid = f"arg-prior-{prefix}-{ii:02d}-{pi:02d}"
                prior_ids.append(pid)
                add_node(
                    node_id=pid,
                    node_type="CLOSEST_PRIOR_WORK",
                    statement=str(prior["statement"]),
                    evidence_ids=prior.get("evidence_ids") or [],
                )
                add_edge(
                    pid,
                    "CONTRASTS_WITH",
                    iid,
                    "最近工作为该创新点提供明确比较基线",
                )

            for ref in inn.get("evaluation_refs") or []:
                if not isinstance(ref, dict):
                    continue
                key = (
                    ref.get("work_package_index"),
                    ref.get("method_index"),
                    ref.get("evaluation_index"),
                )
                eid = eval_lookup.get(key)
                if eid:
                    add_edge(
                        eid,
                        "EVIDENCES",
                        iid,
                        "模型明确指定该评价用于检验此创新点",
                    )

        for fi, foundation in enumerate(thread.get("foundation") or [], 1):
            fid = f"arg-foundation-{prefix}-{fi:02d}"
            foundation_ids.append(fid)
            add_node(
                node_id=fid,
                node_type="TEAM_EVIDENCE",
                statement=str(foundation["statement"]),
                evidence_ids=foundation.get("evidence_ids") or [],
            )
            for ref in foundation.get("supports") or []:
                if not isinstance(ref, dict):
                    continue
                wi = ref.get("work_package_index")
                mi = ref.get("method_index")
                target = (
                    method_lookup.get((wi, mi))
                    if isinstance(mi, int)
                    else wp_lookup.get(wi)
                )
                if target:
                    add_edge(
                        fid,
                        "SUPPORTS",
                        target,
                        "模型明确指定该研究基础支撑此工作包或方法",
                    )

        thread_nodes[t_index - 1] = {
            "gap_id": gap_id,
            "rq_id": rq_id,
            "objective_id": obj_id,
            "work_package_ids": wp_ids,
            "method_ids": method_ids,
            "evaluation_ids": eval_ids,
            "innovation_ids": inn_ids,
            "prior_ids": prior_ids,
            "foundation_ids": foundation_ids,
        }
        matrix.append(
            {
                "research_question_id": rq_id,
                "gap_ids": [gap_id],
                "objective_ids": [obj_id],
                "work_package_ids": wp_ids,
                "method_ids": method_ids,
                "evaluation_ids": eval_ids,
                "innovation_ids": inn_ids,
                "foundation_evidence_ids": foundation_ids,
                "closest_prior_work_ids": prior_ids,
                "falsification_or_comparison_rule": str(
                    thread["falsification_or_comparison_rule"]
                ),
            }
        )

    scope = semantic_output.get("scope") or {}
    graph = {
        "graph_id": "argument-architecture-001",
        "central_proposition": proposition,
        "research_questions": rqs,
        "scope_boundaries": {
            "in_scope": [str(x) for x in scope.get("in_scope") or []],
            "out_of_scope": [str(x) for x in scope.get("out_of_scope") or []],
        },
        "nodes": nodes,
        "edges": edges,
    }

    candidate_for_checks = {
        "argument_architecture": graph,
        "research_design_matrix": matrix,
        "authored_evidence_bindings": bindings,
        "authored_thread_assumptions": thread_assumption_bindings,
    }
    deterministic_receipts = _argument_deterministic_receipts_for_candidate(
        canonical_envelope, candidate_for_checks
    )
    gap_report = [
        _producer_gap_from_receipt(receipt)
        for receipt in deterministic_receipts
        if isinstance(receipt, dict)
    ]
    for index, item in enumerate(
        [x for x in semantic_output.get("evidence_gaps") or [] if isinstance(x, dict)],
        1,
    ):
        gap_report.append(_producer_declared_gap(item, index))
    deduped_gaps: list[dict[str, Any]] = []
    seen_gap_keys: set[str] = set()
    for gap in gap_report:
        key = str(gap.get("defect_key") or gap.get("gap_id") or "")
        if key in seen_gap_keys:
            continue
        seen_gap_keys.add(key)
        deduped_gaps.append(gap)
    gap_report = deduped_gaps
    blocking_gaps = [gap for gap in gap_report if bool(gap.get("blocking"))]
    blocking_ids = [
        str(gap.get("semantic_object_id") or "")
        for gap in gap_report
        if bool(gap.get("blocking")) and str(gap.get("semantic_object_id") or "")
    ]

    user_questions: list[dict[str, Any]] = []
    for i, q in enumerate(semantic_output.get("user_questions") or [], 1):
        user_questions.append(
            {
                "question_id": f"UQ-ARG-{i:03d}",
                "question_type": str(q["question_type"]),
                "question": str(q["question"]),
                "reason": str(q["reason"]),
                "target_paths": [
                    _question_target_path(str(q.get("target_area") or "OTHER"))
                ],
                "answer_schema": _answer_schema(q),
                "blocking": bool(q.get("blocking")),
                "priority": str(q.get("priority") or "P2"),
            }
        )

    cannot = str(semantic_output.get("cannot_proceed_reason") or "").strip()
    has_blocking_question = any(bool(x.get("blocking")) for x in user_questions)
    if has_blocking_question:
        status = "NEED_USER_INPUT"
    elif cannot:
        status = "BLOCK"
    elif blocking_gaps:
        status = "REVISE"
    else:
        status = "PASS"

    main = list(graph["scope_boundaries"]["in_scope"])
    excluded = list(graph["scope_boundaries"]["out_of_scope"])
    for x in contract.get("forbidden_main_body_topics") or []:
        value = str(x)
        if value and value not in excluded:
            excluded.append(value)

    all_refs = _dedupe_source_refs(
        [r for n in nodes for r in n.get("source_refs") or []]
        + proposition.get("source_refs", [])
    )
    unresolved = [
        {
            "item_id": f"ARG-GAP-{i:03d}",
            "type": "UNSUPPORTED",
            "description": str(gap["reason"]),
            "target_paths": ["/result/evidence_gap_report"],
            "required_action": str(
                gap.get("suggested_source_or_question") or "补充可核验材料"
            ),
            "blocking": bool(gap.get("blocking")),
        }
        for i, gap in enumerate(gap_report, 1)
    ]
    return {
        "schema_version": str(canonical_envelope.get("schema_version") or "2.0"),
        "prompt_id": str(
            canonical_envelope.get("prompt_id") or "P-ARGUMENT-ARCHITECTURE"
        ),
        "prompt_version": str(canonical_envelope.get("prompt_version") or "8.0.0"),
        "status": status,
        "result": {
            "authored_state": authored_state,
            "projection_meta": {
                "projection_version": _argument_projection_version(),
                "source_state_sha256": _argument_state_hash(authored_state),
                "projection_input_sha256": argument_projection_input_sha256(
                    canonical_envelope, authored_state
                ),
            },
            "argument_architecture": graph,
            "research_design_matrix": matrix,
            "evidence_gap_report": gap_report,
            "scope_decision": {
                "main_body_focus": main,
                "appendix_topics": [
                    str(x) for x in contract.get("appendix_only_topics") or []
                ],
                "excluded_topics": excluded,
            },
            "readiness": {
                "ready": status == "PASS",
                "blocking_node_ids": list(dict.fromkeys(blocking_ids)),
                "summary": (
                    cannot
                    if cannot
                    else "存在需要用户回答的阻断性问题。"
                    if status == "NEED_USER_INPUT"
                    else "存在可由检索、补证或重新生成处理的证据缺口。"
                    if status == "REVISE"
                    else "论证语义闭环已形成，可进入下一阶段。"
                ),
            },
            "authored_evidence_bindings": bindings,
            "authored_thread_assumptions": thread_assumption_bindings,
        },
        "findings": [],
        "unresolved_items": unresolved,
        "user_questions": user_questions,
        "source_refs": all_refs,
        "warnings": [],
    }



def expand_argument_architecture_model_output(
    canonical_envelope: dict[str, Any],
    semantic_output: dict[str, Any],
) -> dict[str, Any]:
    """Persist model-authored semantics and derive every machine representation from it."""
    return _project_argument_architecture_semantic_state(canonical_envelope, semantic_output)


def project_argument_authoritative_state(
    canonical_envelope: dict[str, Any],
    authored_state: dict[str, Any],
) -> dict[str, Any]:
    """Purely re-project a persisted authoritative state; no derived cache is trusted."""
    envelope = copy.deepcopy(canonical_envelope)
    envelope["prompt_id"] = "P-ARGUMENT-ARCHITECTURE"
    envelope["prompt_version"] = "9.0.0"
    return _project_argument_architecture_semantic_state(
        envelope, copy.deepcopy(authored_state)
    )


def _canonical_argument_candidate(
    canonical_envelope: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    authored_state = candidate.get("authored_state") if isinstance(candidate, dict) else None
    if not isinstance(authored_state, dict):
        return candidate
    return project_argument_authoritative_state(canonical_envelope, authored_state)["result"]



def _critic_object_location(
    candidate: dict[str, Any],
    object_id: str | None,
    *,
    fallback_thread: int | None = None,
    fallback_component: str = "RESEARCH_DESIGN",
) -> dict[str, Any]:
    graph = candidate.get("argument_architecture") or {}
    oid = str(object_id or "")
    _, review_mapping = _critic_review_units(candidate)
    review_key_by_node_id = {
        str(node_id): str(unit_key)
        for unit_key, node_id in review_mapping.items()
        if node_id
    }
    proposition = graph.get("central_proposition") or {}
    if oid and oid == str(proposition.get("node_id") or ""):
        return {
            "semantic_object_id": oid,
            "semantic_component": "CENTRAL_PROPOSITION",
            "semantic_review_unit_key": review_key_by_node_id.get(oid),
            "target_path": "/result/argument_architecture/central_proposition",
        }
    for index, question in enumerate(graph.get("research_questions") or []):
        if isinstance(question, dict) and oid == str(question.get("node_id") or ""):
            return {
                "semantic_object_id": oid,
                "semantic_component": "QUESTION",
                "semantic_review_unit_key": review_key_by_node_id.get(oid),
                "target_path": f"/result/argument_architecture/research_questions/{index}",
            }
    for index, node in enumerate(graph.get("nodes") or []):
        if not isinstance(node, dict) or oid != str(node.get("node_id") or ""):
            continue
        node_type = str(node.get("node_type") or "")
        return {
            "semantic_object_id": oid,
            "semantic_component": _critic_review_component_group(node_type) or fallback_component,
            "semantic_review_unit_key": review_key_by_node_id.get(oid),
            "target_path": f"/result/argument_architecture/nodes/{index}",
        }
    row_path = (
        f"/result/research_design_matrix/{fallback_thread}"
        if isinstance(fallback_thread, int)
        else "/result/research_design_matrix"
    )
    return {
        "semantic_object_id": oid or None,
        "semantic_component": fallback_component,
        "semantic_review_unit_key": None,
        "target_path": row_path,
    }


def _argument_deterministic_receipt(
    *,
    defect_family: str,
    rule_id: str,
    rule_key: str,
    finding_code: str | None = None,
    quality_dimension: str | None = None,
    thread_index: int | None,
    research_question_id: str | None,
    source_id: str | None,
    target_id: str | None,
    source_ids: Iterable[Any] = (),
    target_ids: Iterable[Any] = (),
    missing_relation: str | None,
    semantic_object_id: str | None,
    semantic_component: str,
    semantic_review_unit_key: str | None,
    target_path: str,
    owner_semantic_object_id: str | None = None,
    owner_semantic_component: str | None = None,
    owner_semantic_review_unit_key: str | None = None,
    owner_target_path: str | None = None,
    description: str,
    repair_instruction: str,
    evidence_ids: Iterable[Any] = (),
    required_node_type: str | None = None,
) -> dict[str, Any]:
    policy = _argument_defect_policy(defect_family)
    failure_code = str(policy.get("failure_code") or "")
    configured_finding_code = str(policy.get("finding_code") or "")
    finding_code_source = str(policy.get("finding_code_source") or "").upper()
    resolved_finding_code = str(finding_code or "") if finding_code_source == "REQUIREMENT" else configured_finding_code
    quality_source = str(policy.get("quality_dimension_source") or "").upper()
    resolved_quality_dimension = (
        str(quality_dimension or "")
        if quality_source == "REQUIREMENT"
        else str(policy.get("quality_dimension") or quality_dimension or "")
    )
    if not failure_code or not resolved_finding_code or not resolved_quality_dimension:
        raise ValueError(f"deterministic defect family {defect_family!r} is incomplete")

    owner_id = str(owner_semantic_object_id or semantic_object_id or "") or None
    owner_component = str(owner_semantic_component or semantic_component or "RESEARCH_DESIGN")
    owner_review_key = str(owner_semantic_review_unit_key or semantic_review_unit_key or "") or None
    owner_path = str(owner_target_path or target_path)
    owner_key = str(owner_review_key or owner_id or owner_path)
    failed_key = str(
        semantic_review_unit_key or semantic_object_id or target_path or "UNSCOPED"
    )
    defect_key = _argument_defect_key(
        rule_key,
        thread_index,
        f"OWNER={owner_key}|OBJECT={failed_key}",
        defect_family=defect_family,
    )
    source_values = list(dict.fromkeys(str(x) for x in source_ids if str(x).strip()))
    target_values = list(dict.fromkeys(str(x) for x in target_ids if str(x).strip()))
    evidence_values = list(dict.fromkeys(str(x) for x in evidence_ids if str(x).strip()))
    route = str(policy.get("route") or "").upper()
    blocking = bool(policy.get("blocking", True))
    receipt_id = _stable_receipt_id(
        rule_key, defect_family, failure_code, defect_key, source_id, target_id, missing_relation
    )
    return {
        "receipt_id": receipt_id,
        "defect_family": str(defect_family),
        "rule_id": str(rule_id),
        "failure_code": failure_code,
        "finding_code": resolved_finding_code,
        "thread_index": thread_index if isinstance(thread_index, int) else None,
        "research_question_id": str(research_question_id or "") or None,
        "source_id": str(source_id or "") or None,
        "target_id": str(target_id or "") or None,
        "source_ids": source_values,
        "target_ids": target_values,
        "missing_relation": str(missing_relation or "") or None,
        "semantic_object_id": str(semantic_object_id or "") or None,
        "semantic_component": str(semantic_component or "RESEARCH_DESIGN"),
        "semantic_review_unit_key": str(semantic_review_unit_key or "") or None,
        "target_path": str(target_path),
        "owner_semantic_object_id": owner_id,
        "owner_semantic_component": owner_component,
        "owner_semantic_review_unit_key": owner_review_key,
        "owner_target_path": owner_path,
        "quality_dimension": resolved_quality_dimension,
        "required_node_type": str(
            required_node_type
            or _required_node_type_for_component(owner_component)
            or _required_node_type_for_component(semantic_component)
            or "EVIDENCE"
        ),
        "defect_key": defect_key,
        "description": str(description),
        "repair_instruction": str(repair_instruction),
        "evidence_ids": evidence_values,
        "suggested_route": route,
        "blocking": blocking,
    }


def _row_reference_values(row: dict[str, Any], field: str) -> list[str]:
    raw = row.get(str(field))
    if isinstance(raw, str):
        return [raw] if raw else []
    if isinstance(raw, (list, tuple)):
        return [str(value) for value in raw if str(value).strip()]
    return []


def _matrix_thread_index_by_question(candidate: dict[str, Any]) -> dict[str, int]:
    graph = candidate.get("argument_architecture") or {}
    return {
        str(question.get("node_id")): index
        for index, question in enumerate(graph.get("research_questions") or [])
        if isinstance(question, dict) and question.get("node_id")
    }


def _matrix_expected_bindings(candidate: dict[str, Any], research_question_id: str) -> dict[str, set[str]]:
    graph = candidate.get("argument_architecture") or {}
    required_fields = set(_argument_matrix_covered_fields())
    known: dict[str, set[str]] = {field: set() for field in required_fields}
    known["research_question_id"] = {str(research_question_id)} if research_question_id else set()
    node_type_by_id = {
        str(node.get("node_id")): str(node.get("node_type") or "")
        for node in graph.get("nodes") or []
        if isinstance(node, dict) and node.get("node_id")
    }
    node_type_by_id.update(
        {
            str(q.get("node_id")): "RESEARCH_QUESTION"
            for q in graph.get("research_questions") or []
            if isinstance(q, dict) and q.get("node_id")
        }
    )
    field_node_types = _repair_reference_node_types()
    edges_by_relation: dict[str, list[tuple[str, str]]] = {}
    for edge in graph.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        relation = str(edge.get("relation") or "")
        source = str(edge.get("source_id") or "")
        target = str(edge.get("target_id") or "")
        if relation and source and target:
            edges_by_relation.setdefault(relation, []).append((source, target))

    def field_for_entity(entity_id: str, candidate_fields: list[str]) -> str | None:
        if "research_question_id" in candidate_fields and node_type_by_id.get(entity_id) == "RESEARCH_QUESTION":
            return "research_question_id"
        actual_type = node_type_by_id.get(entity_id, "")
        for field in candidate_fields:
            expected = field_node_types.get(field)
            if expected == "METHOD" and actual_type in _reference_type_members("METHOD"):
                return field
            if expected == actual_type:
                return field
        return candidate_fields[0] if len(candidate_fields) == 1 else None

    for _ in range(max(2, len(_argument_chain_specs()) * 2)):
        changed = False
        for spec in _argument_chain_specs():
            source_field = str(spec.get("source_field") or "")
            target_fields = [str(value) for value in spec.get("target_fields") or ()]
            pairs = edges_by_relation.get(str(spec.get("relation") or ""), [])
            source_values = known.setdefault(source_field, set())
            target_values = set().union(*(known.setdefault(field, set()) for field in target_fields)) if target_fields else set()
            if source_values:
                for source, target in pairs:
                    if source not in source_values:
                        continue
                    field = field_for_entity(target, target_fields)
                    if field and target not in known.setdefault(field, set()):
                        known[field].add(target); changed = True
            if target_values:
                for source, target in pairs:
                    if target not in target_values:
                        continue
                    field = field_for_entity(source, [source_field])
                    if field and source not in known.setdefault(source_field, set()):
                        known[source_field].add(source); changed = True
        if not changed:
            break
    return {field: set(known.get(field) or set()) for field in required_fields}


def _critic_chain_checks(
    candidate: dict[str, Any],
    *,
    with_receipts: bool = False,
) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    graph = candidate.get("argument_architecture") or {}
    matrix = [
        row for row in candidate.get("research_design_matrix") or []
        if isinstance(row, dict)
    ]
    edge_pairs: dict[str, set[tuple[str, str]]] = {}
    for edge in graph.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        relation = str(edge.get("relation") or "")
        source = str(edge.get("source_id") or "")
        target = str(edge.get("target_id") or "")
        if relation and source and target:
            edge_pairs.setdefault(relation, set()).add((source, target))

    checks: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    question_thread_indexes = _matrix_thread_index_by_question(candidate)
    for spec in _argument_chain_specs():
        chain_type = str(spec.get("chain_type") or "")
        source_field = str(spec.get("source_field") or "")
        source_presence = str(spec.get("source_presence") or "REQUIRED").upper()
        target_fields = [str(value) for value in spec.get("target_fields") or ()]
        relation = str(spec.get("relation") or "")
        coverage = str(spec.get("coverage") or "BOTH").upper()
        relation_pairs = edge_pairs.get(relation, set())
        sources: list[str] = []
        targets: list[str] = []
        chain_receipts: list[dict[str, Any]] = []
        complete = bool(matrix)

        for row_position, row in enumerate(matrix):
            research_question_id = str(row.get("research_question_id") or "") or None
            thread_index = question_thread_indexes.get(str(research_question_id or ""))
            row_sources = _row_reference_values(row, source_field)
            row_targets = list(
                dict.fromkeys(
                    value
                    for field in target_fields
                    for value in _row_reference_values(row, field)
                )
            )
            for value in row_sources:
                if value not in sources:
                    sources.append(value)
            for value in row_targets:
                if value not in targets:
                    targets.append(value)
            row_path = f"/result/research_design_matrix/{row_position}"

            if source_presence == "IF_PRESENT" and not row_sources:
                continue
            if not row_sources:
                complete = False
                chain_receipts.append(
                    _argument_deterministic_receipt(
                        rule_id=_ARGUMENT_CHAIN_RULE_ID,
                        rule_key=f"{_ARGUMENT_CHAIN_RULE_ID}:{chain_type}",
                        defect_family="CHAIN_SOURCE_SET_MISSING",
                        thread_index=thread_index,
                        research_question_id=research_question_id,
                        source_id=None,
                        target_id=None,
                        source_ids=(),
                        target_ids=row_targets,
                        missing_relation=relation,
                        semantic_object_id=None,
                        semantic_component=str(
                            _critic_component_for_matrix_field(source_field) or "RESEARCH_DESIGN"
                        ),
                        semantic_review_unit_key=None,
                        target_path=row_path,
                        required_node_type=str(
                            _repair_reference_node_types().get(source_field) or "EVIDENCE"
                        ),
                        description=f"{chain_type} 缺少源语义对象，无法评价显式 {relation} 关系。",
                        repair_instruction="由原 Argument Producer 补齐当前研究线程的缺失语义对象和显式关系；Runtime 不得猜测或制造引用。",
                    )
                )
            if not row_targets:
                complete = False
                chain_receipts.append(
                    _argument_deterministic_receipt(
                        rule_id=_ARGUMENT_CHAIN_RULE_ID,
                        rule_key=f"{_ARGUMENT_CHAIN_RULE_ID}:{chain_type}",
                        defect_family="CHAIN_TARGET_SET_MISSING",
                        thread_index=thread_index,
                        research_question_id=research_question_id,
                        source_id=None,
                        target_id=None,
                        source_ids=row_sources,
                        target_ids=(),
                        missing_relation=relation,
                        semantic_object_id=None,
                        semantic_component="RESEARCH_DESIGN",
                        semantic_review_unit_key=None,
                        target_path=row_path,
                        description=f"{chain_type} 缺少目标语义对象，无法评价显式 {relation} 关系。",
                        repair_instruction="由原 Argument Producer 补齐当前研究线程的缺失语义对象和显式关系；Runtime 不得猜测或制造引用。",
                    )
                )
            if not row_sources or not row_targets:
                continue

            if coverage in {"SOURCE", "BOTH"}:
                for source_id in row_sources:
                    if any((source_id, target_id) in relation_pairs for target_id in row_targets):
                        continue
                    complete = False
                    location = _critic_object_location(
                        candidate, source_id, fallback_thread=thread_index
                    )
                    chain_receipts.append(
                        _argument_deterministic_receipt(
                            rule_id=_ARGUMENT_CHAIN_RULE_ID,
                            rule_key=f"{_ARGUMENT_CHAIN_RULE_ID}:{chain_type}",
                            defect_family="CHAIN_RELATION_MISSING_FROM_SOURCE",
                            thread_index=thread_index,
                            research_question_id=research_question_id,
                            source_id=source_id,
                            target_id=None,
                            source_ids=row_sources,
                            target_ids=row_targets,
                            missing_relation=relation,
                            semantic_object_id=location.get("semantic_object_id"),
                            semantic_component=str(location.get("semantic_component") or "RESEARCH_DESIGN"),
                            semantic_review_unit_key=location.get("semantic_review_unit_key"),
                            target_path=str(location.get("target_path") or row_path),
                            description=f"{chain_type} 在线程内缺少从当前源对象出发的显式 {relation} 关系。",
                            repair_instruction=f"由原 Argument Producer 为当前线程补齐模型明确表达的 {relation} 关系；Runtime 不得跨线程借用关系。",
                        )
                    )
            if coverage in {"TARGET", "BOTH"}:
                for target_id in row_targets:
                    if any((source_id, target_id) in relation_pairs for source_id in row_sources):
                        continue
                    complete = False
                    location = _critic_object_location(
                        candidate, target_id, fallback_thread=thread_index
                    )
                    chain_receipts.append(
                        _argument_deterministic_receipt(
                            rule_id=_ARGUMENT_CHAIN_RULE_ID,
                            rule_key=f"{_ARGUMENT_CHAIN_RULE_ID}:{chain_type}",
                            defect_family="CHAIN_RELATION_MISSING_TO_TARGET",
                            thread_index=thread_index,
                            research_question_id=research_question_id,
                            source_id=None,
                            target_id=target_id,
                            source_ids=row_sources,
                            target_ids=row_targets,
                            missing_relation=relation,
                            semantic_object_id=location.get("semantic_object_id"),
                            semantic_component=str(location.get("semantic_component") or "RESEARCH_DESIGN"),
                            semantic_review_unit_key=location.get("semantic_review_unit_key"),
                            target_path=str(location.get("target_path") or row_path),
                            description=f"{chain_type} 在线程内缺少指向当前目标对象的显式 {relation} 关系。",
                            repair_instruction=f"由原 Argument Producer 为当前线程补齐模型明确表达的 {relation} 关系；Runtime 不得跨线程借用关系。",
                        )
                    )

        optional_source_absent = source_presence == "IF_PRESENT" and not sources
        checks.append(
            {
                "chain_type": chain_type,
                "source_ids": sources,
                "target_ids": targets,
                "complete": complete,
                "evidence": (
                    "当前未声明该可选源语义对象，因此该关系链不适用。"
                    if optional_source_absent
                    else (
                        f"运行时根据同一研究线程内模型明确表达的 {relation} 关系确认该关系链闭合。"
                        if complete
                        else f"运行时发现至少一个研究线程缺少所需的显式 {relation} 关系。"
                    )
                ),
            }
        )
        receipts.extend(chain_receipts)
    return (checks, receipts) if with_receipts else checks


def _critic_design_matrix_checks(
    candidate: dict[str, Any],
    *,
    with_receipts: bool = False,
) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    required = _argument_matrix_required_fields()
    covered = _argument_matrix_covered_fields()
    matrix = [
        row for row in candidate.get("research_design_matrix") or [] if isinstance(row, dict)
    ]
    graph = candidate.get("argument_architecture") or {}
    questions = [
        q for q in graph.get("research_questions") or [] if isinstance(q, dict) and q.get("node_id")
    ]
    question_ids = [str(q.get("node_id")) for q in questions]
    question_index = {qid: index for index, qid in enumerate(question_ids)}
    checks: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []

    if not matrix:
        receipts.append(
            _argument_deterministic_receipt(
                rule_id=_ARGUMENT_MATRIX_RULE_ID, rule_key=_ARGUMENT_MATRIX_RULE_ID,
                defect_family="DESIGN_MATRIX_MISSING", thread_index=None,
                research_question_id=None, source_id=None, target_id=None,
                missing_relation=None, semantic_object_id=None, semantic_component="RESEARCH_DESIGN",
                semantic_review_unit_key=None, target_path="/result/research_design_matrix",
                description="研究设计矩阵为空，无法形成可核验的逐线程语义绑定。",
                repair_instruction="由原 Argument Producer 生成逐研究线程的完整研究设计矩阵；Runtime 不得制造机器引用。",
            )
        )
        return (checks, receipts) if with_receipts else checks

    rows_by_question: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for row_position, row in enumerate(matrix):
        rows_by_question.setdefault(str(row.get("research_question_id") or ""), []).append((row_position, row))

    # Every matrix-covered semantic object must occur in exactly one research-thread row.
    graph_nodes = [node for node in graph.get("nodes") or [] if isinstance(node, dict)]
    reference_node_types = _repair_reference_node_types()
    expected_by_question = {
        qid: _matrix_expected_bindings(candidate, qid) for qid in question_ids
    }
    for field in covered:
        expected_type = str(reference_node_types.get(field) or "")
        if not expected_type:
            continue
        graph_ids = {
            str(node.get("node_id"))
            for node in graph_nodes
            if node.get("node_id")
            and _node_matches_reference_type(str(node.get("node_type") or ""), expected_type)
        }
        reference_rows: dict[str, list[int]] = {}
        for row_position, row in enumerate(matrix):
            for object_id in _row_reference_values(row, field):
                reference_rows.setdefault(object_id, []).append(row_position)
        for object_id in sorted(graph_ids):
            occurrences = reference_rows.get(object_id, [])
            if len(occurrences) == 1:
                continue
            expected_threads = [
                question_index[qid]
                for qid in question_ids
                if object_id in set(expected_by_question.get(qid, {}).get(field) or set())
            ]
            thread_index = expected_threads[0] if len(expected_threads) == 1 else None
            location = _critic_object_location(
                candidate, object_id, fallback_thread=thread_index,
                fallback_component=str(_critic_component_for_matrix_field(field) or "RESEARCH_DESIGN"),
            )
            receipts.append(
                _argument_deterministic_receipt(
                    rule_id=_ARGUMENT_MATRIX_RULE_ID,
                    rule_key=f"{_ARGUMENT_MATRIX_RULE_ID}:OBJECT_COVERAGE:{field}",
                    defect_family="DESIGN_MATRIX_THREAD_COVERAGE_MISMATCH",
                    thread_index=thread_index,
                    research_question_id=(
                        question_ids[thread_index] if isinstance(thread_index, int) and 0 <= thread_index < len(question_ids) else None
                    ),
                    source_id=None, target_id=object_id, source_ids=(), target_ids=(object_id,),
                    missing_relation=None, semantic_object_id=object_id,
                    semantic_component=str(location.get("semantic_component") or _critic_component_for_matrix_field(field) or "RESEARCH_DESIGN"),
                    semantic_review_unit_key=location.get("semantic_review_unit_key"),
                    target_path=str(location.get("target_path") or "/result/research_design_matrix"),
                    description=(
                        f"图中语义对象 {object_id} 未被任何研究设计矩阵行覆盖。"
                        if not occurrences
                        else f"图中语义对象 {object_id} 被多个研究设计矩阵行重复引用。"
                    ),
                    repair_instruction="由原 Argument Producer 从当前论证图重新生成逐线程矩阵绑定，确保每个核心语义对象恰好归属一个研究线程。",
                    required_node_type=expected_type,
                )
            )

    # Matrix rows and graph research questions must form a bijection.
    for qid in question_ids:
        matches = rows_by_question.get(qid, [])
        if len(matches) == 1:
            continue
        thread_index = question_index[qid]
        location = _critic_object_location(candidate, qid, fallback_thread=thread_index, fallback_component="QUESTION")
        receipts.append(
            _argument_deterministic_receipt(
                rule_id=_ARGUMENT_MATRIX_RULE_ID,
                rule_key=f"{_ARGUMENT_MATRIX_RULE_ID}:THREAD_COVERAGE",
                defect_family="DESIGN_MATRIX_THREAD_COVERAGE_MISMATCH",
                thread_index=thread_index, research_question_id=qid, source_id=None, target_id=qid,
                missing_relation=None, semantic_object_id=qid, semantic_component="QUESTION",
                semantic_review_unit_key=location.get("semantic_review_unit_key"),
                target_path=str(location.get("target_path") or "/result/research_design_matrix"),
                description=("研究问题缺少对应的设计矩阵行。" if not matches else "同一研究问题对应多个设计矩阵行。"),
                repair_instruction="由原 Argument Producer 重新生成研究设计矩阵，确保每个研究问题恰好对应一行。",
            )
        )
    for qid, matches in rows_by_question.items():
        if qid in question_index:
            continue
        for row_position, _ in matches:
            receipts.append(
                _argument_deterministic_receipt(
                    rule_id=_ARGUMENT_MATRIX_RULE_ID, rule_key=f"{_ARGUMENT_MATRIX_RULE_ID}:THREAD_COVERAGE",
                    defect_family="DESIGN_MATRIX_THREAD_COVERAGE_MISMATCH", thread_index=None,
                    research_question_id=qid or None, source_id=None, target_id=None, missing_relation=None,
                    semantic_object_id=None, semantic_component="RESEARCH_DESIGN", semantic_review_unit_key=None,
                    target_path=f"/result/research_design_matrix/{row_position}",
                    description="设计矩阵行引用了图中不存在的研究问题。",
                    repair_instruction="由原 Argument Producer 重新生成矩阵行并只引用当前图中的研究问题。",
                )
            )

    for row_position, row in enumerate(matrix):
        research_question_id = str(row.get("research_question_id") or "")
        thread_index = question_index.get(research_question_id)
        missing = [field for field in required if not row.get(field)]
        expected = _matrix_expected_bindings(candidate, research_question_id) if research_question_id in question_index else {}
        mismatched = [
            field for field in covered
            if expected and set(_row_reference_values(row, field)) != set(expected.get(field) or set())
        ]
        incomplete = list(dict.fromkeys([*missing, *mismatched]))
        checks.append(
            {
                "research_question_id": research_question_id,
                "complete": not incomplete and research_question_id in question_index and len(rows_by_question.get(research_question_id, [])) == 1,
                "missing_dimensions": missing,
                "mismatched_dimensions": mismatched,
                "evidence": (
                    "运行时确认矩阵行与图中该研究线程的语义对象逐字段一致。"
                    if not incomplete and research_question_id in question_index and len(rows_by_question.get(research_question_id, [])) == 1
                    else "运行时发现矩阵行与图中研究线程覆盖不一致。"
                ),
            }
        )
        if missing:
            receipts.append(
                _argument_deterministic_receipt(
                    rule_id=_ARGUMENT_MATRIX_RULE_ID, rule_key=_ARGUMENT_MATRIX_RULE_ID,
                    defect_family="DESIGN_MATRIX_REQUIRED_DIMENSION_MISSING", thread_index=thread_index,
                    research_question_id=research_question_id or None, source_id=None, target_id=None, missing_relation=None,
                    semantic_object_id=research_question_id or None, semantic_component="RESEARCH_DESIGN",
                    semantic_review_unit_key=(f"RESEARCH_QUESTION:{thread_index + 1}" if isinstance(thread_index, int) else None),
                    target_path=f"/result/research_design_matrix/{row_position}",
                    description="研究设计矩阵缺失确定性必需维度：" + "、".join(missing),
                    repair_instruction="由原 Argument Producer 补齐研究设计矩阵中缺失的语义组成；不得由局部修复直接制造机器引用。",
                )
            )
        if mismatched:
            location = _critic_object_location(candidate, research_question_id or None, fallback_thread=thread_index, fallback_component="QUESTION")
            receipts.append(
                _argument_deterministic_receipt(
                    rule_id=_ARGUMENT_MATRIX_RULE_ID, rule_key=f"{_ARGUMENT_MATRIX_RULE_ID}:BINDING",
                    defect_family="DESIGN_MATRIX_THREAD_COVERAGE_MISMATCH", thread_index=thread_index,
                    research_question_id=research_question_id or None, source_id=None, target_id=research_question_id or None, missing_relation=None,
                    semantic_object_id=research_question_id or None, semantic_component="QUESTION",
                    semantic_review_unit_key=location.get("semantic_review_unit_key"),
                    target_path=str(location.get("target_path") or f"/result/research_design_matrix/{row_position}"),
                    description="研究设计矩阵与图中线程对象不一致：" + "、".join(mismatched),
                    repair_instruction="由原 Argument Producer 从当前论证图重新生成该线程矩阵绑定；不得跨线程借用对象。",
                )
            )
    return (checks, receipts) if with_receipts else checks


def _legacy_stage4_critic_evidence_checks(
    candidate: dict[str, Any],
) -> list[dict[str, Any]]:
    """Compatibility only for the excluded Stage4/simulated path.

    The database-native v5 path never calls this branch; it always supplies the
    canonical critic envelope and evaluates the Evidence Requirement Registry
    against current evidence records.
    """
    graph = candidate.get("argument_architecture") or {}
    result: list[dict[str, Any]] = []

    def append(node: dict[str, Any], *, foundation: bool = False) -> None:
        refs = [ref for ref in node.get("source_refs") or [] if isinstance(ref, dict)]
        status = str(node.get("status") or "")
        if foundation:
            supported = status in {"SUPPORTED", "CONFIRMED"} and any(
                str(ref.get("source_type") or "") in {"EVIDENCE_MATERIAL", "TECHNICAL_MATERIAL"}
                and bool(str(ref.get("quoted_text") or "").strip())
                for ref in refs
            )
        else:
            supported = bool(refs) and (
                not status or status in {"SUPPORTED", "CONFIRMED"}
            )
        result.append(
            {
                "node_id": str(node.get("node_id") or ""),
                "supported": supported,
                "source_ids": [
                    str(ref.get("source_id"))
                    for ref in refs
                    if ref.get("source_id")
                ],
                "reason": (
                    "Legacy Stage4 candidate carries a usable source binding."
                    if supported
                    else "Legacy Stage4 candidate lacks a usable source binding."
                ),
            }
        )

    proposition = graph.get("central_proposition") or {}
    if isinstance(proposition, dict) and proposition.get("node_id"):
        append(proposition)
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        node_type = str(node.get("node_type") or "")
        if node_type in {
            "RESEARCH_GAP",
            "LIMITATION_MECHANISM",
            "CLOSEST_PRIOR_WORK",
            "TEAM_EVIDENCE",
        }:
            append(node, foundation=node_type == "TEAM_EVIDENCE")
    return result


def _graph_thread_memberships(candidate: dict[str, Any]) -> dict[str, set[int]]:
    """Return canonical thread memberships for core and nested graph objects.

    Core membership comes only from question identity + design-matrix binding. Nested
    objects inherit membership through registry-owned semantic relations, so Critic
    target identity never has to trust a model-supplied thread index.
    """
    graph = candidate.get("argument_architecture") or {}
    questions = [
        question for question in graph.get("research_questions") or []
        if isinstance(question, dict) and question.get("node_id")
    ]
    question_index = {
        str(question.get("node_id")): index for index, question in enumerate(questions)
    }
    memberships: dict[str, set[int]] = {
        question_id: {index} for question_id, index in question_index.items()
    }
    for row in candidate.get("research_design_matrix") or []:
        if not isinstance(row, dict):
            continue
        question_id = str(row.get("research_question_id") or "")
        thread_index = question_index.get(question_id)
        if thread_index is None:
            continue
        for field in _argument_matrix_covered_fields():
            for object_id in _row_reference_values(row, field):
                memberships.setdefault(object_id, set()).add(thread_index)

    nodes = [node for node in graph.get("nodes") or [] if isinstance(node, dict)]
    edges = [edge for edge in graph.get("edges") or [] if isinstance(edge, dict)]
    node_type_by_id = {
        str(node.get("node_id")): str(node.get("node_type") or "")
        for node in nodes if node.get("node_id")
    }
    policies = dict(_argument_graph_ownership_config().get("node_types") or {})
    authored_bindings: dict[str, set[int]] = {}
    for binding in candidate.get("authored_thread_assumptions") or []:
        if not isinstance(binding, dict) or not isinstance(binding.get("thread_index"), int):
            continue
        thread_index = int(binding["thread_index"])
        if not 0 <= thread_index < len(questions):
            continue
        for assumption_id in binding.get("assumption_node_ids") or []:
            authored_bindings.setdefault(str(assumption_id), set()).add(thread_index)

    for _ in range(max(1, len(nodes) + 1)):
        changed = False
        for node in nodes:
            node_id = str(node.get("node_id") or "")
            node_type = str(node.get("node_type") or "")
            raw_policy = policies.get(node_type)
            if not node_id or not isinstance(raw_policy, Mapping):
                continue
            policy = dict(raw_policy)
            inherited: set[int] = set()
            if str(policy.get("mode") or "").upper() == "METHOD_OR_THREAD_BINDING":
                inherited.update(authored_bindings.get(node_id, set()))
                for edge in edges:
                    if (
                        str(edge.get("relation") or "") == "ASSUMES"
                        and str(edge.get("target_id") or "") == node_id
                    ):
                        inherited.update(memberships.get(str(edge.get("source_id") or ""), set()))
            else:
                relation = str(policy.get("relation") or "")
                direction = str(policy.get("direction") or "").upper()
                for edge in edges:
                    if str(edge.get("relation") or "") != relation:
                        continue
                    if direction == "INCOMING" and str(edge.get("target_id") or "") == node_id:
                        inherited.update(memberships.get(str(edge.get("source_id") or ""), set()))
                    elif direction == "OUTGOING" and str(edge.get("source_id") or "") == node_id:
                        inherited.update(memberships.get(str(edge.get("target_id") or ""), set()))
            if inherited - memberships.get(node_id, set()):
                memberships.setdefault(node_id, set()).update(inherited)
                changed = True
        if not changed:
            break
    return memberships


def _graph_relation_signatures() -> dict[str, list[tuple[set[str], set[str]]]]:
    signatures: dict[str, list[tuple[set[str], set[str]]]] = {}
    for spec in _argument_chain_specs():
        source_type = _reference_type_for_matrix_field(str(spec.get("source_field") or ""))
        target_types = {
            member
            for field in spec.get("target_fields") or ()
            for member in _reference_type_members(
                str(_reference_type_for_matrix_field(str(field)) or "")
            )
        }
        source_types = _reference_type_members(str(source_type or ""))
        relation = str(spec.get("relation") or "")
        if relation and source_types and target_types:
            signatures.setdefault(relation, []).append((source_types, target_types))

    graph_config = _argument_graph_ownership_config()
    for child_type, raw_policy in dict(graph_config.get("node_types") or {}).items():
        policy = dict(raw_policy) if isinstance(raw_policy, Mapping) else {}
        mode = str(policy.get("mode") or "").upper()
        if mode == "METHOD_OR_THREAD_BINDING":
            signatures.setdefault("ASSUMES", []).append(
                (_reference_type_members("METHOD"), {str(child_type)})
            )
            continue
        relation = str(policy.get("relation") or "")
        direction = str(policy.get("direction") or "").upper()
        owner_types = _reference_type_members(str(policy.get("owner_reference_type") or ""))
        child_types = {str(child_type)}
        if not relation or not owner_types:
            continue
        pair = (
            (owner_types, child_types)
            if direction == "INCOMING"
            else (child_types, owner_types)
        )
        signatures.setdefault(relation, []).append(pair)
    return signatures


def _critic_graph_topology_checks(
    candidate: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Check that every graph object and edge has one registry-defined semantic owner."""
    graph = candidate.get("argument_architecture") or {}
    nodes = [node for node in graph.get("nodes") or [] if isinstance(node, dict)]
    questions = [
        question
        for question in graph.get("research_questions") or []
        if isinstance(question, dict)
    ]
    edges = [edge for edge in graph.get("edges") or [] if isinstance(edge, dict)]
    node_type_by_id = {
        str(node.get("node_id")): str(node.get("node_type") or "")
        for node in nodes
        if node.get("node_id")
    }
    node_type_by_id.update(
        {
            str(question.get("node_id")): "RESEARCH_QUESTION"
            for question in questions
            if question.get("node_id")
        }
    )
    memberships = _graph_thread_memberships(candidate)
    question_ids = [
        str(question.get("node_id"))
        for question in questions
        if question.get("node_id")
    ]
    topology = _argument_graph_ownership_config()
    policies = dict(topology.get("node_types") or {})
    defect_family = str(topology.get("deterministic_defect_family") or "GRAPH_TOPOLOGY_INVALID")
    finding_code = str(topology.get("finding_code") or "RESEARCH_DESIGN_INCOMPLETE")
    quality_dimension = str(topology.get("quality_dimension") or "ARGUMENT_CHAIN")
    default_reason = str(topology.get("reason") or "论证图语义对象归属不完整。")
    repair_instruction = str(
        topology.get("repair_instruction")
        or "由原 Argument Producer 重新生成论证图语义对象及其归属关系。"
    )
    checks: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []

    def emit(
        *,
        rule_key: str,
        object_id: str | None,
        thread_index: int | None,
        target_path: str,
        description: str,
        component: str = "RESEARCH_DESIGN",
        target_id: str | None = None,
    ) -> None:
        location = _critic_object_location(
            candidate,
            object_id,
            fallback_thread=thread_index,
            fallback_component=component,
        )
        path = str(location.get("target_path") or target_path)
        receipts.append(
            _argument_deterministic_receipt(
                rule_id=_ARGUMENT_STRUCTURAL_RULE_ID,
                rule_key=rule_key,
                defect_family=defect_family,
                finding_code=finding_code,
                quality_dimension=quality_dimension,
                thread_index=thread_index,
                research_question_id=(
                    question_ids[thread_index]
                    if isinstance(thread_index, int) and 0 <= thread_index < len(question_ids)
                    else None
                ),
                source_id=None,
                target_id=target_id or object_id,
                source_ids=(),
                target_ids=(target_id or object_id,) if (target_id or object_id) else (),
                missing_relation=None,
                semantic_object_id=object_id,
                semantic_component=str(location.get("semantic_component") or component),
                semantic_review_unit_key=location.get("semantic_review_unit_key"),
                target_path=path,
                owner_semantic_object_id=object_id,
                owner_semantic_component=str(location.get("semantic_component") or component),
                owner_semantic_review_unit_key=location.get("semantic_review_unit_key"),
                owner_target_path=path,
                description=description,
                repair_instruction=repair_instruction,
                required_node_type=str(node_type_by_id.get(str(object_id or "")) or "EVIDENCE"),
            )
        )

    # IDs are machine identity across the entire graph object namespace, not only nodes[].
    object_id_paths: dict[str, list[str]] = {}
    proposition = graph.get("central_proposition") or {}
    proposition_id = str(proposition.get("node_id") or "") if isinstance(proposition, dict) else ""
    if proposition_id:
        object_id_paths.setdefault(proposition_id, []).append(
            "/result/argument_architecture/central_proposition"
        )
    for index, question in enumerate(questions):
        question_id = str(question.get("node_id") or "")
        if question_id:
            object_id_paths.setdefault(question_id, []).append(
                f"/result/argument_architecture/research_questions/{index}"
            )
    for index, node in enumerate(nodes):
        node_id = str(node.get("node_id") or "")
        if node_id:
            object_id_paths.setdefault(node_id, []).append(
                f"/result/argument_architecture/nodes/{index}"
            )
    for object_id, paths in object_id_paths.items():
        if len(paths) <= 1:
            continue
        checks.append({
            "requirement_id": "GRAPH_OBJECT_ID_UNIQUE",
            "semantic_object_id": object_id,
            "satisfied": False,
            "reason": "论证图存在跨对象集合重复 node_id，语义对象身份不唯一。",
        })
        emit(
            rule_key=f"{_ARGUMENT_STRUCTURAL_RULE_ID}:GRAPH_OBJECT_ID_UNIQUE:{object_id}",
            object_id=None,
            thread_index=None,
            target_path=paths[0],
            description="论证图存在跨对象集合重复 node_id，语义对象身份不唯一。",
        )

    edge_id_positions: dict[str, list[int]] = {}
    for index, edge in enumerate(edges):
        edge_id = str(edge.get("edge_id") or "")
        if edge_id:
            edge_id_positions.setdefault(edge_id, []).append(index)
    for edge_id, positions in edge_id_positions.items():
        if len(positions) <= 1:
            continue
        checks.append({
            "requirement_id": "GRAPH_EDGE_ID_UNIQUE",
            "semantic_object_id": None,
            "satisfied": False,
            "reason": "论证图存在重复 edge_id，关系身份不唯一。",
        })
        emit(
            rule_key=f"{_ARGUMENT_STRUCTURAL_RULE_ID}:GRAPH_EDGE_ID_UNIQUE:{edge_id}",
            object_id=None,
            thread_index=None,
            target_path=f"/result/argument_architecture/edges/{positions[0]}",
            description="论证图存在重复 edge_id，关系身份不唯一。",
        )

    # Every edge must match a relation signature derivable from the chain/topology registries.
    signatures = _graph_relation_signatures()
    for edge_index, edge in enumerate(edges):
        relation = str(edge.get("relation") or "")
        source_id = str(edge.get("source_id") or "")
        target_id = str(edge.get("target_id") or "")
        source_type = node_type_by_id.get(source_id, "")
        target_type = node_type_by_id.get(target_id, "")
        valid = bool(
            source_id
            and target_id
            and source_type
            and target_type
            and any(
                source_type in source_types and target_type in target_types
                for source_types, target_types in signatures.get(relation, [])
            )
        )
        checks.append({
            "requirement_id": "GRAPH_EDGE_SIGNATURE",
            "semantic_object_id": target_id or source_id or None,
            "satisfied": valid,
            "reason": (
                "论证图关系端点类型与注册语义签名一致。"
                if valid
                else "论证图关系不存在注册语义签名，或其 source/target 类型不兼容。"
            ),
        })
        if valid:
            continue
        thread_candidates = set(memberships.get(source_id, set())) | set(memberships.get(target_id, set()))
        thread_index = next(iter(thread_candidates)) if len(thread_candidates) == 1 else None
        object_id = target_id if target_id in node_type_by_id else source_id if source_id in node_type_by_id else None
        emit(
            rule_key=f"{_ARGUMENT_STRUCTURAL_RULE_ID}:GRAPH_EDGE_SIGNATURE:{edge_index}",
            object_id=object_id,
            thread_index=thread_index,
            target_path=f"/result/argument_architecture/edges/{edge_index}",
            description="论证图关系不存在注册语义签名，或其 source/target 类型不兼容。",
            target_id=target_id or None,
        )

    thread_bindings: dict[str, list[int]] = {}
    for binding in candidate.get("authored_thread_assumptions") or []:
        if not isinstance(binding, dict) or not isinstance(binding.get("thread_index"), int):
            continue
        thread_index = int(binding["thread_index"])
        for assumption_id in binding.get("assumption_node_ids") or []:
            thread_bindings.setdefault(str(assumption_id), []).append(thread_index)

    for node_index, node in enumerate(nodes):
        node_id = str(node.get("node_id") or "")
        node_type = str(node.get("node_type") or "")
        raw_policy = policies.get(node_type)
        if not node_id or not isinstance(raw_policy, Mapping):
            continue
        policy = dict(raw_policy)
        mode = str(policy.get("mode") or "").upper()
        owner_ids: list[str] = []
        thread_candidates: set[int] = set()
        valid = False
        if mode == "METHOD_OR_THREAD_BINDING":
            method_owner_ids = [
                str(edge.get("source_id"))
                for edge in edges
                if str(edge.get("relation") or "") == "ASSUMES"
                and str(edge.get("target_id") or "") == node_id
                and _node_matches_reference_type(
                    node_type_by_id.get(str(edge.get("source_id") or ""), ""), "METHOD"
                )
            ]
            binding_threads = thread_bindings.get(node_id, [])
            owner_ids = method_owner_ids
            for owner_id in method_owner_ids:
                thread_candidates.update(memberships.get(owner_id, set()))
            thread_candidates.update(binding_threads)
            valid = (
                len(method_owner_ids) + len(binding_threads) == 1
                and len(thread_candidates) == 1
            )
        else:
            relation = str(policy.get("relation") or "")
            direction = str(policy.get("direction") or "").upper()
            expected_owner_type = str(policy.get("owner_reference_type") or "")
            relation_edges = [
                edge
                for edge in edges
                if str(edge.get("relation") or "") == relation
                and (
                    str(edge.get("target_id") or "") == node_id
                    if direction == "INCOMING"
                    else str(edge.get("source_id") or "") == node_id
                )
            ]
            owner_ids = [
                str(edge.get("source_id") or "")
                if direction == "INCOMING"
                else str(edge.get("target_id") or "")
                for edge in relation_edges
            ]
            valid_owner_ids = [
                owner_id
                for owner_id in owner_ids
                if _node_matches_reference_type(
                    node_type_by_id.get(owner_id, ""), expected_owner_type
                )
            ]
            for owner_id in valid_owner_ids:
                thread_candidates.update(memberships.get(owner_id, set()))
            valid = (
                len(relation_edges) == 1
                and len(valid_owner_ids) == 1
                and len(thread_candidates) == 1
            )
        thread_index = next(iter(thread_candidates)) if len(thread_candidates) == 1 else None
        reason = (
            f"{node_type} 具有唯一且线程一致的注册语义 owner。"
            if valid
            else default_reason
        )
        checks.append({
            "requirement_id": f"GRAPH_OWNERSHIP:{node_type}",
            "semantic_object_id": node_id,
            "satisfied": valid,
            "reason": reason,
        })
        if not valid:
            emit(
                rule_key=f"{_ARGUMENT_STRUCTURAL_RULE_ID}:GRAPH_OWNERSHIP:{node_type}",
                object_id=node_id,
                thread_index=thread_index,
                target_path=f"/result/argument_architecture/nodes/{node_index}",
                description=default_reason,
                component=_critic_review_component_group(node_type) or "RESEARCH_DESIGN",
            )
        elif thread_index is not None:
            memberships.setdefault(node_id, set()).add(thread_index)

    # Thread-assumption bindings may not reference missing/non-assumption objects or invalid threads.
    for assumption_id, bound_threads in thread_bindings.items():
        valid = (
            node_type_by_id.get(assumption_id) == "ASSUMPTION"
            and len(bound_threads) == 1
            and 0 <= bound_threads[0] < len(question_ids)
        )
        if valid:
            continue
        checks.append({
            "requirement_id": "THREAD_ASSUMPTION_BINDING",
            "semantic_object_id": assumption_id or None,
            "satisfied": False,
            "reason": "authored_thread_assumptions 引用了无效、重复或错误类型的假设对象。",
        })
        thread_index = bound_threads[0] if len(bound_threads) == 1 and 0 <= bound_threads[0] < len(question_ids) else None
        emit(
            rule_key=f"{_ARGUMENT_STRUCTURAL_RULE_ID}:THREAD_ASSUMPTION_BINDING:{assumption_id}",
            object_id=assumption_id if assumption_id in node_type_by_id else None,
            thread_index=thread_index,
            target_path="/result/authored_thread_assumptions",
            description="authored_thread_assumptions 引用了无效、重复或错误类型的假设对象。",
        )

    return checks, receipts


def _critic_structural_checks(
    canonical_envelope: dict[str, Any],
    *,
    with_receipts: bool = False,
) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    payload = canonical_envelope.get("payload") or {}
    candidate = payload.get("architecture_candidate") or {}
    semantic_tree = _critic_candidate_semantics(canonical_envelope, include_machine_ids=True)
    checks: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    question_by_thread = {
        index: str(row.get("research_question_id") or "") or None
        for index, row in enumerate(candidate.get("research_design_matrix") or [])
        if isinstance(row, dict)
    }
    for item in _argument_structural_requirement_checks(semantic_tree):
        satisfied = bool(item.get("satisfied"))
        thread_index = item.get("thread_index") if isinstance(item.get("thread_index"), int) else None
        object_id = str(item.get("semantic_object_id") or "")
        component = str(item.get("semantic_component") or "RESEARCH_DESIGN")
        location = _critic_object_location(
            candidate, object_id or None,
            fallback_thread=thread_index, fallback_component=component,
        )
        checks.append({
            "requirement_id": str(item.get("requirement_id") or ""),
            "semantic_object_id": str(location.get("semantic_object_id") or object_id) or None,
            "satisfied": satisfied,
            "reason": (
                f"{item.get('requirement_id')} 由 Structural Requirement Registry 判定为满足。"
                if satisfied else str(item.get("reason") or "确定性结构要求未满足。")
            ),
        })
        if satisfied:
            continue
        path = str(location.get("target_path") or "/result")
        review_key = location.get("semantic_review_unit_key")
        receipts.append(_argument_deterministic_receipt(
            rule_id=_ARGUMENT_STRUCTURAL_RULE_ID,
            rule_key=f"{_ARGUMENT_STRUCTURAL_RULE_ID}:{item.get('requirement_id')}",
            defect_family=str(item.get("deterministic_defect_family") or "STRUCTURAL_REQUIREMENT_UNSATISFIED"),
            finding_code=str(item.get("finding_code") or ""),
            quality_dimension=str(item.get("quality_dimension") or ""),
            thread_index=thread_index,
            research_question_id=question_by_thread.get(thread_index),
            source_id=None, target_id=object_id or None,
            source_ids=(), target_ids=(object_id,) if object_id else (),
            missing_relation=None,
            semantic_object_id=object_id or None, semantic_component=component,
            semantic_review_unit_key=review_key, target_path=path,
            owner_semantic_object_id=object_id or None, owner_semantic_component=component,
            owner_semantic_review_unit_key=review_key, owner_target_path=path,
            description=str(item.get("reason") or "确定性结构要求未满足。"),
            repair_instruction=str(item.get("repair_instruction") or "由原 Argument Producer 补齐缺失的结构语义。"),
            required_node_type=str(item.get("required_node_type") or "EVIDENCE"),
        ))
    topology_checks, topology_receipts = _critic_graph_topology_checks(candidate)
    checks.extend(topology_checks)
    receipts.extend(topology_receipts)
    return (checks, receipts) if with_receipts else checks


def _critic_evidence_checks(
    canonical_envelope_or_candidate: dict[str, Any],
    *,
    with_receipts: bool = False,
) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if "payload" not in canonical_envelope_or_candidate:
        checks = _legacy_stage4_critic_evidence_checks(canonical_envelope_or_candidate)
        return (checks, []) if with_receipts else checks

    canonical_envelope = canonical_envelope_or_candidate
    payload = canonical_envelope.get("payload") or {}
    candidate = payload.get("architecture_candidate") or {}
    _, records = _evidence_records(canonical_envelope)
    semantic_tree = _critic_candidate_semantics(
        canonical_envelope,
        include_machine_ids=True,
    )
    checks: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    for requirement_check in _argument_evidence_requirement_checks(
        semantic_tree, records
    ):
        object_id = str(requirement_check.get("semantic_object_id") or "")
        thread_index = (
            requirement_check.get("thread_index")
            if isinstance(requirement_check.get("thread_index"), int)
            else None
        )
        location = _critic_object_location(
            candidate, object_id or None, fallback_thread=thread_index,
            fallback_component=str(requirement_check.get("semantic_component") or "EVIDENCE"),
        )
        owner_object_id = str(requirement_check.get("owner_semantic_object_id") or object_id or "")
        owner_location = _critic_object_location(
            candidate, owner_object_id or None, fallback_thread=thread_index,
            fallback_component=str(requirement_check.get("semantic_component") or "EVIDENCE"),
        )
        evidence_ids = [
            str(value)
            for value in requirement_check.get("evidence_ids") or []
            if str(value).strip()
        ]
        refs = _refs_for_evidence_ids(evidence_ids, records)
        supported = bool(requirement_check.get("supported"))
        reason = str(requirement_check.get("reason") or "确定性证据要求未满足。")
        node_id = str(location.get("semantic_object_id") or object_id)
        if not node_id:
            # Every critic obligation should resolve to an existing semantic object.
            # Keep the check auditable without inventing an entity identifier.
            node_id = str(
                (candidate.get("argument_architecture") or {})
                .get("central_proposition", {})
                .get("node_id")
                or ""
            )
        if node_id:
            checks.append(
                {
                    "node_id": node_id,
                    "supported": supported,
                    "source_ids": [
                        str(ref.get("source_id"))
                        for ref in refs
                        if isinstance(ref, dict) and ref.get("source_id")
                    ],
                    "reason": (
                        f"{requirement_check.get('requirement_id')} 由 Evidence Requirement Registry 判定为满足。"
                        if supported
                        else reason
                    ),
                }
            )
        if supported:
            continue
        requirement_id = str(requirement_check.get("requirement_id") or "")
        receipts.append(
            _argument_deterministic_receipt(
                rule_id=_ARGUMENT_EVIDENCE_RULE_ID,
                rule_key=f"{_ARGUMENT_EVIDENCE_RULE_ID}:{requirement_id}",
                defect_family=str(
                    requirement_check.get("deterministic_defect_family")
                    or "EVIDENCE_REQUIREMENT_UNSATISFIED"
                ),
                finding_code=str(
                    requirement_check.get("finding_code")
                    or "ARGUMENT_EVIDENCE_UNSUPPORTED"
                ),
                thread_index=thread_index,
                research_question_id=None,
                source_id=None,
                target_id=object_id or None,
                missing_relation=None,
                semantic_object_id=location.get("semantic_object_id") or object_id or None,
                semantic_component=str(location.get("semantic_component") or requirement_check.get("semantic_component") or "EVIDENCE"),
                semantic_review_unit_key=location.get("semantic_review_unit_key"),
                target_path=str(location.get("target_path") or "/result/argument_architecture"),
                owner_semantic_object_id=owner_location.get("semantic_object_id") or owner_object_id or None,
                owner_semantic_component=str(requirement_check.get("semantic_component") or owner_location.get("semantic_component") or "EVIDENCE"),
                owner_semantic_review_unit_key=owner_location.get("semantic_review_unit_key"),
                owner_target_path=str(owner_location.get("target_path") or location.get("target_path") or "/result/argument_architecture"),
                quality_dimension=str(requirement_check.get("quality_dimension") or "EVIDENCE_SUPPORT"),
                required_node_type=str(requirement_check.get("required_node_type") or "EVIDENCE"),
                description=reason,
                repair_instruction=(
                    "由原 Argument Producer 基于当前可核验证据重新生成该语义部件；"
                    "若证据确实不存在则保持未知或提出明确的信息需求，不得伪造来源。"
                ),
                evidence_ids=evidence_ids,
            )
        )
    return (checks, receipts) if with_receipts else checks


def _critic_target_canonical_thread(
    candidate: dict[str, Any], target: dict[str, Any]
) -> int | None:
    component = str(target.get("component") or "").upper()
    if component in {"CENTRAL_PROPOSITION", "SCOPE"}:
        return None
    graph = candidate.get("argument_architecture") or {}
    question_count = len([q for q in graph.get("research_questions") or [] if isinstance(q, dict)])
    if component == "THREAD":
        raw = target.get("thread_index")
        return raw if isinstance(raw, int) and 0 <= raw < question_count else None
    review_key = str(target.get("review_unit_key") or "").strip()
    if review_key:
        _, mapping = _critic_review_units(candidate)
        object_id = str(mapping.get(review_key) or "")
        memberships = _graph_thread_memberships(candidate).get(object_id, set())
        if len(memberships) == 1:
            return next(iter(memberships))
    raw = target.get("thread_index")
    return raw if isinstance(raw, int) and 0 <= raw < question_count else None


def _critic_target_path(candidate, target):
    graph = candidate.get("argument_architecture") or {}
    matrix = candidate.get("research_design_matrix") or []
    component = str(target.get("component") or "THREAD")
    review_key = str(target.get("review_unit_key") or "").strip()

    if review_key:
        _, mapping = _critic_review_units(candidate)
        node_id = str(mapping.get(review_key) or "")
        if review_key == "CENTRAL_PROPOSITION" and node_id:
            return "/result/argument_architecture/central_proposition"
        for question_index, question in enumerate(
            graph.get("research_questions") or []
        ):
            if (
                isinstance(question, dict)
                and str(question.get("node_id") or "") == node_id
            ):
                return (
                    "/result/argument_architecture/research_questions/"
                    f"{question_index}"
                )
        for node_index, node in enumerate(graph.get("nodes") or []):
            if (
                isinstance(node, dict)
                and str(node.get("node_id") or "") == node_id
            ):
                return f"/result/argument_architecture/nodes/{node_index}"

    thread_index = target.get("thread_index")
    item_index = target.get("item_index")
    if component == "CENTRAL_PROPOSITION":
        return "/result/argument_architecture/central_proposition"
    if component == "SCOPE":
        return "/result/argument_architecture/scope_boundaries"
    if not (
        isinstance(thread_index, int)
        and 0 <= thread_index < len(matrix)
    ):
        return "/result"
    if component == "THREAD":
        return f"/result/research_design_matrix/{thread_index}"
    if component == "QUESTION":
        return (
            "/result/argument_architecture/research_questions/"
            f"{thread_index}"
        )
    field = _critic_matrix_field_by_component().get(component)
    if not field:
        return f"/result/research_design_matrix/{thread_index}"
    ids = [str(x) for x in matrix[thread_index].get(field) or []]
    selected_index = item_index if isinstance(item_index, int) else 0
    if not 0 <= selected_index < len(ids):
        return (
            f"/result/research_design_matrix/{thread_index}/{field}"
        )
    selected = ids[selected_index]
    for node_index, node in enumerate(graph.get("nodes") or []):
        if (
            isinstance(node, dict)
            and str(node.get("node_id") or "") == selected
        ):
            return f"/result/argument_architecture/nodes/{node_index}"
    return (
        f"/result/research_design_matrix/{thread_index}/"
        f"{field}/{selected_index}"
    )

def _critic_canonical_questions(questions):
    result=[]
    for i,q in enumerate(questions,1):
        if isinstance(q,dict): result.append({"question_id":f"UQ-ARG-CRITIC-{i:03d}","question_type":str(q["question_type"]),"question":str(q["question"]),"reason":str(q["reason"]),"target_paths":[_question_target_path(str(q.get("target_area") or "OTHER"))],"answer_schema":_answer_schema(q),"blocking":bool(q.get("blocking")),"priority":str(q.get("priority") or "P2")})
    return result



def _critic_deterministic_findings(
    receipts: list[dict[str, Any]],
    *,
    start_index: int = 1,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for receipt in receipts:
        if not isinstance(receipt, dict):
            continue
        findings.append(
            {
                "finding_instance_id": f"F-ARG-DETERMINISTIC-{start_index + len(findings):03d}",
                "defect_namespace": "MACHINE_DEFECT",
                "defect_key": str(receipt.get("defect_key") or ""),
                "code": str(receipt.get("finding_code") or "RESEARCH_DESIGN_INCOMPLETE"),
                "severity": "P1",
                "category": "ARGUMENT",
                "target_type": "ARGUMENT_SEMANTIC_COMPONENT",
                "target_path_or_span": str(receipt.get("owner_target_path") or receipt.get("target_path") or "/result"),
                "semantic_component": str(receipt.get("owner_semantic_component") or receipt.get("semantic_component") or "RESEARCH_DESIGN"),
                "semantic_thread": receipt.get("thread_index")
                if isinstance(receipt.get("thread_index"), int)
                else None,
                "semantic_review_unit_key": str(receipt.get("owner_semantic_review_unit_key") or receipt.get("semantic_review_unit_key") or "") or None,
                "description": str(receipt.get("description") or "确定性语义契约检查未通过。"),
                "evidence_refs": [
                    str(value)
                    for value in receipt.get("evidence_ids") or []
                    if str(value).strip()
                ],
                "repairable": False,
                "repair_instruction": str(receipt.get("repair_instruction") or "返回原 Argument Producer 重新生成。"),
                "suggested_route": str(receipt["suggested_route"]),
                "blocking": bool(receipt.get("blocking", True)),
            }
        )
    return findings


def _critic_unresolved_from_findings(
    findings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    unresolved: list[dict[str, Any]] = []
    for finding in findings:
        if not isinstance(finding, dict) or not bool(finding.get("blocking")):
            continue
        route = str(finding.get("suggested_route") or "").upper()
        if route not in {"USER", "BLOCK"}:
            continue
        unresolved.append(
            {
                "item_id": f"ARG-CRITIC-UNRESOLVED-{len(unresolved) + 1:03d}",
                "type": "MISSING" if route == "USER" else "UNSUPPORTED",
                "description": str(finding.get("description") or "存在阻断性问题。"),
                "target_paths": [str(finding.get("target_path_or_span") or "/result")],
                "required_action": str(finding.get("repair_instruction") or "处理该阻断性问题。"),
                "blocking": True,
            }
        )
    return unresolved


def _critic_final_status_from_canonical_state(
    findings: list[dict[str, Any]],
    questions: list[dict[str, Any]],
) -> tuple[str, str]:
    blocking_routes = {
        str(finding.get("suggested_route") or "").upper()
        for finding in findings
        if isinstance(finding, dict) and bool(finding.get("blocking"))
    }
    if "BLOCK" in blocking_routes:
        return "BLOCK", "BLOCK"
    if "USER" in blocking_routes and any(bool(q.get("blocking")) for q in questions):
        return "NEED_USER_INPUT", "REVISE"
    if findings:
        return "REVISE", "REVISE"
    return "PASS", "ACCEPT"


def _canonical_critic_work_items(
    semantic_observations: list[dict[str, Any]],
    machine_defects: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build canonical work items without cross-namespace identity guessing.

    Semantic observations are run-scoped and keyed only by finding_instance_id.
    Machine defects alone carry stable defect_key identities.  The two namespaces
    always coexist; Runtime never infers that an LLM judgement paraphrases a
    deterministic rule.
    """
    work_items: list[dict[str, Any]] = []
    seen_instances: set[str] = set()
    seen_machine_keys: set[str] = set()

    for finding in semantic_observations:
        if not isinstance(finding, dict):
            continue
        if str(finding.get("defect_namespace") or "") != "SEMANTIC_OBSERVATION":
            raise ValueError("model critic finding must use SEMANTIC_OBSERVATION namespace")
        if finding.get("defect_key") is not None:
            raise ValueError("semantic observation defect_key must be null; finding_instance_id is run-scoped identity")
        instance_id = str(finding.get("finding_instance_id") or "")
        if not instance_id or instance_id in seen_instances:
            raise ValueError(f"duplicate or missing semantic observation finding_instance_id: {instance_id!r}")
        seen_instances.add(instance_id)
        work_items.append(finding)

    for finding in machine_defects:
        if not isinstance(finding, dict):
            continue
        if str(finding.get("defect_namespace") or "") != "MACHINE_DEFECT":
            raise ValueError("deterministic critic finding must use MACHINE_DEFECT namespace")
        key = str(finding.get("defect_key") or "")
        if not key or key in seen_machine_keys:
            raise ValueError(f"duplicate or missing machine defect_key: {key!r}")
        seen_machine_keys.add(key)
        instance_id = str(finding.get("finding_instance_id") or "")
        if not instance_id or instance_id in seen_instances:
            raise ValueError(f"duplicate or missing critic finding_instance_id: {instance_id!r}")
        seen_instances.add(instance_id)
        work_items.append(finding)

    return work_items

def _argument_deterministic_receipts_for_candidate(
    canonical_envelope: dict[str, Any], candidate: dict[str, Any]
) -> list[dict[str, Any]]:
    check_envelope = copy.deepcopy(canonical_envelope)
    payload = check_envelope.setdefault("payload", {})
    payload["architecture_candidate"] = candidate
    _, chain_receipts = _critic_chain_checks(candidate, with_receipts=True)
    _, matrix_receipts = _critic_design_matrix_checks(candidate, with_receipts=True)
    _, structural_receipts = _critic_structural_checks(check_envelope, with_receipts=True)
    _, evidence_receipts = _critic_evidence_checks(check_envelope, with_receipts=True)
    return [*chain_receipts, *matrix_receipts, *structural_receipts, *evidence_receipts]


def _canonical_quality_dimensions(
    model_dimensions: Iterable[Any],
    deterministic_receipts: Iterable[dict[str, Any]],
    semantic_issues: Iterable[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Canonicalize Critic quality dimensions from observations and Runtime facts.

    Model scores/evidence remain advisory.  Pass/fail and required action are
    Runtime-owned and are derived from concrete semantic issues plus deterministic
    receipts.
    """
    result = [copy.deepcopy(item) for item in model_dimensions if isinstance(item, dict)]
    by_name = {str(item.get("dimension") or ""): item for item in result}

    semantic_failures: dict[str, list[dict[str, Any]]] = {}
    for issue in semantic_issues:
        if not isinstance(issue, Mapping):
            continue
        target = issue.get("target") if isinstance(issue.get("target"), Mapping) else {}
        dimension = _critic_dimension_for_issue(
            str(issue.get("code") or ""),
            str(target.get("component") or ""),
        )
        semantic_failures.setdefault(dimension, []).append(dict(issue))

    deterministic_failures: dict[str, list[dict[str, Any]]] = {}
    for receipt in deterministic_receipts:
        if not isinstance(receipt, dict) or not bool(receipt.get("blocking")):
            continue
        dimension = str(receipt.get("quality_dimension") or "")
        if dimension:
            deterministic_failures.setdefault(dimension, []).append(receipt)

    deterministic_failure_score = _critic_deterministic_failure_score()
    semantic_failure_max_score = _critic_semantic_failure_max_score()

    for dimension, item in by_name.items():
        semantic = semantic_failures.get(dimension, [])
        deterministic = deterministic_failures.get(dimension, [])
        failed = bool(semantic or deterministic)
        item["passed"] = not failed

        model_evidence = [
            str(value)
            for value in item.get("evidence") or []
            if str(value).strip()
        ]
        if deterministic:
            item["score"] = deterministic_failure_score
        elif semantic:
            try:
                item["score"] = min(float(item.get("score")), semantic_failure_max_score)
            except (TypeError, ValueError):
                item["score"] = semantic_failure_max_score

        if not failed:
            item["evidence"] = model_evidence[:8]
            item["required_action"] = None
            continue

        semantic_messages = [
            str(issue.get("description") or "")
            for issue in semantic
            if str(issue.get("description") or "").strip()
        ]
        deterministic_messages = [
            str(receipt.get("description") or "")
            for receipt in deterministic
            if str(receipt.get("description") or "").strip()
        ]
        item["evidence"] = list(
            dict.fromkeys([*model_evidence, *semantic_messages, *deterministic_messages])
        )[:8] or ["Runtime canonical quality policy detected an unresolved issue."]

        semantic_actions = [
            str(issue.get("repair_instruction") or "")
            for issue in semantic
            if str(issue.get("repair_instruction") or "").strip()
        ]
        deterministic_actions = [
            str(receipt.get("repair_instruction") or "")
            for receipt in deterministic
            if str(receipt.get("repair_instruction") or "").strip()
        ]
        actions = list(dict.fromkeys([*semantic_actions, *deterministic_actions]))
        item["required_action"] = "；".join(actions[:4]) or "修复该论证质量问题。"

    return result


def expand_argument_architecture_critic_model_output(
    canonical_envelope: dict[str, Any],
    semantic_output: dict[str, Any],
) -> dict[str, Any]:
    payload = canonical_envelope.get("payload") or {}
    raw_candidate = payload.get("architecture_candidate") or {}
    if not isinstance(raw_candidate.get("authored_state"), dict):
        raise ValueError(
            "Argument Architecture critic candidate is missing authoritative authored_state"
        )
    candidate = _canonical_argument_candidate(canonical_envelope, raw_candidate)
    canonical_envelope = copy.deepcopy(canonical_envelope)
    canonical_envelope.setdefault("payload", {})["architecture_candidate"] = candidate
    _, review_mapping = _critic_review_units(candidate)
    reviewed_keys = [
        str(x)
        for x in semantic_output.get("reviewed_unit_keys") or []
        if str(x).strip()
    ]
    checked = list(
        dict.fromkeys(
            review_mapping[key]
            for key in reviewed_keys
            if key in review_mapping
        )
    )

    model_findings: list[dict[str, Any]] = []
    for i, issue in enumerate(semantic_output.get("issues") or [], 1):
        if not isinstance(issue, dict):
            continue
        policy = _critic_semantic_issue_policy(str(issue.get("code") or ""))
        needs_user_input = _critic_issue_needs_user_input(issue)
        requires_structure_change = _critic_issue_requires_structure_change(issue)
        if needs_user_input:
            route, repairable = "USER", False
        elif requires_structure_change:
            route, repairable = "ORIGINAL_PRODUCER", False
        else:
            route = str(policy["route"])
            repairable = bool(policy["repairable"])

        evidence_ids = [
            str(x) for x in issue.get("evidence_ids") or [] if str(x).strip()
        ]
        blocking = bool(policy["blocking"]) or needs_user_input or requires_structure_change
        target = issue.get("target") or {}
        target_path = _critic_target_path(candidate, target)
        semantic_thread = _critic_target_canonical_thread(candidate, target)
        review_key = str(target.get("review_unit_key") or "").strip() or None
        finding_instance_id = f"F-ARG-CRITIC-{i:03d}"
        model_findings.append(
            {
                "finding_instance_id": finding_instance_id,
                "defect_namespace": "SEMANTIC_OBSERVATION",
                # Semantic observations are run-scoped model judgements, not
                # deterministic defects.  Do not manufacture a cross-run identity
                # from natural-language description or issue ordering.
                "defect_key": None,
                "code": str(issue["code"]),
                "severity": str(policy["severity"]),
                "category": "ARGUMENT",
                "target_type": "ARGUMENT_SEMANTIC_COMPONENT",
                "target_path_or_span": target_path,
                "semantic_component": str(target.get("component") or "") or None,
                "semantic_thread": semantic_thread,
                "semantic_review_unit_key": review_key,
                "description": str(issue["description"]),
                "evidence_refs": evidence_ids,
                "repairable": repairable,
                "repair_instruction": str(issue["repair_instruction"]),
                "suggested_route": route,
                "blocking": blocking,
            }
        )

    chain_checks, chain_receipts = _critic_chain_checks(candidate, with_receipts=True)
    matrix_checks, matrix_receipts = _critic_design_matrix_checks(candidate, with_receipts=True)
    structural_checks, structural_receipts = _critic_structural_checks(canonical_envelope, with_receipts=True)
    evidence_checks, evidence_receipts = _critic_evidence_checks(canonical_envelope, with_receipts=True)
    deterministic_receipts = [*chain_receipts, *matrix_receipts, *structural_receipts, *evidence_receipts]
    deterministic_findings = _critic_deterministic_findings(
        deterministic_receipts,
        start_index=len(model_findings) + 1,
    )
    # Machine defects and LLM semantic observations are separate namespaces.
    # They coexist even when code/dimension/target overlap; Runtime must not infer
    # that an independent semantic observation is merely a paraphrase of a rule.
    findings = _canonical_critic_work_items(model_findings, deterministic_findings)

    canonical_routes = {
        str(finding.get("suggested_route") or "").upper()
        for finding in findings
        if isinstance(finding, dict) and bool(finding.get("blocking"))
    }
    questions = (
        _critic_canonical_questions(semantic_output.get("user_questions") or [])
        if "USER" in canonical_routes
        else []
    )
    unresolved = _critic_unresolved_from_findings(findings)
    status, verdict = _critic_final_status_from_canonical_state(findings, questions)

    _, records = _evidence_records(canonical_envelope)
    used_evidence_ids = list(
        dict.fromkeys(
            str(eid)
            for finding in findings
            if isinstance(finding, dict)
            for eid in finding.get("evidence_refs") or []
            if str(eid).strip()
        )
    )
    refs = _refs_for_evidence_ids(used_evidence_ids, records)
    return {
        "schema_version": str(canonical_envelope.get("schema_version") or "2.0"),
        "prompt_id": str(
            canonical_envelope.get("prompt_id")
            or "P-ARGUMENT-ARCHITECTURE-CRITIC"
        ),
        "prompt_version": str(canonical_envelope.get("prompt_version") or "8.0.0"),
        "status": status,
        "result": {
            "verdict": verdict,
            "checked_node_ids": checked,
            "chain_checks": chain_checks,
            "design_matrix_checks": matrix_checks,
            "structural_checks": structural_checks,
            "evidence_checks": evidence_checks,
            "deterministic_receipts": deterministic_receipts,
            "quality_dimensions": _canonical_quality_dimensions(
                semantic_output.get("quality_dimensions") or [],
                deterministic_receipts,
                semantic_output.get("issues") or [],
            ),
        },
        "findings": findings,
        "unresolved_items": unresolved,
        "user_questions": questions,
        "source_refs": refs,
        "warnings": [],
    }


def expand_targeted_repair_model_output(canonical_envelope,semantic_output):
    payload=canonical_envelope.get("payload") or {}; original=copy.deepcopy((payload.get("original_object") or {}).get("content") or {}); requested=[str(x.get("finding_instance_id") or "") for x in payload.get("findings_to_repair") or [] if isinstance(x,dict) and x.get("finding_instance_id")]
    if str(semantic_output.get("decision") or "")=="ESCALATE":
        reason=str(semantic_output.get("escalation_reason") or "局部修复无法在授权范围内闭合。")
        return {"schema_version":str(canonical_envelope.get("schema_version") or "2.0"),"prompt_id":"P-TARGETED-REPAIR","prompt_version":str(canonical_envelope.get("prompt_version") or "8.0.0"),"status":"REVISE",
                "result":{"repaired_object":original,"changed_paths":[],"unchanged_protected_hashes":copy.deepcopy(payload.get("protected_hashes") or []),"resolved_finding_ids":[],"unresolved_finding_ids":requested},
                "findings":[],"unresolved_items":[{"item_id":"TARGETED-REPAIR-ESCALATION","type":"UNSUPPORTED","description":reason,"target_paths":[str(x) for x in payload.get("allowed_paths") or []] or ["/content"],"required_action":"返回原生成阶段重新生成相关语义结构，或补充完成修复所需的信息。","blocking":True}],"user_questions":[],"source_refs":[],"warnings":[]}
    repaired=copy.deepcopy(original)
    for change in semantic_output.get("changes") or []: _set_existing_pointer(repaired,str(change["path"]),change.get("value"))
    diff=[_repair_canonical_path(p) for p in _semantic_diff_paths(original,repaired)]
    return {"schema_version":str(canonical_envelope.get("schema_version") or "2.0"),"prompt_id":"P-TARGETED-REPAIR","prompt_version":str(canonical_envelope.get("prompt_version") or "8.0.0"),"status":"PASS",
            "result":{"repaired_object":repaired,"changed_paths":diff,"unchanged_protected_hashes":copy.deepcopy(payload.get("protected_hashes") or []),"resolved_finding_ids":requested,"unresolved_finding_ids":[]},
            "findings":[],"unresolved_items":[],"user_questions":[],"source_refs":[],"warnings":[]}


def expand_semantic_model_output(prompt_id: str, canonical_envelope: dict[str, Any], semantic_output: dict[str, Any]) -> dict[str, Any]:
    if prompt_id=="P-ARGUMENT-ARCHITECTURE": return expand_argument_architecture_model_output(canonical_envelope,semantic_output)
    if prompt_id=="P-ARGUMENT-ARCHITECTURE-CRITIC": return expand_argument_architecture_critic_model_output(canonical_envelope,semantic_output)
    if prompt_id=="P-TARGETED-REPAIR": return expand_targeted_repair_model_output(canonical_envelope,semantic_output)
    if prompt_id=="P-SAFE-ONLINE-PACKAGE": return expand_safe_online_package_model_output(canonical_envelope,semantic_output)
    if prompt_id=="P-SAFE-ONLINE-PACKAGE-CRITIC": return expand_safe_online_package_critic_model_output(canonical_envelope,semantic_output)
    if prompt_id=="P-PUBLIC-RESEARCH-PLAN": return expand_public_research_plan_model_output(canonical_envelope,semantic_output)
    if prompt_id=="P-PUBLIC-RESEARCH-PLAN-SCOPE-CRITIC": return expand_public_research_plan_scope_critic_model_output(canonical_envelope,semantic_output)
    if prompt_id=="P-PUBLIC-RESEARCH-SYNTHESIS": return expand_public_research_synthesis_model_output(canonical_envelope,semantic_output)
    if prompt_id=="P-PUBLIC-RESEARCH-CRITIC": return expand_public_research_critic_model_output(canonical_envelope,semantic_output)
    if prompt_id=="P-ONLINE-RESULT-IMPORT-CRITIC": return expand_online_result_import_critic_model_output(canonical_envelope,semantic_output)
    raise KeyError(f"No semantic model output expander registered for {prompt_id}")
