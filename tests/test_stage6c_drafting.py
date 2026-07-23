from __future__ import annotations

import copy
import json
from pathlib import Path

from stage6c_tools.stage6c_drafting import (
    canonical_markdown,
    deterministic_validate_batch,
    deterministic_validate_section,
    next_writer_repair_attempt,
    section_contract,
    semantic_identity_errors,
    writer_repair_response_path,
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


def prepare_upstream_snapshots(run: Path) -> None:
    snapshots = run / "source_snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    for name in ("stage6a_batch_draft.json", "stage6b_batch_draft.json"):
        (snapshots / name).write_text(json.dumps({"sections": []}), encoding="utf-8")


def test_stage6c_frozen_sections_pass():
    for name in ["stage6c_sec09_writer_response.json", "stage6c_sec10_writer_response.json", "stage6c_sec11_writer_response.json"]:
        report = validate(load(name))
        assert report["verdict"] == "PASS", report


def test_innovation_requires_prior_work_and_falsification_chain():
    response = load("stage6c_sec09_writer_response.json")
    for sub in response["candidate"]["subsections"]:
        for p in sub["paragraphs"]:
            p["text"] = p["text"].replace("反证", "检验")
    rebuild(response)
    assert "INNOVATION_CHAIN_INCOMPLETE" in codes(validate(response))


def test_evaluation_requires_all_metrics_and_failure_reporting():
    response = load("stage6c_sec10_writer_response.json")
    for sub in response["candidate"]["subsections"]:
        for p in sub["paragraphs"]:
            p["text"] = p["text"].replace("MET-8", "第八项指标")
    rebuild(response)
    assert "EVALUATION_PROTOCOL_INCOMPLETE" in codes(validate(response))


def test_provisional_target_cannot_be_written_as_result():
    response = load("stage6c_sec10_writer_response.json")
    response["candidate"]["subsections"][1]["paragraphs"][1]["text"] += "已经提高10%。"
    rebuild(response)
    assert "PROVISIONAL_TARGET_WRITTEN_AS_RESULT" in codes(validate(response))


def test_foundation_requires_open_evidence_items():
    response = load("stage6c_sec11_writer_response.json")
    response["candidate"]["unresolved_open_item_ids"] = []
    rebuild(response)
    assert "FOUNDATION_OPEN_ITEMS_MISSING" in codes(validate(response))


def test_expression_identity_blocks_source_change():
    original = load("stage6c_sec11_writer_response.json")["candidate"]
    polished = copy.deepcopy(original)
    polished["subsections"][0]["paragraphs"][0]["source_ids"] = []
    assert semantic_identity_errors(original, polished)


def test_repair_attempt_paths_are_unique(tmp_path: Path):
    (tmp_path / "responses").mkdir()
    assert next_writer_repair_attempt(tmp_path, "SEC-09") == 1
    p1 = writer_repair_response_path(tmp_path, "SEC-09", 1)
    p1.write_text("{}", encoding="utf-8")
    assert next_writer_repair_attempt(tmp_path, "SEC-09") == 2
    p2 = writer_repair_response_path(tmp_path, "SEC-09", 2)
    assert p1 != p2
    assert p1.name.startswith("109_")
    assert p2.name.startswith("209_")


def test_batch_validator_passes_final_candidates(tmp_path: Path):
    run = tmp_path / "run"
    prepare_upstream_snapshots(run)
    candidates = {
        "SEC-09": load("stage6c_sec09_writer_response.json")["candidate"],
        "SEC-10": load("stage6c_sec10_writer_response.json")["candidate"],
        "SEC-11": load("stage6c_sec11_writer_response.json")["candidate"],
    }
    report = deterministic_validate_batch(run, candidates)
    assert report["verdict"] == "PASS", report


def test_ungrammatical_comparison_phrase_is_blocked():
    response = load("stage6c_sec09_writer_response.json")
    p = response["candidate"]["subsections"][1]["paragraphs"][0]
    p["text"] = p["text"].replace("不将“多智能体辩论”本身视为创新", "非“多智能体辩论”本身作为本项目创新")
    rebuild(response)
    assert "UNGRAMMATICAL_COMPARISON_PHRASE" in codes(validate(response))
