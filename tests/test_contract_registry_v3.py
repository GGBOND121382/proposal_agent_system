from __future__ import annotations

import copy
import json
from pathlib import Path

from app.contract_registry import (
    CONTRACT_REGISTRY_VERSION,
    augment_prompt_with_enum_contract,
    normalize_against_schema,
)
from app.status_ontology import normalize_stage2_candidate, normalize_stage3_candidate

ROOT = Path(__file__).resolve().parents[1]


def load(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def test_claim_type_is_normalized_independently_from_legal_knowledge_status() -> None:
    schema = {
        "type": "object",
        "properties": {
            "knowledge_status": {"enum": ["DOCUMENT_EXTRACTED", "ESTIMATED"]},
            "claim_type": {"enum": ["FACT", "PLAN"]},
            "temporal_status": {"enum": ["CURRENT", "PLANNED"]},
        },
    }
    raw = {
        "knowledge_status": "DOCUMENT_EXTRACTED",
        "claim_type": "PROJECT_DESIGN",
        "temporal_status": "CURRENT",
    }
    normalized, report = normalize_against_schema(raw, schema, contract_id="test")
    assert normalized == {
        "knowledge_status": "DOCUMENT_EXTRACTED",
        "claim_type": "PLAN",
        "temporal_status": "CURRENT",
    }
    assert report["normalized_count"] == 1


def test_cross_field_swap_is_repaired_only_when_both_values_fit_other_fields() -> None:
    schema = {
        "type": "object",
        "properties": {
            "knowledge_status": {"enum": ["DOCUMENT_EXTRACTED", "ESTIMATED"]},
            "claim_type": {"enum": ["FACT", "PLAN"]},
        },
    }
    raw = {"knowledge_status": "PLAN", "claim_type": "DOCUMENT_EXTRACTED"}
    normalized, report = normalize_against_schema(raw, schema, contract_id="test")
    assert normalized == {"knowledge_status": "DOCUMENT_EXTRACTED", "claim_type": "PLAN"}
    assert sum(change["rule"] == "FIELD_SWAP" for change in report["changes"]) == 2


def test_unknown_enum_drift_is_not_silently_guessed() -> None:
    schema = {"type": "object", "properties": {"claim_type": {"enum": ["FACT", "PLAN"]}}}
    raw = {"claim_type": "AI_APPROVED_DESIGN"}
    normalized, report = normalize_against_schema(raw, schema, contract_id="test")
    assert normalized == raw
    assert report["normalized_count"] == 0
    assert report["unresolved_count"] == 1


def test_stage2_role_and_time_aliases_are_migrated_before_schema_validation() -> None:
    candidate = {
        "schema_version": "1.1",
        "source_registry": [],
        "facts": [
            {
                "fact_id": "FACT-001",
                "knowledge_status": "DOCUMENT_EXTRACTED",
                "fact_role": "PROJECT_DESIGN",
                "temporal_status": "CURRENT",
                "source_refs": [],
                "assertion_policy": "DIRECT",
                "requires_qualification": False,
            }
        ],
        "writing_permissions": {
            "direct_fact_ids": ["FACT-001"],
            "qualified_fact_ids": [],
            "prohibited_fact_ids": [],
        },
    }
    normalized, report = normalize_stage2_candidate(candidate)
    fact = normalized["facts"][0]
    assert fact["fact_role"] == "DESIGN"
    assert fact["temporal_status"] == "PLANNED"
    assert report["unresolved_count"] == 0


def test_stage3_role_and_time_aliases_are_migrated_independently() -> None:
    candidate = {
        "central_proposition": {
            "knowledge_status": "CONFIRMED",
            "claim_role": "PROJECT_DESIGN",
            "temporal_status": "PROJECT_DESIGN",
        }
    }
    normalized, report = normalize_stage3_candidate(candidate)
    assert normalized["central_proposition"]["claim_role"] == "DESIGN_HYPOTHESIS"
    assert normalized["central_proposition"]["temporal_status"] == "PLANNED"
    assert report["normalized_count"] == 2


def test_stage4_node_status_uses_node_type_context() -> None:
    schema = {
        "type": "object",
        "properties": {
            "nodes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "node_type": {"type": "string"},
                        "status": {
                            "enum": [
                                "CONFIRMED_DESIGN",
                                "PROVISIONAL_TARGET",
                                "TO_BE_VALIDATED",
                                "UNKNOWN",
                                "SUPPORTED",
                            ]
                        },
                    },
                },
            }
        },
    }
    raw = {
        "nodes": [
            {"node_type": "EVALUATION_METRIC", "status": "PROJECT_DESIGN"},
            {"node_type": "NOVEL_MECHANISM", "status": "PROJECT_DESIGN"},
            {"node_type": "TEAM_EVIDENCE", "status": "PROJECT_DESIGN"},
            {"node_type": "MECHANISM", "status": "PROJECT_DESIGN"},
        ]
    }
    normalized, _ = normalize_against_schema(raw, schema, contract_id="stage4")
    assert [node["status"] for node in normalized["nodes"]] == [
        "PROVISIONAL_TARGET",
        "TO_BE_VALIDATED",
        "UNKNOWN",
        "CONFIRMED_DESIGN",
    ]


def test_stage3_to_stage4_implements_relation_reverses_direction() -> None:
    schema = {
        "type": "object",
        "properties": {
            "relations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "from_id": {"type": "string"},
                        "to_id": {"type": "string"},
                        "relation": {"enum": ["REALIZED_BY", "MOTIVATES"]},
                    },
                },
            }
        },
    }
    raw = {"relations": [{"from_id": "RC-1", "to_id": "OBJ-1", "relation": "IMPLEMENTS"}]}
    normalized, report = normalize_against_schema(raw, schema, contract_id="stage4")
    relation = normalized["relations"][0]
    assert relation == {"from_id": "OBJ-1", "to_id": "RC-1", "relation": "REALIZED_BY"}
    assert any(change["rule"] == "RELATION_DIRECTION_ADAPTER" for change in report["changes"])


def test_stage6_claim_status_alias_is_mapped_to_project_plan() -> None:
    schema = load("stage6a_tools/section_draft.schema.json")
    response = load("tests/fixtures/stage6a_sec01_writer_response.json")
    response = copy.deepcopy(response)
    paragraph = response["candidate"]["subsections"][0]["paragraphs"][0]
    paragraph["claim_status"] = "PROJECT_DESIGN"
    normalized, report = normalize_against_schema(response, schema, contract_id="stage6a")
    assert normalized["candidate"]["subsections"][0]["paragraphs"][0]["claim_status"] == "PROJECT_PLAN"
    assert report["unresolved_count"] == 0


def test_critic_decision_vocabularies_are_path_aware() -> None:
    accept_schema = {"type": "object", "properties": {"verdict": {"enum": ["ACCEPT", "REVISE", "BLOCK"]}}}
    reject_schema = {"type": "object", "properties": {"verdict": {"enum": ["ACCEPT", "REVISE", "REJECT"]}}}
    pass_schema = {"type": "object", "properties": {"status": {"enum": ["PASS", "REVISE", "BLOCK"]}}}
    assert normalize_against_schema({"verdict": "PASS"}, accept_schema, contract_id="a")[0]["verdict"] == "ACCEPT"
    assert normalize_against_schema({"verdict": "BLOCK"}, reject_schema, contract_id="b")[0]["verdict"] == "REJECT"
    assert normalize_against_schema({"status": "ACCEPT"}, pass_schema, contract_id="c")[0]["status"] == "PASS"


def test_chain_type_vocabularies_convert_in_both_directions() -> None:
    stage4_schema = {"type": "object", "properties": {"chain_type": {"enum": ["GAP_TO_RQ"]}}}
    pack_schema = {"type": "object", "properties": {"chain_type": {"enum": ["GAP_TO_QUESTION"]}}}
    assert normalize_against_schema({"chain_type": "GAP_TO_QUESTION"}, stage4_schema, contract_id="s4")[0]["chain_type"] == "GAP_TO_RQ"
    assert normalize_against_schema({"chain_type": "GAP_TO_RQ"}, pack_schema, contract_id="pack")[0]["chain_type"] == "GAP_TO_QUESTION"


def test_prompt_contract_is_generated_from_schema() -> None:
    schema = {
        "type": "object",
        "properties": {
            "claim_type": {"enum": ["FACT", "PLAN"]},
            "status": {"enum": ["PASS", "REVISE"]},
        },
    }
    prompt = augment_prompt_with_enum_contract("基础提示", schema, contract_id="unit-test")
    assert "unit-test" in prompt
    assert "$.claim_type" in prompt
    assert "FACT / PLAN" in prompt
    assert CONTRACT_REGISTRY_VERSION in prompt


def test_prompt_contract_injection_is_idempotent():
    from app.contract_registry import augment_prompt_with_enum_contract

    schema = {"type": "object", "properties": {"status": {"enum": ["PASS", "REVISE"]}}}
    once = augment_prompt_with_enum_contract("base prompt", schema, contract_id="test:once")
    twice = augment_prompt_with_enum_contract(once, schema, contract_id="test:twice")
    assert twice.count("UNIFIED_ENUM_CONTRACT:START") == 1
    assert twice.count("UNIFIED_ENUM_CONTRACT:END") == 1
    assert "test:once" not in twice
    assert "test:twice" in twice
