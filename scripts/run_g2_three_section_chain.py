from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else "UNAVAILABLE"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run G2 three-section cross-chapter orchestration acceptance.")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "recovery_evidence" / "g2_three_sections")
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    junit = output_dir / "junit.xml"
    log = output_dir / "pytest.log"
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "tests/test_g2_three_section_chain.py",
        f"--junitxml={junit}",
        "--tb=short",
    ]
    completed = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    log.write_text(completed.stdout, encoding="utf-8")
    report = {
        "gate": "G2_THREE_SECTION_CROSS_CHAPTER",
        "status": "PASS" if completed.returncode == 0 else "FAIL",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "source_commit": source_commit(),
        "runtime_mode": "SIMULATED",
        "semantic_model_acceptance": False,
        "scope": [
            "freeze exactly one BACKGROUND_AND_SIGNIFICANCE section",
            "freeze exactly one RESEARCH_CONTENT section",
            "freeze exactly one TECHNICAL_ROUTE section",
            "run complete section production chains",
            "route a blocking cross-section finding to responsible section",
            "regenerate only the affected section",
            "resume after a persisted interruption checkpoint",
            "close P1 only after a later independent Integration Critic run",
            "preserve workflow, gate, run, quality and export API routes",
        ],
        "test_command": command,
        "return_code": completed.returncode,
        "evidence": {
            "pytest_log": str(log),
            "pytest_log_sha256": sha256_file(log),
            "junit": str(junit) if junit.exists() else None,
            "junit_sha256": sha256_file(junit) if junit.exists() else None,
        },
        "limitations": [
            "This gate verifies deterministic orchestration, persistence, routing, repair and review behavior.",
            "It does not claim LIVE model semantic quality; G3 must run configured real models and real Skills.",
        ],
    }
    json_path = output_dir / "G2_THREE_SECTION_ACCEPTANCE.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md = [
        "# G2 三章节跨章链验收",
        "",
        f"- 结果：**{report['status']}**",
        f"- 源提交：`{report['source_commit']}`",
        "- 运行模式：`SIMULATED`（仅用于编排、持久化和质量生命周期回归）",
        f"- Pytest 日志 SHA-256：`{report['evidence']['pytest_log_sha256']}`",
        "",
        "## 已验证",
        "",
    ]
    md.extend(f"- {item}" for item in report["scope"])
    md.extend([
        "",
        "## 能力边界",
        "",
        "该报告不把确定性模拟器结果冒充真实模型语义验收。真实模型、真实公开研究和真实交付质量由 G3 单独验收。",
        "",
    ])
    (output_dir / "G2_THREE_SECTION_ACCEPTANCE.md").write_text("\n".join(md), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
