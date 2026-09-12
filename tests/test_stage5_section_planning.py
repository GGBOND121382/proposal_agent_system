from __future__ import annotations

import copy
import json
from pathlib import Path

from stage5_tools.stage5_section_planning import deterministic_validate, sha256_file

FIX = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def inputs():
    paths = {
        "stage1": FIX / "stage5_stage1_design_input.json",
        "stage3": FIX / "stage5_stage3_project_definition.json",
        "stage4": FIX / "stage5_stage4_argument_architecture.json",
        "stage4a": FIX / "stage5_stage4a_evidence_completion.json",
    }
    data = {k: json.loads(v.read_text(encoding="utf-8")) for k, v in paths.items()}
    hashes = {k: sha256_file(v) for k, v in paths.items()}
    return data, hashes


def validate(candidate):
    data, hashes = inputs()
    return deterministic_validate(candidate, data["stage1"], data["stage3"], data["stage4"], data["stage4a"], hashes)


def codes(report):
    return {x["code"] for x in report["findings"]}


def test_frozen_stage5_candidate_passes():
    report = validate(load("stage5_section_plan_candidate.json"))
    assert report["verdict"] == "PASS", report
    assert report["statistics"]["sections"] == 14
    assert report["statistics"]["target_pages"] == 16.9
    assert report["statistics"]["max_pages"] == 20.0


def test_top_level_central_proposition_and_rq_ids_are_valid_argument_ids():
    candidate = load("stage5_section_plan_candidate.json")
    required = set(candidate["sections"][0]["required_node_ids"])
    assert {"CP-1", "RQ-1", "RQ-2", "RQ-3"}.issubset(required)
    report = validate(candidate)
    assert "SECTION_UNKNOWN_NODE" not in codes(report)


def test_stage1_section_identity_and_page_budget_are_frozen():
    candidate = load("stage5_section_plan_candidate.json")
    candidate["sections"][1]["target_pages"] = 1.2
    report = validate(candidate)
    assert report["verdict"] == "FAIL"
    assert "SECTION_TARGET_PAGE_DRIFT" in codes(report)


def test_innovation_section_requires_prior_and_novel_mechanism_bindings():
    candidate = load("stage5_section_plan_candidate.json")
    section = next(x for x in candidate["sections"] if x["section_id"] == "SEC-09")
    section["required_node_ids"].remove("PRIOR-2")
    report = validate(candidate)
    assert report["verdict"] == "FAIL"
    assert "SECTION_REQUIRED_BINDING_MISSING" in codes(report)


def test_conclusion_requires_central_proposition_all_rqs_and_innovations():
    candidate = load("stage5_section_plan_candidate.json")
    section = next(x for x in candidate["sections"] if x["section_id"] == "SEC-14")
    section["required_node_ids"].remove("INNO-H3")
    report = validate(candidate)
    assert report["verdict"] == "FAIL"
    assert "SECTION_REQUIRED_BINDING_MISSING" in codes(report)


def test_draft_batches_must_partition_all_sections_once():
    candidate = load("stage5_section_plan_candidate.json")
    candidate["draft_batches"][3]["section_ids"][-1] = "SEC-01"
    report = validate(candidate)
    assert report["verdict"] == "FAIL"
    assert "BATCH_SECTION_PARTITION_INVALID" in codes(report)


def test_rq_coverage_index_must_match_section_contracts():
    candidate = load("stage5_section_plan_candidate.json")
    candidate["cross_section_controls"]["rq_coverage"]["RQ-1"].remove("SEC-01")
    report = validate(candidate)
    assert report["verdict"] == "FAIL"
    assert "RQ_COVERAGE_INDEX_MISMATCH" in codes(report)


def test_final_submission_cannot_be_released_in_stage5():
    candidate = load("stage5_section_plan_candidate.json")
    candidate["readiness"]["ready_for_final_submission"] = True
    report = validate(candidate)
    assert report["verdict"] == "FAIL"
    assert "READINESS_CLASSIFICATION_INVALID" in codes(report)


def test_unknown_argument_node_is_blocked():
    candidate = load("stage5_section_plan_candidate.json")
    candidate["sections"][0]["required_node_ids"].append("UNKNOWN-NODE")
    report = validate(candidate)
    assert report["verdict"] == "FAIL"
    assert "SECTION_UNKNOWN_NODE" in codes(report)
