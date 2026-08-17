from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from scripts.verify_semantic_contract_delivery import (
    DeliveryVerificationError,
    build_unified_patch,
    changed_paths,
    file_sha256,
    normalized_tree_hash,
    verify_archive_artifacts,
    verify_archive_matches_source,
    verify_changeset,
    verify_published_semantics,
)


def _write_tree(root: Path, values: dict[str, str]) -> None:
    for relative, content in values.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _changeset(baseline: Path, candidate: Path, patch: Path) -> dict:
    paths = changed_paths(baseline, candidate)
    base_files = {p.relative_to(baseline).as_posix(): p for p in baseline.rglob("*") if p.is_file()}
    final_files = {p.relative_to(candidate).as_posix(): p for p in candidate.rglob("*") if p.is_file()}
    hashes = {}
    for relative in paths:
        left = base_files.get(relative)
        right = final_files.get(relative)
        hashes[relative] = {
            "baseline_sha256": file_sha256(left) if left else None,
            "final_sha256": file_sha256(right) if right else None,
        }
    return {
        "baseline": {
            "source_file_count": len(base_files),
            "source_tree_sha256": normalized_tree_hash(baseline),
        },
        "source_integrity": {
            "current_file_count": len(final_files),
            "changed_path_count": len(paths),
            "final_source_tree_sha256": normalized_tree_hash(candidate),
        },
        "changed_paths": paths,
        "changed_file_hashes": hashes,
        "patch": {"sha256": file_sha256(patch)},
    }


def test_delivery_patch_and_changeset_are_candidate_derived(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    _write_tree(
        baseline,
        {
            "app/a.py": "VALUE = 1\n",
            "prompt_pack/SHA256SUMS.txt": "",
            "prompt_pack/MANIFEST.md": "# Manifest\n",
        },
    )
    _write_tree(
        candidate,
        {
            "app/a.py": "VALUE = 2\n",
            "prompt_pack/SHA256SUMS.txt": "",
            "prompt_pack/MANIFEST.md": "# Manifest\n",
        },
    )
    patch = tmp_path / "change.patch"
    patch.write_text(build_unified_patch(baseline, candidate), encoding="utf-8")
    changeset_path = tmp_path / "changeset.json"
    changeset_path.write_text(
        json.dumps(_changeset(baseline, candidate, patch), indent=2),
        encoding="utf-8",
    )

    result = verify_changeset(baseline, candidate, patch, changeset_path)
    assert result["changed_paths"] == 1
    assert result["baseline_tree_sha256"] != result["candidate_tree_sha256"]


def test_delivery_verifier_rejects_post_changeset_source_mutation(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    _write_tree(
        baseline,
        {
            "app/a.py": "VALUE = 1\n",
            "prompt_pack/SHA256SUMS.txt": "",
            "prompt_pack/MANIFEST.md": "# Manifest\n",
        },
    )
    _write_tree(
        candidate,
        {
            "app/a.py": "VALUE = 2\n",
            "prompt_pack/SHA256SUMS.txt": "",
            "prompt_pack/MANIFEST.md": "# Manifest\n",
        },
    )
    patch = tmp_path / "change.patch"
    patch.write_text(build_unified_patch(baseline, candidate), encoding="utf-8")
    changeset_path = tmp_path / "changeset.json"
    changeset_path.write_text(
        json.dumps(_changeset(baseline, candidate, patch), indent=2),
        encoding="utf-8",
    )
    (candidate / "app/a.py").write_text("VALUE = 3\n", encoding="utf-8")

    with pytest.raises(DeliveryVerificationError, match="final_source_tree_sha256 mismatch"):
        verify_changeset(baseline, candidate, patch, changeset_path)


def test_delivery_verifier_rejects_archive_drift(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    _write_tree(candidate, {"app/a.py": "VALUE = 2\n"})
    archive = tmp_path / "delivery.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("source/app/a.py", "VALUE = 99\n")

    with pytest.raises(DeliveryVerificationError, match="archive source differs"):
        verify_archive_matches_source(archive, candidate)


def test_normalized_tree_hash_ignores_runtime_cache_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_tree(source, {"app/a.py": "VALUE = 1\n"})
    before = normalized_tree_hash(source)
    _write_tree(
        source,
        {
            "app/__pycache__/a.cpython-313.pyc": "cache",
            ".pytest_cache/v/cache/nodeids": "[]",
        },
    )
    assert normalized_tree_hash(source) == before


def test_delivery_verifier_rejects_embedded_artifact_drift(tmp_path: Path) -> None:
    archive = tmp_path / "delivery.zip"
    patch = tmp_path / "change.patch"
    patch.write_text("external\n", encoding="utf-8")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("change.patch", "stale\n")

    with pytest.raises(DeliveryVerificationError, match="published artifact differs"):
        verify_archive_artifacts(archive, [patch])


def test_delivery_patch_builder_rejects_newline_only_ambiguity(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    _write_tree(baseline, {"app/a.py": "VALUE = 1\n"})
    _write_tree(candidate, {"app/a.py": "VALUE = 2"})

    with pytest.raises(DeliveryVerificationError, match="must end with LF"):
        build_unified_patch(baseline, candidate)


def _prompt_audit_payload() -> dict:
    return {
        "status": "PASS",
        "summary": {
            "prompt_count": 30,
            "replay_count": 150,
            "blocking_issue_count": 0,
            "informational_issue_count": 1,
            "replay_status_counts": {"BLOCK": 30, "NEED_USER_INPUT": 60, "PASS": 30},
        },
    }


def _report_for_prompt_audit() -> str:
    return "\n".join(
        [
            "### Prompt Contract Audit",
            "- status：**PASS**",
            "- prompts：**30**",
            "- replay cases：**150**",
            "- blocking issues：**0**",
            "- informational issues：**1**",
            "",
        ]
    )


def test_delivery_verifier_checks_cross_artifact_prompt_audit_semantics(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps(_prompt_audit_payload(), ensure_ascii=False), encoding="utf-8")
    changeset_path = tmp_path / "changeset.json"
    changeset_path.write_text(
        json.dumps(
            {
                "validation": {
                    "prompt_contract_audit": {
                        "status": "PASS",
                        "prompt_count": 30,
                        "replay_count": 150,
                        "blocking_issue_count": 0,
                        "informational_issue_count": 1,
                        "replay_status_counts": {
                            "BLOCK": 30,
                            "NEED_USER_INPUT": 60,
                            "PASS": 30,
                        },
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    report_path = tmp_path / "report.md"
    report_path.write_text(_report_for_prompt_audit(), encoding="utf-8")

    summary = verify_published_semantics(changeset_path, report_path, audit_path)
    assert summary["prompt_count"] == 30
    assert summary["replay_count"] == 150


def test_delivery_verifier_rejects_cross_artifact_prompt_audit_drift(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps(_prompt_audit_payload(), ensure_ascii=False), encoding="utf-8")
    changeset_path = tmp_path / "changeset.json"
    changeset_path.write_text(
        json.dumps(
            {
                "validation": {
                    "prompt_contract_audit": {
                        "status": "PASS",
                        "prompt_count": None,
                        "replay_count": None,
                        "blocking_issue_count": None,
                        "informational_issue_count": None,
                        "replay_status_counts": None,
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    report_path = tmp_path / "report.md"
    report_path.write_text(
        "### Prompt Contract Audit\n- status：**PASS**\n- prompts：**None**\n",
        encoding="utf-8",
    )

    with pytest.raises(DeliveryVerificationError, match="prompt_count mismatch"):
        verify_published_semantics(changeset_path, report_path, audit_path)
