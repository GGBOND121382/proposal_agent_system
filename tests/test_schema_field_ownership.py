from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from app.contract_registry import (
    _collect_runtime_objects,
    augment_prompt_with_field_ownership_contract,
    augment_prompt_with_reference_integrity_contract,
    normalize_registered_enum_aliases_against_schema,
    repair_field_ownership_against_schema,
)
from app.executor import PromptExecutor
from app.pack import PromptPack

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def pack() -> PromptPack:
    return PromptPack(ROOT / "prompt_pack")


def _executor(pack: PromptPack) -> PromptExecutor:
    executor = PromptExecutor.__new__(PromptExecutor)
    executor.pack = pack
    return executor


def _cursor(root: Any, path: tuple[Any, ...]) -> Any:
    current = root
    for token in path:
        current = current[token]
    return current


def test_template_source_exclusions_are_lifted_from_template(pack: PromptPack) -> None:
    output = pack.replay_output("P-TEMPLATE-EXTRACT")
    exclusions = output["result"].pop("source_fact_exclusions")
    output["result"]["template"]["source_fact_exclusions"] = exclusions

    normalized = _executor(pack)._normalize_output("P-TEMPLATE-EXTRACT", output)

    assert normalized["result"]["source_fact_exclusions"] == exclusions
    assert "source_fact_exclusions" not in normalized["result"]["template"]
    assert pack.validate("P-TEMPLATE-EXTRACT", "output", normalized) == []
    assert any(
        "SYSTEM_FIELD_OWNERSHIP_NORMALIZATION" in warning
        for warning in normalized["warnings"]
    )


def test_schema_generated_prompt_contract_names_direct_parent_paths(pack: PromptPack) -> None:
    schema = pack.inlined_schema("P-TEMPLATE-EXTRACT", "output")
    prompt = augment_prompt_with_field_ownership_contract(
        "base",
        schema,
        contract_id="test-contract",
    )

    assert "`$.result` 的直接字段仅允许" in prompt
    assert "`$.result.template` 的直接字段仅允许" in prompt
    assert "source_fact_exclusions*" in prompt
    assert prompt.count("FIELD_OWNERSHIP_CONTRACT:START") == 1


def test_schema_bound_enum_alias_normalizer_is_narrow(pack: PromptPack) -> None:
    schema = pack.inlined_schema("P-PROJECT-READINESS-CRITIC", "output")
    output = pack.replay_output("P-PROJECT-READINESS-CRITIC")
    output["result"]["domain_scores"][0]["missing_item_types"] = [
        "CLOSEST_PRIOR_WORK"
    ]
    output["result"]["critical_readiness_checks"][0]["dimension"] = (
        "TEAM_AND_IMPLEMENTATION"
    )

    normalized, report = normalize_registered_enum_aliases_against_schema(
        output,
        schema,
        contract_id="test-schema-bound-enum-aliases",
    )

    assert normalized["result"]["domain_scores"][0]["missing_item_types"] == [
        "EXISTING_APPROACH"
    ]
    assert normalized["result"]["critical_readiness_checks"][0]["dimension"] == (
        "TEAM_AND_IMPLEMENTATION"
    )
    assert report["normalized_count"] == 1
    assert report["changes"][0]["rule"] == "REGISTERED_ALIAS"


def test_write_content_trace_source_kind_has_explicit_container_ownership(
    pack: PromptPack,
) -> None:
    prompt = (ROOT / "prompt_pack/prompts/writing/write_content.md").read_text(
        encoding="utf-8"
    )
    trace_schema = json.loads(
        (ROOT / "prompt_pack/schemas/common/trace_link.schema.json").read_text(
            encoding="utf-8"
        )
    )
    description = trace_schema["properties"]["source_kind"]["description"]
    registry = json.loads(
        (ROOT / "prompt_pack/config/prompt_registry.json").read_text(
            encoding="utf-8"
        )
    )
    write_content_entry = next(
        item
        for item in registry["prompts"]
        if item["prompt_id"] == "P-WRITE-CONTENT"
    )

    assert "payload.confirmed_facts" in prompt
    assert "`FACT`" in prompt
    assert "claim_type=EXPECTED_RESULT" in prompt
    assert "payload.confirmed_facts to FACT" in description
    assert "claim_type" in description
    assert write_content_entry["prompt_version"] == "3.2.0"
    build_source = (ROOT / "prompt_pack/tools/build_v2.py").read_text(
        encoding="utf-8"
    )
    assert "'P-WRITE-CONTENT': '3.2.0'" in build_source
    assert "payload.confirmed_facts to FACT" in build_source
    assert "'ARGUMENT_NODE','SECTION_CONTRACT','SKILL_ARTIFACT'" in build_source
    assert pack.schema("P-WRITE-CONTENT", "input")["properties"]["prompt_version"] == {
        "const": "3.2.0"
    }
    assert pack.schema("P-WRITE-CONTENT", "output")["properties"]["prompt_version"] == {
        "const": "3.2.0"
    }
    for fixture_path in (
        ROOT / "prompt_pack/replay/cases/write_content"
    ).glob("*.json"):
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        assert fixture["input"]["prompt_version"] == "3.2.0"
        if fixture["expected_output"] is not None:
            assert fixture["expected_output"]["prompt_version"] == "3.2.0"


def test_schema_generated_reference_contract_names_definitions_and_references(
    pack: PromptPack,
) -> None:
    schema = pack.inlined_schema("P-WRITE-CONTENT", "output")
    generated = augment_prompt_with_reference_integrity_contract(
        "base",
        schema,
        contract_id="test-reference-integrity",
    )

    assert "自动生成的引用完整性契约" in generated
    assert "$.result.paragraphs[*].trace_link_ids" in generated
    assert "$.result.trace_links[*].trace_id" in generated
    assert "禁止悬空引用" in generated
    assert generated.count("REFERENCE_INTEGRITY_CONTRACT:START") == 1

    regenerated = augment_prompt_with_reference_integrity_contract(
        generated,
        schema,
        contract_id="test-reference-integrity",
    )
    assert regenerated.count("REFERENCE_INTEGRITY_CONTRACT:START") == 1


def test_claim_type_is_not_postprocessed_into_trace_source_kind(pack: PromptPack) -> None:
    output = pack.replay_output("P-WRITE-CONTENT")
    output["result"]["trace_links"][0]["source_kind"] = "EXPECTED_RESULT"

    normalized = _executor(pack)._normalize_output("P-WRITE-CONTENT", output)

    assert normalized["result"]["trace_links"][0]["source_kind"] == "EXPECTED_RESULT"
    assert any(
        "source_kind" in error and "EXPECTED_RESULT" in error
        for error in pack.validate("P-WRITE-CONTENT", "output", normalized)
    )


def test_all_normal_replays_remain_schema_valid_after_full_normalization(
    pack: PromptPack,
) -> None:
    executor = _executor(pack)
    failures: list[str] = []
    for prompt_id in pack.prompt_ids():
        output = pack.replay_output(prompt_id)
        envelope = pack.replay_input(prompt_id)
        try:
            normalized = executor._normalize_output(prompt_id, output, envelope)
        except Exception as exc:  # pragma: no cover - diagnostic aggregation
            failures.append(f"{prompt_id}: {type(exc).__name__}: {exc}")
            continue
        errors = pack.validate(prompt_id, "output", normalized)
        if errors:
            failures.append(f"{prompt_id}: {errors[:5]}")

    assert not failures, "\n".join(failures)


def test_unique_ancestor_chain_moves_are_repaired_across_prompt_pack(pack: PromptPack) -> None:
    """Exercise generic upward/downward drift on every replay schema.

    Only scalar/list fields are perturbed; moving a container into itself would
    create a circular Python object and does not represent provider JSON.
    """

    exercised = 0
    failures: list[str] = []
    for prompt_id in pack.prompt_ids():
        output = pack.replay_output(prompt_id)
        schema = pack.inlined_schema(prompt_id, "output")
        objects = _collect_runtime_objects(output, schema, schema)
        object_map = {path: (obj, obj_schema) for path, obj, obj_schema in objects}

        for path, obj, obj_schema in objects:
            if not path or not isinstance(path[-1], str):
                continue
            parent_path = path[:-1]
            if parent_path not in object_map:
                continue
            parent_obj, parent_schema = object_map[parent_path]
            child_properties = obj_schema.get("properties") or {}
            parent_properties = parent_schema.get("properties") or {}

            # Correct child field emitted one level too high.
            for field in obj_schema.get("required") or []:
                value = obj.get(field)
                if (
                    field not in obj
                    or field in parent_properties
                    or isinstance(value, dict)
                ):
                    continue
                malformed = copy.deepcopy(output)
                malformed_child = _cursor(malformed, path)
                malformed_parent = _cursor(malformed, parent_path)
                malformed_parent[field] = malformed_child.pop(field)
                repaired, report = repair_field_ownership_against_schema(
                    malformed,
                    schema,
                    contract_id=f"test:{prompt_id}",
                )
                errors = pack.validate(prompt_id, "output", repaired)
                exercised += 1
                if errors or not report["normalized_count"]:
                    failures.append(
                        f"{prompt_id} upward {path}.{field}: {errors[:2]} report={report}"
                    )
                break

            # Correct parent field emitted one level too low.
            if obj_schema.get("additionalProperties") is not False:
                continue
            for field in parent_schema.get("required") or []:
                value = parent_obj.get(field)
                if (
                    field not in parent_obj
                    or field in child_properties
                    or isinstance(value, dict)
                ):
                    continue
                malformed = copy.deepcopy(output)
                malformed_child = _cursor(malformed, path)
                malformed_parent = _cursor(malformed, parent_path)
                malformed_child[field] = malformed_parent.pop(field)
                repaired, report = repair_field_ownership_against_schema(
                    malformed,
                    schema,
                    contract_id=f"test:{prompt_id}",
                )
                errors = pack.validate(prompt_id, "output", repaired)
                exercised += 1
                if errors or not report["normalized_count"]:
                    failures.append(
                        f"{prompt_id} downward {path}.{field}: {errors[:2]} report={report}"
                    )
                break

    assert exercised >= 80
    assert not failures, "\n".join(failures[:20])


def test_sibling_field_move_is_not_guessed() -> None:
    schema = {
        "type": "object",
        "properties": {
            "left": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            "right": {
                "type": "object",
                "properties": {"other": {"type": "string"}},
                "required": ["other"],
                "additionalProperties": False,
            },
        },
        "required": ["left", "right"],
        "additionalProperties": False,
    }
    malformed = {"left": {}, "right": {"other": "ok", "value": "do-not-guess"}}

    repaired, report = repair_field_ownership_against_schema(
        malformed,
        schema,
        contract_id="sibling-ambiguity",
    )

    assert repaired == malformed
    assert report["normalized_count"] == 0


def test_targeted_repair_open_object_wrapper_fields_are_not_guessed(pack: PromptPack) -> None:
    output = pack.replay_output("P-TARGETED-REPAIR")
    envelope = pack.replay_input("P-TARGETED-REPAIR")
    envelope["payload"]["original_object"]["content"] = {"text": "old"}
    envelope["payload"]["allowed_paths"] = ["/content/text"]
    envelope["payload"]["findings_to_repair"] = [{
        "finding_instance_id": "finding-text-fix-001",
        "code": "TEXT_FIX",
        "target_path_or_span": "/content/text",
    }]
    output["status"] = "REVISE"
    output["result"]["repaired_object"] = {
        "content": {"text": "new"},
        "changed_paths": ["/content/text"],
    }
    output["result"].pop("changed_paths")
    output["result"]["resolved_finding_ids"] = ["finding-text-fix-001"]
    output["result"]["unresolved_finding_ids"] = []
    output["findings"] = []

    normalized = _executor(pack)._normalize_output(
        "P-TARGETED-REPAIR",
        output,
        envelope,
    )

    assert "changed_paths" not in normalized["result"]
    assert normalized["result"]["repaired_object"]["changed_paths"] == ["/content/text"]
    errors = pack.validate("P-TARGETED-REPAIR", "output", normalized)
    assert any("changed_paths" in error for error in errors)


@pytest.mark.parametrize("prompt_id", ["P-WRITE-CONTENT", "P-EXPRESSION-POLISH"])
def test_mirrored_unresolved_items_are_losslessly_synchronized(
    pack: PromptPack,
    prompt_id: str,
) -> None:
    output = pack.replay_output(prompt_id)
    envelope = pack.replay_input(prompt_id)
    item = copy.deepcopy(output["unresolved_items"])
    assert item == []
    root_item = {
        "item_id": "unresolved-root-1",
        "type": "MISSING",
        "description": "root item",
        "target_paths": ["/result/candidate_text"],
        "required_action": "provide source",
        "blocking": False,
    }
    result_item = {
        "item_id": "unresolved-result-1",
        "type": "UNCERTAIN",
        "description": "result item",
        "target_paths": ["/result/paragraphs"],
        "required_action": "verify content",
        "blocking": False,
    }
    output["unresolved_items"] = [root_item]
    output["result"]["unresolved_items"] = [result_item]

    normalized = _executor(pack)._normalize_output(prompt_id, output, envelope)

    assert normalized["unresolved_items"] == normalized["result"]["unresolved_items"]
    assert {item["item_id"] for item in normalized["unresolved_items"]} == {
        "unresolved-root-1",
        "unresolved-result-1",
    }
    assert pack.validate(prompt_id, "output", normalized) == []


def test_required_array_owner_merges_misplaced_values_when_target_exists() -> None:
    schema = {
        "type": "object",
        "properties": {
            "warnings": {"type": "array", "items": {"type": "string"}},
            "result": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        },
        "required": ["warnings", "result"],
        "additionalProperties": False,
    }
    malformed = {
        "warnings": ["top-level warning"],
        "result": {
            "value": "ok",
            "warnings": ["nested warning", "top-level warning"],
        },
    }

    repaired, report = repair_field_ownership_against_schema(
        malformed,
        schema,
        contract_id="required-array-owner",
    )

    assert repaired == {
        "warnings": ["top-level warning", "nested warning"],
        "result": {"value": "ok"},
    }
    assert report["normalized_count"] == 1
    assert report["changes"][0]["rule"] == "SCHEMA_FIELD_OWNERSHIP_ARRAY_MERGE"
    assert Draft202012Validator(schema).is_valid(repaired)


def test_optional_same_named_ancestor_field_is_not_moved() -> None:
    schema = {
        "type": "object",
        "properties": {
            "warnings": {"type": "array", "items": {"type": "string"}},
            "result": {
                "type": "object",
                "properties": {
                    "payload": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                        "additionalProperties": False,
                    }
                },
                "required": ["payload"],
                "additionalProperties": False,
            },
        },
        "required": ["result"],
        "additionalProperties": False,
    }
    malformed = {
        "result": {
            "payload": {
                "value": "ok",
                "warnings": ["ambiguous business-level warning"],
            }
        }
    }

    repaired, report = repair_field_ownership_against_schema(
        malformed,
        schema,
        contract_id="optional-owner",
    )

    assert repaired == malformed
    assert report["normalized_count"] == 0


def test_flattened_required_child_object_is_recreated() -> None:
    schema = {
        "type": "object",
        "properties": {
            "result": {
                "type": "object",
                "properties": {
                    "template": {
                        "type": "object",
                        "properties": {
                            "template_id": {"type": "string"},
                            "components": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["template_id", "components"],
                        "additionalProperties": False,
                    },
                    "coverage": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["template", "coverage"],
                "additionalProperties": False,
            }
        },
        "required": ["result"],
        "additionalProperties": False,
    }
    malformed = {
        "result": {
            "template_id": "tpl-1",
            "components": ["c1"],
            "coverage": ["all"],
        }
    }

    repaired, report = repair_field_ownership_against_schema(
        malformed,
        schema,
        contract_id="flattened-required-child",
    )

    assert repaired == {
        "result": {
            "template": {"template_id": "tpl-1", "components": ["c1"]},
            "coverage": ["all"],
        }
    }
    assert report["normalized_count"] == 1
    assert report["changes"][0]["rule"] == "CREATE_REQUIRED_OBJECT_AND_MOVE_FIELDS"
    assert Draft202012Validator(schema).is_valid(repaired)


def test_required_object_flattening_is_repaired_across_prompt_pack(pack: PromptPack) -> None:
    exercised = 0
    failures: list[str] = []
    for prompt_id in pack.prompt_ids():
        output = pack.replay_output(prompt_id)
        schema = pack.inlined_schema(prompt_id, "output")
        objects = _collect_runtime_objects(output, schema, schema)
        object_map = {path: (obj, obj_schema) for path, obj, obj_schema in objects}

        for path, child_object, child_schema in objects:
            if not path or not isinstance(path[-1], str):
                continue
            parent_path = path[:-1]
            child_name = path[-1]
            if parent_path not in object_map or not isinstance(child_object, dict):
                continue
            parent_object, parent_schema = object_map[parent_path]
            if child_name not in set(parent_schema.get("required") or []):
                continue
            if child_schema.get("additionalProperties") is not False:
                continue
            if any(field in parent_object for field in child_object):
                continue
            if not set(child_schema.get("required") or []).intersection(child_object):
                continue

            malformed = copy.deepcopy(output)
            malformed_child = copy.deepcopy(_cursor(malformed, path))
            malformed_parent = _cursor(malformed, parent_path)
            malformed_parent.pop(child_name)
            malformed_parent.update(malformed_child)

            repaired, report = repair_field_ownership_against_schema(
                malformed,
                schema,
                contract_id=f"flattened:{prompt_id}",
            )
            errors = pack.validate(prompt_id, "output", repaired)
            exercised += 1
            if errors or not report["normalized_count"]:
                failures.append(
                    f"{prompt_id} flattened {path}: {errors[:3]} report={report}"
                )
            break

    assert exercised == len(pack.prompt_ids())
    assert not failures, "\n".join(failures[:20])


def test_wrong_typed_uniquely_owned_required_field_is_reported_without_mutation() -> None:
    schema = {
        "type": "object",
        "properties": {
            "warnings": {"type": "array", "items": {"type": "string"}},
            "result": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        },
        "required": ["warnings", "result"],
        "additionalProperties": False,
    }
    malformed = {
        "result": {
            "value": "ok",
            "warnings": {"message": "wrong shape"},
        }
    }

    repaired, report = repair_field_ownership_against_schema(
        malformed,
        schema,
        contract_id="wrong-typed-required-owner",
    )

    assert repaired == malformed
    assert report["normalized_count"] == 0
    assert report["invalid_misplacement_count"] == 1
    invalid = report["invalid_misplacements"][0]
    assert invalid["source_path"] == "/result/warnings"
    assert invalid["target_path"] == "/warnings"
    assert invalid["validation_errors"]
