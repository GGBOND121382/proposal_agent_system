from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from app.executor import PromptExecutionError, PromptExecutor
from app.output_integrity import validate_reference_ids
from app.pack import PromptPack


ROOT = Path(__file__).resolve().parents[1]


def _contract_pair() -> tuple[dict, dict]:
    pack = PromptPack(ROOT / "prompt_pack")
    return (
        pack.replay_input("P-TARGETED-REPAIR"),
        pack.replay_output("P-TARGETED-REPAIR", "normal"),
    )


def test_targeted_repair_v3_requires_instance_ids_and_never_defaults_missing_business_fields() -> None:
    pack = PromptPack(ROOT / "prompt_pack")
    envelope, output = _contract_pair()

    assert pack.validate("P-TARGETED-REPAIR", "input", envelope) == []
    assert pack.validate("P-TARGETED-REPAIR", "output", output) == []
    PromptExecutor._validate_output_semantics(
        "P-TARGETED-REPAIR", envelope, output
    )

    malformed = copy.deepcopy(output)
    malformed["result"].pop("unresolved_finding_ids")
    errors = pack.validate("P-TARGETED-REPAIR", "output", malformed)
    assert any("unresolved_finding_ids" in error for error in errors)
    assert "unresolved_finding_ids" not in malformed["result"]


def test_duplicate_finding_codes_are_closed_by_distinct_instance_identity() -> None:
    envelope, output = _contract_pair()
    duplicate = copy.deepcopy(envelope["payload"]["findings_to_repair"][0])
    duplicate["finding_instance_id"] = "finding-replay-002"
    duplicate["target_path_or_span"] = "/content/paragraphs/1"
    envelope["payload"]["findings_to_repair"].append(duplicate)
    output["result"]["resolved_finding_ids"] = [
        "finding-replay-001",
        "finding-replay-002",
    ]

    PromptExecutor._validate_output_semantics(
        "P-TARGETED-REPAIR", envelope, output
    )

    output["result"]["resolved_finding_ids"] = ["finding-replay-001"]
    with pytest.raises(PromptExecutionError) as exc_info:
        PromptExecutor._validate_output_semantics(
            "P-TARGETED-REPAIR", envelope, output
        )
    assert any(
        "finding-replay-002" in error
        for error in exc_info.value.validation_errors
    )


def test_pass_cannot_hide_an_unresolved_finding_instance() -> None:
    envelope, output = _contract_pair()
    output["result"]["resolved_finding_ids"] = []
    output["result"]["unresolved_finding_ids"] = ["finding-replay-001"]

    with pytest.raises(PromptExecutionError) as exc_info:
        PromptExecutor._validate_output_semantics(
            "P-TARGETED-REPAIR", envelope, output
        )
    assert any(
        "PASS requires unresolved_finding_ids to be empty" in error
        for error in exc_info.value.validation_errors
    )


def test_repair_contract_rejects_real_diff_outside_allowed_paths() -> None:
    envelope, output = _contract_pair()
    output["result"]["repaired_object"]["content"]["unauthorized"] = "new"

    with pytest.raises(PromptExecutionError) as exc_info:
        PromptExecutor._validate_output_semantics(
            "P-TARGETED-REPAIR", envelope, output
        )

    assert any(
        "outside allowed_paths" in error
        for error in exc_info.value.validation_errors
    )


def test_repair_contract_rejects_declared_path_without_real_diff() -> None:
    envelope, output = _contract_pair()
    output["result"]["changed_paths"].append("/content/unused")

    with pytest.raises(PromptExecutionError) as exc_info:
        PromptExecutor._validate_output_semantics(
            "P-TARGETED-REPAIR", envelope, output
        )

    assert any(
        "has no corresponding object diff" in error
        for error in exc_info.value.validation_errors
    )


def test_repair_contract_requires_exact_protected_hash_receipt() -> None:
    envelope, output = _contract_pair()
    output["result"]["unchanged_protected_hashes"][0]["hash"] = "b" * 64

    with pytest.raises(PromptExecutionError) as exc_info:
        PromptExecutor._validate_output_semantics(
            "P-TARGETED-REPAIR", envelope, output
        )

    assert any(
        "must exactly echo payload.protected_hashes" in error
        for error in exc_info.value.validation_errors
    )


def test_repair_entity_refs_accept_original_producer_inherited_namespace() -> None:
    envelope, output = _contract_pair()
    envelope["payload"]["original_object"]["content"] = {
        "paragraphs": [{"paragraph_id": "P-1", "evidence_ids": []}]
    }
    envelope["payload"]["allowed_paths"] = [
        "/content/paragraphs/0/evidence_ids"
    ]
    envelope["payload"]["inherited_source_catalog"][0]["source_id"] = "RC-002"
    output["result"]["repaired_object"] = {
        "content": {
            "paragraphs": [
                {"paragraph_id": "P-1", "evidence_ids": ["RC-002"]}
            ]
        }
    }
    output["result"]["changed_paths"] = [
        "/content/paragraphs/0/evidence_ids"
    ]

    assert validate_reference_ids(output, envelope) == []
    PromptExecutor._validate_output_semantics(
        "P-TARGETED-REPAIR", envelope, output
    )


def test_non_repair_catalog_source_id_does_not_authorize_entity_ref() -> None:
    envelope, output = _contract_pair()
    envelope["prompt_id"] = "P-WRITE-CONTENT"
    output["result"]["repaired_object"] = {
        "content": {"evidence_ids": ["catalog-source-only"]}
    }
    envelope["payload"]["inherited_source_catalog"][0][
        "source_id"
    ] = "catalog-source-only"

    errors = validate_reference_ids(output, envelope)

    assert any("catalog-source-only" in error for error in errors)


def test_prompt_and_model_capability_declare_the_real_contract_mode() -> None:
    prompt = (ROOT / "prompt_pack/prompts/repair/targeted_repair.md").read_text(
        encoding="utf-8"
    )
    assert "`repair_targets`" in prompt
    assert "`reference_context`" in prompt
    assert "`APPLY`" in prompt
    assert "`ESCALATE`" in prompt
    assert "不要重新输出原对象" in prompt
    assert "finding_instance_id" not in prompt
    assert "protected_hash" not in prompt

    config = yaml.safe_load(
        (ROOT / "prompt_pack/config/models.yaml").read_text(encoding="utf-8")
    )
    for model in config["models"]:
        capabilities = model["capabilities"]
        assert capabilities["strict_json_schema"] == "PROVIDER_NEGOTIATED"
        assert capabilities["json_object_fallback"] is True
