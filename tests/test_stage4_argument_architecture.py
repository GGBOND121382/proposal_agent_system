from __future__ import annotations

import copy
import json
from pathlib import Path

from stage4_tools.stage4_argument_architecture import (
    _active_candidate_path,
    deterministic_validate,
    sha256_file,
)

FIX = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def inputs():
    paths = {
        "stage1": FIX / "stage4_stage1_design_input.json",
        "stage2": FIX / "stage4_stage2_guide_fact_base.json",
        "stage3": FIX / "stage4_stage3_project_definition.json",
    }
    data = {k: load(p.name) for k, p in paths.items()}
    hashes = {k: sha256_file(p) for k, p in paths.items()}
    return data, hashes


def validate(candidate):
    data, hashes = inputs()
    return deterministic_validate(candidate, data["stage1"], data["stage2"], data["stage3"], hashes)


def test_frozen_stage4_candidate_passes():
    report = validate(load("stage4_argument_architecture_candidate.json"))
    assert report["verdict"] == "PASS", report
    assert report["statistics"]["nodes"] == 66
    assert report["statistics"]["relations"] == 104


def test_missing_frozen_method_node_is_blocked():
    candidate = load("stage4_argument_architecture_candidate.json")
    candidate["nodes"] = [n for n in candidate["nodes"] if n["node_id"] != "M-1"]
    report = validate(candidate)
    assert report["verdict"] == "FAIL"
    assert any(x["code"] == "DESIGN_NODE_MISSING" for x in report["findings"])


def test_prior_work_cannot_be_promoted_without_research():
    candidate = load("stage4_argument_architecture_candidate.json")
    node = next(n for n in candidate["nodes"] if n["node_id"] == "PRIOR-1")
    node["status"] = "SUPPORTED"
    report = validate(candidate)
    assert report["verdict"] == "FAIL"
    assert any(x["code"] == "PRIOR_WORK_STATUS_INVALID" for x in report["findings"])


def test_innovation_cannot_be_confirmed_early():
    candidate = load("stage4_argument_architecture_candidate.json")
    node = next(n for n in candidate["nodes"] if n["node_id"] == "INNO-H1")
    node["status"] = "SUPPORTED"
    report = validate(candidate)
    assert report["verdict"] == "FAIL"
    assert any(x["code"] == "INNOVATION_PREMATURE" for x in report["findings"])


def test_blocking_evidence_gaps_hold_section_planning():
    candidate = load("stage4_argument_architecture_candidate.json")
    candidate["readiness"]["ready_for_section_planning"] = True
    report = validate(candidate)
    assert report["verdict"] == "FAIL"
    assert any(x["code"] == "FALSE_READY" for x in report["findings"])


def test_repaired_candidate_is_the_active_candidate(tmp_path: Path):
    intermediate = tmp_path / "intermediate"
    intermediate.mkdir()
    (intermediate / "argument_architecture_candidate.json").write_text("{}", encoding="utf-8")
    repaired = intermediate / "argument_architecture_candidate_repaired.json"
    repaired.write_text('{"repaired": true}', encoding="utf-8")
    assert _active_candidate_path(tmp_path) == repaired
