from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from app.staged_workflow_config import (
    all_section_ids,
    page_totals,
    project_title,
    safe_output_stem,
    section_ids_for_batch,
    stage_boundary,
)


def dynamic_plan() -> dict:
    return {
        "project_title": "自定义联运优化项目",
        "draft_batches": [
            {"batch_id": "STAGE-6A", "section_ids": ["SEC-A", "SEC-B"]},
            {"batch_id": "STAGE-6B", "section_ids": ["SEC-C"]},
            {"batch_id": "STAGE-6C", "section_ids": ["SEC-D"]},
            {"batch_id": "STAGE-6D", "section_ids": ["SEC-E"]},
        ],
        "sections": [
            {"section_id": "SEC-B", "order": 2, "target_pages": 1.5, "max_pages": 2.0},
            {"section_id": "SEC-A", "order": 1, "target_pages": 1.0, "max_pages": 1.25},
            {"section_id": "SEC-C", "order": 3, "target_pages": 2.0, "max_pages": 2.5},
            {"section_id": "SEC-D", "order": 4, "target_pages": 1.0, "max_pages": 1.5},
            {"section_id": "SEC-E", "order": 5, "target_pages": 0.5, "max_pages": 1.0},
        ],
    }


def test_staged_configuration_uses_contract_not_fixed_fourteen_sections():
    plan = dynamic_plan()
    assert project_title(plan) == "自定义联运优化项目"
    assert all_section_ids(plan) == ["SEC-A", "SEC-B", "SEC-C", "SEC-D", "SEC-E"]
    assert section_ids_for_batch(plan, "STAGE-6A") == ["SEC-A", "SEC-B"]
    assert page_totals(plan, ["SEC-A", "SEC-B"]) == (2.5, 3.25)
    assert stage_boundary("STAGE-6A", ["SEC-A", "SEC-B"]) == "STAGE_6A_SEC_A_SEC_B_ONLY"


def test_batch_critic_schemas_accept_dynamic_section_sets():
    for label in "abcd":
        schema_path = Path(f"stage6{label}_tools/batch_critic.schema.json")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        checked = schema["properties"]["checked_section_ids"]
        errors = list(Draft202012Validator(checked).iter_errors(["SEC-A", "SEC-B"]))
        assert errors == []


def test_export_stem_is_title_driven_and_filesystem_safe():
    assert safe_output_stem("海陆/联运:方案*优化?") == "海陆_联运_方案_优化"
