from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from app.executor import PromptExecutionError
from app.db import Database
from app.runtime_api import PromptExecutor
from app.pack import PromptPack
from app.output_integrity import (
    attach_trusted_source_catalog,
    bind_trusted_source_refs,
    normalize_reference_id_aliases,
    validate_reference_ids,
)
from app.proposal_quality import ProposalQualityGuard
from app.staged_contracts import (
    clear_contract_trace_context,
    contract_validation_errors,
    normalize_in_place,
    require_model_response_envelope,
    set_contract_trace_context,
)
from app.status_ontology import normalize_stage2_candidate, normalize_stage3_candidate

ROOT = Path(__file__).resolve().parents[1]


def _type_accepts(schema: dict[str, Any], value: Any) -> bool:
    declared = schema.get("type")
    if declared is None:
        return True
    types = [declared] if isinstance(declared, str) else list(declared)
    if value is None:
        return "null" in types
    if isinstance(value, dict):
        return "object" in types
    if isinstance(value, list):
        return "array" in types
    if isinstance(value, bool):
        return "boolean" in types
    if isinstance(value, int):
        return "integer" in types or "number" in types
    if isinstance(value, float):
        return "number" in types
    if isinstance(value, str):
        return "string" in types
    return False


def _branch_for(schema: dict[str, Any], value: Any) -> dict[str, Any]:
    branches = schema.get("anyOf")
    if not isinstance(branches, list):
        return schema
    candidates = [branch for branch in branches if isinstance(branch, dict)]
    matching = [branch for branch in candidates if _type_accepts(branch, value)]
    selected = matching[0] if matching else (candidates[0] if candidates else {})
    return {**{key: item for key, item in schema.items() if key != "anyOf"}, **selected}


def _declared_container_paths(
    value: Any,
    schema: dict[str, Any],
    path: tuple[Any, ...] = (),
) -> list[tuple[Any, ...]]:
    schema = _branch_for(schema, value)
    paths: list[tuple[Any, ...]] = []
    if path and isinstance(value, (dict, list)):
        declared = schema.get("type")
        types = [declared] if isinstance(declared, str) else list(declared or [])
        expected = "object" if isinstance(value, dict) else "array"
        if expected in types:
            paths.append(path)
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        additional = schema.get("additionalProperties")
        for key, child in value.items():
            child_schema = properties.get(key)
            if child_schema is None and isinstance(additional, dict):
                child_schema = additional
            if isinstance(child_schema, dict):
                paths.extend(_declared_container_paths(child, child_schema, (*path, key)))
    elif isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, child in enumerate(value):
                paths.extend(_declared_container_paths(child, item_schema, (*path, index)))
    return paths


def _replace(value: Any, path: tuple[Any, ...], replacement: Any) -> Any:
    changed = copy.deepcopy(value)
    cursor = changed
    for token in path[:-1]:
        cursor = cursor[token]
    cursor[path[-1]] = replacement
    return changed


def _pointer(path: tuple[Any, ...]) -> str:
    return "/" + "/".join(str(token) for token in path)


@pytest.fixture(scope="module")
def pack() -> PromptPack:
    return PromptPack(ROOT / "prompt_pack")


def test_all_replay_outputs_pass_container_preflight(pack: PromptPack) -> None:
    failures: list[str] = []
    for prompt_id in pack.prompt_ids():
        output = pack.replay_output(prompt_id)
        errors = pack.validate_structure(prompt_id, "output", output)
        if errors:
            failures.append(f"{prompt_id}: {errors[:3]}")
    assert not failures, "\n".join(failures)


def test_every_declared_replay_container_mismatch_is_rejected_before_normalization(
    pack: PromptPack,
) -> None:
    executor = PromptExecutor.__new__(PromptExecutor)
    executor.pack = pack
    failures: list[str] = []
    exercised = 0

    for prompt_id in pack.prompt_ids():
        output = pack.replay_output(prompt_id)
        structure_schema = pack.structure_schema(prompt_id, "output")
        for path in _declared_container_paths(output, structure_schema):
            original = output
            for token in path:
                original = original[token]
            replacement = [{"unexpected": True}] if isinstance(original, dict) else {"unexpected": True}
            malformed = _replace(output, path, replacement)
            exercised += 1
            try:
                executor._normalize_output(prompt_id, malformed)
            except PromptExecutionError as exc:
                if "container structure" not in str(exc):
                    failures.append(
                        f"{prompt_id}{_pointer(path)} raised controlled but unexpected error: {exc}"
                    )
            except Exception as exc:  # pragma: no cover - regression diagnostic
                failures.append(
                    f"{prompt_id}{_pointer(path)} leaked {type(exc).__name__}: {exc}"
                )
            else:
                failures.append(f"{prompt_id}{_pointer(path)} was not rejected")

    assert exercised >= 100
    assert not failures, "\n".join(failures[:30])


def test_root_list_is_reported_as_controlled_contract_error(pack: PromptPack) -> None:
    executor = PromptExecutor.__new__(PromptExecutor)
    executor.pack = pack
    with pytest.raises(PromptExecutionError, match="container structure"):
        executor._normalize_output("P-FACT-EXTRACT", [{"unexpected": True}])


def test_container_preflight_does_not_reject_repairable_scalar_drift(
    pack: PromptPack,
) -> None:
    output = pack.replay_output("P-SAFE-ONLINE-PACKAGE")
    output["source_refs"] = [{
        "source_id": "need-001",
        "source_type": "MODEL_INFERENCE",
        "document_version_id": 1,
        "section_id": None,
        "span_start": None,
        "span_end": None,
        "quoted_text": None,
        "source_hash": None,
        "authority_rank": 60,
        "security_level": "INTERNAL",
    }]

    assert pack.validate_structure("P-SAFE-ONLINE-PACKAGE", "output", output) == []
    assert any(
        "/source_refs/0/document_version_id" in error
        for error in pack.validate("P-SAFE-ONLINE-PACKAGE", "output", output)
    )


def test_safe_package_source_refs_are_rebuilt_from_persisted_document_metadata(
    pack: PromptPack,
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "runtime.sqlite3")
    parsed_document = {
        "document_id": "doc-001",
        "document_version_id": "docv-real-001",
        "document_role": "APPLICATION_GUIDE",
        "document_hash": "a" * 64,
        "authority_rank": 95,
        "security_level": "INTERNAL",
        "sections": [],
    }
    db.execute(
        """INSERT INTO projects(
               id,name,description,security_level,config_json,created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?)""",
        (
            "project-001", "Project", "Description", "INTERNAL", "{}",
            "2026-07-29T00:00:00Z", "2026-07-29T00:00:00Z",
        ),
    )
    db.execute(
        """INSERT INTO documents(
               id,project_id,filename,role,security_level,document_hash,
               file_path,parsed_json,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            "doc-001", "project-001", "guide.txt", "APPLICATION_GUIDE",
            "INTERNAL", "a" * 64, "guide.txt",
            json.dumps(parsed_document, ensure_ascii=False),
            "2026-07-29T00:00:00Z",
        ),
    )
    envelope = pack.replay_input("P-SAFE-ONLINE-PACKAGE")
    envelope["scope"]["project_id"] = "project-001"
    envelope["payload"]["source_items"] = [{
        "object_id": "doc-001",
        "object_type": "SOURCE_DOCUMENT:APPLICATION_GUIDE",
        "version": 1,
        "object_hash": "a" * 64,
        "security_level": "INTERNAL",
        "display_name": "申报指南",
    }]
    output = pack.replay_output("P-SAFE-ONLINE-PACKAGE")
    output["source_refs"] = [{
        "source_id": "doc-001",
        "source_type": "APPLICATION_GUIDE",
        "document_version_id": 1,
        "section_id": "invented-section",
        "span_start": 0,
        "span_end": 10,
        "quoted_text": "invented quote",
        "source_hash": "b" * 64,
        "authority_rank": 1,
        "security_level": "PUBLIC",
    }]
    executor = PromptExecutor.__new__(PromptExecutor)
    executor.pack = pack
    executor.db = db

    normalized = executor._normalize_output(
        "P-SAFE-ONLINE-PACKAGE",
        output,
        envelope,
    )

    source_ref = normalized["source_refs"][0]
    assert source_ref == {
        "source_id": "doc-001",
        "source_type": "APPLICATION_GUIDE",
        "document_version_id": "docv-real-001",
        "section_id": None,
        "span_start": None,
        "span_end": None,
        "quoted_text": None,
        "source_hash": "a" * 64,
        "authority_rank": 95,
        "security_level": "INTERNAL",
    }
    assert pack.validate("P-SAFE-ONLINE-PACKAGE", "output", normalized) == []


def test_misplaced_critic_warning_with_wrong_type_is_controlled(pack: PromptPack) -> None:
    executor = PromptExecutor.__new__(PromptExecutor)
    executor.pack = pack
    output = pack.replay_output("P-FACT-CRITIC")
    output.pop("warnings", None)
    output["result"]["warnings"] = {"message": "wrong container"}

    with pytest.raises(PromptExecutionError, match="Misplaced response-envelope field") as exc_info:
        executor._normalize_output("P-FACT-CRITIC", output)
    assert "/result/warnings" in " ".join(exc_info.value.validation_errors)


def test_targeted_repair_list_is_rejected_without_attribute_error(pack: PromptPack) -> None:
    executor = PromptExecutor.__new__(PromptExecutor)
    executor.pack = pack
    output = pack.replay_output("P-TARGETED-REPAIR")
    output["result"]["repaired_object"] = [{"claim_id": "METRIC-PROJ-001"}]

    with pytest.raises(PromptExecutionError, match="container structure"):
        executor._normalize_output("P-TARGETED-REPAIR", output, {"payload": {}})


def test_null_declared_containers_never_leak_raw_type_errors(pack: PromptPack) -> None:
    executor = PromptExecutor.__new__(PromptExecutor)
    executor.pack = pack
    guard = ProposalQualityGuard()
    failures: list[str] = []
    exercised = 0

    for prompt_id in pack.prompt_ids():
        output = pack.replay_output(prompt_id)
        envelope = pack.replay_input(prompt_id)
        structure_schema = pack.structure_schema(prompt_id, "output")
        for path in _declared_container_paths(output, structure_schema):
            exercised += 1
            malformed = _replace(output, path, None)
            try:
                normalized = executor._normalize_output(prompt_id, malformed, envelope)
                guard.apply(prompt_id, envelope, normalized)
            except PromptExecutionError:
                pass
            except Exception as exc:  # pragma: no cover - regression diagnostic
                failures.append(
                    f"{prompt_id}{_pointer(path)} leaked {type(exc).__name__}: {exc}"
                )

    assert exercised >= 100
    assert not failures, "\n".join(failures[:30])


def test_staged_bridge_envelope_rejects_non_object_roots_and_outputs() -> None:
    with pytest.raises(SystemExit, match="envelope must be a JSON object"):
        require_model_response_envelope([{"output": {}}], label="test-stage")
    with pytest.raises(SystemExit, match="envelope.output must be a JSON object"):
        require_model_response_envelope({"output": [{"unexpected": True}]}, label="test-stage")


def test_stage_status_normalizers_return_schema_rejectable_objects_for_wrong_roots() -> None:
    stage2, report2 = normalize_stage2_candidate([{"unexpected": True}])
    stage3, report3 = normalize_stage3_candidate([{"unexpected": True}])
    assert stage2 == {}
    assert stage3 == {}
    assert report2["unresolved_count"] == 1
    assert report3["unresolved_count"] == 1


def test_required_null_containers_are_never_defaulted_into_valid_outputs(
    pack: PromptPack,
) -> None:
    """A required null container is an omission, not a harmless empty value."""
    from app.contract_registry import required_null_container_errors

    executor = PromptExecutor.__new__(PromptExecutor)
    executor.pack = pack
    failures: list[str] = []
    exercised = 0

    for prompt_id in pack.prompt_ids():
        output = pack.replay_output(prompt_id)
        envelope = pack.replay_input(prompt_id)
        structure_schema = pack.structure_schema(prompt_id, "output")
        output_schema = pack.inlined_schema(prompt_id, "output")
        for path in _declared_container_paths(output, structure_schema):
            malformed = _replace(output, path, None)
            contract_errors = required_null_container_errors(malformed, output_schema)
            if not contract_errors:
                continue
            exercised += 1
            try:
                executor._normalize_output(prompt_id, malformed, envelope)
            except PromptExecutionError as exc:
                if "Required output container is null" not in str(exc):
                    failures.append(
                        f"{prompt_id}{_pointer(path)} raised unexpected controlled error: {exc}"
                    )
            except Exception as exc:  # pragma: no cover - regression diagnostic
                failures.append(
                    f"{prompt_id}{_pointer(path)} leaked {type(exc).__name__}: {exc}"
                )
            else:
                failures.append(
                    f"{prompt_id}{_pointer(path)} was silently defaulted despite being required"
                )

    assert exercised >= 200
    assert not failures, "\n".join(failures[:30])


def test_staged_contract_gateway_injects_field_paths_and_repairs_unique_ownership(tmp_path) -> None:
    from app.staged_contracts import normalize_in_place, prepare_staged_artifact

    schema = {
        "type": "object",
        "properties": {
            "result": {
                "type": "object",
                "properties": {
                    "template": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                        "additionalProperties": False,
                    },
                    "source_fact_exclusions": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["template", "source_fact_exclusions"],
                "additionalProperties": False,
            }
        },
        "required": ["result"],
        "additionalProperties": False,
    }
    request_path = tmp_path / "requests" / "request.json"
    request_path.parent.mkdir()
    request = prepare_staged_artifact(
        request_path,
        {"prompt_id": "P-STAGED", "system_prompt": "base", "output_schema": schema},
    )
    assert "FIELD_OWNERSHIP_CONTRACT:START" in request["system_prompt"]
    assert request["model_contract"]["field_ownership_contract_registry_version"]

    value = {
        "result": {
            "template": {"name": "template", "source_fact_exclusions": ["fact"]}
        }
    }
    report = normalize_in_place(value, schema, contract_id="test-staged")
    assert value["result"]["source_fact_exclusions"] == ["fact"]
    assert "source_fact_exclusions" not in value["result"]["template"]
    assert report["normalized_count"] >= 1


def test_staged_contract_gateway_preserves_required_null_for_strict_rejection() -> None:
    from app.staged_contracts import normalize_in_place

    schema = {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {"type": "string"}}},
        "required": ["items"],
        "additionalProperties": False,
    }
    value = {"items": None}
    report = normalize_in_place(value, schema, contract_id="test-required-null")
    assert value["items"] is None
    assert report["required_null_errors"]


def _declared_scalar_paths(
    value: Any,
    schema: dict[str, Any],
    path: tuple[Any, ...] = (),
) -> list[tuple[Any, ...]]:
    schema = _branch_for(schema, value)
    paths: list[tuple[Any, ...]] = []
    declared = schema.get("type")
    types = [declared] if isinstance(declared, str) else list(declared or [])
    if path and not isinstance(value, (dict, list)) and any(
        item in types for item in ("string", "integer", "number", "boolean", "null")
    ):
        paths.append(path)
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        additional = schema.get("additionalProperties")
        for key, child in value.items():
            child_schema = properties.get(key)
            if child_schema is None and isinstance(additional, dict):
                child_schema = additional
            if isinstance(child_schema, dict):
                paths.extend(_declared_scalar_paths(child, child_schema, (*path, key)))
    elif isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, child in enumerate(value):
                paths.extend(_declared_scalar_paths(child, item_schema, (*path, index)))
    return paths


def test_declared_scalar_positions_reject_container_values_without_leaking_type_errors(
    pack: PromptPack,
) -> None:
    executor = PromptExecutor.__new__(PromptExecutor)
    executor.pack = pack
    failures: list[str] = []
    exercised = 0
    for prompt_id in pack.prompt_ids():
        output = pack.replay_output(prompt_id)
        schema_value = pack.inlined_schema(prompt_id, "output")
        for path in _declared_scalar_paths(output, schema_value):
            for replacement in ({"unexpected": True}, ["unexpected"]):
                malformed = _replace(output, path, replacement)
                exercised += 1
                try:
                    executor._normalize_output(prompt_id, malformed)
                except PromptExecutionError as exc:
                    if "container structure" not in str(exc):
                        failures.append(
                            f"{prompt_id}{_pointer(path)} raised unexpected contract error: {exc}"
                        )
                except Exception as exc:  # pragma: no cover - regression diagnostic
                    failures.append(
                        f"{prompt_id}{_pointer(path)} leaked {type(exc).__name__}: {exc}"
                    )
                else:
                    failures.append(f"{prompt_id}{_pointer(path)} accepted {type(replacement).__name__}")
    assert exercised >= 500
    assert not failures, "\n".join(failures[:30])


def test_global_provenance_binder_rejects_invented_nested_source_refs(pack: PromptPack) -> None:
    executor = PromptExecutor.__new__(PromptExecutor)
    executor.pack = pack
    envelope = pack.replay_input("P-FACT-EXTRACT")
    output = pack.replay_output("P-FACT-EXTRACT")
    output["result"]["fact_candidates"][0]["source_refs"][0] = {
        "source_id": "invented-source-999",
        "source_type": "APPLICATION_GUIDE",
        "document_version_id": "invented-version-999",
        "section_id": "invented-section-999",
        "span_start": 0,
        "span_end": 8,
        "quoted_text": "invented",
        "source_hash": "b" * 64,
        "authority_rank": 95,
        "security_level": "PUBLIC",
    }
    with pytest.raises(PromptExecutionError, match="provenance"):
        executor._normalize_output("P-FACT-EXTRACT", output, envelope)


def test_global_provenance_binder_replaces_model_authored_metadata(pack: PromptPack) -> None:
    executor = PromptExecutor.__new__(PromptExecutor)
    executor.pack = pack
    envelope = pack.replay_input("P-FACT-EXTRACT")
    output = pack.replay_output("P-FACT-EXTRACT")
    original = output["result"]["fact_candidates"][0]["source_refs"][0]
    source_id = original["source_id"]
    original.update({
        "document_version_id": 999,
        "source_hash": "b" * 64,
        "authority_rank": 1,
        "security_level": "PUBLIC",
    })
    normalized = executor._normalize_output("P-FACT-EXTRACT", output, envelope)
    rebound = normalized["result"]["fact_candidates"][0]["source_refs"][0]
    assert rebound["source_id"] == source_id
    assert rebound["document_version_id"] != 999
    assert rebound["source_hash"] != "b" * 64
    assert rebound["authority_rank"] != 1


def test_reference_integrity_rejects_unknown_claim_ids(pack: PromptPack) -> None:
    executor = PromptExecutor.__new__(PromptExecutor)
    executor.pack = pack
    envelope = pack.replay_input("P-ONLINE-RESULT-IMPORT-CRITIC")
    output = pack.replay_output("P-ONLINE-RESULT-IMPORT-CRITIC")
    output["result"]["accepted_claim_ids"] = ["not-a-real-claim"]
    with pytest.raises(PromptExecutionError, match="reference integrity"):
        executor._normalize_output("P-ONLINE-RESULT-IMPORT-CRITIC", output, envelope)


def test_reference_integrity_accepts_named_structured_input_container(
    pack: PromptPack,
) -> None:
    envelope = pack.replay_input("P-REVISION-PLAN")
    output = {
        "status": "PASS",
        "result": {
            "tasks": [{
                "task_id": "task-001",
                "required_input_ids": ["proposal_contract"],
            }],
        },
        "findings": [{
            "code": "PLAN_TEST_FINDING",
            "evidence_refs": ["proposal_contract"],
        }],
    }

    assert validate_reference_ids(output, envelope) == []


def test_reference_integrity_does_not_treat_nested_field_path_as_container_id(
    pack: PromptPack,
) -> None:
    envelope = pack.replay_input("P-REVISION-PLAN")
    output = {
        "status": "PASS",
        "findings": [{
            "code": "PLAN_TEST_FINDING",
            "evidence_refs": ["proposal_contract.mandatory_sections"],
        }],
    }

    errors = validate_reference_ids(output, envelope)

    assert len(errors) == 1
    assert "proposal_contract.mandatory_sections" in errors[0]


def test_reference_alias_normalizer_binds_unresolved_descriptor_suffix_only():
    envelope = {
        "payload": {
            "argument_graph": {
                "nodes": [
                    {"node_id": "BASE-001"},
                    {"node_id": "INNO-004"},
                ],
            },
        },
    }
    output = {
        "result": {
            "blueprint": {
                "unresolved_slot_ids": [
                    "BASE-001-UNKNOWN",
                    "INNO-004-ABSENT-FACT",
                ],
                "required_evidence_ids": ["BASE-001-UNKNOWN"],
            },
        },
    }

    normalized, report = normalize_reference_id_aliases(output, envelope)

    blueprint = normalized["result"]["blueprint"]
    assert blueprint["unresolved_slot_ids"] == ["BASE-001", "INNO-004"]
    assert blueprint["required_evidence_ids"] == ["BASE-001-UNKNOWN"]
    assert report["normalized_count"] == 2


def test_reference_alias_normalizer_binds_evidence_field_path_to_entity_only():
    envelope = {
        "payload": {
            "section_contract": {
                "section_contract_id": "SC-001",
                "unique_information_keys": ["key-001"],
            },
        },
    }
    output = {
        "findings": [{
            "code": "BLUEPRINT_TEST",
            "evidence_refs": ["SC-001.unique_information_keys.0"],
        }],
        "result": {
            "required_input_ids": ["SC-001.unique_information_keys.0"],
        },
    }

    normalized, report = normalize_reference_id_aliases(output, envelope)

    assert normalized["findings"][0]["evidence_refs"] == ["SC-001"]
    assert normalized["result"]["required_input_ids"] == [
        "SC-001.unique_information_keys.0"
    ]
    assert report["normalized_count"] == 1


def test_reference_alias_normalizer_binds_colon_evidence_path_to_entity_only():
    envelope = {
        "payload": {
            "section_contract": {
                "section_contract_id": "SC-001",
                "required_argument_roles": ["CONTEXT", "METHOD"],
            },
            "blueprint_candidate": {
                "paragraphs": [{"paragraph_id": "P-ABS-002"}],
            },
        },
    }
    output = {
        "findings": [{
            "code": "BLUEPRINT_TEST",
            "evidence_refs": [
                "SC-001:required_argument_roles.CONTEXT.METHOD",
                "P-ABS-002:argument_role.RESEARCH_QUESTION",
            ],
        }],
    }

    normalized, report = normalize_reference_id_aliases(output, envelope)

    assert normalized["findings"][0]["evidence_refs"] == [
        "SC-001",
        "P-ABS-002",
    ]
    assert report["normalized_count"] == 2
    assert validate_reference_ids(normalized, envelope) == []


def test_reference_integrity_allows_propagating_input_reference_not_new_output():
    envelope = {
        "payload": {
            "original_object": {
                "required_evidence_ids": ["FACT-UPSTREAM-001"],
            },
        },
    }
    preserved = {
        "status": "PASS",
        "result": {
            "required_evidence_ids": ["FACT-UPSTREAM-001"],
        },
    }
    invented = {
        "status": "PASS",
        "result": {
            "required_evidence_ids": ["FACT-NEW-999"],
        },
    }

    assert validate_reference_ids(preserved, envelope) == []
    errors = validate_reference_ids(invented, envelope)
    assert len(errors) == 1
    assert "FACT-NEW-999" in errors[0]


def _stage_reference_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["source_registry", "facts", "checked_item_ids"],
        "properties": {
            "source_registry": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["source_id"],
                    "properties": {"source_id": {"type": "string"}},
                },
            },
            "facts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["item_id", "source_refs"],
                    "properties": {
                        "item_id": {"type": "string"},
                        "source_refs": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
            },
            "checked_item_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
    }


def _write_latest_stage_request(run_dir: Path) -> None:
    request_dir = run_dir / "requests"
    request_dir.mkdir(parents=True)
    (request_dir / "001_request.json").write_text(
        json.dumps(
            {
                "input_envelope": {
                    "source_registry": [{"source_id": "SRC-001"}],
                    "candidate": {"facts": [{"item_id": "ITEM-001"}]},
                }
            }
        ),
        encoding="utf-8",
    )


def test_staged_contract_rejects_unknown_source_and_entity_references(tmp_path: Path) -> None:
    _write_latest_stage_request(tmp_path)
    value = {
        "source_registry": [{"source_id": "SRC-001"}],
        "facts": [{"item_id": "ITEM-002", "source_refs": ["SRC-999"]}],
        "checked_item_ids": ["ITEM-999"],
    }
    set_contract_trace_context(tmp_path, "staged-reference-test")
    try:
        report = normalize_in_place(
            value,
            _stage_reference_schema(),
            contract_id="staged:test:reference-integrity",
        )
    finally:
        clear_contract_trace_context()
    errors = contract_validation_errors(report)
    assert any("SRC-999" in error for error in errors)
    assert any("ITEM-999" in error for error in errors)


def test_staged_contract_accepts_input_backed_references(tmp_path: Path) -> None:
    _write_latest_stage_request(tmp_path)
    value = {
        "source_registry": [{"source_id": "SRC-001"}],
        "facts": [{"item_id": "ITEM-002", "source_refs": ["SRC-001"]}],
        "checked_item_ids": ["ITEM-001", "ITEM-002"],
    }
    set_contract_trace_context(tmp_path, "staged-reference-test")
    try:
        report = normalize_in_place(
            value,
            _stage_reference_schema(),
            contract_id="staged:test:reference-integrity",
        )
    finally:
        clear_contract_trace_context()
    assert not contract_validation_errors(report)


def test_all_replay_outputs_pass_full_contract_normalization(pack: PromptPack) -> None:
    executor = PromptExecutor.__new__(PromptExecutor)
    executor.pack = pack
    executor.db = None
    failures: list[str] = []
    for prompt_id in pack.prompt_ids():
        try:
            executor._normalize_output(
                prompt_id,
                pack.replay_output(prompt_id),
                pack.replay_input(prompt_id),
            )
        except Exception as exc:  # pragma: no cover - aggregate diagnostic
            failures.append(f"{prompt_id}: {type(exc).__name__}: {exc}")
    assert not failures, "\n".join(failures)


def test_all_prompt_inputs_accept_attached_trusted_source_catalog(pack: PromptPack) -> None:
    failures: list[str] = []
    for prompt_id in pack.prompt_ids():
        envelope = pack.replay_input(prompt_id)
        prepared = attach_trusted_source_catalog(envelope)
        errors = pack.validate(prompt_id, "input", prepared)
        if errors:
            failures.append(f"{prompt_id}: {errors[:3]}")
            continue
        catalog = prepared.get("trusted_source_catalog") or []
        source_ids = [item.get("source_id") for item in catalog if isinstance(item, dict)]
        if not source_ids or len(source_ids) != len(set(source_ids)):
            failures.append(f"{prompt_id}: missing or duplicate trusted source IDs")
    assert not failures, "\n".join(failures)


def test_safe_package_critic_field_name_sources_bind_to_stable_input_objects(
    pack: PromptPack,
) -> None:
    envelope = pack.replay_input("P-SAFE-ONLINE-PACKAGE-CRITIC")
    prepared = attach_trusted_source_catalog(envelope)
    catalog_by_path = {
        item["object_path"]: item
        for item in prepared["trusted_source_catalog"]
        if isinstance(item, dict)
    }
    assert catalog_by_path["payload.security_policy"]["source_id"] == "security-001"
    scan_id = catalog_by_path["payload.deterministic_scan"]["source_id"]
    assert scan_id.startswith("input-p-safe-online-package-critic-deterministic-scan-")

    output = pack.replay_output("P-SAFE-ONLINE-PACKAGE-CRITIC")
    output["source_refs"] = [
        {"source_id": "deterministic_scan"},
        {"source_id": "security_policy"},
    ]
    executor = PromptExecutor.__new__(PromptExecutor)
    executor.pack = pack
    executor.db = None

    normalized = executor._normalize_output(
        "P-SAFE-ONLINE-PACKAGE-CRITIC",
        output,
        prepared,
    )

    refs = {item["source_id"]: item for item in normalized["source_refs"]}
    assert refs[scan_id]["source_type"] == "EVIDENCE_MATERIAL"
    assert refs[scan_id]["document_version_id"] is None
    assert refs["security-001"]["source_type"] == "CONTRACT"
    assert refs["security-001"]["source_hash"] == "a" * 64
    assert pack.validate("P-SAFE-ONLINE-PACKAGE-CRITIC", "output", normalized) == []


def test_trusted_source_catalog_is_stable_and_contains_no_input_prose(pack: PromptPack) -> None:
    envelope = pack.replay_input("P-SAFE-ONLINE-PACKAGE-CRITIC")
    first = attach_trusted_source_catalog(envelope)["trusted_source_catalog"]
    second = attach_trusted_source_catalog(copy.deepcopy(envelope))["trusted_source_catalog"]
    assert first == second
    serialized = json.dumps(first, ensure_ascii=False)
    assert "通用研究需求" not in serialized
    assert "真实项目名称" not in serialized


def test_every_referenceable_payload_object_has_one_resolvable_field_name_alias(
    pack: PromptPack,
) -> None:
    failures: list[str] = []
    exercised = 0
    for prompt_id in pack.prompt_ids():
        prepared = attach_trusted_source_catalog(pack.replay_input(prompt_id))
        for field_name, value in (prepared.get("payload") or {}).items():
            if not isinstance(value, (dict, list)):
                continue
            exercised += 1
            normalized, report = bind_trusted_source_refs(
                {"source_refs": [{"source_id": field_name}]},
                prepared,
            )
            if report.get("errors"):
                failures.append(f"{prompt_id}.payload.{field_name}: {report['errors']}")
                continue
            resolved = normalized["source_refs"][0]["source_id"]
            catalog_ids = {
                item["source_id"]
                for item in prepared["trusted_source_catalog"]
                if isinstance(item, dict)
            }
            if resolved not in catalog_ids:
                failures.append(
                    f"{prompt_id}.payload.{field_name}: resolved to unregistered {resolved}"
                )
    assert exercised >= 200
    assert not failures, "\n".join(failures[:30])


def test_all_prompts_reject_invented_root_provenance(pack: PromptPack) -> None:
    executor = PromptExecutor.__new__(PromptExecutor)
    executor.pack = pack
    executor.db = None
    invented = {
        "source_id": "invented-source-999",
        "source_type": "APPLICATION_GUIDE",
        "document_version_id": "invented-version-999",
        "section_id": None,
        "span_start": None,
        "span_end": None,
        "quoted_text": None,
        "source_hash": "b" * 64,
        "authority_rank": 95,
        "security_level": "PUBLIC",
    }
    accepted: list[str] = []
    for prompt_id in pack.prompt_ids():
        output = pack.replay_output(prompt_id)
        output["source_refs"] = [dict(invented)]
        try:
            executor._normalize_output(prompt_id, output, pack.replay_input(prompt_id))
        except PromptExecutionError:
            continue
        accepted.append(prompt_id)
    assert not accepted, f"invented provenance accepted by: {accepted}"


def test_gate_confirmed_research_need_source_prefix_is_rebound_as_user_confirmation(
    pack: PromptPack,
) -> None:
    envelope = pack.replay_input("P-SAFE-ONLINE-PACKAGE")
    need = envelope["payload"]["research_need"]
    envelope["payload"]["human_resolutions"] = [{
        "resolution_id": "human-wf3-001",
        "gate_id": "gate-wf3-001",
        "prompt_id": "P-SAFE-ONLINE-PACKAGE",
        "question_id": "wf3-research-question",
        "question": "需要联网检索并核验的公开问题是什么？",
        "target_paths": ["research_need.question"],
        "answer": need["question"],
        "decided_by": "pytest",
        "decided_role": "PROJECT_OWNER",
    }]
    output = pack.replay_output("P-SAFE-ONLINE-PACKAGE")
    output["source_refs"] = [{
        "source_id": f"source-{need['need_id']}",
        "source_type": "MODEL_INFERENCE",
        "document_version_id": 1,
        "section_id": None,
        "span_start": None,
        "span_end": None,
        "quoted_text": None,
        "source_hash": None,
        "authority_rank": 60,
        "security_level": "INTERNAL",
    }]
    executor = PromptExecutor.__new__(PromptExecutor)
    executor.pack = pack
    executor.db = None

    normalized = executor._normalize_output(
        "P-SAFE-ONLINE-PACKAGE",
        output,
        envelope,
    )

    source_ref = normalized["source_refs"][0]
    assert source_ref["source_id"] == need["need_id"]
    assert source_ref["source_type"] == "USER_CONFIRMATION"
    assert source_ref["document_version_id"] is None
    assert source_ref["authority_rank"] == 100
    assert source_ref["source_hash"]
    assert pack.validate("P-SAFE-ONLINE-PACKAGE", "output", normalized) == []


def test_source_prefix_alias_binding_applies_to_nested_refs_for_all_prompts(
    pack: PromptPack,
) -> None:
    envelope = pack.replay_input("P-FACT-EXTRACT")
    output = pack.replay_output("P-FACT-EXTRACT")
    nested_ref = output["result"]["fact_candidates"][0]["source_refs"][0]
    nested_ref["source_id"] = "source-src-001"
    nested_ref["source_type"] = "MODEL_INFERENCE"
    nested_ref["authority_rank"] = 1
    executor = PromptExecutor.__new__(PromptExecutor)
    executor.pack = pack
    executor.db = None

    normalized = executor._normalize_output("P-FACT-EXTRACT", output, envelope)

    rebound = normalized["result"]["fact_candidates"][0]["source_refs"][0]
    assert rebound["source_id"] == "src-001"
    assert rebound["source_type"] == "USER_CONFIRMATION"
    assert rebound["authority_rank"] == 100
    assert pack.validate("P-FACT-EXTRACT", "output", normalized) == []


@pytest.mark.parametrize(
    "prefix",
    [
        "source-", "Source:", "SOURCE/", "source_",
        "src-", "Src:", "SRC/", "src_",
        "ref-", "Ref:", "REF/", "ref_",
    ],
)
def test_registered_source_presentation_prefix_variants_are_canonicalized(prefix: str) -> None:
    envelope = {
        "security_context": {"input_max_security_level": "INTERNAL"},
        "payload": {
            "research_need": {
                "need_id": "need-prefix-001",
                "question": "公开问题",
                "reason_online_needed": "需要公开证据",
                "desired_output": "公开来源清单",
            },
            "human_resolutions": [{
                "resolution_id": "human-prefix-001",
                "gate_id": "gate-prefix-001",
                "prompt_id": "P-SAFE-ONLINE-PACKAGE",
                "question_id": "wf3-research-question",
                "question": "需要联网检索并核验的公开问题是什么？",
                "target_paths": ["research_need.question"],
                "answer": "公开问题",
                "decided_by": "pytest",
                "decided_role": "PROJECT_OWNER",
            }],
        },
    }
    output = {"source_refs": [{"source_id": prefix + "need-prefix-001"}]}

    normalized, report = bind_trusted_source_refs(output, envelope)

    assert report["errors"] == []
    assert normalized["source_refs"][0]["source_id"] == "need-prefix-001"
    assert normalized["source_refs"][0]["source_type"] == "USER_CONFIRMATION"


def test_source_alias_resolution_is_exact_first_and_never_fuzzy() -> None:
    from app.output_integrity import (
    bind_trusted_source_refs,
    normalize_reference_id_aliases,
    validate_reference_ids,
)

    envelope = {
        "security_context": {"input_max_security_level": "INTERNAL"},
        "payload": {
            "sources": [
                {
                    "source_id": "need-001",
                    "source_type": "MODEL_INFERENCE",
                    "authority_rank": 60,
                    "security_level": "INTERNAL",
                },
                {
                    "source_id": "source-need-001",
                    "source_type": "USER_CONFIRMATION",
                    "authority_rank": 100,
                    "security_level": "INTERNAL",
                },
            ]
        },
    }
    exact, exact_report = bind_trusted_source_refs(
        {"source_refs": [{"source_id": "source-need-001"}]},
        envelope,
    )
    assert exact_report["errors"] == []
    assert exact["source_refs"][0]["source_id"] == "source-need-001"
    assert exact["source_refs"][0]["source_type"] == "USER_CONFIRMATION"

    unknown, unknown_report = bind_trusted_source_refs(
        {"source_refs": [{"source_id": "source-need-002"}]},
        envelope,
    )
    assert unknown["source_refs"][0]["source_id"] == "source-need-002"
    assert unknown_report["unresolved_count"] == 1


def test_unknown_source_id_rebinds_only_by_unique_trusted_hash() -> None:
    trusted_hash = "a" * 64
    envelope = {
        "security_context": {"input_max_security_level": "PUBLIC"},
        "payload": {
            "sources": [
                {
                    "source_id": "public-src-06192d512e864ed2",
                    "source_type": "PUBLIC_SOURCE",
                    "source_hash": trusted_hash,
                    "authority_rank": 78,
                    "security_level": "PUBLIC",
                }
            ]
        },
    }
    output = {
        "source_refs": [
            {
                "source_id": "public-src-06192d512e8642",
                "source_hash": trusted_hash,
            }
        ]
    }

    normalized, report = bind_trusted_source_refs(output, envelope)

    assert report["errors"] == []
    assert normalized["source_refs"][0]["source_id"] == "public-src-06192d512e864ed2"
    assert report["changes"][0]["alias_kind"] == "UNIQUE_SOURCE_HASH"


def test_duplicate_trusted_hash_does_not_resolve_unknown_source_id() -> None:
    trusted_hash = "b" * 64
    envelope = {
        "security_context": {"input_max_security_level": "PUBLIC"},
        "payload": {
            "sources": [
                {
                    "source_id": source_id,
                    "source_type": "PUBLIC_SOURCE",
                    "source_hash": trusted_hash,
                    "authority_rank": 78,
                    "security_level": "PUBLIC",
                }
                for source_id in ("public-src-one", "public-src-two")
            ]
        },
    }

    normalized, report = bind_trusted_source_refs(
        {
            "source_refs": [
                {
                    "source_id": "public-src-truncated",
                    "source_hash": trusted_hash,
                }
            ]
        },
        envelope,
    )

    assert normalized["source_refs"][0]["source_id"] == "public-src-truncated"
    assert report["unresolved_count"] == 1


def test_duplicate_document_hash_rebinds_to_exact_trusted_section() -> None:
    trusted_hash = "c" * 64
    section_id = "section-exact-001"
    shared = {
        "source_type": "CURRENT_PROPOSAL",
        "document_version_id": "document-version-001",
        "section_id": section_id,
        "source_hash": trusted_hash,
        "authority_rank": 85,
        "security_level": "INTERNAL",
    }
    envelope = {
        "security_context": {"input_max_security_level": "INTERNAL"},
        "payload": {
            "sources": [
                {"source_id": "document-001", **shared},
                {"source_id": section_id, **shared},
            ]
        },
    }

    normalized, report = bind_trusted_source_refs(
        {
            "source_refs": [
                {
                    "source_id": "provider-invented-label",
                    "section_id": section_id,
                    "source_hash": trusted_hash,
                }
            ]
        },
        envelope,
    )

    assert report["errors"] == []
    assert normalized["source_refs"][0]["source_id"] == section_id
    assert report["changes"][0]["alias_kind"] == "EXACT_SECTION_HASH"


def test_duplicate_document_hash_without_requested_section_uses_unique_section() -> None:
    trusted_hash = "e" * 64
    section_id = "section-exact-003"
    shared = {
        "source_type": "CURRENT_PROPOSAL",
        "document_version_id": "document-version-003",
        "section_id": section_id,
        "source_hash": trusted_hash,
        "authority_rank": 85,
        "security_level": "INTERNAL",
    }
    envelope = {
        "security_context": {"input_max_security_level": "INTERNAL"},
        "payload": {
            "sources": [
                {"source_id": "document-003", **shared},
                {"source_id": section_id, **shared},
            ]
        },
    }

    normalized, report = bind_trusted_source_refs(
        {
            "source_refs": [{
                "source_id": "RC-002",
                "source_hash": trusted_hash,
            }]
        },
        envelope,
    )

    assert report["errors"] == []
    assert normalized["source_refs"][0]["source_id"] == section_id
    assert report["changes"][0]["alias_kind"] == "UNIQUE_SECTION_FOR_HASH"


def test_unknown_source_label_rebinds_to_exact_trusted_section_id() -> None:
    section_id = "section-exact-002"
    envelope = {
        "security_context": {"input_max_security_level": "INTERNAL"},
        "payload": {
            "sources": [{
                "source_id": section_id,
                "source_type": "CURRENT_PROPOSAL",
                "document_version_id": "document-version-002",
                "section_id": section_id,
                "source_hash": "d" * 64,
                "authority_rank": 85,
                "security_level": "INTERNAL",
            }]
        },
    }

    normalized, report = bind_trusted_source_refs(
        {
            "source_refs": [{
                "source_id": "RC-002",
                "source_type": "CURRENT_PROPOSAL",
                "section_id": section_id,
            }]
        },
        envelope,
    )

    assert report["errors"] == []
    assert normalized["source_refs"][0]["source_id"] == section_id
    assert report["changes"][0]["alias_kind"] == "EXACT_SECTION_ID"


def test_missing_item_descriptors_do_not_require_existing_entities() -> None:
    from app.output_integrity import validate_reference_ids

    assert validate_reference_ids(
        {
            "result": {
                "chapter_readiness": [{
                    "missing_item_ids": [
                        "RC-001.methods",
                        "TEAM_MEMBER",
                        "BASE-006",
                    ]
                }]
            }
        },
        {},
    ) == []


def test_cross_reference_presentation_prefix_is_normalized_only_to_visible_id() -> None:
    envelope = {
        "payload": {
            "claims": [
                {"claim_id": "claim-visible-001"},
            ]
        }
    }
    output = {
        "accepted_claim_ids": ["ref-claim-visible-001"],
        "rejected_claim_ids": ["ref-claim-unknown-999"],
    }

    normalized, report = normalize_reference_id_aliases(output, envelope)

    assert normalized["accepted_claim_ids"] == ["claim-visible-001"]
    assert normalized["rejected_claim_ids"] == ["ref-claim-unknown-999"]
    assert report["normalized_count"] == 1
    errors = validate_reference_ids(normalized, envelope)
    assert len(errors) == 1
    assert "ref-claim-unknown-999" in errors[0]


def test_staged_source_prefix_alias_is_normalized_before_integrity_validation() -> None:
    from app.output_integrity import (
        normalize_staged_source_ref_aliases,
        validate_staged_reference_integrity,
    )

    output = {"items": [{"source_refs": ["source-src-001"]}]}
    trusted = {"sources": [{"source_id": "src-001"}]}
    normalized, report = normalize_staged_source_ref_aliases(output, trusted)

    assert normalized["items"][0]["source_refs"] == ["src-001"]
    assert report["normalized_count"] == 1
    assert validate_staged_reference_integrity(normalized, trusted) == []


def test_all_replay_source_refs_accept_only_registered_presentation_prefix_aliases(
    pack: PromptPack,
) -> None:
    executor = PromptExecutor.__new__(PromptExecutor)
    executor.pack = pack
    executor.db = None
    failures: list[str] = []
    exercised = 0

    def iter_refs(node: Any):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "source_refs" and isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict) and isinstance(item.get("source_id"), str) and item["source_id"]:
                            yield item
                yield from iter_refs(value)
        elif isinstance(node, list):
            for item in node:
                yield from iter_refs(item)

    for prompt_id in pack.prompt_ids():
        envelope = pack.replay_input(prompt_id)
        output = pack.replay_output(prompt_id)
        refs = list(iter_refs(output))
        if not refs:
            continue
        for ref in refs:
            ref["source_id"] = "Source-" + ref["source_id"]
        exercised += len(refs)
        try:
            normalized = executor._normalize_output(prompt_id, output, envelope)
        except Exception as exc:  # pragma: no cover - regression diagnostic
            failures.append(f"{prompt_id}: {type(exc).__name__}: {exc}")
            continue
        schema_errors = pack.validate(prompt_id, "output", normalized)
        if schema_errors:
            failures.append(f"{prompt_id}: {schema_errors[:3]}")

    assert exercised >= 10
    assert not failures, "\n".join(failures)


def test_staged_contract_gateway_applies_source_alias_normalization(tmp_path) -> None:
    from app.staged_contracts import normalize_in_place

    run_dir = tmp_path / "stage-run"
    request_dir = run_dir / "requests"
    request_dir.mkdir(parents=True)
    (request_dir / "request.json").write_text(
        json.dumps(
            {
                "input_envelope": {
                    "sources": [{"source_id": "src-stage-001"}],
                    "claims": [{"claim_id": "claim-stage-001"}],
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source_refs": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "accepted_claim_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["source_refs", "accepted_claim_ids"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }
    value = {
        "items": [{
            "source_refs": ["source-src-stage-001"],
            "accepted_claim_ids": ["ref-claim-stage-001"],
        }]
    }
    set_contract_trace_context(run_dir, "stage-source-alias")
    try:
        report = normalize_in_place(value, schema, contract_id="stage-source-alias")
    finally:
        clear_contract_trace_context()

    assert value["items"][0]["source_refs"] == ["src-stage-001"]
    assert value["items"][0]["accepted_claim_ids"] == ["claim-stage-001"]
    assert report["source_alias_report"]["normalized_count"] == 1
    assert report["reference_alias_report"]["normalized_count"] == 1
    assert report["reference_integrity_errors"] == []
