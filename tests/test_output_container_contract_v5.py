from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from app.executor import PromptExecutionError
from app.runtime_api import PromptExecutor
from app.pack import PromptPack
from app.proposal_quality import ProposalQualityGuard
from app.staged_contracts import require_model_response_envelope
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
