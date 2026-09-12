from __future__ import annotations

from jsonschema import Draft202012Validator

from app.contract_registry import prepare_schema_contract
from app.contracts.semantic_contract import (
    ReferenceSemantic,
    annotate_schema_reference_semantics,
    get_semantic_contract,
)
from app.output_integrity import validate_reference_ids


def _schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "invalid_slot_refs": {
                "type": "array",
                "items": {
                    "type": "string",
                    "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                },
            },
            "checked_item_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
            "warnings": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "additionalProperties": False,
    }


def test_schema_annotation_and_shape_come_from_field_registry() -> None:
    annotated = annotate_schema_reference_semantics(_schema())
    diagnostic = annotated["properties"]["invalid_slot_refs"]
    entity_refs = annotated["properties"]["checked_item_ids"]
    untouched = annotated["properties"]["warnings"]

    assert diagnostic["x-reference-semantic"] == ReferenceSemantic.UNRESOLVED_DESCRIPTOR.value
    assert "pattern" not in diagnostic["items"]
    assert entity_refs["x-reference-semantic"] == ReferenceSemantic.ENTITY_REF.value
    assert entity_refs["items"]["pattern"].startswith("^[A-Za-z0-9]")
    assert "x-reference-semantic" not in untouched

    assert list(Draft202012Validator(annotated).iter_errors({
        "invalid_slot_refs": ["第 3 段的信息键不属于合同"],
        "checked_item_ids": ["item-001"],
        "warnings": [],
    })) == []


def test_contract_registry_prepares_the_same_semantic_schema() -> None:
    assert prepare_schema_contract(_schema()) == annotate_schema_reference_semantics(_schema())


def test_diagnostic_descriptors_do_not_authorize_entity_references() -> None:
    trusted_input = {"invalid_slot_refs": ["ghost-id"]}
    output = {"checked_item_ids": ["ghost-id"]}

    errors = validate_reference_ids(output, trusted_input)

    assert len(errors) == 1
    assert "ghost-id" in errors[0]
    assert "ENTITY_REF" in errors[0]


def test_entity_references_remain_strict_and_visible_input_refs_are_reusable() -> None:
    trusted_input = {
        "project_definition": {
            "items": [{"item_id": "item-001", "item_type": "PROBLEM"}],
        },
        "checked_item_ids": ["item-001"],
    }
    assert validate_reference_ids({"checked_item_ids": ["item-001"]}, trusted_input) == []
    assert get_semantic_contract().requires_existing_target("checked_item_ids") is True
    assert get_semantic_contract().requires_existing_target("invalid_slot_refs") is False


def test_reference_normalization_is_immutable_and_limited_to_registered_presentations() -> None:
    from copy import deepcopy

    from app.output_integrity import normalize_reference_id_aliases

    trusted_input = {
        "project_definition": {
            "items": [
                {"item_id": "item-001", "item_type": "PROBLEM"},
                {"item_id": "GAP-1", "item_type": "GAP"},
            ]
        }
    }
    output = {
        "checked_item_ids": [
            "ref-item-001",
            "GAP-PROJ-1",
            "item-001-UNKNOWN",
        ],
        "evidence_refs": [
            "item-001.argument_role",
            "item-001:source_refs.0",
        ],
    }
    original = deepcopy(output)

    normalized, report = normalize_reference_id_aliases(output, trusted_input)

    assert output == original
    assert normalized["checked_item_ids"] == [
        "item-001",
        "GAP-PROJ-1",
        "item-001-UNKNOWN",
    ]
    assert normalized["evidence_refs"] == ["item-001", "item-001"]
    assert report["normalization_policy"] == "EXACT_OR_REGISTERED_PRESENTATION_ONLY"
    assert {item["alias_kind"] for item in report["changes"]} == {
        "PRESENTATION_PREFIX",
        "ENTITY_FIELD_PATH",
    }


def test_entity_field_path_is_not_accepted_for_plain_entity_reference_fields() -> None:
    from app.output_integrity import normalize_reference_id_aliases

    trusted_input = {"project_definition": {"items": [{"item_id": "item-001"}]}}
    output = {"checked_item_ids": ["item-001:argument_role"]}

    normalized, report = normalize_reference_id_aliases(output, trusted_input)

    assert normalized == output
    assert report["normalized_count"] == 0
    errors = validate_reference_ids(normalized, trusted_input)
    assert len(errors) == 1
    assert "item-001:argument_role" in errors[0]


def test_unknown_or_legacy_namespace_aliases_are_not_silently_repaired() -> None:
    from app.output_integrity import normalize_reference_id_aliases

    trusted_input = {"project_definition": {"items": [{"item_id": "GAP-1"}]}}
    output = {"checked_item_ids": ["GAP-PROJ-1", "GAP-1-MISSING"]}

    normalized, report = normalize_reference_id_aliases(output, trusted_input)

    assert normalized == output
    assert report["normalized_count"] == 0
    errors = validate_reference_ids(normalized, trusted_input)
    assert len(errors) == 2


def test_json_pointer_and_object_reference_shapes_are_distinct() -> None:
    schema = {
        "type": "object",
        "properties": {
            "allowed_paths": {
                "type": "array",
                "items": {"type": "string"},
            },
            "original_input_refs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"object_id": {"type": "string"}},
                },
            },
        },
    }
    annotated = annotate_schema_reference_semantics(schema)
    pointer = annotated["properties"]["allowed_paths"]
    object_refs = annotated["properties"]["original_input_refs"]

    assert pointer["x-reference-semantic"] == ReferenceSemantic.JSON_POINTER.value
    assert pointer["items"]["pattern"].startswith("^/")
    assert object_refs["x-reference-semantic"] == ReferenceSemantic.OBJECT_REF.value
    assert "pattern" not in object_refs["items"]

    validator = Draft202012Validator(annotated)
    assert list(validator.iter_errors({
        "allowed_paths": ["/content/paragraphs/0/text"],
        "original_input_refs": [{"object_id": "obj-1"}],
    })) == []
    assert list(validator.iter_errors({
        "allowed_paths": ["content.paragraphs[0].text"],
        "original_input_refs": [{"object_id": "obj-1"}],
    }))


def test_named_input_objects_are_a_separate_exact_reference_namespace() -> None:
    contract = get_semantic_contract()
    trusted_input = {
        "payload": {
            "proposal_contract": {
                "mandatory_sections": ["abstract"],
            }
        }
    }

    assert contract.allows_input_object("required_input_ids") is True
    assert contract.allows_input_object("evidence_refs") is True
    assert contract.allows_input_object("checked_item_ids") is False
    assert validate_reference_ids(
        {
            "required_input_ids": ["proposal_contract"],
            "evidence_refs": ["proposal_contract"],
        },
        trusted_input,
    ) == []

    errors = validate_reference_ids(
        {
            "checked_item_ids": ["proposal_contract"],
            "evidence_refs": ["proposal_contract.mandatory_sections"],
        },
        trusted_input,
    )
    assert len(errors) == 2
    assert "checked_item_ids" in errors[0]
    assert "proposal_contract.mandatory_sections" in errors[1]


def test_descriptor_suffix_aliases_are_contract_registered_and_field_scoped() -> None:
    from app.output_integrity import normalize_reference_id_aliases

    trusted_input = {
        "payload": {
            "argument_graph": {
                "nodes": [
                    {"node_id": "BASE-001"},
                    {"node_id": "INNO-004"},
                ]
            }
        }
    }
    output = {
        "unresolved_slot_ids": [
            "BASE-001-UNKNOWN",
            "INNO-004-ABSENT-FACT",
            "BASE-001-UNREGISTERED",
        ],
        "required_evidence_ids": ["BASE-001-UNKNOWN"],
    }

    normalized, report = normalize_reference_id_aliases(output, trusted_input)

    assert normalized["unresolved_slot_ids"] == [
        "BASE-001",
        "INNO-004",
        "BASE-001-UNREGISTERED",
    ]
    assert normalized["required_evidence_ids"] == ["BASE-001-UNKNOWN"]
    assert [item["alias_kind"] for item in report["changes"]] == [
        "REGISTERED_DESCRIPTOR_SUFFIX",
        "REGISTERED_DESCRIPTOR_SUFFIX",
    ]
