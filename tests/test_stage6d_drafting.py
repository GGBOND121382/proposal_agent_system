from __future__ import annotations

import copy
import json
from pathlib import Path

from stage6d_tools.stage6d_drafting import (
    canonical_markdown,
    deterministic_validate_batch,
    deterministic_validate_section,
)

ROOT = Path(__file__).resolve().parent
FIX = ROOT / "fixtures"


def load(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def inputs():
    return (
        load("stage4_argument_architecture_candidate.json"),
        load("stage4a_evidence_completion_candidate.json"),
        load("stage5_section_plan_candidate.json"),
    )


def contract(stage5, section_id):
    return next(s for s in stage5["sections"] if s["section_id"] == section_id)


def rebuild(response):
    c = response["candidate"]
    c["markdown"] = canonical_markdown(c["section_name"], c)
    return response


def validate_fixture(name: str, section_id: str):
    stage4, stage4a, stage5 = inputs()
    response = load(name)
    return deterministic_validate_section(response, contract(stage5, section_id), stage4, stage4a, [])


def test_stage6d_final_fixtures_pass():
    assert validate_fixture("stage6d_sec12_writer_response.json", "SEC-12")["verdict"] == "PASS"
    assert validate_fixture("stage6d_sec13_writer_repaired.json", "SEC-13")["verdict"] == "PASS"
    assert validate_fixture("stage6d_sec14_writer_repaired.json", "SEC-14")["verdict"] == "PASS"


def test_negated_final_readiness_is_not_premature():
    report = validate_fixture("stage6d_sec14_writer_repaired.json", "SEC-14")
    assert not any(f["code"] == "CONCLUSION_PREMATURE_CLAIM" for f in report["findings"])


def test_positive_final_readiness_is_blocked():
    stage4, stage4a, stage5 = inputs()
    response = load("stage6d_sec14_writer_repaired.json")
    response = copy.deepcopy(response)
    p = response["candidate"]["subsections"][-1]["paragraphs"][-1]
    p["text"] = p["text"].replace("不代表最终申报条件已经齐备", "表明最终申报条件已经齐备")
    rebuild(response)
    report = deterministic_validate_section(response, contract(stage5, "SEC-14"), stage4, stage4a, [])
    assert any(f["code"] == "CONCLUSION_PREMATURE_CLAIM" for f in report["findings"])


def test_plan_cannot_invent_annual_schedule():
    stage4, stage4a, stage5 = inputs()
    response = copy.deepcopy(load("stage6d_sec12_writer_response.json"))
    response["candidate"]["subsections"][0]["paragraphs"][0]["text"] += "第一年完成基础建模。"
    rebuild(response)
    report = deterministic_validate_section(response, contract(stage5, "SEC-12"), stage4, stage4a, [])
    assert any(f["code"] == "UNSUPPORTED_SCHEDULE_OR_RESULT" for f in report["findings"])


def test_plan_must_keep_schedule_open_items():
    stage4, stage4a, stage5 = inputs()
    response = copy.deepcopy(load("stage6d_sec12_writer_response.json"))
    response["candidate"]["unresolved_open_item_ids"] = []
    report = deterministic_validate_section(response, contract(stage5, "SEC-12"), stage4, stage4a, [])
    assert any(f["code"] == "PLAN_OPEN_ITEMS_MISSING" for f in report["findings"])


def test_risk_section_must_bind_standard_source():
    stage4, stage4a, stage5 = inputs()
    response = copy.deepcopy(load("stage6d_sec13_writer_repaired.json"))
    for sub in response["candidate"]["subsections"]:
        for p in sub["paragraphs"]:
            p["source_ids"] = []
    report = deterministic_validate_section(response, contract(stage5, "SEC-13"), stage4, stage4a, [])
    assert any(f["code"] == "RISK_STANDARD_SOURCE_MISSING" for f in report["findings"])


def test_conclusion_must_bind_all_innovation_hypotheses():
    stage4, stage4a, stage5 = inputs()
    response = copy.deepcopy(load("stage6d_sec14_writer_repaired.json"))
    p = response["candidate"]["subsections"][1]["paragraphs"][0]
    p["node_ids"].remove("INNO-H3")
    report = deterministic_validate_section(response, contract(stage5, "SEC-14"), stage4, stage4a, [])
    assert any(f["code"] == "REQUIRED_NODE_NOT_COVERED" for f in report["findings"])


def test_initial_long_drafts_are_rejected_without_false_readiness_finding():
    risk = validate_fixture("stage6d_sec13_writer_original.json", "SEC-13")
    conclusion = validate_fixture("stage6d_sec14_writer_original.json", "SEC-14")
    assert any(f["code"] == "SECTION_TOO_LONG" for f in risk["findings"])
    assert any(f["code"] == "SECTION_TOO_LONG" for f in conclusion["findings"])
    assert not any(f["code"] == "CONCLUSION_PREMATURE_CLAIM" for f in conclusion["findings"])


def test_stage6d_batch_passes_and_duplicate_key_is_blocked(tmp_path):
    snapshots = tmp_path / "source_snapshots"
    snapshots.mkdir()
    for name in ["stage6a_batch_draft.json", "stage6b_batch_draft.json", "stage6c_batch_draft.json"]:
        (snapshots / name).write_text('{"sections": []}\n', encoding="utf-8")
    candidates = {
        "SEC-12": load("stage6d_sec12_writer_response.json")["candidate"],
        "SEC-13": load("stage6d_sec13_writer_repaired.json")["candidate"],
        "SEC-14": load("stage6d_sec14_writer_repaired.json")["candidate"],
    }
    assert deterministic_validate_batch(tmp_path, candidates)["verdict"] == "PASS"
    broken = copy.deepcopy(candidates)
    broken["SEC-14"]["subsections"][0]["paragraphs"][0]["novel_content_key"] = broken["SEC-12"]["subsections"][0]["paragraphs"][0]["novel_content_key"]
    report = deterministic_validate_batch(tmp_path, broken)
    assert any(f["code"] == "CROSS_SECTION_INFORMATION_KEY_DUPLICATE" for f in report["findings"])
