from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.build_g0_recovery_bundle import build_bundle
from scripts.validate_g0 import ROOT, validate_repository
from scripts.verify_g0_recovery_bundle import extract_bundle, verify_bundle


def test_g0_semantic_contract_passes() -> None:
    report = validate_repository(ROOT, skip_git_history=True)
    assert report["status"] == "PASS", report["errors"]


def test_g0_frozen_paths_pass_when_git_history_is_available() -> None:
    probe = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--is-inside-work-tree"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if probe.returncode != 0:
        pytest.skip("Git history is not available")
    report = validate_repository(ROOT)
    assert report["status"] == "PASS", report["errors"]


def test_g0_recovery_bundle_is_self_verifying(tmp_path: Path) -> None:
    probe = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if probe.returncode != 0:
        pytest.skip("Git checkout is required to build the recovery bundle")

    bundle = tmp_path / "g0-recovery.zip"
    result = build_bundle(bundle, root=ROOT)
    assert result["status"] == "PASS"
    report = verify_bundle(bundle)
    assert report["status"] == "PASS", report["errors"]

    restored = tmp_path / "restored"
    extract_bundle(bundle, restored)
    assert (restored / "source" / "pyproject.toml").is_file()
    assert (restored / "source" / "prompt_pack" / "config" / "prompt_registry.json").is_file()
    manifest = json.loads((restored / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["gate"] == "G0"
    assert manifest["source_commit"] == probe.stdout.strip()
