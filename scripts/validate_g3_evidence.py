from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def validate(report_path: Path, *, evidence_root: Path | None = None) -> dict[str, Any]:
    report = _load(report_path)
    errors: list[str] = []
    if report.get("gate") != "G3":
        errors.append("GATE_NOT_G3")
    if report.get("status") != "PASS":
        errors.append(f"G3_NOT_PASS:{report.get('status')}")
    checks = report.get("checks") or {}
    failed = sorted(str(key) for key, value in checks.items() if value is not True)
    errors.extend(f"CHECK_FAILED:{key}" for key in failed)
    metrics = report.get("metrics") or {}
    if int(metrics.get("section_count") or 0) != 14:
        errors.append("SECTION_COUNT_NOT_14")
    if int(metrics.get("concurrent_group_count") or 0) != 5:
        errors.append("GROUP_COUNT_NOT_5")
    if int(metrics.get("cross_chapter_review_count") or 0) != 5:
        errors.append("CROSS_REVIEW_COUNT_NOT_5")
    if int(metrics.get("research_source_count") or 0) < 8:
        errors.append("INSUFFICIENT_LIVE_RESEARCH_SOURCES")
    if int(metrics.get("open_blocker_count") or -1) != 0:
        errors.append("OPEN_BLOCKERS_REMAIN")
    source_commit = str(report.get("source_commit") or "")
    if not source_commit or source_commit == "UNAVAILABLE":
        errors.append("SOURCE_COMMIT_MISSING")
    preflight = report.get("preflight") or {}
    if preflight.get("status") != "PASS":
        errors.append("PREFLIGHT_NOT_PASS")
    if evidence_root:
        required = [
            "G3_ACCEPTANCE.json", "G3_ACCEPTANCE.md", "G3_PREFLIGHT.json",
            "workflow_checkpoint.sqlite", "cross_chapter_review_history.json",
            "formal_materials/material_manifest.json", "requests", "responses",
            "prompt_traces", "model_calls",
        ]
        for relative in required:
            if not (evidence_root / relative).exists():
                errors.append(f"EVIDENCE_MISSING:{relative}")
    return {
        "schema_version": "1.0",
        "gate": "G3_EVIDENCE_VALIDATION",
        "status": "PASS" if not errors else "FAIL",
        "report": str(report_path),
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate G3 formal capability evidence.")
    parser.add_argument("report", type=Path)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    result = validate(
        args.report.resolve(),
        evidence_root=args.evidence_root.resolve() if args.evidence_root else None,
    )
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
