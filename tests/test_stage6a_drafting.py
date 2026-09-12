from __future__ import annotations

import copy
import json
from pathlib import Path

from stage6a_tools.stage6a_drafting import (
    canonical_markdown,
    deterministic_validate_section,
    section_contract,
    semantic_identity_errors,
)

FIX = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def upstream():
    return (
        load("stage5_stage4_argument_architecture.json"),
        load("stage5_stage4a_evidence_completion.json"),
        load("stage5_section_plan_candidate.json"),
    )


def validate(response, prior_digest=None):
    stage4, stage4a, stage5 = upstream()
    contract = section_contract(stage5, response["section_id"])
    return deterministic_validate_section(response, contract, stage4, stage4a, prior_digest or [])


def codes(report):
    return {item["code"] for item in report["findings"]}


def rebuild_markdown(response):
    response["candidate"]["markdown"] = canonical_markdown(
        response["candidate"]["section_name"], response["candidate"]
    )


def test_frozen_sec01_writer_response_passes():
    report = validate(load("stage6a_sec01_writer_response.json"))
    assert report["verdict"] == "PASS", report
    assert report["paragraph_count"] == 4


def test_repaired_related_work_response_passes():
    report = validate(load("stage6a_sec03_writer_repaired.json"))
    assert report["verdict"] == "PASS", report


def test_related_work_requires_explicit_limitation_semantics():
    response = load("stage6a_sec03_writer_repaired.json")
    for subsection in response["candidate"]["subsections"]:
        for paragraph in subsection["paragraphs"]:
            paragraph["text"] = paragraph["text"].replace("局限", "不足")
    rebuild_markdown(response)
    report = validate(response)
    assert report["verdict"] == "FAIL"
    assert "LITERATURE_REVIEW_CHAIN_INCOMPLETE" in codes(report)


def test_required_argument_node_cannot_be_omitted():
    response = load("stage6a_sec01_writer_response.json")
    stage4, stage4a, stage5 = upstream()
    contract = section_contract(stage5, "SEC-01")
    missing = contract["required_node_ids"][0]
    for p in [p for s in response["candidate"]["subsections"] for p in s["paragraphs"]]:
        p["node_ids"] = [x for x in p["node_ids"] if x != missing]
    rebuild_markdown(response)
    report = deterministic_validate_section(response, contract, stage4, stage4a, [])
    assert report["verdict"] == "FAIL"
    assert "REQUIRED_NODE_NOT_COVERED" in codes(report)


def test_project_plan_cannot_be_written_as_existing_result():
    response = load("stage6a_sec01_writer_response.json")
    p = response["candidate"]["subsections"][0]["paragraphs"][0]
    p["text"] += "实验证明本项目已经达到预期效果。"
    rebuild_markdown(response)
    report = validate(response)
    assert "PLAN_WRITTEN_AS_RESULT" in codes(report)


def test_placeholder_text_is_blocked():
    response = load("stage6a_sec01_writer_response.json")
    response["candidate"]["subsections"][0]["paragraphs"][0]["text"] += "……"
    rebuild_markdown(response)
    report = validate(response)
    assert "PLACEHOLDER_TEXT" in codes(report)


def test_cross_section_information_key_reuse_is_blocked():
    response = load("stage6a_sec01_writer_response.json")
    key = response["candidate"]["subsections"][0]["paragraphs"][0]["novel_content_key"]
    report = validate(response, [{"section_id": "SEC-X", "new_information_keys": [key]}])
    assert "CROSS_SECTION_INFORMATION_KEY_REUSE" in codes(report)


def test_expression_review_detects_semantic_metadata_change():
    original = load("stage6a_sec01_writer_response.json")["candidate"]
    polished = copy.deepcopy(original)
    polished["subsections"][0]["paragraphs"][0]["source_ids"] = ["SRC-PUB-01"]
    errors = semantic_identity_errors(original, polished)
    assert any("source_ids changed" in item for item in errors)


def test_expression_review_allows_text_only_polish():
    original = load("stage6a_sec01_writer_response.json")["candidate"]
    polished = copy.deepcopy(original)
    polished["subsections"][0]["paragraphs"][0]["text"] += "该表述仅用于增强句间衔接。"
    rebuild = canonical_markdown(polished["section_name"], polished)
    polished["markdown"] = rebuild
    assert semantic_identity_errors(original, polished) == []


def test_repair_keeps_candidate_identity_and_contract():
    original = load("stage6a_sec03_writer_original.json")
    repaired = load("stage6a_sec03_writer_repaired.json")
    assert original["candidate"]["candidate_id"] == repaired["candidate"]["candidate_id"]
    assert original["candidate"]["section_name"] == repaired["candidate"]["section_name"]
    assert [s["subsection_id"] for s in original["candidate"]["subsections"]] == [
        s["subsection_id"] for s in repaired["candidate"]["subsections"]
    ]
    assert validate(repaired)["verdict"] == "PASS"
