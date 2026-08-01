from __future__ import annotations

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
