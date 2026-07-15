from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPORT_NAMES = {
    "S1": "S1_ACCEPTANCE.json",
    "S2": "G2_THREE_SECTION_ACCEPTANCE.json",
    "S3": "S3_ACCEPTANCE.json",
}


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_unique(root: Path, filename: str) -> Path:
    matches = sorted(path for path in root.rglob(filename) if path.is_file())
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one {filename} under {root}, found {len(matches)}")
    return matches[0]


def validate_s1(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if report.get("status") != "PASS":
        errors.append("G2_S1_STATUS")
    invariants = report.get("invariants") or {}
    required_true = {
        "exactly_one_section_in_s1_mode",
        "blueprint_repair_requires_critic_rereview",
        "content_repair_requires_critic_rereview",
        "expression_critic_must_pass_without_manual_override",
        "export_uses_expression_critic_approved_candidate_only",
        "open_p0_p1_findings_block_export",
        "checkpoint_progress_is_persisted_after_each_run",
        "no_manual_body_edit_is_repair_evidence",
        "responsible_agent_performs_targeted_repair",
    }
    for key in sorted(required_true):
        if invariants.get(key) is not True:
            errors.append(f"G2_S1_INVARIANT:{key}")
    if int(invariants.get("section_scoped_repair_budget", 0)) != 1:
        errors.append("G2_S1_REPAIR_BUDGET")
    return errors


def validate_s2(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if report.get("status") != "PASS":
        errors.append("G2_S2_STATUS")
    if report.get("runtime_mode") != "SIMULATED":
        errors.append("G2_S2_RUNTIME_MODE")
    invariants = report.get("invariants") or {}
    required_true = {
        "exact_three_section_contract",
        "no_manual_body_edit_is_repair_evidence",
        "responsible_agent_repairs_only_affected_section",
        "independent_later_integration_critic_required",
        "p0_p1_close_only_after_repair_and_rereview",
        "restart_reuses_unaffected_sections",
    }
    for key in sorted(required_true):
        if invariants.get(key) is not True:
            errors.append(f"G2_S2_INVARIANT:{key}")
    return errors


def validate_s3(report: dict[str, Any], report_path: Path, reports_root: Path) -> list[str]:
    errors: list[str] = []
    if report.get("status") != "PASS":
        errors.append("G2_S3_STATUS")
    if report.get("semantic_evidence_mode") not in {
        "G2_ORCHESTRATION_FIXTURE_NOT_LIVE_SEMANTIC_PROOF",
        "APPROVED_CONNECTOR_ARCHIVE",
    }:
        errors.append("G2_S3_EVIDENCE_MODE")
    try:
        restart_path = find_unique(reports_root, "S3_REVERIFY.json")
    except ValueError:
        try:
            restart_path = find_unique(reports_root, "S3_RESTART_VERIFY.json")
        except ValueError as exc:
            restart_path = None
            errors.append(f"G2_S3_RESTART_REPORT:{exc}")
    if restart_path is not None:
        restart = load_json(restart_path)
        if restart.get("status") != "PASS":
            errors.append("G2_S3_RESTART")
    try:
        manifest_path = find_unique(reports_root, "S3_CHAIN_MANIFEST.json")
        manifest = load_json(manifest_path)
    except Exception as exc:
        manifest = {}
        errors.append(f"G2_S3_MANIFEST:{exc}")
    roles: set[str] = set()
    for record in (manifest.get("research") or {}).get("records") or []:
        if isinstance(record, dict):
            roles.add(str(record.get("role") or ""))
    claim_record = (manifest.get("claim_binding") or {}).get("record")
    if isinstance(claim_record, dict):
        roles.add(str(claim_record.get("role") or ""))
    for record in (manifest.get("mermaid") or {}).get("records") or []:
        if isinstance(record, dict):
            roles.add(str(record.get("role") or ""))
    for record in (manifest.get("export") or {}).get("records") or []:
        if isinstance(record, dict):
            roles.add(str(record.get("role") or ""))
    required_roles = {
        "RESEARCH_MANIFEST",
        "RESEARCH_SOURCE_INDEX",
        "PUBLIC_CLAIM_BINDINGS",
        "MERMAID_SOURCE",
        "MERMAID_SVG",
        "MERMAID_PNG",
        "DOCX",
        "PDF",
        "DELIVERY_VALIDATION",
        "WORKFLOW_CHECKPOINT",
    }
    missing = sorted(required_roles - roles)
    if missing:
        errors.append("G2_S3_ARTIFACT_ROLES:" + ",".join(missing))
    if (manifest.get("export") or {}).get("delivery_status") != "PASS":
        errors.append("G2_S3_DELIVERY_STATUS")
    if int((manifest.get("export") or {}).get("blocking_finding_count") or 0) != 0:
        errors.append("G2_S3_BLOCKING_FINDINGS")
    source_commit = str(report.get("source_commit") or "")
    expected = os.getenv("GITHUB_SHA")
    if expected and source_commit and source_commit != expected:
        errors.append(f"G2_S3_SOURCE_COMMIT expected={expected} actual={source_commit}")
    if not report_path.is_file():
        errors.append("G2_S3_REPORT_MISSING")
    return errors


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# G2 小规模集成总验收",
        "",
        f"- 结果：**{summary['status']}**",
        f"- 源提交：`{summary['source_commit']}`",
        f"- 生成时间：`{summary['generated_at']}`",
        "- 正文人工修改：**禁止，且不作为返修证据**",
        "- 返修责任：由责任 Agent 定向返修，随后由不同 Critic 运行独立复审",
        "",
        "| 场景 | 状态 | 报告 SHA-256 |",
        "|---|---|---|",
    ]
    for track in ("S1", "S2", "S3"):
        item = summary["scenarios"][track]
        lines.append(f"| {track} | {item['status']} | `{item['report_sha256']}` |")
    if summary["errors"]:
        lines.extend(["", "## 错误", ""])
        lines.extend(f"- `{error}`" for error in summary["errors"])
    lines.extend([
        "",
        "## 能力边界",
        "",
        "G2 验证同一提交上的确定性编排、责任返修、独立复审、持久化、公开来源归档、真实 Mermaid 渲染和 DOCX/PDF 交付链。S1/S2 的 SIMULATED 响应以及 S3 的固定连接器夹具不作为真实模型语义质量证明；真实模型与真实 Skill 的完整申请书能力由 G3 验收。",
        "",
    ])
    return "\n".join(lines)


def aggregate(reports_dir: Path, report_json: Path, report_md: Path) -> dict[str, Any]:
    errors: list[str] = []
    scenarios: dict[str, Any] = {}
    validators = {"S1": validate_s1, "S2": validate_s2}
    for scenario, filename in REPORT_NAMES.items():
        try:
            path = find_unique(reports_dir, filename)
            report = load_json(path)
        except Exception as exc:
            errors.append(f"G2_{scenario}_REPORT:{exc}")
            scenarios[scenario] = {"status": "FAIL", "report_path": None, "report_sha256": None}
            continue
        scenario_errors = validate_s3(report, path, reports_dir) if scenario == "S3" else validators[scenario](report)
        expected_commit = os.getenv("GITHUB_SHA")
        declared_commit = str(report.get("source_commit") or "")
        if expected_commit and declared_commit != expected_commit:
            scenario_errors.append(
                f"G2_{scenario}_SOURCE_COMMIT expected={expected_commit} actual={declared_commit or 'MISSING'}"
            )
        errors.extend(scenario_errors)
        scenarios[scenario] = {
            "status": "PASS" if not scenario_errors else "FAIL",
            "declared_status": report.get("status"),
            "report_path": str(path),
            "report_sha256": sha256_file(path),
        }

    summary = {
        "schema_version": "1.0",
        "gate": "G2",
        "status": "PASS" if not errors and set(scenarios) == set(REPORT_NAMES) else "FAIL",
        "source_commit": os.getenv("GITHUB_SHA", "UNAVAILABLE"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scenarios": scenarios,
        "requirements": {
            "parallel_same_commit_validation": True,
            "manual_body_edit_allowed": False,
            "responsible_agent_autonomous_repair_required": True,
            "independent_rereview_required": True,
            "recovery_bundle_required": True,
        },
        "errors": errors,
    }
    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_md.parent.mkdir(parents=True, exist_ok=True)
    report_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_md.write_text(render_markdown(summary), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggregate G2 S1/S2/S3 reports into a single hard gate.")
    parser.add_argument("--reports-dir", type=Path, required=True)
    parser.add_argument("--report-json", type=Path, required=True)
    parser.add_argument("--report-md", type=Path, required=True)
    args = parser.parse_args()
    summary = aggregate(args.reports_dir.resolve(), args.report_json.resolve(), args.report_md.resolve())
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
