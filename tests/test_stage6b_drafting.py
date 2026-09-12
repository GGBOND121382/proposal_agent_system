from __future__ import annotations

import copy
import json
from pathlib import Path

from stage6b_tools.stage6b_drafting import (
    canonical_markdown,
    deterministic_validate_batch,
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


def validate(response):
    stage4, stage4a, stage5 = upstream()
    contract = section_contract(stage5, response["section_id"])
    return deterministic_validate_section(response, contract, stage4, stage4a, [])


def codes(report):
    return {x["code"] for x in report["findings"]}


def rebuild(response):
    response["candidate"]["markdown"] = canonical_markdown(
        response["candidate"]["section_name"], response["candidate"]
    )


def prepare_stage6a_snapshot(run: Path) -> None:
    snapshot = run / "source_snapshots" / "stage6a_batch_draft.json"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(json.dumps({"sections": []}), encoding="utf-8")


def test_stage6b_frozen_sections_pass():
    for name in ["stage6b_sec06_writer_response.json", "stage6b_sec07_writer_response.json", "stage6b_sec08_writer_response.json"]:
        report = validate(load(name))
        assert report["verdict"] == "PASS", report


def test_research_content_requires_explicit_work_package_chain():
    response = load("stage6b_sec06_writer_response.json")
    for sub in response["candidate"]["subsections"]:
        for p in sub["paragraphs"]:
            p["text"] = p["text"].replace("WP-5", "第五工作包")
    rebuild(response)
    report = validate(response)
    assert "RESEARCH_CONTENT_ID_NOT_EXPLICIT" in codes(report)


def test_key_problem_requires_falsification_semantics():
    response = load("stage6b_sec07_writer_response.json")
    for sub in response["candidate"]["subsections"]:
        for p in sub["paragraphs"]:
            p["text"] = p["text"].replace("反证", "验证")
    rebuild(response)
    report = validate(response)
    assert "KEY_PROBLEM_FALSIFICATION_INCOMPLETE" in codes(report)


def test_technical_route_requires_replaceable_api_boundary():
    response = load("stage6b_sec08_writer_response.json")
    for sub in response["candidate"]["subsections"]:
        for p in sub["paragraphs"]:
            p["text"] = p["text"].replace("模型API", "模型接口")
    rebuild(response)
    report = validate(response)
    assert "TECHNICAL_ROUTE_CLOSURE_INCOMPLETE" in codes(report)


def test_expression_identity_blocks_metadata_change():
    original = load("stage6b_sec08_writer_response.json")["candidate"]
    polished = copy.deepcopy(original)
    polished["subsections"][0]["paragraphs"][0]["node_ids"] = ["FM-1"]
    assert semantic_identity_errors(original, polished)


def test_batch_validator_passes_final_candidates(tmp_path: Path):
    run = tmp_path / "run"
    prepare_stage6a_snapshot(run)
    candidates = {sid: load(f"stage6b_sec{sid[-2:]}_writer_response.json")["candidate"] for sid in ["SEC-06", "SEC-07", "SEC-08"]}
    report = deterministic_validate_batch(run, candidates)
    assert report["verdict"] == "PASS", report


def test_batch_validator_blocks_duplicate_paragraph(tmp_path: Path):
    run = tmp_path / "run"
    prepare_stage6a_snapshot(run)
    candidates = {sid: load(f"stage6b_sec{sid[-2:]}_writer_response.json")["candidate"] for sid in ["SEC-06", "SEC-07", "SEC-08"]}
    candidates["SEC-07"]["subsections"][0]["paragraphs"][0]["text"] = candidates["SEC-06"]["subsections"][0]["paragraphs"][0]["text"]
    report = deterministic_validate_batch(run, candidates)
    assert any(x["code"] == "CROSS_SECTION_PARAGRAPH_DUPLICATE" for x in report["findings"])
