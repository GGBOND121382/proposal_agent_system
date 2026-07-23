from __future__ import annotations

import copy
import json
from pathlib import Path

from stage4a_tools.stage4a_evidence_completion import deterministic_validate, sha256_file

FIX = Path(__file__).parent / "fixtures"
STAGE4_PATH = FIX / "stage4a_stage4_argument_architecture.json"
CANDIDATE_PATH = FIX / "stage4a_evidence_completion_candidate.json"


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def validate(candidate):
    return deterministic_validate(candidate, load(STAGE4_PATH), sha256_file(STAGE4_PATH))


def codes(report):
    return {finding["code"] for finding in report["findings"]}


def frozen_candidate():
    return load(CANDIDATE_PATH)


def test_frozen_stage4a_candidate_passes():
    report = validate(frozen_candidate())
    assert report["verdict"] == "PASS", report
    assert report["statistics"] == {
        "sources": 18,
        "peer_reviewed_or_standard_sources": 14,
        "prior_work_nodes": 3,
        "foundation_nodes": 3,
        "metrics": 8,
        "gap_dispositions": 5,
        "remaining_open_items": 12,
    }


def test_official_guide_and_template_only_block_final_contract():
    candidate = frozen_candidate()
    gaps = {row["gap_id"]: row for row in candidate["gap_disposition"]}
    for gap_id in ("EVID-GAP-01", "EVID-GAP-02"):
        row = gaps[gap_id]
        assert row["disposition"] == "RECLASSIFIED_FINAL_COMPLIANCE"
        assert row["content_planning_blocking"] is False
        assert row["final_submission_blocking"] is True
    assert candidate["readiness"]["ready_for_provisional_section_planning"] is True
    assert candidate["readiness"]["ready_for_final_section_contract"] is False
    assert validate(candidate)["verdict"] == "PASS"


def test_compliance_gap_cannot_be_silently_downgraded():
    candidate = frozen_candidate()
    row = next(x for x in candidate["gap_disposition"] if x["gap_id"] == "EVID-GAP-01")
    row["final_submission_blocking"] = False
    report = validate(candidate)
    assert report["verdict"] == "FAIL"
    assert "COMPLIANCE_GAP_CLASSIFICATION" in codes(report)


def test_public_research_source_must_remain_verified_public():
    candidate = frozen_candidate()
    source = next(x for x in candidate["source_registry"] if x["source_type"] == "PEER_REVIEWED_PAPER")
    source["verification_status"] = "USER_ASSERTED_UNVERIFIED"
    report = validate(candidate)
    assert report["verdict"] == "FAIL"
    assert "PUBLIC_SOURCE_NOT_VERIFIED" in codes(report)


def test_user_assertion_cannot_be_promoted_to_verified_evidence():
    candidate = frozen_candidate()
    source = next(x for x in candidate["source_registry"] if x["source_type"] == "USER_ASSERTED")
    source["verification_status"] = "VERIFIED_PUBLIC"
    report = validate(candidate)
    assert report["verdict"] == "FAIL"
    assert "USER_ASSERTION_OVERCLAIMED" in codes(report)


def test_prior_work_open_item_must_close_after_public_mapping():
    candidate = frozen_candidate()
    candidate["open_items_remaining"].append("OPEN-013")
    report = validate(candidate)
    assert report["verdict"] == "FAIL"
    assert "PRIOR_WORK_OPEN_NOT_CLOSED" in codes(report)


def test_metric_thresholds_must_remain_provisional():
    candidate = frozen_candidate()
    candidate["metric_justification"][0]["threshold_status"] = "EMPIRICALLY_CONFIRMED"
    report = validate(candidate)
    assert report["verdict"] == "FAIL"
    assert "SCHEMA_ERROR" in codes(report) or "METRIC_THRESHOLD_OVERCLAIM" in codes(report)


def test_final_section_contract_cannot_be_released_in_stage4a():
    candidate = frozen_candidate()
    candidate["readiness"]["ready_for_final_section_contract"] = True
    report = validate(candidate)
    assert report["verdict"] == "FAIL"
    assert "READINESS_INVALID" in codes(report)


def test_closest_work_must_use_public_sources_with_matching_rq_scope():
    candidate = frozen_candidate()
    user_source = next(x for x in candidate["source_registry"] if x["source_type"] == "USER_ASSERTED")
    candidate["prior_work_updates"][0]["closest_work_source_ids"][0] = user_source["source_id"]
    report = validate(candidate)
    assert report["verdict"] == "FAIL"
    assert "PRIOR_NONPUBLIC_SOURCE" in codes(report)
