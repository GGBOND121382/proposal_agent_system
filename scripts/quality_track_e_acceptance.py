from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEST_FILE = "tests/test_quality_track_e.py"
CASES = {
    "E1": ["test_e1_relation_fact_metric_and_source_rules_are_deterministic"],
    "E2": ["test_e2_section_gate_uses_profile_specific_responsibility"],
    "E3": ["test_e3_e4_integration_checks_conflict_mapping_and_full_argument_chain"],
    "E4": ["test_e3_e4_integration_checks_conflict_mapping_and_full_argument_chain"],
    "E5": ["test_e5_delivery_findings_route_engineering_and_writing_separately"],
    "E6": [
        "test_e6_p1_requires_repair_and_independent_critic_review",
        "test_e6_export_gate_cannot_override_open_quality_blocker",
        "test_quality_matrix_is_auditable_and_append_only",
    ],
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Track E deterministic quality acceptance.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "recovery_evidence" / "track_e",
        help="Directory for acceptance.json, acceptance.md, pytest.log and junit.xml.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    junit = output_dir / "junit.xml"
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        TEST_FILE,
        f"--junitxml={junit}",
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    log = output_dir / "pytest.log"
    log.write_text(completed.stdout, encoding="utf-8")
    status = "PASS" if completed.returncode == 0 else "FAIL"
    report = {
        "track": "E",
        "status": status,
        "command": command,
        "returncode": completed.returncode,
        "requirements": {
            work_package: {
                "status": status,
                "tests": tests,
            }
            for work_package, tests in CASES.items()
        },
        "evidence": {
            "test_file": TEST_FILE,
            "test_file_sha256": sha256(ROOT / TEST_FILE),
            "pytest_log": display_path(log),
            "pytest_log_sha256": sha256(log),
            "junit_xml": display_path(junit) if junit.exists() else None,
            "junit_xml_sha256": sha256(junit) if junit.exists() else None,
        },
        "scope_boundary": (
            "This report proves deterministic Track E rules, routing and lifecycle behavior. "
            "It does not treat REPLAY, MOCK or SIMULATED text as evidence of real-model semantic quality."
        ),
    }
    (output_dir / "acceptance.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# Track E Quality Acceptance",
        "",
        f"- Overall: **{status}**",
        f"- Command: `{' '.join(command)}`",
        f"- Focused test log: `{report['evidence']['pytest_log']}`",
        "",
        "## Work packages",
        "",
        "| Work package | Result | Executable evidence |",
        "|---|---|---|",
    ]
    for work_package, item in report["requirements"].items():
        tests = "<br>".join(f"`{name}`" for name in item["tests"])
        lines.append(f"| {work_package} | **{item['status']}** | {tests} |")
    lines.extend([
        "",
        "## Scope boundary",
        "",
        report["scope_boundary"],
        "",
    ])
    (output_dir / "acceptance.md").write_text("\n".join(lines), encoding="utf-8")
    print(completed.stdout, end="")
    print(f"Track E acceptance: {status}")
    print(f"Evidence: {output_dir}")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
