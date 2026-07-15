from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.track_b import TrackBAgentPromptValidator


def render_markdown(report: dict) -> str:
    lines = [
        "# 轨道 B：Agent、Prompt 与论证生成内核验收报告",
        "",
        f"- 总体状态：**{report['status']}**",
        "- 验收范围：B1—B10",
        "- 说明：本报告验证仓库契约与确定性质量门；真实模型语义能力仍需在 LIVE 能力验收中单独证明。",
        "",
        "| ID | 状态 | 验收结论 | 证据 |",
        "|---|---|---|---|",
    ]
    for track_id in sorted(report["checks"], key=lambda value: int(value[1:])):
        item = report["checks"][track_id]
        evidence = "<br>".join(f"`{value}`" for value in item["evidence"])
        lines.append(
            f"| {track_id} | {'PASS' if item['passed'] else 'FAIL'} | {item['detail']} | {evidence} |"
        )
    lines.extend([
        "",
        "## 本地复核",
        "",
        "```bash",
        "python scripts/validate_track_b.py --json-out recovery_evidence/track_b/acceptance.json --md-out recovery_evidence/track_b/acceptance.md",
        "python -m pytest -q tests/test_track_b_agent_prompt.py",
        "```",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Track B Agent/Prompt kernel acceptance conditions.")
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--md-out", type=Path)
    args = parser.parse_args()

    report = TrackBAgentPromptValidator.validate_repository(ROOT)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text + "\n", encoding="utf-8")
    if args.md_out:
        args.md_out.parent.mkdir(parents=True, exist_ok=True)
        args.md_out.write_text(render_markdown(report), encoding="utf-8")

    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
