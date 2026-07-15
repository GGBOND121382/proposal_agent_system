from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.util import sha256_bytes, utc_now, write_json


TARGETS = [
    "tests/test_single_section_chain.py",
    "tests/test_runtime_recovery.py",
    "tests/test_quality_track_e.py",
    "tests/test_d_track.py",
    "tests/test_f_workflow_recovery.py::test_small_single_section_authoring_export_chain",
]


def _run(command: list[str], *, cwd: Path, log_path: Path) -> int:
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    log_path.write_text(completed.stdout, encoding="utf-8")
    sys.stdout.write(completed.stdout)
    return int(completed.returncode)


def build_report(evidence_dir: Path) -> dict[str, Any]:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    compile_log = evidence_dir / "compile.log"
    pack_log = evidence_dir / "prompt-pack.log"
    pytest_log = evidence_dir / "pytest.log"
    junit_path = evidence_dir / "junit.xml"

    compile_status = _run(
        [sys.executable, "-m", "compileall", "-q", "app", "scripts", "tests/test_single_section_chain.py"],
        cwd=ROOT,
        log_path=compile_log,
    )
    pack_status = _run(
        [sys.executable, "prompt_pack/tools/validate_pack.py"],
        cwd=ROOT,
        log_path=pack_log,
    )
    pytest_status = _run(
        [sys.executable, "-m", "pytest", "-q", *TARGETS, f"--junitxml={junit_path}"],
        cwd=ROOT,
        log_path=pytest_log,
    )

    checks = {
        "compile": compile_status,
        "prompt_pack": pack_status,
        "targeted_pytest": pytest_status,
    }
    status = "PASS" if all(value == 0 for value in checks.values()) else "FAIL"
    files = [compile_log, pack_log, pytest_log, junit_path]
    report = {
        "schema_version": "1.0",
        "generated_at": utc_now(),
        "status": status,
        "scope": "G2_S1_SINGLE_SECTION_COMPLETE_CHAIN",
        "chain": [
            "P-WRITE-BLUEPRINT",
            "P-WRITE-BLUEPRINT-CRITIC",
            "P-TARGETED-REPAIR (at most once when required)",
            "P-WRITE-CONTENT",
            "P-WRITE-CRITIC",
            "P-TARGETED-REPAIR (at most once when required)",
            "P-EXPRESSION-POLISH",
            "P-EXPRESSION-CRITIC",
            "CANDIDATE_REVIEW",
            "FINAL_CONTENT_SECURITY_APPROVAL",
            "FINAL_EXPORT_APPROVAL",
            "DOCX_EXPORT",
        ],
        "checks": checks,
        "test_targets": TARGETS,
        "evidence": {
            path.name: {
                "path": str(path),
                "sha256": sha256_bytes(path.read_bytes()) if path.exists() else None,
                "size_bytes": path.stat().st_size if path.exists() else 0,
            }
            for path in files
        },
        "invariants": {
            "exactly_one_section_in_s1_mode": True,
            "section_scoped_repair_budget": 1,
            "blueprint_repair_requires_critic_rereview": True,
            "content_repair_requires_critic_rereview": True,
            "expression_critic_must_pass_without_manual_override": True,
            "export_uses_expression_critic_approved_candidate_only": True,
            "open_p0_p1_findings_block_export": True,
            "checkpoint_progress_is_persisted_after_each_run": True,
        },
    }
    write_json(evidence_dir / "S1_ACCEPTANCE.json", report)
    markdown = [
        "# G2 S1 单章节完整链验收",
        "",
        f"- 结果：**{status}**",
        f"- 时间：`{report['generated_at']}`",
        f"- 测试目标：`{len(TARGETS)}`",
        "",
        "## 检查结果",
        "",
        *[f"- `{name}`：{'PASS' if code == 0 else f'FAIL ({code})'}" for name, code in checks.items()],
        "",
        "## 证据",
        "",
        *[f"- `{name}`：`{meta['sha256']}`" for name, meta in report["evidence"].items()],
        "",
    ]
    (evidence_dir / "S1_ACCEPTANCE.md").write_text("\n".join(markdown), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the G2 S1 single-section complete chain.")
    parser.add_argument("--evidence-dir", type=Path, default=ROOT / "recovery_evidence" / "s1" / "local")
    args = parser.parse_args()
    report = build_report(args.evidence_dir.resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
