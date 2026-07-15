from __future__ import annotations

import copy
import json
from pathlib import Path

from scripts.validate_g1 import TRACK_IDS, matrix_payload, parse_junit
from scripts.validate_g1_runner import GIT_SHA_RE, SHA256_RE, validate_manifest_strict


ROOT = Path(__file__).resolve().parents[1]


def test_g1_manifest_pins_all_six_components():
    manifest = json.loads(
        (ROOT / "governance" / "g1" / "components.json").read_text(encoding="utf-8")
    )
    assert validate_manifest_strict(manifest) == []
    assert [item["id"] for item in manifest["tracks"]] == TRACK_IDS
    assert len({item["sha"] for item in manifest["tracks"]}) == 6
    matrix = matrix_payload(manifest)
    assert [item["track"] for item in matrix["include"]] == TRACK_IDS
    assert all(GIT_SHA_RE.fullmatch(item["sha"]) for item in matrix["include"])


def test_g1_distinguishes_git_sha_from_sha256_content_digest():
    assert GIT_SHA_RE.fullmatch("a" * 40)
    assert not GIT_SHA_RE.fullmatch("a" * 64)
    assert SHA256_RE.fullmatch("b" * 64)
    assert not SHA256_RE.fullmatch("b" * 40)

    manifest = json.loads(
        (ROOT / "governance" / "g1" / "components.json").read_text(encoding="utf-8")
    )
    invalid = copy.deepcopy(manifest)
    invalid["tracks"][0]["sha"] = "c" * 64
    assert "G1_A_SHA" in validate_manifest_strict(invalid)


def test_g1_and_baseline_workflows_use_node24_actions_and_concurrency():
    for relative in (
        ".github/workflows/ci.yml",
        ".github/workflows/g0.yml",
        ".github/workflows/g1.yml",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "actions/checkout@v4" not in text
        assert "actions/setup-python@v5" not in text
        assert "actions/checkout@v7" in text
        assert "actions/setup-python@v6" in text
        assert "concurrency:" in text
        assert "cancel-in-progress: true" in text


def test_g1_junit_parser_records_executed_test_names(tmp_path: Path):
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuite tests="2" failures="0" errors="0" skipped="0">'
        '<testcase classname="g1" name="test_positive" />'
        '<testcase classname="g1" name="test_restart[param]" />'
        "</testsuite>",
        encoding="utf-8",
    )
    parsed = parse_junit(junit)
    assert parsed["tests"] == 2
    assert parsed["failures"] == 0
    assert parsed["errors"] == 0
    assert parsed["testcase_names"] == ["test_positive", "test_restart[param]"]
