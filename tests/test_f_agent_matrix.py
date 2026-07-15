from __future__ import annotations

import copy
from pathlib import Path

import pytest

from app.pack import PromptPack
from scripts.validate_f import validate

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def pack() -> PromptPack:
    return PromptPack(ROOT / "prompt_pack")


@pytest.fixture(scope="module")
def reloaded_pack() -> PromptPack:
    return PromptPack(ROOT / "prompt_pack")


def test_f_manifest_is_complete():
    report = validate()
    assert report["status"] == "PASS", report["errors"]
    assert report["counts"]["prompts"] == 30
    assert report["counts"]["replay_cases"] == 150
    assert report["counts"]["agent_matrix"] == {
        "positive": 3,
        "negative": 5,
        "edge": 1,
        "restart": 1,
    }


@pytest.mark.parametrize("prompt_id", PromptPack(ROOT / "prompt_pack").prompt_ids())
def test_each_agent_has_required_matrix(prompt_id: str, pack: PromptPack, reloaded_pack: PromptPack):
    # Three positive fixtures already carried by the Prompt Pack.
    for case_type in ("normal", "high_risk", "need_user_input"):
        case = pack.replay_case(prompt_id, case_type)
        assert pack.validate(prompt_id, "input", case["input"]) == []
        assert case.get("expected_output") is not None
        assert pack.validate(prompt_id, "output", case["expected_output"]) == []

    # Two authored negative fixtures.
    for case_type in ("missing_input", "schema_error"):
        case = pack.replay_case(prompt_id, case_type)
        assert pack.validate(prompt_id, "input", case["input"]), case_type

    # Three deterministic negative mutations independent of fixture wording.
    normal = pack.replay_case(prompt_id, "normal")
    bad_input_id = copy.deepcopy(normal["input"])
    bad_input_id["prompt_id"] = "P-NOT-REGISTERED"
    assert pack.validate(prompt_id, "input", bad_input_id)

    bad_output_status = copy.deepcopy(normal["expected_output"])
    bad_output_status["status"] = "NOT_A_STATUS"
    assert pack.validate(prompt_id, "output", bad_output_status)

    bad_output_id = copy.deepcopy(normal["expected_output"])
    bad_output_id["prompt_id"] = "P-NOT-REGISTERED"
    assert pack.validate(prompt_id, "output", bad_output_id)

    # Boundary: the need-user-input fixture must be internally self-consistent.
    edge = pack.replay_case(prompt_id, "need_user_input")
    assert edge["expected_validation"]["expected_status"] == edge["expected_output"]["status"]

    # Restart: a newly loaded Prompt Pack must resolve exactly the same Agent contract.
    assert reloaded_pack.entry(prompt_id) == pack.entry(prompt_id)
    assert reloaded_pack.prompt_text(prompt_id) == pack.prompt_text(prompt_id)
    assert reloaded_pack.schema(prompt_id, "input") == pack.schema(prompt_id, "input")
    assert reloaded_pack.schema(prompt_id, "output") == pack.schema(prompt_id, "output")
    assert reloaded_pack.replay_case(prompt_id, "normal") == normal
