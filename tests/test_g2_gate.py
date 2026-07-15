from __future__ import annotations

import json
from pathlib import Path

from scripts.validate_g2 import aggregate


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _reports(root: Path) -> None:
    _write(root / "s1" / "S1_ACCEPTANCE.json", {
        "status": "PASS",
        "invariants": {
            "exactly_one_section_in_s1_mode": True,
            "section_scoped_repair_budget": 1,
            "blueprint_repair_requires_critic_rereview": True,
            "content_repair_requires_critic_rereview": True,
            "expression_critic_must_pass_without_manual_override": True,
            "export_uses_expression_critic_approved_candidate_only": True,
            "open_p0_p1_findings_block_export": True,
            "checkpoint_progress_is_persisted_after_each_run": True,
            "no_manual_body_edit_is_repair_evidence": True,
            "responsible_agent_performs_targeted_repair": True,
        },
    })
    _write(root / "s2" / "G2_THREE_SECTION_ACCEPTANCE.json", {
        "status": "PASS",
        "runtime_mode": "SIMULATED",
        "invariants": {
            "exact_three_section_contract": True,
            "no_manual_body_edit_is_repair_evidence": True,
            "responsible_agent_repairs_only_affected_section": True,
            "independent_later_integration_critic_required": True,
            "p0_p1_close_only_after_repair_and_rereview": True,
            "restart_reuses_unaffected_sections": True,
        },
    })
    _write(root / "s3" / "S3_ACCEPTANCE.json", {
        "status": "PASS",
        "semantic_evidence_mode": "G2_ORCHESTRATION_FIXTURE_NOT_LIVE_SEMANTIC_PROOF",
        "source_commit": "fixture",
    })
    _write(root / "s3" / "S3_REVERIFY.json", {"status": "PASS", "failures": []})
    _write(root / "s3" / "S3_CHAIN_MANIFEST.json", {
        "status": "PASS",
        "research": {"records": [
            {"role": "RESEARCH_MANIFEST"}, {"role": "RESEARCH_SOURCE_INDEX"}
        ]},
        "claim_binding": {"record": {"role": "PUBLIC_CLAIM_BINDINGS"}},
        "mermaid": {"records": [
            {"role": "MERMAID_SOURCE"}, {"role": "MERMAID_SVG"}, {"role": "MERMAID_PNG"}
        ]},
        "export": {
            "delivery_status": "PASS", "blocking_finding_count": 0,
            "records": [
                {"role": "DOCX"}, {"role": "PDF"}, {"role": "DELIVERY_VALIDATION"},
                {"role": "WORKFLOW_CHECKPOINT"}
            ],
        },
    })


def test_g2_aggregate_passes_only_with_three_strict_reports(tmp_path: Path, monkeypatch):
    reports = tmp_path / "reports"
    _reports(reports)
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    summary = aggregate(reports, tmp_path / "G2.json", tmp_path / "G2.md")
    assert summary["status"] == "PASS"
    assert summary["requirements"]["manual_body_edit_allowed"] is False
    assert set(summary["scenarios"]) == {"S1", "S2", "S3"}


def test_g2_aggregate_rejects_manual_override_or_missing_rereview(tmp_path: Path, monkeypatch):
    reports = tmp_path / "reports"
    _reports(reports)
    path = reports / "s2" / "G2_THREE_SECTION_ACCEPTANCE.json"
    payload = json.loads(path.read_text())
    payload["invariants"]["no_manual_body_edit_is_repair_evidence"] = False
    payload["invariants"]["independent_later_integration_critic_required"] = False
    _write(path, payload)
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    summary = aggregate(reports, tmp_path / "G2.json", tmp_path / "G2.md")
    assert summary["status"] == "FAIL"
    assert any("no_manual_body_edit" in error for error in summary["errors"])
    assert any("independent_later" in error for error in summary["errors"])


def test_g2_aggregate_rejects_tampered_s3_chain(tmp_path: Path, monkeypatch):
    reports = tmp_path / "reports"
    _reports(reports)
    path = reports / "s3" / "S3_ACCEPTANCE.json"
    restart_path = reports / "s3" / "S3_REVERIFY.json"
    _write(restart_path, {"status": "FAIL", "failures": ["tampered"]})
    manifest_path = reports / "s3" / "S3_CHAIN_MANIFEST.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["export"]["records"] = [
        item for item in manifest["export"]["records"] if item["role"] != "PDF"
    ]
    _write(manifest_path, manifest)
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    summary = aggregate(reports, tmp_path / "G2.json", tmp_path / "G2.md")
    assert summary["status"] == "FAIL"
    assert "G2_S3_RESTART" in summary["errors"]
    assert any("PDF" in error for error in summary["errors"])
