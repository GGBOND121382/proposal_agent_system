from __future__ import annotations

import copy
from pathlib import Path

import pytest

from app.contracts import ReferenceSemantic, get_semantic_contract
from app.executor import PromptExecutionError, PromptExecutor
from app.output_integrity import validate_reference_ids
from app.pack import PromptPack


ROOT = Path(__file__).resolve().parents[1]


def _executor() -> PromptExecutor:
    executor = PromptExecutor.__new__(PromptExecutor)
    executor.pack = PromptPack(ROOT / "prompt_pack")
    executor.db = None
    return executor


def test_every_schema_reference_field_is_contract_annotated() -> None:
    pack = PromptPack(ROOT / "prompt_pack")
    contract = get_semantic_contract()
    assert contract.version == "2.4.1"
    assert contract.rule_registry_version == "1.3.0"
    assert len(contract.reference_field_semantics) >= 90
    # PromptPack construction performs the complete inlined-schema coverage and
    # x-reference-semantic agreement check.  This assertion keeps the object live.
    assert pack.prompt_ids()


def test_diagnostic_text_is_not_forced_into_an_entity_id() -> None:
    pack = PromptPack(ROOT / "prompt_pack")
    output = pack.replay_output("P-WRITE-BLUEPRINT-CRITIC")
    output["result"]["invalid_slot_refs"] = [
        "段落 P-1 的 fact_slots 指向了当前输入中不存在的事实"
    ]
    errors = pack.validate("P-WRITE-BLUEPRINT-CRITIC", "output", output)
    assert errors == []
    assert (
        get_semantic_contract().field_semantic("invalid_slot_refs")
        is ReferenceSemantic.UNRESOLVED_DESCRIPTOR
    )


def test_executor_preserves_critic_status_verdict_and_findings() -> None:
    executor = _executor()
    output = executor.pack.replay_output("P-WRITE-BLUEPRINT-CRITIC")
    output["status"] = "REVISE"
    output["result"]["verdict"] = "REVISE"
    output["findings"] = [{
        "code": "CONTRACT_CONFLICT",
        "severity": "ERROR",
        "message": "critic finding must remain immutable",
        "evidence_refs": [],
        "suggested_fix": "repair producer output",
        "owner": "WRITING_AGENT",
    }]
    before = copy.deepcopy(output)
    normalized = executor._normalize_output("P-WRITE-BLUEPRINT-CRITIC", output)
    assert normalized["status"] == before["status"]
    assert normalized["result"]["verdict"] == before["result"]["verdict"]
    assert normalized["findings"] == before["findings"]


def test_executor_does_not_create_project_entities_or_rewrite_ids() -> None:
    executor = _executor()
    output = executor.pack.replay_output("P-PROJECT-DEFINITION-EXTRACT")
    output["result"]["project_definition"]["items"] = []
    output["result"]["project_definition"]["relations"] = []
    output["result"]["unmapped_source_spans"] = ["natural language is not an id"]
    before = copy.deepcopy(output["result"])
    normalized = executor._normalize_output("P-PROJECT-DEFINITION-EXTRACT", output)
    assert normalized["result"]["project_definition"]["items"] == []
    assert normalized["result"]["project_definition"]["relations"] == []
    assert normalized["result"]["unmapped_source_spans"] == before["unmapped_source_spans"]


def test_enum_normalization_never_inferrs_other_semantic_fields() -> None:
    output = {
        "warnings": [],
        "result": {
            "fact": {
                "knowledge_status": "PROJECT_DESIGN",
                "claim_type": "FACT",
                "temporal_status": "UNKNOWN",
                "source_refs": [],
            }
        },
    }
    normalized = PromptExecutor._normalize_semantic_enum_tree(output)
    fact = normalized["result"]["fact"]
    assert fact["claim_type"] == "FACT"
    assert fact["temporal_status"] == "UNKNOWN"


def test_dangling_reference_is_rejected_without_synthesizing_entity() -> None:
    output = {
        "result": {
            "paragraphs": [{
                "paragraph_id": "P-1",
                "required_evidence_ids": ["MISSING-FACT"],
            }]
        }
    }
    errors = validate_reference_ids(output, {"payload": {"facts": []}})
    assert errors
    assert "MISSING-FACT" in errors[0]
    assert output["result"]["paragraphs"][0]["required_evidence_ids"] == ["MISSING-FACT"]


def test_runtime_has_no_project_specific_identifier_repair() -> None:
    prohibited = ("RC-", "RQ-", "OBJ-", "EXP-", "F-077", "new-abstract")
    for relative in ("app/executor.py", "app/output_integrity.py"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert not any(token in text for token in prohibited), relative
