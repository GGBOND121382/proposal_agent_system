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
    return MappingProxyType(rules)


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
