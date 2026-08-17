from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from app.contracts.semantic_contract import (
    RuleResponsibility,
    get_semantic_contract,
    load_semantic_contract,
)


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "app" / "contracts" / "semantic_contract.yaml"


def _write_contract(tmp_path: Path, raw: dict) -> Path:
    path = tmp_path / "semantic_contract.yaml"
    path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def test_registry_is_the_only_machine_rule_source() -> None:
    raw = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert set(raw) == {"version", "rule_registry_version", "rules"}
    assert not ({"argument_roles", "information_keys", "claim_coverage", "reference_fields"} & set(raw))
    assert set(raw["rules"]) == get_semantic_contract().rule_ids


def test_rule_registry_entries_are_immutable_and_queryable() -> None:
    contract = get_semantic_contract()
    rule = contract.rule("SC-ARGUMENT-ROLE-COMPATIBILITY")
    assert rule.responsibility is RuleResponsibility.DETERMINISTIC_GUARD
    assert rule.category == "ARGUMENT_ROLE"
    assert rule.blocking is True
    with pytest.raises(TypeError):
        rule.config["canonical"] = ()
    with pytest.raises(KeyError, match="unknown semantic rule"):
        contract.rule("SC-NOT-REGISTERED")


def test_loader_rejects_legacy_duplicate_rule_blocks(tmp_path: Path) -> None:
    raw = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    raw["argument_roles"] = {"canonical": ["PROBLEM"]}
    with pytest.raises(ValueError, match="duplicates rule configuration"):
        load_semantic_contract(_write_contract(tmp_path, raw))


def test_loader_rejects_missing_required_rule(tmp_path: Path) -> None:
    raw = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    del raw["rules"]["SC-EVIDENCE-SELF-REFERENCE"]
    with pytest.raises(ValueError, match="missing required rules"):
        load_semantic_contract(_write_contract(tmp_path, raw))


def test_loader_rejects_reference_field_with_two_semantics(tmp_path: Path) -> None:
    raw = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    groups = raw["rules"]["SC-REFERENCE-FIELD-SEMANTICS"]["config"]["groups"]
    groups["HUMAN_TEXT"].append("source_refs")
    with pytest.raises(ValueError, match="registered as both"):
        load_semantic_contract(_write_contract(tmp_path, raw))


def test_role_alias_and_compatibility_refer_only_to_canonical_roles(tmp_path: Path) -> None:
    raw = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    role_config = raw["rules"]["SC-ARGUMENT-ROLE-COMPATIBILITY"]["config"]
    role_config["aliases"]["BROKEN_ALIAS"] = "NOT_CANONICAL"
    with pytest.raises(ValueError, match="aliases target non-canonical roles"):
        load_semantic_contract(_write_contract(tmp_path, raw))


def test_schema_builder_uses_registry_annotation_instead_of_field_specific_exceptions() -> None:
    builder = (ROOT / "prompt_pack" / "tools" / "build_v2.py").read_text(encoding="utf-8")
    assert "annotate_schema_reference_semantics" in builder
    assert "if path.name.endswith('.schema.json')" in builder
    assert "invalid_slot_refs':arr(s()" not in builder



def test_v5_argument_chain_schema_enum_matches_registry() -> None:
    contract = get_semantic_contract()
    chain_rule = contract.rule("SC-ARGUMENT-DETERMINISTIC-CHAINS").config
    registered = {
        str(item.get("chain_type"))
        for item in chain_rule.get("chains") or ()
    }
    schema = json.loads(
        (
            ROOT
            / "prompt_pack"
            / "schemas"
            / "prompts"
            / "argument_architecture_critic_output.schema.json"
        ).read_text(encoding="utf-8")
    )
    schema_enum = set(
        schema["properties"]["result"]["properties"]["chain_checks"]
        ["items"]["properties"]["chain_type"]["enum"]
    )
    assert registered
    assert schema_enum == registered
    expected = {
        "GAP_TO_QUESTION": ("gap_ids", ("research_question_id",), "MOTIVATES", "SOURCE"),
        "QUESTION_TO_OBJECTIVE": ("research_question_id", ("objective_ids",), "ADDRESSED_BY", "SOURCE"),
        "OBJECTIVE_TO_WORK_PACKAGE": ("objective_ids", ("work_package_ids",), "DECOMPOSES_TO", "BOTH"),
        "WORK_PACKAGE_TO_METHOD": ("work_package_ids", ("method_ids",), "USES", "BOTH"),
        "METHOD_TO_EVALUATION": ("method_ids", ("evaluation_ids",), "VALIDATED_BY", "BOTH"),
        "EVALUATION_TO_INNOVATION": ("evaluation_ids", ("innovation_ids",), "EVIDENCES", "TARGET"),
        "PRIOR_WORK_TO_INNOVATION": ("closest_prior_work_ids", ("innovation_ids",), "CONTRASTS_WITH", "BOTH"),
        "FOUNDATION_TO_FEASIBILITY": (
            "foundation_evidence_ids",
            ("work_package_ids", "method_ids"),
            "SUPPORTS",
            "SOURCE",
        ),
    }
    actual = {
        str(item.get("chain_type")): (
            str(item.get("source_field")),
            tuple(str(value) for value in item.get("target_fields") or ()),
            str(item.get("relation")),
            str(item.get("coverage")),
        )
        for item in chain_rule.get("chains") or ()
    }
    assert actual == expected


def test_v6_evidence_requirement_registry_matches_complete_obligation_matrix() -> None:
    config = get_semantic_contract().rule("SC-ARGUMENT-EVIDENCE-REQUIREMENTS").config
    requirements = list(config.get("requirements") or ())
    actual = {
        str(item.get("requirement_id")): (
            str(item.get("selector")),
            str(item.get("subject")),
            str(item.get("presence")),
            str(item.get("coverage")),
            str(item.get("source_policy")),
        )
        for item in requirements
    }
    expected = {
        "CENTRAL_PROPOSITION_SUPPORT": (
            "central_proposition", "OBJECT", "REQUIRED", "ALL", "STANDARD"
        ),
        "RESEARCH_GAP_SUPPORT": (
            "research_threads/*/gap", "OBJECT", "REQUIRED", "ALL", "STANDARD"
        ),
        "LIMITATION_MECHANISM_SUPPORT": (
            "research_threads/*/gap/limitation_mechanism",
            "OBJECT",
            "IF_PRESENT",
            "ALL",
            "STANDARD",
        ),
        "INNOVATION_PRIOR_WORK_SUPPORT": (
            "research_threads/*/innovations/*/closest_prior_work",
            "COLLECTION",
            "REQUIRED",
            "ALL",
            "STANDARD",
        ),
        "FOUNDATION_SUPPORT": (
            "research_threads/*/foundation",
            "COLLECTION",
            "IF_PRESENT",
            "ALL",
            "FOUNDATION",
        ),
        "EVALUATION_BASELINE_SUPPORT": (
            "research_threads/*/work_packages/*/methods/*/evaluations/*/baselines",
            "COLLECTION",
            "REQUIRED",
            "ALL",
            "STANDARD",
        ),
    }
    assert actual == expected
    assert all(str(item.get("required_node_type") or "") for item in requirements)


def test_v5_argument_repair_policy_cannot_make_machine_fields_editable() -> None:
    config = get_semantic_contract().rule("SC-ARGUMENT-TARGETED-REPAIR-POLICY").config
    editable = {str(value) for value in config.get("editable_fields") or ()}
    machine = {str(value) for value in config.get("machine_fields") or ()}
    suffixes = tuple(str(value) for value in config.get("machine_suffixes") or ())
    assert editable
    assert editable.isdisjoint(machine)
    assert not any(
        field.endswith(suffix)
        for field in editable
        for suffix in suffixes
    )


def test_v5_model_runtime_has_no_legacy_argument_rule_tables() -> None:
    source = (ROOT / "app" / "model_semantic_contracts.py").read_text(encoding="utf-8")
    prohibited = (
        "_SUPPORTED_KNOWLEDGE",
        "_QUALIFIED_FOUNDATION_SOURCE_TYPES",
        "_ARGUMENT_REPAIR_EDITABLE_FIELDS",
        "_ARGUMENT_REPAIR_CONTEXT_MACHINE_FIELDS",
        "_REPAIR_REFERENCE_NODE_TYPES",
        "evidence_requirement_by_code_component",
        "chain_by_component",
    )
    assert not any(token in source for token in prohibited)
