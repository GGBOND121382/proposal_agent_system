from __future__ import annotations

import copy
import json
from pathlib import Path

from stage3_tools.stage3_project_definition import (
    _active_candidate_path,
    _schedule_project_definition_repair,
    deterministic_validate,
    sha256_file,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def validate(candidate: dict) -> dict:
    stage1_path = FIXTURES / "stage3_stage1_design_input.json"
    stage2_path = FIXTURES / "stage3_stage2_repaired.json"
    return deterministic_validate(
        candidate,
        load(stage1_path.name),
        load(stage2_path.name),
        sha256_file(stage1_path),
        sha256_file(stage2_path),
    )


def test_frozen_stage3_candidate_passes() -> None:
    report = validate(load("stage3_project_definition_candidate.json"))
    assert report["verdict"] == "PASS", report
    assert report["statistics"]["research_questions"] == 3
    assert report["statistics"]["research_contents"] == 4


def test_research_question_set_cannot_drift() -> None:
    candidate = load("stage3_project_definition_candidate.json")
    candidate["research_questions"][0]["rq_id"] = "RQ-X"
    report = validate(candidate)
    assert report["verdict"] == "FAIL"
    assert any(
        finding["code"] in {"SCHEMA_ERROR", "RQ_SET_MISMATCH", "RELATION_UNKNOWN_NODE", "OBJECTIVE_RQ_INVALID"}
        for finding in report["findings"]
    )


def test_novelty_cannot_be_confirmed_before_research() -> None:
    candidate = load("stage3_project_definition_candidate.json")
    candidate["innovation_hypotheses"][0]["closest_prior_work_status"] = "RESEARCHED"
    candidate["innovation_hypotheses"][0]["novelty_status"] = "CONFIRMED"
    report = validate(candidate)
    assert report["verdict"] == "FAIL"
    assert any(finding["code"] in {"SCHEMA_ERROR", "NOVELTY_PREMATURELY_CONFIRMED"} for finding in report["findings"])


def test_section_planning_is_not_released_in_stage3() -> None:
    candidate = load("stage3_project_definition_candidate.json")
    candidate["readiness"]["ready_for_section_planning"] = True
    report = validate(candidate)
    assert report["verdict"] == "FAIL"
    assert any(finding["code"] in {"SCHEMA_ERROR", "PREMATURE_SECTION_PLANNING"} for finding in report["findings"])


def test_repair_request_preserves_failed_candidate_and_uses_one_round(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    for name in ("intermediate", "quality", "requests"):
        (run_dir / name).mkdir(parents=True, exist_ok=True)
    candidate = load("stage3_project_definition_candidate.json")
    candidate["innovation_hypotheses"][1]["comparison_dimensions"][1] = "信号"
    report = {"verdict": "FAIL", "findings": [{"code": "JSON_SCHEMA", "severity": "BLOCKING"}]}
    (run_dir / "intermediate" / "project_definition_candidate.json").write_text(
        json.dumps(candidate, ensure_ascii=False), encoding="utf-8"
    )
    (run_dir / "quality" / "deterministic_project_definition_report.json").write_text(
        json.dumps(report, ensure_ascii=False), encoding="utf-8"
    )

    _schedule_project_definition_repair(run_dir)

    request = json.loads((run_dir / "requests" / "005_project_definition_repair.json").read_text(encoding="utf-8"))
    assert request["call_key"] == "stage3-project-definition-repair-001"
    assert request["model_contract"]["max_repair_rounds"] == 1
    assert request["input_envelope"]["candidate"] == candidate
    assert _active_candidate_path(run_dir).name == "project_definition_candidate.json"

    repaired = copy.deepcopy(candidate)
    repaired["innovation_hypotheses"][1]["comparison_dimensions"][1] = "资源调度信号"
    (run_dir / "intermediate" / "project_definition_candidate_repaired.json").write_text(
        json.dumps(repaired, ensure_ascii=False), encoding="utf-8"
    )
    assert _active_candidate_path(run_dir).name == "project_definition_candidate_repaired.json"
