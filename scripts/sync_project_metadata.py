from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
START = "<!-- BEGIN GENERATED BUILD STATS -->"
END = "<!-- END GENERATED BUILD STATS -->"


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def project_version() -> str:
    import tomllib
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def collected_tests() -> int:
    cp = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=120,
    )
    output = "\n".join([cp.stdout, cp.stderr])
    match = re.search(r"(\d+) tests? collected", output)
    if cp.returncode not in {0, 5} or not match:
        raise RuntimeError(f"pytest collection failed:\n{output[-4000:]}")
    return int(match.group(1))


def counts() -> dict[str, int | str]:
    registry = read_json(ROOT / "prompt_pack/config/prompt_registry.json")
    replay = read_json(ROOT / "prompt_pack/replay/manifest.json")
    return {
        "version": project_version(),
        "prompt_count": len(registry["prompts"]),
        "prompt_schema_files": len(list((ROOT / "prompt_pack/schemas").rglob("*.json"))),
        "staged_schema_files": len(list(ROOT.glob("stage*_tools/*.schema.json"))),
        "replay_cases": len(replay["cases"]),
        "automated_tests": collected_tests(),
    }


def update_runtime_report(stats: dict[str, int | str]) -> None:
    path = ROOT / "RUNTIME_BUILD_REPORT.json"
    previous = read_json(path) if path.exists() else {}
    old_counts = previous.get("counts") or {}
    current_counts = {
        "prompt_files": stats["prompt_count"],
        "prompt_schema_files": stats["prompt_schema_files"],
        "staged_schema_files": stats["staged_schema_files"],
        "replay_cases": stats["replay_cases"],
        "automated_tests": stats["automated_tests"],
    }
    stable = previous.get("version") == stats["version"] and all(old_counts.get(k) == v for k, v in current_counts.items())
    built_at = previous.get("built_at") if stable and previous.get("built_at") else datetime.now(timezone.utc).isoformat()

    historical = previous.get("historical_validation_snapshot")
    if historical is None:
        historical_keys = [
            "runtime_mode_tested", "prompt_pack_validation", "workflow_end_to_end",
            "docx_export_and_audit_package", "docx_render_visual_review",
            "docker_runtime_test", "live_model_test", "neutral_material_context",
            "online_privacy", "multi_section_authoring", "document_quality",
            "regression_fixes", "known_boundaries",
        ]
        historical = {key: previous[key] for key in historical_keys if key in previous}

    report = {
        "version": stats["version"],
        "built_at": built_at,
        "status": "METADATA_SYNCED",
        "scope": "Current code-derived metadata only; semantic and end-to-end pass claims require a fresh CI or acceptance run.",
        "counts": current_counts,
        "validation": {
            "metadata_sync": "PASS",
            "pytest_collection": "PASS",
            "full_test_suite": "NOT_EXECUTED_BY_METADATA_SYNC",
        },
        "historical_validation_snapshot": historical,
        "metadata_source": "scripts/sync_project_metadata.py",
    }
    write_json(path, report)


def update_readme(stats: dict[str, int | str]) -> None:
    path = ROOT / "README.md"
    text = path.read_text(encoding="utf-8")
    block = (
        f"{START}\n"
        "## 当前构建统计\n\n"
        f"- 产品版本：`{stats['version']}`；\n"
        f"- Prompt 注册项：{stats['prompt_count']}；\n"
        f"- Prompt Pack Schema：{stats['prompt_schema_files']}；Stage Schema：{stats['staged_schema_files']}；\n"
        f"- Replay 用例：{stats['replay_cases']}；\n"
        f"- pytest 收集用例：{stats['automated_tests']}。\n\n"
        "以上统计由 `scripts/sync_project_metadata.py` 从代码与测试自动生成；历史版本章节中的旧数字仅描述当时版本。\n"
        f"{END}"
    )
    if START in text and END in text:
        text = text[: text.index(START)] + block + text[text.index(END) + len(END):]
    else:
        insert_at = text.find("\n", text.find("\n") + 1)
        text = text[: insert_at + 1] + "\n" + block + "\n" + text[insert_at + 1:]
    text = re.sub(
        r"当前\s*(?:自动测试|pytest 收集用例)共\s*\d+\s*项，覆盖：",
        f"当前 pytest 收集用例共{stats['automated_tests']}项，覆盖：",
        text,
    )
    text = re.sub(r"- \d+ 个 Prompt 的正常 Replay 输入/输出；", f"- {stats['prompt_count']} 个已注册 Prompt 的正常 Replay 输入/输出；", text)
    text = text.replace("五条工作流和十二章逐章编制", "五条工作流和模板定义的全部章节逐章编制")
    text = text.replace("- 十二章模拟模型端到端申请书生成；", "- 模板定义章节的模拟模型端到端申请书生成；")
    text = re.sub(r"- 41章复杂申请书、\d+/\d+ Prompt覆盖和定向修复闭环；", "- 复杂申请书、全部已注册 Prompt 覆盖和定向修复闭环；", text)
    path.write_text(text, encoding="utf-8")


def update_status(stats: dict[str, int | str]) -> None:
    path = ROOT / "DEVELOPMENT_STATUS.md"
    text = path.read_text(encoding="utf-8")
    current_status = f"- 当前注册 {stats['prompt_count']} 个 Prompt、{stats['replay_cases']} 组 Replay；pytest 可收集 {stats['automated_tests']} 项测试。测试通过状态以最新 CI/本地回归报告为准。"
    text = re.sub(
        r"- 自动测试\d+项全部通过，Prompt Pack \d+个Prompt、\d+组Replay静态验证通过。",
        current_status,
        text,
    )
    text = re.sub(
        r"- 当前注册 \d+ 个 Prompt、\d+ 组 Replay；pytest 可收集 \d+ 项测试。测试通过状态以最新 CI/本地回归报告为准。",
        current_status,
        text,
    )
    text = re.sub(
        r"- Python静态编译、Prompt Pack校验和\d+项pytest：PASS；",
        "- Python静态编译与 Prompt Pack 校验由 CI 执行；pytest 数量从代码自动收集，禁止手工固化旧统计；",
        text,
    )
    path.write_text(text, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail when generated metadata would change")
    args = parser.parse_args()
    tracked = [ROOT / "RUNTIME_BUILD_REPORT.json", ROOT / "README.md", ROOT / "DEVELOPMENT_STATUS.md"]
    before = {path: path.read_text(encoding="utf-8") for path in tracked}
    stats = counts()
    update_runtime_report(stats)
    update_readme(stats)
    update_status(stats)
    changed = [str(path.relative_to(ROOT)) for path in tracked if path.read_text(encoding="utf-8") != before[path]]
    if args.check and changed:
        print(json.dumps({"status": "STALE", "changed": changed, "stats": stats}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
    print(json.dumps({"status": "PASS", "changed": changed, "stats": stats}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
