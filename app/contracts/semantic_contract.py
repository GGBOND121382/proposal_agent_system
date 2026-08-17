from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

import yaml


_CONTRACT_PATH = Path(__file__).with_name("semantic_contract.yaml")
SEMANTIC_CONTRACT_PROMPT_MARKER = "{{SEMANTIC_CONTRACT_RUNTIME}}"
_SEMANTIC_CONTRACT_PROMPT_HEADING = "# 统一语义契约（运行时生成）"
_RULE_ID_PATTERN = re.compile(r"^SC-[A-Z0-9]+(?:-[A-Z0-9]+)*$")
_REQUIRED_RULE_IDS = frozenset(
    {
        "SC-ARGUMENT-ROLE-COMPATIBILITY",
        "SC-INFORMATION-KEY-HIERARCHY",
        "SC-CLAIM-COVERAGE",
        "SC-EVIDENCE-SELF-REFERENCE",
        "SC-EVIDENCE-CONTRACT-COVERAGE",
        "SC-ARGUMENT-DETERMINISTIC-CHAINS",
        "SC-ARGUMENT-DESIGN-MATRIX-COMPLETENESS",
        "SC-ARGUMENT-EVIDENCE-REQUIREMENTS",
        "SC-ARGUMENT-STRUCTURAL-REQUIREMENTS",
        "SC-ARGUMENT-CRITIC-ISSUE-TAXONOMY",
        "SC-ARGUMENT-DETERMINISTIC-DEFECTS",
        "SC-ARGUMENT-STATE-OWNERSHIP",
        "SC-ARGUMENT-LIFECYCLE-COMPOSITION",
        "SC-ARGUMENT-TARGETED-REPAIR-POLICY",
        "SC-REFERENCE-FIELD-SEMANTICS",
    }
)
_LEGACY_TOP_LEVEL_RULE_BLOCKS = frozenset(
    {"argument_roles", "information_keys", "claim_coverage", "reference_fields"}
)


class ReferenceSemantic(str, Enum):
    """Meaning of a schema field containing identifiers or path-like values."""

    ENTITY_REF = "ENTITY_REF"
    SOURCE_REF = "SOURCE_REF"
    FINDING_REF = "FINDING_REF"
    ENTITY_OR_FIELD_PATH = "ENTITY_OR_FIELD_PATH"
    JSON_POINTER = "JSON_POINTER"
    OBJECT_REF = "OBJECT_REF"
    UNRESOLVED_DESCRIPTOR = "UNRESOLVED_DESCRIPTOR"
    HUMAN_TEXT = "HUMAN_TEXT"
    PROTOCOL_REF = "PROTOCOL_REF"
    NEW_ENTITY_ID = "NEW_ENTITY_ID"
    LOCAL_STRUCTURED_REF = "LOCAL_STRUCTURED_REF"



_REFERENCE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_JSON_POINTER_PATTERN = r"^/(?:[^~/]|~[01])*(?:/(?:[^~/]|~[01])*)*$"
_REFERENCE_TEXT_SEMANTICS = frozenset(
    {ReferenceSemantic.UNRESOLVED_DESCRIPTOR, ReferenceSemantic.HUMAN_TEXT}
)
_REFERENCE_TARGET_SEMANTICS = frozenset(
    {
        ReferenceSemantic.ENTITY_REF,
        ReferenceSemantic.SOURCE_REF,
        ReferenceSemantic.FINDING_REF,
        ReferenceSemantic.ENTITY_OR_FIELD_PATH,
    }
)

class RuleResponsibility(str, Enum):
    """Runtime component that owns a semantic rule's authoritative decision."""

    DETERMINISTIC_GUARD = "DETERMINISTIC_GUARD"
    OUTPUT_INTEGRITY = "OUTPUT_INTEGRITY"


@dataclass(frozen=True)
class SemanticRule:
    """Immutable registry entry for one machine-enforceable semantic rule."""

    rule_id: str
    responsibility: RuleResponsibility
    category: str
    blocking: bool
    description: str
    config: Mapping[str, Any]


@dataclass(frozen=True)
class SemanticContract:
    version: str
    rule_registry_version: str
    rules: Mapping[str, SemanticRule]
    canonical_roles: frozenset[str]
    role_aliases: Mapping[str, str]
    role_compatibility: Mapping[str, frozenset[str]]
    information_key_separator: str
    information_key_allow_exact: bool
    information_key_allow_children: bool
    information_key_allow_legacy_dash: bool
    claim_coverage_fields: tuple[str, ...]
    primary_claim_required_roles: frozenset[str]
    evidence_fields: tuple[str, ...]
    forbid_self_evidence: bool
    required_evidence_contract_field: str
    reference_field_semantics: Mapping[str, ReferenceSemantic]
    reference_path_semantics: Mapping[str, tuple[tuple[tuple[str, ...], ReferenceSemantic], ...]]
    input_object_reference_fields: frozenset[str]
    reference_suffix_aliases: Mapping[str, tuple[str, ...]]

    @property
    def rule_ids(self) -> frozenset[str]:
        return frozenset(self.rules)

    def rule(self, rule_id: str) -> SemanticRule:
        try:
            return self.rules[str(rule_id)]
        except KeyError as exc:
            raise KeyError(f"unknown semantic rule: {rule_id}") from exc

    def rules_for(
        self,
        responsibility: RuleResponsibility | str | None = None,
        *,
        category: str | None = None,
    ) -> tuple[SemanticRule, ...]:
        owner = RuleResponsibility(responsibility) if responsibility is not None else None
        normalized_category = str(category).strip().upper() if category is not None else None
        return tuple(
            rule
            for rule in self.rules.values()
            if (owner is None or rule.responsibility is owner)
            and (normalized_category is None or rule.category == normalized_category)
        )

    def canonical_role(self, value: Any) -> str:
        role = str(value or "").strip().upper()
        return str(self.role_aliases.get(role, role))

    def acceptable_roles(self, required_role: Any) -> frozenset[str]:
        canonical = self.canonical_role(required_role)
        configured = self.role_compatibility.get(canonical)
        return configured or frozenset({canonical})

    def missing_required_roles(
        self,
        required_values: Iterable[Any],
        actual_values: Iterable[Any],
    ) -> list[str]:
        actual = {self.canonical_role(value) for value in actual_values if value}
        missing: list[str] = []
        for value in required_values:
            canonical = self.canonical_role(value)
            if not (self.acceptable_roles(canonical) & actual):
                missing.append(canonical)
        return sorted(set(missing))

    def information_key_belongs(self, key: Any, roots: Iterable[Any]) -> bool:
        candidate = str(key or "").strip()
        if not candidate:
            return False
        for raw_root in roots:
            root = str(raw_root or "").strip()
            if not root:
                continue
            if self.information_key_allow_exact and candidate == root:
                return True
            if self.information_key_allow_children and candidate.startswith(
                root + self.information_key_separator
            ):
                return True
            if self.information_key_allow_legacy_dash and candidate.startswith(root + "-"):
                return True
        return False

    @staticmethod
    def _ids(value: Any) -> set[str]:
        if value is None:
            return set()
        if isinstance(value, str):
            return {value} if value else set()
        if isinstance(value, (list, tuple, set, frozenset)):
            return {str(item) for item in value if item}
        return set()

    def claim_coverage_ids(self, paragraph: Mapping[str, Any]) -> set[str]:
        covered: set[str] = set()
        for field in self.claim_coverage_fields:
            covered.update(self._ids(paragraph.get(field)))
        return covered

    def evidence_ids(self, paragraph: Mapping[str, Any]) -> set[str]:
        evidence: set[str] = set()
        for field in self.evidence_fields:
            evidence.update(self.evidence_ids_for_field(paragraph, field))
        return evidence

    def evidence_ids_for_field(
        self,
        paragraph: Mapping[str, Any],
        field: str,
    ) -> set[str]:
        """Return evidence IDs from one contract-registered evidence field."""
        if str(field) not in self.evidence_fields:
            return set()
        return self._ids(paragraph.get(field))

    def required_evidence_ids(self, section_contract: Mapping[str, Any]) -> set[str]:
        return self._ids(section_contract.get(self.required_evidence_contract_field))

    def covered_evidence_ids(self, paragraphs: Iterable[Mapping[str, Any]]) -> set[str]:
        covered: set[str] = set()
        for paragraph in paragraphs:
            covered.update(self.evidence_ids(paragraph))
        return covered

    def missing_required_evidence_ids(
        self,
        section_contract: Mapping[str, Any],
        paragraphs: Iterable[Mapping[str, Any]],
    ) -> set[str]:
        return self.required_evidence_ids(section_contract) - self.covered_evidence_ids(paragraphs)

    def self_evidence_ids(self, paragraph: Mapping[str, Any]) -> set[str]:
        if not self.forbid_self_evidence:
            return set()
        primary = str(paragraph.get("primary_claim_id") or "")
        return ({primary} & self.evidence_ids(paragraph)) if primary else set()

    @staticmethod
    def _path_matches(path: Iterable[Any], pattern: tuple[str, ...]) -> bool:
        normalized = tuple("*" if isinstance(part, int) else str(part) for part in path)
        return len(normalized) == len(pattern) and all(
            expected == "*" or actual == expected
            for actual, expected in zip(normalized, pattern)
        )

    def field_semantic(
        self,
        field_name: str,
        *,
        prompt_id: str | None = None,
        path: Iterable[Any] | None = None,
    ) -> ReferenceSemantic | None:
        if path is not None:
            for scope in (str(prompt_id or ""), "*"):
                if not scope:
                    continue
                for pattern, semantic in self.reference_path_semantics.get(scope, ()):
                    if self._path_matches(path, pattern):
                        return semantic
        return self.reference_field_semantics.get(str(field_name))

    def reference_annotation(
        self,
        field_name: str,
        *,
        prompt_id: str | None = None,
        path: Iterable[Any] | None = None,
    ) -> Mapping[str, str]:
        semantic = self.field_semantic(field_name, prompt_id=prompt_id, path=path)
        return MappingProxyType(
            {"x-reference-semantic": semantic.value} if semantic is not None else {}
        )

    def requires_existing_target(self, field_name: str) -> bool:
        return self.field_semantic(field_name) in _REFERENCE_TARGET_SEMANTICS

    def allows_entity_field_path(self, field_name: str) -> bool:
        return self.field_semantic(field_name) is ReferenceSemantic.ENTITY_OR_FIELD_PATH

    def allows_input_object(self, field_name: str) -> bool:
        return str(field_name) in self.input_object_reference_fields

    def registered_reference_suffixes(self, field_name: str) -> tuple[str, ...]:
        return self.reference_suffix_aliases.get(str(field_name), ())

    def is_diagnostic_field(self, field_name: str) -> bool:
        return self.field_semantic(field_name) in {
            ReferenceSemantic.UNRESOLVED_DESCRIPTOR,
            ReferenceSemantic.HUMAN_TEXT,
        }

    def unregistered_reference_fields(self, field_names: Iterable[str]) -> list[str]:
        return sorted({str(name) for name in field_names if self.field_semantic(str(name)) is None})

    def canonical_document(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "rule_registry_version": self.rule_registry_version,
            "rules": {
                rule_id: {
                    "responsibility": rule.responsibility.value,
                    "category": rule.category,
                    "blocking": rule.blocking,
                    "description": rule.description,
                    "config": _deep_thaw(rule.config),
                }
                for rule_id, rule in sorted(self.rules.items())
            },
        }

    @property
    def contract_hash(self) -> str:
        canonical = json.dumps(
            self.canonical_document(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def prompt_identity(self) -> str:
        return (
            f"Semantic-Contract-Version: {self.version}\n"
            f"Semantic-Rule-Registry-Version: {self.rule_registry_version}\n"
            f"Semantic-Contract-SHA256: {self.contract_hash}"
        )

    def prompt_summary(self) -> str:
        compatibility = "; ".join(
            f"{role} accepts {', '.join(sorted(values))}"
            for role, values in sorted(self.role_compatibility.items())
        )
        rule_lines = "\n".join(
            f"- `{rule.rule_id}` | owner=`{rule.responsibility.value}` | "
            f"blocking={str(rule.blocking).lower()} | {rule.description}"
            for rule in self.rules.values()
        )
        coverage = ", ".join(self.claim_coverage_fields)
        evidence = ", ".join(self.evidence_fields)
        entity_field_path_fields = ", ".join(
            sorted(
                field_name
                for field_name, semantic in self.reference_field_semantics.items()
                if semantic is ReferenceSemantic.ENTITY_OR_FIELD_PATH
            )
        )
        return (
            f"{self.prompt_identity()}\n\n"
            "## 已登记的机器规则\n"
            f"{rule_lines}\n\n"
            "## 统一解释\n"
            f"Argument-role compatibility: {compatibility}.\n"
            f"A novel_content_key belongs to a contract only when it is exactly a declared root "
            f"or a child separated by '{self.information_key_separator}'. Legacy dash children "
            f"are {'allowed' if self.information_key_allow_legacy_dash else 'not allowed'}.\n"
            f"Required-claim coverage is the union of: {coverage}. primary_claim_id is singular "
            "but not every covered claim must be a primary claim unless its role requires it.\n"
            f"Evidence fields are: {evidence}. A claim must never cite itself as evidence.\n"
            "Reference fields are governed by the registered field semantics used by schema and "
            "runtime validation; diagnostic descriptors and human text are not entity references.\n"
            f"ENTITY_OR_FIELD_PATH fields are: {entity_field_path_fields}. Every value in these fields "
            "must be either (1) an exact entity ID visible in the input/current output, (2) an exact "
            "registered top-level input-object name, or (3) a field path anchored by such an entity ID, "
            "using '<entity_id>.<field>' or '<entity_id>:<field>'. Never emit an unanchored property "
            "name such as 'must_answer', 'source_refs', or 'required_evidence_ids'. For example, cite "
            "'P-ABS-005' or 'P-ABS-005.must_answer', never 'must_answer' alone. If field-level precision "
            "is unnecessary, cite only the owning entity ID.\n"
            "The deterministic owner of each registered rule is authoritative for that rule. "
            "Producer, Critic, and Repair must not create a competing local interpretation."
        )

    def render_prompt_contract(self) -> str:
        return f"{_SEMANTIC_CONTRACT_PROMPT_HEADING}\n\n{self.prompt_summary()}"

    def inject_into_prompt(self, base_prompt: str) -> str:
        marker_count = base_prompt.count(SEMANTIC_CONTRACT_PROMPT_MARKER)
        if marker_count > 1:
            raise ValueError(
                "shared prompt contains more than one semantic contract injection marker"
            )
        rendered = self.render_prompt_contract()
        if marker_count == 1:
            injected = base_prompt.replace(SEMANTIC_CONTRACT_PROMPT_MARKER, rendered, 1)
        else:
            injected = base_prompt.rstrip() + "\n\n" + rendered
        if injected.count(_SEMANTIC_CONTRACT_PROMPT_HEADING) != 1:
            raise ValueError("semantic contract must be injected exactly once")
        return injected


def _deep_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_deep_thaw(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_deep_thaw(item) for item in value)
    if isinstance(value, Enum):
        return value.value
    return value


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_deep_freeze(item) for item in value)
    return value


def _unique_strings(values: Iterable[Any], *, location: str, allow_empty: bool = False) -> tuple[str, ...]:
    normalized = tuple(str(value).strip() for value in values)
    if not allow_empty and any(not value for value in normalized):
        raise ValueError(f"{location} contains an empty value")
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{location} contains duplicate values")
    return normalized


def _load_rule_registry(raw: Mapping[str, Any]) -> Mapping[str, SemanticRule]:
    legacy = sorted(_LEGACY_TOP_LEVEL_RULE_BLOCKS & set(raw))
    if legacy:
        raise ValueError(
            "semantic contract duplicates rule configuration outside the registry: "
            + ", ".join(legacy)
        )

    registry = raw.get("rules")
    if not isinstance(registry, Mapping) or not registry:
        raise ValueError("semantic contract rules must be a non-empty mapping")

    rules: dict[str, SemanticRule] = {}
    for raw_rule_id, raw_entry in registry.items():
        rule_id = str(raw_rule_id).strip().upper()
        if not _RULE_ID_PATTERN.fullmatch(rule_id):
            raise ValueError(f"invalid semantic rule_id: {raw_rule_id!r}")
        if rule_id in rules:
            raise ValueError(f"duplicate semantic rule_id: {rule_id}")
        if not isinstance(raw_entry, Mapping):
            raise ValueError(f"semantic rule {rule_id} must be an object")
        try:
            responsibility = RuleResponsibility(str(raw_entry.get("responsibility") or "").upper())
        except ValueError as exc:
            raise ValueError(
                f"semantic rule {rule_id} has an unknown responsibility: "
                f"{raw_entry.get('responsibility')!r}"
            ) from exc
        category = str(raw_entry.get("category") or "").strip().upper()
        description = str(raw_entry.get("description") or "").strip()
        config = raw_entry.get("config")
        if not category:
            raise ValueError(f"semantic rule {rule_id} is missing category")
        if not description:
            raise ValueError(f"semantic rule {rule_id} is missing description")
        if not isinstance(config, Mapping):
            raise ValueError(f"semantic rule {rule_id} config must be an object")
        rules[rule_id] = SemanticRule(
            rule_id=rule_id,
            responsibility=responsibility,
            category=category,
            blocking=bool(raw_entry.get("blocking", True)),
            description=description,
            config=_deep_freeze(config),
        )

    missing = sorted(_REQUIRED_RULE_IDS - set(rules))
    if missing:
        raise ValueError("semantic contract is missing required rules: " + ", ".join(missing))
    _validate_argument_meta_rules(rules)
    return MappingProxyType(rules)



def _validate_argument_meta_rules(rules: Mapping[str, SemanticRule]) -> None:
    """Validate the executable Argument meta-contract before runtime use."""

    evidence_rule = rules["SC-ARGUMENT-EVIDENCE-REQUIREMENTS"]
    evidence_config = evidence_rule.config
    supported_statuses = tuple(
        str(value).strip()
        for value in evidence_config.get("supported_knowledge_statuses") or ()
        if str(value).strip()
    )
    if not supported_statuses or len(set(supported_statuses)) != len(supported_statuses):
        raise ValueError(
            "SC-ARGUMENT-EVIDENCE-REQUIREMENTS supported_knowledge_statuses "
            "must be non-empty and unique"
        )

    source_policies = evidence_config.get("source_policies") or {}
    if not isinstance(source_policies, Mapping) or not source_policies:
        raise ValueError(
            "SC-ARGUMENT-EVIDENCE-REQUIREMENTS source_policies must be a non-empty mapping"
        )
    for policy_id, policy in source_policies.items():
        if not str(policy_id).strip() or not isinstance(policy, Mapping):
            raise ValueError("argument evidence source policy entries must be named objects")
        if "require_source_ref" in policy and not isinstance(policy.get("require_source_ref"), bool):
            raise ValueError(
                f"argument evidence source policy {policy_id!r} require_source_ref must be boolean"
            )
        if "require_quoted_text" in policy and not isinstance(policy.get("require_quoted_text"), bool):
            raise ValueError(
                f"argument evidence source policy {policy_id!r} require_quoted_text must be boolean"
            )
        allowed_types = tuple(
            str(value).strip()
            for value in policy.get("allowed_source_types") or ()
            if str(value).strip()
        )
        if len(set(allowed_types)) != len(allowed_types):
            raise ValueError(
                f"argument evidence source policy {policy_id!r} allowed_source_types must be unique"
            )

    requirements = evidence_config.get("requirements") or ()
    if not isinstance(requirements, (tuple, list)) or not requirements:
        raise ValueError(
            "SC-ARGUMENT-EVIDENCE-REQUIREMENTS requirements must be non-empty"
        )
    allowed_subjects = {"OBJECT", "COLLECTION"}
    allowed_presence = {"REQUIRED", "IF_PRESENT"}
    allowed_coverage = {"ALL", "ANY"}

    taxonomy_config = rules["SC-ARGUMENT-CRITIC-ISSUE-TAXONOMY"].config
    taxonomy_dimensions = taxonomy_config.get("dimensions") or {}
    if not isinstance(taxonomy_dimensions, Mapping) or not taxonomy_dimensions:
        raise ValueError(
            "SC-ARGUMENT-CRITIC-ISSUE-TAXONOMY dimensions must be a non-empty mapping"
        )
    dimension_issue_codes: dict[str, set[str]] = {}
    all_issue_codes: set[str] = set()
    for dimension, config in taxonomy_dimensions.items():
        dimension_name = str(dimension).strip()
        if not dimension_name or not isinstance(config, Mapping):
            raise ValueError("argument critic taxonomy dimension entries must be named objects")
        codes = tuple(
            str(value).strip()
            for value in config.get("issue_codes") or ()
            if str(value).strip()
        )
        if not codes or len(set(codes)) != len(codes):
            raise ValueError(
                f"argument critic taxonomy dimension {dimension_name!r} issue_codes must be non-empty and unique"
            )
        dimension_issue_codes[dimension_name] = set(codes)
        all_issue_codes.update(codes)
    revision_component_by_code = taxonomy_config.get("revision_component_by_code") or {}
    if not isinstance(revision_component_by_code, Mapping):
        raise ValueError(
            "SC-ARGUMENT-CRITIC-ISSUE-TAXONOMY revision_component_by_code must be a mapping"
        )
    if {str(code) for code in revision_component_by_code} != all_issue_codes:
        raise ValueError(
            "SC-ARGUMENT-CRITIC-ISSUE-TAXONOMY revision_component_by_code must cover every issue code exactly"
        )
    if any(not str(component).strip() for component in revision_component_by_code.values()):
        raise ValueError(
            "SC-ARGUMENT-CRITIC-ISSUE-TAXONOMY revision components must be non-empty"
        )

    component_by_node_type = taxonomy_config.get("component_by_node_type") or {}
    if not isinstance(component_by_node_type, Mapping) or not component_by_node_type:
        raise ValueError(
            "SC-ARGUMENT-CRITIC-ISSUE-TAXONOMY component_by_node_type must be a non-empty mapping"
        )
    semantic_components = {
        str(value).strip()
        for value in component_by_node_type.values()
        if str(value).strip()
    } | {"SCOPE", "THREAD", "RESEARCH_DESIGN"}
    if len(semantic_components) < 3:
        raise ValueError("argument critic semantic component registry is empty")
    precise_components = tuple(
        str(value).strip()
        for value in taxonomy_config.get("precise_target_components") or ()
        if str(value).strip()
    )
    if not precise_components or len(set(precise_components)) != len(precise_components):
        raise ValueError(
            "SC-ARGUMENT-CRITIC-ISSUE-TAXONOMY precise_target_components must be non-empty and unique"
        )
    unknown_precise = sorted(set(precise_components) - semantic_components)
    if unknown_precise:
        raise ValueError(
            "SC-ARGUMENT-CRITIC-ISSUE-TAXONOMY precise target components are unknown: "
            + ", ".join(unknown_precise)
        )
    allowed_targets = taxonomy_config.get("allowed_target_components_by_code") or {}
    if not isinstance(allowed_targets, Mapping) or {str(code) for code in allowed_targets} != all_issue_codes:
        raise ValueError(
            "SC-ARGUMENT-CRITIC-ISSUE-TAXONOMY allowed_target_components_by_code must cover every issue code exactly"
        )
    for issue_code, components in allowed_targets.items():
        values = tuple(
            str(value).strip() for value in components or () if str(value).strip()
        )
        if not values or len(set(values)) != len(values):
            raise ValueError(
                f"argument critic issue {issue_code!r} must define non-empty unique target components"
            )
        unknown = sorted(set(values) - semantic_components)
        if unknown:
            raise ValueError(
                f"argument critic issue {issue_code!r} references unknown target components: "
                + ", ".join(unknown)
            )
    matrix_fields = taxonomy_config.get("matrix_field_by_component") or {}
    if not isinstance(matrix_fields, Mapping) or not matrix_fields:
        raise ValueError(
            "SC-ARGUMENT-CRITIC-ISSUE-TAXONOMY matrix_field_by_component must be a non-empty mapping"
        )
    reference_type_members = taxonomy_config.get("reference_type_members") or {}
    if not isinstance(reference_type_members, Mapping):
        raise ValueError(
            "SC-ARGUMENT-CRITIC-ISSUE-TAXONOMY reference_type_members must be a mapping"
        )
    known_node_types = {str(value).strip() for value in component_by_node_type if str(value).strip()}
    for reference_type, members in reference_type_members.items():
        values = tuple(str(value).strip() for value in members or () if str(value).strip())
        if not str(reference_type).strip() or not values or len(values) != len(set(values)):
            raise ValueError("argument critic reference_type_members entries must be named, non-empty and unique")
        unknown_members = sorted(set(values) - known_node_types)
        if unknown_members:
            raise ValueError(
                f"argument critic reference type {reference_type!r} has unknown members: "
                + ", ".join(unknown_members)
            )

    failure_score = taxonomy_config.get("deterministic_failure_score")
    if not isinstance(failure_score, int) or not 1 <= failure_score <= 5:
        raise ValueError(
            "SC-ARGUMENT-CRITIC-ISSUE-TAXONOMY deterministic_failure_score must be an integer in [1,5]"
        )

    defect_rule = rules["SC-ARGUMENT-DETERMINISTIC-DEFECTS"]
    defect_config = defect_rule.config
    identity_recipe = str(defect_config.get("identity_recipe") or "").upper()
    if identity_recipe != "RULE_FAMILY_THREAD_OWNER_OBJECT":
        raise ValueError(
            "SC-ARGUMENT-DETERMINISTIC-DEFECTS identity_recipe must be RULE_FAMILY_THREAD_OWNER_OBJECT"
        )
    families = defect_config.get("families") or {}
    if not isinstance(families, Mapping) or not families:
        raise ValueError(
            "SC-ARGUMENT-DETERMINISTIC-DEFECTS families must be a non-empty mapping"
        )
    for field_name in (
        "critic_result_required_fields",
        "critic_finding_required_fields",
        "receipt_required_fields",
    ):
        values = tuple(
            str(value).strip()
            for value in defect_config.get(field_name) or ()
            if str(value).strip()
        )
        if not values or len(set(values)) != len(values):
            raise ValueError(
                f"SC-ARGUMENT-DETERMINISTIC-DEFECTS {field_name} must be non-empty and unique"
            )

    allowed_routes = {"ORIGINAL_PRODUCER", "ARGUMENT_ARCHITECTURE_AGENT", "USER", "BLOCK"}
    for family_id, family in families.items():
        if not isinstance(family, Mapping):
            raise ValueError(f"deterministic defect family {family_id!r} must be an object")
        failure_code = str(family.get("failure_code") or "").strip()
        route = str(family.get("route") or "").strip().upper()
        finding_code = str(family.get("finding_code") or "").strip()
        finding_code_source = str(family.get("finding_code_source") or "").strip().upper()
        if not failure_code:
            raise ValueError(f"deterministic defect family {family_id!r} is missing failure_code")
        if route not in allowed_routes:
            raise ValueError(
                f"deterministic defect family {family_id!r} has unknown route {route!r}"
            )
        ownership_scope = str(family.get("ownership_scope") or "").strip().upper()
        if ownership_scope not in {"THREAD_OR_OBJECT", "OWNER_OBJECT"}:
            raise ValueError(
                f"deterministic defect family {family_id!r} has unknown ownership_scope {ownership_scope!r}"
            )
        if bool(finding_code) == bool(finding_code_source):
            raise ValueError(
                f"deterministic defect family {family_id!r} must configure exactly one of "
                "finding_code or finding_code_source"
            )
        if finding_code_source and finding_code_source != "REQUIREMENT":
            raise ValueError(
                f"deterministic defect family {family_id!r} has unsupported "
                f"finding_code_source {finding_code_source!r}"
            )
        quality_dimension = str(family.get("quality_dimension") or "").strip()
        quality_dimension_source = str(family.get("quality_dimension_source") or "").strip().upper()
        if bool(quality_dimension) == bool(quality_dimension_source):
            raise ValueError(
                f"deterministic defect family {family_id!r} must configure exactly one of "
                "quality_dimension or quality_dimension_source"
            )
        if quality_dimension and quality_dimension not in dimension_issue_codes:
            raise ValueError(
                f"deterministic defect family {family_id!r} references unknown quality_dimension {quality_dimension!r}"
            )
        if quality_dimension_source and quality_dimension_source != "REQUIREMENT":
            raise ValueError(
                f"deterministic defect family {family_id!r} has unsupported quality_dimension_source "
                f"{quality_dimension_source!r}"
            )
        if "model_issue_owners" in family:
            raise ValueError(
                f"deterministic defect family {family_id!r} must not own model semantic observations"
            )

    seen_requirement_ids: set[str] = set()
    seen_status_node_types: set[str] = set()
    for requirement in requirements:
        if not isinstance(requirement, Mapping):
            raise ValueError("argument evidence requirement entries must be objects")
        requirement_id = str(requirement.get("requirement_id") or "").strip()
        if not requirement_id or requirement_id in seen_requirement_ids:
            raise ValueError(
                f"argument evidence requirement_id must be unique and non-empty: {requirement_id!r}"
            )
        seen_requirement_ids.add(requirement_id)
        status_node_type = str(requirement.get("status_node_type") or "").strip()
        if not status_node_type or status_node_type not in known_node_types:
            raise ValueError(
                f"argument evidence requirement {requirement_id} has unknown or missing status_node_type {status_node_type!r}"
            )
        if status_node_type in seen_status_node_types:
            raise ValueError(
                f"argument evidence status_node_type must be unique: {status_node_type!r}"
            )
        seen_status_node_types.add(status_node_type)
        selector = str(requirement.get("selector") or "").strip()
        if not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*(?:/(?:\*|[A-Za-z_][A-Za-z0-9_]*))*",
            selector,
        ):
            raise ValueError(
                f"argument evidence requirement {requirement_id} has invalid selector {selector!r}"
            )
        owner_selector = str(requirement.get("owner_selector") or "").strip()
        if owner_selector:
            if not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*(?:/(?:\*|[A-Za-z_][A-Za-z0-9_]*))*",
                owner_selector,
            ):
                raise ValueError(
                    f"argument evidence requirement {requirement_id} has invalid owner_selector {owner_selector!r}"
                )
            if owner_selector.count("*") != selector.count("*"):
                raise ValueError(
                    f"argument evidence requirement {requirement_id} owner_selector must use the same wildcard captures as selector"
                )
        subject = str(requirement.get("subject") or "").upper()
        presence = str(requirement.get("presence") or "").upper()
        coverage = str(requirement.get("coverage") or "").upper()
        source_policy = str(requirement.get("source_policy") or "")
        defect_family = str(requirement.get("deterministic_defect_family") or "")
        for required_field in (
            "finding_code",
            "semantic_component",
            "quality_dimension",
            "reason",
            "suggested_question",
        ):
            if not str(requirement.get(required_field) or "").strip():
                raise ValueError(
                    f"argument evidence requirement {requirement_id} is missing {required_field}"
                )
        if subject not in allowed_subjects:
            raise ValueError(
                f"argument evidence requirement {requirement_id} has unknown subject {subject!r}"
            )
        if presence not in allowed_presence:
            raise ValueError(
                f"argument evidence requirement {requirement_id} has unknown presence {presence!r}"
            )
        if coverage not in allowed_coverage:
            raise ValueError(
                f"argument evidence requirement {requirement_id} has unknown coverage {coverage!r}"
            )
        if subject == "OBJECT" and coverage != "ALL":
            raise ValueError(
                f"argument evidence requirement {requirement_id} OBJECT coverage must be ALL"
            )
        if source_policy not in source_policies:
            raise ValueError(
                f"argument evidence requirement {requirement_id} references unknown "
                f"source_policy {source_policy!r}"
            )
        if defect_family not in families:
            raise ValueError(
                f"argument evidence requirement {requirement_id} references unknown "
                f"deterministic_defect_family {defect_family!r}"
            )
        family = families[defect_family]
        if str(family.get("finding_code_source") or "").upper() == "REQUIREMENT":
            if not str(requirement.get("finding_code") or "").strip():
                raise ValueError(
                    f"argument evidence requirement {requirement_id} must provide finding_code"
                )

        if str(family.get("quality_dimension_source") or "").upper() == "REQUIREMENT":
            quality_dimension = str(requirement.get("quality_dimension") or "").strip()
            if quality_dimension not in dimension_issue_codes:
                raise ValueError(
                    f"argument evidence requirement {requirement_id} references unknown quality_dimension {quality_dimension!r}"
                )

    structural_config = rules["SC-ARGUMENT-STRUCTURAL-REQUIREMENTS"].config
    structural_requirements = structural_config.get("requirements") or ()
    if not isinstance(structural_requirements, (tuple, list)) or not structural_requirements:
        raise ValueError(
            "SC-ARGUMENT-STRUCTURAL-REQUIREMENTS requirements must be non-empty"
        )
    seen_structural_ids: set[str] = set()
    for requirement in structural_requirements:
        if not isinstance(requirement, Mapping):
            raise ValueError("argument structural requirement entries must be objects")
        requirement_id = str(requirement.get("requirement_id") or "").strip()
        if not requirement_id or requirement_id in seen_structural_ids:
            raise ValueError(
                f"argument structural requirement_id must be unique and non-empty: {requirement_id!r}"
            )
        seen_structural_ids.add(requirement_id)
        selector = str(requirement.get("selector") or "").strip()
        owner_selector = str(requirement.get("owner_selector") or "").strip()
        selector_pattern = r"[A-Za-z_][A-Za-z0-9_]*(?:/(?:\*|[A-Za-z_][A-Za-z0-9_]*))*"
        if not re.fullmatch(selector_pattern, selector):
            raise ValueError(
                f"argument structural requirement {requirement_id} has invalid selector {selector!r}"
            )
        if not owner_selector or not re.fullmatch(selector_pattern, owner_selector):
            raise ValueError(
                f"argument structural requirement {requirement_id} has invalid owner_selector {owner_selector!r}"
            )
        if owner_selector.count("*") != selector.count("*"):
            raise ValueError(
                f"argument structural requirement {requirement_id} owner_selector must use the same wildcard captures as selector"
            )
        presence = str(requirement.get("presence") or "").upper()
        if presence not in allowed_presence:
            raise ValueError(
                f"argument structural requirement {requirement_id} has unknown presence {presence!r}"
            )
        defect_family = str(requirement.get("deterministic_defect_family") or "")
        if defect_family not in families:
            raise ValueError(
                f"argument structural requirement {requirement_id} references unknown deterministic_defect_family {defect_family!r}"
            )
        for required_field in (
            "finding_code",
            "semantic_component",
            "quality_dimension",
            "required_node_type",
            "reason",
            "repair_instruction",
        ):
            if not str(requirement.get(required_field) or "").strip():
                raise ValueError(
                    f"argument structural requirement {requirement_id} is missing {required_field}"
                )
        family = families[defect_family]
        if str(family.get("finding_code_source") or "").upper() != "REQUIREMENT":
            raise ValueError(
                f"argument structural requirement {requirement_id} defect family must source finding_code from REQUIREMENT"
            )
        if str(family.get("quality_dimension_source") or "").upper() != "REQUIREMENT":
            raise ValueError(
                f"argument structural requirement {requirement_id} defect family must source quality_dimension from REQUIREMENT"
            )
        finding_code = str(requirement.get("finding_code") or "")
        quality_dimension = str(requirement.get("quality_dimension") or "")
        if finding_code not in dimension_issue_codes.get(quality_dimension, set()):
            raise ValueError(
                f"argument structural requirement {requirement_id} has incompatible quality_dimension/finding_code"
            )

    graph_ownership = structural_config.get("graph_ownership") or {}
    if not isinstance(graph_ownership, Mapping):
        raise ValueError("SC-ARGUMENT-STRUCTURAL-REQUIREMENTS graph_ownership must be an object")
    graph_defect_family = str(graph_ownership.get("deterministic_defect_family") or "")
    graph_finding_code = str(graph_ownership.get("finding_code") or "")
    graph_quality_dimension = str(graph_ownership.get("quality_dimension") or "")
    if graph_defect_family not in families:
        raise ValueError("argument graph_ownership references unknown deterministic defect family")
    graph_family = families[graph_defect_family]
    if str(graph_family.get("finding_code") or "") != graph_finding_code:
        raise ValueError("argument graph_ownership finding_code must match its deterministic family")
    if str(graph_family.get("quality_dimension") or "") != graph_quality_dimension:
        raise ValueError("argument graph_ownership quality_dimension must match its deterministic family")
    if graph_finding_code not in dimension_issue_codes.get(graph_quality_dimension, set()):
        raise ValueError("argument graph_ownership finding_code/quality_dimension are incompatible")
    for field_name in ("reason", "repair_instruction"):
        if not str(graph_ownership.get(field_name) or "").strip():
            raise ValueError(f"argument graph_ownership is missing {field_name}")
    ownership_node_types = graph_ownership.get("node_types") or {}
    if not isinstance(ownership_node_types, Mapping) or not ownership_node_types:
        raise ValueError("argument graph_ownership node_types must be a non-empty mapping")
    for node_type, policy in ownership_node_types.items():
        node_type_name = str(node_type).strip()
        if node_type_name not in component_by_node_type:
            raise ValueError(f"argument graph_ownership references unknown node type {node_type_name!r}")
        if not isinstance(policy, Mapping):
            raise ValueError(f"argument graph_ownership policy for {node_type_name!r} must be an object")
        mode = str(policy.get("mode") or "").upper()
        if mode:
            if mode != "METHOD_OR_THREAD_BINDING" or node_type_name != "ASSUMPTION":
                raise ValueError(f"argument graph_ownership has unsupported mode {mode!r}")
            continue
        relation = str(policy.get("relation") or "").strip()
        direction = str(policy.get("direction") or "").upper()
        owner_reference_type = str(policy.get("owner_reference_type") or "").strip()
        if not relation or direction not in {"INCOMING", "OUTGOING"} or not owner_reference_type:
            raise ValueError(f"argument graph_ownership policy for {node_type_name!r} is incomplete")
        owner_members = set(reference_type_members.get(owner_reference_type) or (owner_reference_type,))
        if not owner_members or not owner_members <= set(component_by_node_type):
            raise ValueError(f"argument graph_ownership policy for {node_type_name!r} has unknown owner type")

    state_config = rules["SC-ARGUMENT-STATE-OWNERSHIP"].config
    authoritative_root = str(state_config.get("authoritative_root") or "").strip()
    if authoritative_root != "result/authored_state":
        raise ValueError(
            "SC-ARGUMENT-STATE-OWNERSHIP authoritative_root must be result/authored_state"
        )
    if str(state_config.get("projection_version") or "") != "ARGUMENT_PROJECTOR_V2":
        raise ValueError(
            "SC-ARGUMENT-STATE-OWNERSHIP projection_version must be ARGUMENT_PROJECTOR_V2"
        )
    if str(state_config.get("overlap_policy") or "").upper() != "COEXIST":
        raise ValueError(
            "SC-ARGUMENT-STATE-OWNERSHIP overlap_policy must be COEXIST"
        )
    if str(state_config.get("repair_mode") or "").upper() != "AUTHORITATIVE_STATE_TRANSACTION":
        raise ValueError(
            "SC-ARGUMENT-STATE-OWNERSHIP repair_mode must be AUTHORITATIVE_STATE_TRANSACTION"
        )
    if str(state_config.get("semantic_observation_identity") or "").upper() != "RUN_SCOPED_INSTANCE":
        raise ValueError(
            "SC-ARGUMENT-STATE-OWNERSHIP semantic_observation_identity must be RUN_SCOPED_INSTANCE"
        )
    if str(state_config.get("quality_guard_mode") or "").upper() != "OBSERVE_CANONICAL_ONLY":
        raise ValueError(
            "SC-ARGUMENT-STATE-OWNERSHIP quality_guard_mode must be OBSERVE_CANONICAL_ONLY"
        )
    if str(state_config.get("decision_arbiter_mode") or "").upper() != "AUDIT_ONLY":
        raise ValueError(
            "SC-ARGUMENT-STATE-OWNERSHIP decision_arbiter_mode must be AUDIT_ONLY"
        )
    field_writers = state_config.get("field_writers") or {}
    if not isinstance(field_writers, Mapping) or not field_writers:
        raise ValueError(
            "SC-ARGUMENT-STATE-OWNERSHIP field_writers must be a non-empty mapping"
        )
    if any(not str(path).strip() or not str(writer).strip() for path, writer in field_writers.items()):
        raise ValueError(
            "SC-ARGUMENT-STATE-OWNERSHIP field_writers contains an empty path/writer"
        )
    authoritative_fields = tuple(
        str(value).strip() for value in state_config.get("authoritative_fields") or ()
        if str(value).strip()
    )
    derived_result_fields = tuple(
        str(value).strip() for value in state_config.get("derived_result_fields") or ()
        if str(value).strip()
    )
    if not authoritative_fields or len(set(authoritative_fields)) != len(authoritative_fields):
        raise ValueError(
            "SC-ARGUMENT-STATE-OWNERSHIP authoritative_fields must be non-empty and unique"
        )
    if not derived_result_fields or len(set(derived_result_fields)) != len(derived_result_fields):
        raise ValueError(
            "SC-ARGUMENT-STATE-OWNERSHIP derived_result_fields must be non-empty and unique"
        )
    if set(authoritative_fields) & set(derived_result_fields):
        raise ValueError(
            "SC-ARGUMENT-STATE-OWNERSHIP authored and derived fields must be disjoint"
        )
    expected_writers = {
        authoritative_root: "ARGUMENT_STATE_COMMITTER",
        **{f"result/{field}": "ARGUMENT_PROJECTOR" for field in derived_result_fields},
        "producer/status": "ARGUMENT_PROJECTOR",
        "producer/findings": "ARGUMENT_PROJECTOR",
        "producer/unresolved_items": "ARGUMENT_PROJECTOR",
        "producer/user_questions": "ARGUMENT_PROJECTOR",
        "producer/source_refs": "ARGUMENT_PROJECTOR",
        "producer/warnings": "ARGUMENT_PROJECTOR",
        "critic/result/checked_node_ids": "ARGUMENT_CRITIC_RUNTIME_FROM_MODEL_REVIEW",
        "critic/result/chain_checks": "DETERMINISTIC_RUNTIME",
        "critic/result/design_matrix_checks": "DETERMINISTIC_RUNTIME",
        "critic/result/structural_checks": "DETERMINISTIC_RUNTIME",
        "critic/result/evidence_checks": "DETERMINISTIC_RUNTIME",
        "critic/result/deterministic_receipts": "DETERMINISTIC_RUNTIME",
        "critic/result/verdict": "CANONICAL_WORK_ITEM_RESOLVER",
        "critic/findings": "CANONICAL_WORK_ITEM_RESOLVER",
        "critic/result/quality_dimensions": "CANONICAL_WORK_ITEM_RESOLVER",
        "critic/user_questions": "CANONICAL_WORK_ITEM_RESOLVER",
        "critic/unresolved_items": "CANONICAL_WORK_ITEM_RESOLVER",
        "critic/source_refs": "CANONICAL_WORK_ITEM_RESOLVER",
        "critic/warnings": "CANONICAL_WORK_ITEM_RESOLVER",
        "critic/status": "CANONICAL_WORK_ITEM_RESOLVER",
    }
    normalized_writers = {str(path): str(writer) for path, writer in field_writers.items()}
    if normalized_writers != expected_writers:
        raise ValueError(
            "SC-ARGUMENT-STATE-OWNERSHIP field_writers must assign exactly one declared writer to every persisted authoritative/derived semantic field"
        )
    namespace_writers = {
        str(namespace): str(writer)
        for namespace, writer in (state_config.get("namespace_writers") or {}).items()
    }
    if namespace_writers != {
        "MACHINE_DEFECT": "DETERMINISTIC_RUNTIME",
        "SEMANTIC_OBSERVATION": "ARGUMENT_CRITIC_RUNTIME_FROM_MODEL_ISSUES",
    }:
        raise ValueError(
            "SC-ARGUMENT-STATE-OWNERSHIP namespace_writers must keep machine defects and semantic observations in separate writer namespaces"
        )

    lifecycle_config = rules["SC-ARGUMENT-LIFECYCLE-COMPOSITION"].config
    if str(lifecycle_config.get("producer_prompt") or "") != "P-ARGUMENT-ARCHITECTURE":
        raise ValueError(
            "SC-ARGUMENT-LIFECYCLE-COMPOSITION producer_prompt must identify the authoritative Argument producer"
        )
    expected_shapes = {
        "producer_persisted_output": "PRODUCER_PROTOCOL_OUTPUT",
        "producer_consumer_value": "PRODUCER_RESULT",
        "critic_architecture_candidate": "PRODUCER_RESULT",
        "targeted_repair_original_content": "PRODUCER_RESULT",
        "repair_application_value": "PRODUCER_RESULT",
        "repair_canonical_output": "PRODUCER_PROTOCOL_OUTPUT",
        "authoritative_state": "AUTHORED_STATE",
        "argument_graph_consumer": "ARGUMENT_GRAPH",
    }
    actual_shapes = {
        str(key): str(value)
        for key, value in (lifecycle_config.get("shapes") or {}).items()
    }
    if actual_shapes != expected_shapes:
        raise ValueError(
            "SC-ARGUMENT-LIFECYCLE-COMPOSITION shapes must define the exact ProducerProtocolOutput/ProducerResult/AuthoredState boundaries"
        )
    expected_transitions = {
        "PRODUCER_PERSIST_TO_CONTEXT": ("PRODUCER_PROTOCOL_OUTPUT", "PRODUCER_RESULT", "OUTPUT_RESULT"),
        "CONTEXT_TO_CRITIC": ("PRODUCER_RESULT", "PRODUCER_RESULT", "AUTHORITATIVE_REPROJECT"),
        "CRITIC_TO_TARGETED_REPAIR": ("PRODUCER_RESULT", "PRODUCER_RESULT", "IDENTITY"),
        "TARGETED_REPAIR_TO_PROJECTOR": ("PRODUCER_RESULT", "AUTHORED_STATE", "RESULT_AUTHORED_STATE"),
        "PROJECTOR_TO_REPAIR_APPLICATION": ("PRODUCER_PROTOCOL_OUTPUT", "PRODUCER_RESULT", "OUTPUT_RESULT"),
        "REPAIR_APPLICATION_TO_CONTEXT": ("PRODUCER_RESULT", "PRODUCER_RESULT", "ACTIVE_REPAIR_VALUE"),
        "CONTEXT_TO_CRITIC_REREVIEW": ("PRODUCER_RESULT", "PRODUCER_RESULT", "AUTHORITATIVE_REPROJECT"),
        "CONTEXT_TO_DOWNSTREAM": ("PRODUCER_RESULT", "PRODUCER_RESULT", "AUTHORITATIVE_REPROJECT"),
        "CONTEXT_TO_WF3_RESEARCH": ("PRODUCER_RESULT", "ARGUMENT_GRAPH", "AUTHORITATIVE_REPROJECT_ARGUMENT_GRAPH"),
    }
    transitions = lifecycle_config.get("transitions") or {}
    if set(str(key) for key in transitions) != set(expected_transitions):
        raise ValueError(
            "SC-ARGUMENT-LIFECYCLE-COMPOSITION transitions must cover every registered Argument lifecycle handoff"
        )
    for transition_id, expected in expected_transitions.items():
        raw = transitions.get(transition_id) or {}
        actual = (
            str(raw.get("writer_shape") or ""),
            str(raw.get("reader_shape") or ""),
            str(raw.get("adapter") or ""),
        )
        if actual != expected:
            raise ValueError(
                f"SC-ARGUMENT-LIFECYCLE-COMPOSITION {transition_id} shape contract mismatch: {actual!r}"
            )
    lifecycle_rules = {
        str(key): str(value)
        for key, value in (lifecycle_config.get("lifecycle_rules") or {}).items()
    }
    if lifecycle_rules != {
        "repair_commit_and_rereview_checkpoint": "ATOMIC",
        "original_producer_regeneration": "SUPERSEDES_ACTIVE_REPAIR",
        "integration_argument_regeneration": "SUPERSEDES_ACTIVE_REPAIR",
        "human_input_rereview": "PRESERVES_ACTIVE_REPAIR",
        "prerequisite_source_scope": "TRANSITIVE_FROZEN_CLOSURE",
        "post_projection_contract_failure": "REGENERATE_OR_RUNTIME_FAIL_NO_LLM_ENVELOPE_REPAIR",
    }:
        raise ValueError(
            "SC-ARGUMENT-LIFECYCLE-COMPOSITION lifecycle_rules must close repair commit, regeneration and human-input rereview semantics"
        )

    repair_config = rules["SC-ARGUMENT-TARGETED-REPAIR-POLICY"].config
    repair_authoritative_root = str(repair_config.get("authoritative_root") or "").strip()
    persisted_tokens = tuple(token for token in authoritative_root.split("/") if token)
    expected_repair_root = "/" + "/".join(persisted_tokens[1:])
    if not persisted_tokens or persisted_tokens[0] != "result" or repair_authoritative_root != expected_repair_root:
        raise ValueError(
            "SC-ARGUMENT-TARGETED-REPAIR-POLICY authoritative_root must be the ProducerResult-relative form of the persisted state root"
        )
    editable_fields = {
        str(value).strip()
        for value in repair_config.get("editable_fields") or ()
        if str(value).strip()
    }
    machine_fields = {
        str(value).strip()
        for value in repair_config.get("machine_fields") or ()
        if str(value).strip()
    }
    machine_suffixes = tuple(
        str(value).strip()
        for value in repair_config.get("machine_suffixes") or ()
        if str(value).strip()
    )
    if not editable_fields or not machine_fields or not machine_suffixes:
        raise ValueError(
            "SC-ARGUMENT-TARGETED-REPAIR-POLICY must define editable_fields, "
            "machine_fields, and machine_suffixes"
        )
    overlap = sorted(editable_fields & machine_fields)
    if overlap:
        raise ValueError(
            "SC-ARGUMENT-TARGETED-REPAIR-POLICY editable/machine fields overlap: "
            + ", ".join(overlap)
        )
    suffix_collisions = sorted(
        field
        for field in editable_fields
        if any(field.endswith(suffix) for suffix in machine_suffixes)
    )
    if suffix_collisions:
        raise ValueError(
            "SC-ARGUMENT-TARGETED-REPAIR-POLICY editable fields match machine suffixes: "
            + ", ".join(suffix_collisions)
        )
    for bound_name in ("local_context_max_depth", "local_context_max_items"):
        value = repair_config.get(bound_name)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(
                f"SC-ARGUMENT-TARGETED-REPAIR-POLICY {bound_name} must be a positive integer"
            )
    reference_node_types = repair_config.get("reference_node_types") or {}
    if not isinstance(reference_node_types, Mapping) or not reference_node_types:
        raise ValueError(
            "SC-ARGUMENT-TARGETED-REPAIR-POLICY reference_node_types must be a non-empty mapping"
        )
    registered_reference_fields = {str(field) for field in reference_node_types}
    matrix_node_types: set[str] = set()
    for reference_type in reference_node_types.values():
        ref_type = str(reference_type)
        members = reference_type_members.get(ref_type) or (ref_type,)
        matrix_node_types.update(str(value) for value in members)
    topology_node_types = {str(value) for value in ownership_node_types}
    graph_node_types = set(component_by_node_type) - {"CENTRAL_PROPOSITION", "RESEARCH_QUESTION"}
    uncovered_graph_types = sorted(graph_node_types - matrix_node_types - topology_node_types)
    if uncovered_graph_types:
        raise ValueError(
            "argument semantic graph node types lack matrix/topology ownership: "
            + ", ".join(uncovered_graph_types)
        )
    unknown_matrix_fields = sorted(
        str(field) for field in matrix_fields.values() if str(field) not in registered_reference_fields
    )
    if unknown_matrix_fields:
        raise ValueError(
            "SC-ARGUMENT-CRITIC-ISSUE-TAXONOMY matrix fields are not registered repair reference fields: "
            + ", ".join(unknown_matrix_fields)
        )
    if any(
        not str(field).strip() or not str(node_type).strip()
        for field, node_type in reference_node_types.items()
    ):
        raise ValueError(
            "SC-ARGUMENT-TARGETED-REPAIR-POLICY reference_node_types contains an empty entry"
        )


def _load_reference_semantics(groups: Mapping[str, Any]) -> dict[str, ReferenceSemantic]:
    semantics: dict[str, ReferenceSemantic] = {}
    for semantic_name, fields in groups.items():
        semantic = ReferenceSemantic(str(semantic_name).upper())
        for field in fields or []:
            field_name = str(field)
            previous = semantics.get(field_name)
            if previous and previous is not semantic:
                raise ValueError(
                    f"reference field {field_name!r} is registered as both "
                    f"{previous.value!r} and {semantic.value!r}"
                )
            semantics[field_name] = semantic
    return semantics


def _validate_role_config(
    canonical_roles: frozenset[str],
    aliases: Mapping[str, str],
    compatibility: Mapping[str, frozenset[str]],
) -> None:
    unknown_alias_targets = sorted(set(aliases.values()) - canonical_roles)
    if unknown_alias_targets:
        raise ValueError(
            "argument role aliases target non-canonical roles: " + ", ".join(unknown_alias_targets)
        )
    unknown_compatibility_roles = sorted(
        (set(compatibility) | {value for values in compatibility.values() for value in values})
        - canonical_roles
    )
    if unknown_compatibility_roles:
        raise ValueError(
            "argument role compatibility contains non-canonical roles: "
            + ", ".join(unknown_compatibility_roles)
        )
    missing_self = sorted(role for role, values in compatibility.items() if role not in values)
    if missing_self:
        raise ValueError(
            "argument role compatibility must include each configured role itself: "
            + ", ".join(missing_self)
        )


def _apply_reference_value_shape(
    schema: dict[str, Any],
    semantic: ReferenceSemantic,
) -> None:
    """Apply only the scalar constraints owned by reference-field semantics."""

    target = schema
    if schema.get("type") == "array" and isinstance(schema.get("items"), Mapping):
        target = schema["items"]
    if not isinstance(target, dict) or target.get("type") != "string":
        return
    if semantic in _REFERENCE_TEXT_SEMANTICS:
        target.pop("pattern", None)
        target.setdefault("minLength", 1)
        return
    if semantic is ReferenceSemantic.JSON_POINTER:
        target["minLength"] = 1
        target["pattern"] = _JSON_POINTER_PATTERN
        return
    if semantic in {
        ReferenceSemantic.ENTITY_REF,
        ReferenceSemantic.SOURCE_REF,
        ReferenceSemantic.FINDING_REF,
        ReferenceSemantic.ENTITY_OR_FIELD_PATH,
        ReferenceSemantic.PROTOCOL_REF,
        ReferenceSemantic.NEW_ENTITY_ID,
    }:
        target.setdefault("minLength", 1)
        target.setdefault("pattern", _REFERENCE_ID_PATTERN)


def annotate_schema_reference_semantics(
    schema: Mapping[str, Any],
    *,
    contract: SemanticContract | None = None,
) -> dict[str, Any]:
    """Return a schema copy annotated and shaped from the single field registry.

    The function never infers semantics from a suffix. A field is changed only
    when it is explicitly registered in ``semantic_contract.yaml``.
    """

    active = contract or get_semantic_contract()
    annotated = copy.deepcopy(dict(schema))
    prompt_id = str(
        (((annotated.get("properties") or {}).get("prompt_id") or {}).get("const") or "")
    ).strip() or None

    def visit(node: Any, path: tuple[str, ...] = ()) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item, path)
            return
        if not isinstance(node, dict):
            return
        properties = node.get("properties")
        if isinstance(properties, dict):
            for raw_name, child in properties.items():
                name = str(raw_name)
                if isinstance(child, dict):
                    child_path = (*path, name)
                    semantic = active.field_semantic(
                        name, prompt_id=prompt_id, path=child_path
                    )
                    if semantic is not None:
                        child["x-reference-semantic"] = semantic.value
                        _apply_reference_value_shape(child, semantic)
                    visit(child, child_path)
        items = node.get("items")
        if isinstance(items, dict):
            visit(items, (*path, "*"))
        for key, value in node.items():
            if key not in {"properties", "items"}:
                visit(value, path)

    visit(annotated)
    return annotated


def collect_schema_reference_fields(schema: Any) -> set[str]:
    """Return identifier/path array property names declared by a JSON Schema.

    The detector is deliberately schema-driven. It does not decide semantics;
    every detected name must be explicitly classified by the YAML registry.
    """
    fields: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, Mapping):
            return
        properties = node.get("properties")
        if isinstance(properties, Mapping):
            for raw_name, child in properties.items():
                name = str(raw_name)
                if isinstance(child, Mapping) and child.get("type") == "array":
                    items = child.get("items") if isinstance(child.get("items"), Mapping) else {}
                    pattern = str(items.get("pattern") or "")
                    identifier_shaped = "A-Za-z0-9" in pattern
                    convention_shaped = (
                        name.endswith("_ids")
                        or name.endswith("_refs")
                        or name.endswith("_slots")
                        or name
                        in {
                            "sections",
                            "methods",
                            "open_conflicts",
                            "affected_scenarios",
                            "required_user_confirmations",
                            "original_input_refs",
                        }
                    )
                    if identifier_shaped or convention_shaped:
                        fields.add(name)
                visit(child)
        for key, value in node.items():
            if key != "properties":
                visit(value)

    visit(schema)
    return fields


def assert_schema_reference_coverage(
    schemas: Iterable[Mapping[str, Any]],
    *,
    contract: SemanticContract | None = None,
) -> None:
    active = contract or get_semantic_contract()
    discovered: set[str] = set()
    annotation_errors: list[str] = []

    def inspect_annotations(
        node: Any,
        *,
        prompt_id: str | None,
        semantic_path: tuple[str, ...] = (),
        display_path: str = "",
    ) -> None:
        if isinstance(node, list):
            for index, item in enumerate(node):
                inspect_annotations(
                    item,
                    prompt_id=prompt_id,
                    semantic_path=semantic_path,
                    display_path=f"{display_path}/{index}",
                )
            return
        if not isinstance(node, Mapping):
            return

        properties = node.get("properties")
        if isinstance(properties, Mapping):
            for raw_name, child in properties.items():
                name = str(raw_name)
                if not isinstance(child, Mapping):
                    continue
                child_semantic_path = (*semantic_path, name)
                semantic = active.field_semantic(
                    name,
                    prompt_id=prompt_id,
                    path=child_semantic_path,
                )
                if semantic is not None:
                    annotated = child.get("x-reference-semantic")
                    if annotated != semantic.value:
                        annotation_errors.append(
                            f"{display_path}/properties/{name}: expected "
                            f"x-reference-semantic={semantic.value!r}, got {annotated!r}"
                        )
                inspect_annotations(
                    child,
                    prompt_id=prompt_id,
                    semantic_path=child_semantic_path,
                    display_path=f"{display_path}/properties/{name}",
                )

        items = node.get("items")
        if isinstance(items, Mapping):
            inspect_annotations(
                items,
                prompt_id=prompt_id,
                semantic_path=(*semantic_path, "*"),
                display_path=f"{display_path}/items",
            )

        for key, value in node.items():
            if key in {"properties", "items"}:
                continue
            if isinstance(value, (Mapping, list)):
                inspect_annotations(
                    value,
                    prompt_id=prompt_id,
                    semantic_path=semantic_path,
                    display_path=f"{display_path}/{key}",
                )

    for schema in schemas:
        discovered.update(collect_schema_reference_fields(schema))
        prompt_id = str(
            (((schema.get("properties") or {}).get("prompt_id") or {}).get("const") or "")
        ).strip() or None
        inspect_annotations(schema, prompt_id=prompt_id)

    missing = active.unregistered_reference_fields(discovered)
    if missing:
        raise ValueError(
            "semantic contract does not classify schema reference fields: " + ", ".join(missing)
        )
    if annotation_errors:
        raise ValueError(
            "schema reference annotations disagree with semantic contract: "
            + "; ".join(annotation_errors[:20])
        )


def load_semantic_contract(path: Path | str = _CONTRACT_PATH) -> SemanticContract:
    contract_path = Path(path)
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, Mapping):
        raise ValueError("semantic contract root must be an object")

    rules = _load_rule_registry(raw)
    role_config = rules["SC-ARGUMENT-ROLE-COMPATIBILITY"].config
    information = rules["SC-INFORMATION-KEY-HIERARCHY"].config
    coverage = rules["SC-CLAIM-COVERAGE"].config
    evidence = rules["SC-EVIDENCE-SELF-REFERENCE"].config
    evidence_coverage = rules["SC-EVIDENCE-CONTRACT-COVERAGE"].config
    reference_config = rules["SC-REFERENCE-FIELD-SEMANTICS"].config

    canonical_roles = frozenset(
        value.upper()
        for value in _unique_strings(role_config.get("canonical") or (), location="canonical roles")
    )
    aliases = MappingProxyType(
        {
            str(key).upper(): str(value).upper()
            for key, value in (role_config.get("aliases") or {}).items()
        }
    )
    compatibility = MappingProxyType(
        {
            str(key).upper(): frozenset(str(value).upper() for value in values or ())
            for key, values in (role_config.get("compatibility") or {}).items()
        }
    )
    _validate_role_config(canonical_roles, aliases, compatibility)

    separator = str(information.get("hierarchy_separator") or "")
    if not separator:
        raise ValueError("information key hierarchy separator must not be empty")

    claim_fields = _unique_strings(
        coverage.get("fields") or (), location="claim coverage fields"
    )
    if not claim_fields:
        raise ValueError("claim coverage fields must not be empty")
    primary_roles = frozenset(
        value.upper()
        for value in _unique_strings(
            coverage.get("primary_claim_required_roles") or (),
            location="primary claim required roles",
        )
    )
    unknown_primary_roles = sorted(primary_roles - canonical_roles)
    if unknown_primary_roles:
        raise ValueError(
            "primary claim required roles are not canonical: " + ", ".join(unknown_primary_roles)
        )

    evidence_fields = _unique_strings(
        evidence.get("evidence_fields") or (), location="evidence fields"
    )
    if not evidence_fields:
        raise ValueError("evidence fields must not be empty")
    required_evidence_contract_field = str(
        evidence_coverage.get("contract_field") or ""
    ).strip()
    if not required_evidence_contract_field:
        raise ValueError("evidence coverage contract field must not be empty")

    groups = reference_config.get("groups") or {}
    if not isinstance(groups, Mapping):
        raise ValueError("reference semantic groups must be an object")
    semantics = MappingProxyType(_load_reference_semantics(groups))

    raw_path_overrides = reference_config.get("path_overrides") or {}
    if not isinstance(raw_path_overrides, Mapping):
        raise ValueError("reference path overrides must be an object")
    path_semantics: dict[str, tuple[tuple[tuple[str, ...], ReferenceSemantic], ...]] = {}
    for raw_prompt_id, raw_groups in raw_path_overrides.items():
        if not isinstance(raw_groups, Mapping):
            raise ValueError(
                f"reference path overrides for {raw_prompt_id!r} must be an object"
            )
        entries: list[tuple[tuple[str, ...], ReferenceSemantic]] = []
        for raw_semantic, raw_paths in raw_groups.items():
            try:
                semantic = ReferenceSemantic(str(raw_semantic))
            except ValueError as exc:
                raise ValueError(
                    f"unknown reference path semantic {raw_semantic!r}"
                ) from exc
            for raw_path in _unique_strings(
                raw_paths or (),
                location=f"reference path overrides for {raw_prompt_id}/{raw_semantic}",
            ):
                parts = tuple(part for part in raw_path.strip("/").split("/") if part)
                if not parts or parts[-1] == "*":
                    raise ValueError(f"invalid reference path override: {raw_path!r}")
                entries.append((parts, semantic))
        path_semantics[str(raw_prompt_id)] = tuple(entries)

    input_object_fields = frozenset(
        _unique_strings(
            reference_config.get("input_object_fields") or (),
            location="input object reference fields",
        )
    )
    unknown_input_object_fields = sorted(input_object_fields - set(semantics))
    if unknown_input_object_fields:
        raise ValueError(
            "input object reference fields are not registered reference fields: "
            + ", ".join(unknown_input_object_fields)
        )

    raw_suffix_aliases = reference_config.get("registered_suffix_aliases") or {}
    if not isinstance(raw_suffix_aliases, Mapping):
        raise ValueError("registered reference suffix aliases must be an object")
    suffix_aliases: dict[str, tuple[str, ...]] = {}
    for raw_field, raw_suffixes in raw_suffix_aliases.items():
        field_name = str(raw_field)
        if field_name not in semantics:
            raise ValueError(
                f"registered suffix alias field {field_name!r} is not a reference field"
            )
        suffixes = _unique_strings(
            raw_suffixes or (), location=f"registered suffix aliases for {field_name}"
        )
        invalid_suffixes = [
            suffix for suffix in suffixes
            if suffix.startswith("-") or not re.fullmatch(r"[A-Z0-9]+(?:-[A-Z0-9]+)*", suffix)
        ]
        if invalid_suffixes:
            raise ValueError(
                f"registered suffix aliases for {field_name!r} are invalid: "
                + ", ".join(invalid_suffixes)
            )
        suffix_aliases[field_name] = suffixes

    return SemanticContract(
        version=str(raw.get("version") or "0"),
        rule_registry_version=str(raw.get("rule_registry_version") or "0"),
        rules=rules,
        canonical_roles=canonical_roles,
        role_aliases=aliases,
        role_compatibility=compatibility,
        information_key_separator=separator,
        information_key_allow_exact=bool(information.get("exact_root_is_valid", True)),
        information_key_allow_children=bool(information.get("child_keys_are_valid", True)),
        information_key_allow_legacy_dash=bool(information.get("legacy_dash_children", False)),
        claim_coverage_fields=claim_fields,
        primary_claim_required_roles=primary_roles,
        evidence_fields=evidence_fields,
        forbid_self_evidence=bool(evidence.get("forbid_self_evidence", True)),
        required_evidence_contract_field=required_evidence_contract_field,
        reference_field_semantics=semantics,
        reference_path_semantics=MappingProxyType(path_semantics),
        input_object_reference_fields=input_object_fields,
        reference_suffix_aliases=MappingProxyType(suffix_aliases),
    )


@lru_cache(maxsize=1)
def get_semantic_contract() -> SemanticContract:
    return load_semantic_contract(_CONTRACT_PATH)
