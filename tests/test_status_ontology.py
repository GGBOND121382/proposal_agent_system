from __future__ import annotations

import copy
import json
from pathlib import Path

from app.executor import PromptExecutor
from app.status_ontology import (
    CANONICAL_CLAIM_TYPES,
    CANONICAL_KNOWLEDGE_STATUSES,
    CANONICAL_TEMPORAL_STATUSES,
    normalize_claim_type,
    normalize_knowledge_status,
    normalize_temporal_status,
    normalize_stage2_candidate,
    normalize_stage3_candidate,
)
from stage2_tools.stage2_guide_fact_base import deterministic_validate

FIXTURE = Path(__file__).parent / "fixtures" / "stage2_candidate_missing_open_mapping.json"


def _candidate() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_project_design_alias_uses_confirmed_artifact_provenance() -> None:
    candidate = _candidate()
    fact = next(x for x in candidate["facts"] if x["knowledge_status"] == "CONFIRMED_DESIGN")
    fact["knowledge_status"] = "PROJECT_DESIGN"

    normalized, report = normalize_stage2_candidate(candidate)
    migrated = next(x for x in normalized["facts"] if x["fact_id"] == fact["fact_id"])

    assert migrated["knowledge_status"] == "CONFIRMED"
    assert migrated["fact_role"] == "DESIGN"
    assert migrated["temporal_status"] == "PLANNED"
    assert report["normalized_count"] >= 1


def test_working_assumption_without_human_source_is_estimated() -> None:
    decision = normalize_knowledge_status(
        "WORKING_ASSUMPTION",
        source_refs=[{"source_id": "m1", "source_type": "MODEL_INFERENCE"}],
    )
    assert decision.canonical_status == "ESTIMATED"


def test_provisional_target_keeps_provenance_but_not_achievement_semantics() -> None:
    candidate = _candidate()
    fact = next(x for x in candidate["facts"] if x["knowledge_status"] == "PROVISIONAL_TARGET")

    normalized, _ = normalize_stage2_candidate(candidate)
    migrated = next(x for x in normalized["facts"] if x["fact_id"] == fact["fact_id"])

    assert migrated["knowledge_status"] in {"CONFIRMED", "USER_ASSERTED", "DOCUMENT_EXTRACTED"}
    assert migrated["fact_role"] == "TARGET"
    assert migrated["temporal_status"] == "EXPECTED"
    assert migrated["assertion_policy"] == "QUALIFIED"
    assert migrated["requires_qualification"] is True


def test_unknown_arbitrary_status_is_not_silently_guessed() -> None:
    candidate = _candidate()
    candidate["facts"][0]["knowledge_status"] = "MODEL_GUESSED"

    normalized, report = normalize_stage2_candidate(candidate)
    assert normalized["facts"][0]["knowledge_status"] == "MODEL_GUESSED"
    assert report["unresolved_count"] == 1
    validation = deterministic_validate(candidate)
    assert validation["verdict"] == "FAIL"
    assert any(x["code"] == "SCHEMA_ERROR" for x in validation["findings"])


def test_prompt_output_alias_migrates_semantic_dimensions() -> None:
    output = {
        "status": "PASS",
        "result": {
            "fact_candidates": [
                {
                    "claim_id": "c1",
                    "claim_text": "本项目拟构建验证原型。",
                    "claim_type": "FACT",
                    "subject_id": "project",
                    "temporal_status": "CURRENT",
                    "qualifiers": [],
                    "numeric_values": [],
                    "source_refs": [
                        {
                            "source_id": "u1",
                            "source_type": "USER_CONFIRMATION",
                            "authority_rank": 100,
                            "security_level": "INTERNAL",
                        }
                    ],
                    "knowledge_status": "PROJECT_DESIGN",
                    "security_level": "INTERNAL",
                }
            ]
        },
        "warnings": [],
    }

    normalized = PromptExecutor._normalize_fact_output(output)
    fact = normalized["result"]["fact_candidates"][0]
    assert fact["knowledge_status"] == "CONFIRMED"
    assert fact["claim_type"] == "PLAN"
    assert fact["temporal_status"] == "PLANNED"



def test_stage3_project_design_alias_uses_confirmed_human_gated_design() -> None:
    candidate = {
        "central_proposition": {
            "knowledge_status": "PROJECT_DESIGN",
            "source_fact_ids": ["F-1"],
        }
    }
    stage2 = {"facts": [{"fact_id": "F-1", "knowledge_status": "USER_ASSERTED"}]}

    normalized, report = normalize_stage3_candidate(candidate, stage2)
    proposition = normalized["central_proposition"]
    assert proposition["knowledge_status"] == "CONFIRMED"
    assert proposition["claim_role"] == "DESIGN_HYPOTHESIS"
    assert proposition["temporal_status"] == "PLANNED"
    assert report["normalized_count"] == 3


def test_claim_type_project_design_drifts_independently_of_knowledge_status() -> None:
    output = {
        "status": "PASS",
        "result": {
            "fact_candidates": [
                {
                    "claim_id": "c-claim-drift",
                    "claim_text": "本项目拟构建人机协同决策原型。",
                    "claim_type": "PROJECT_DESIGN",
                    "subject_id": "project",
                    "temporal_status": "CURRENT",
                    "qualifiers": [],
                    "numeric_values": [],
                    "source_refs": [
                        {
                            "source_id": "brief-1",
                            "source_type": "HISTORICAL_DOCUMENT",
                            "authority_rank": 90,
                            "security_level": "INTERNAL",
                        }
                    ],
                    "knowledge_status": "DOCUMENT_EXTRACTED",
                    "security_level": "INTERNAL",
                }
            ]
        },
        "warnings": [],
    }

    normalized = PromptExecutor._normalize_fact_output(output)
    fact = normalized["result"]["fact_candidates"][0]
    assert fact["knowledge_status"] == "DOCUMENT_EXTRACTED"
    assert fact["claim_type"] == "PLAN"
    assert fact["temporal_status"] == "PLANNED"
    assert any("/claim_type: PROJECT_DESIGN->PLAN" in warning for warning in normalized["warnings"])


def test_registered_claim_and_temporal_aliases_are_dimension_specific() -> None:
    assert normalize_claim_type("PROVISIONAL_TARGET").canonical_value == "EXPECTED_RESULT"
    assert normalize_claim_type("WORKING_ASSUMPTION").canonical_value == "MODEL_INFERENCE"
    assert normalize_temporal_status("PROVISIONAL_TARGET").canonical_value == "EXPECTED"
    assert normalize_temporal_status("PROJECT_DESIGN").canonical_value == "PLANNED"


def test_unknown_claim_type_is_not_silently_guessed() -> None:
    decision = normalize_claim_type("MODEL_PROPOSED_DESIGN")
    assert decision.normalized is False
    assert decision.canonical_value == "MODEL_PROPOSED_DESIGN"


def _walk_knowledge_status_schemas(node, path="$"):
    if isinstance(node, list):
        for index, item in enumerate(node):
            yield from _walk_knowledge_status_schemas(item, f"{path}/{index}")
    elif isinstance(node, dict):
        for key, value in node.items():
            current = f"{path}/{key}"
            if key == "knowledge_status" and isinstance(value, dict):
                yield current, value
            yield from _walk_knowledge_status_schemas(value, current)


def test_all_schema_knowledge_status_values_are_canonical() -> None:
    root = Path(__file__).resolve().parents[1]
    schema_files = list((root / "prompt_pack" / "schemas").rglob("*.json"))
    schema_files += list(root.glob("stage*_tools/*.schema.json"))
    seen = 0
    violations = []
    for path in schema_files:
        schema = json.loads(path.read_text(encoding="utf-8"))
        for field_path, definition in _walk_knowledge_status_schemas(schema):
            seen += 1
            values = set(definition.get("enum", []))
            if "const" in definition:
                values.add(definition["const"])
            illegal = values - set(CANONICAL_KNOWLEDGE_STATUSES)
            if illegal:
                violations.append((str(path.relative_to(root)), field_path, sorted(illegal)))
    assert seen > 0
    assert violations == []


def _walk_named_enum_schemas(node, field_name, path="$"):
    if isinstance(node, list):
        for index, item in enumerate(node):
            yield from _walk_named_enum_schemas(item, field_name, f"{path}/{index}")
    elif isinstance(node, dict):
        for key, value in node.items():
            current = f"{path}/{key}"
            if key == field_name and isinstance(value, dict):
                yield current, value
            yield from _walk_named_enum_schemas(value, field_name, current)


def test_claim_type_and_temporal_status_schema_values_are_canonical() -> None:
    root = Path(__file__).resolve().parents[1]
    schema_files = list((root / "prompt_pack" / "schemas").rglob("*.json"))
    schema_files += list(root.glob("stage*_tools/*.schema.json"))
    contracts = {
        "claim_type": set(CANONICAL_CLAIM_TYPES),
        "temporal_status": set(CANONICAL_TEMPORAL_STATUSES),
    }
    seen = {field: 0 for field in contracts}
    violations = []
    for path in schema_files:
        schema = json.loads(path.read_text(encoding="utf-8"))
        for field, allowed in contracts.items():
            for field_path, definition in _walk_named_enum_schemas(schema, field):
                seen[field] += 1
                values = set(definition.get("enum", []))
                if "const" in definition:
                    values.add(definition["const"])
                illegal = values - allowed
                if illegal:
                    violations.append((str(path.relative_to(root)), field_path, sorted(illegal)))
    assert seen["claim_type"] > 0
    assert seen["temporal_status"] > 0
    assert violations == []
