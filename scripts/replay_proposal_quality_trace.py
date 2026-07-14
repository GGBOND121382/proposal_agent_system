#!/usr/bin/env python3
from __future__ import annotations

import argparse
import collections
import copy
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.proposal_quality import ProposalQualityGuard


RESPONSIBILITY = {
    "P-PROJECT-DEFINITION-EXTRACT": {
        "agent": "PROJECT_KNOWLEDGE_AGENT",
        "failure": "将零散目标过早确认为完整项目知识图谱，缺少差距、问题、方法、验证、创新和基础节点。",
        "control": "proposal_contract + argument_graph_seed + 图谱完整性与来源等级校验。",
    },
    "P-PROJECT-READINESS-CRITIC": {
        "agent": "PROJECT_READINESS_CRITIC",
        "failure": "只对已出现的少量对象评分，未检查关键对象类型是否缺失，产生错误READY。",
        "control": "分阶段准备度、必需节点类型、前期基础证据和完整关系链校验。",
    },
    "P-TEMPLATE-EXTRACT": {
        "agent": "TEMPLATE_AGENT",
        "failure": "主要提取标题和格式，未把优秀范例中的论证顺序、段落功能和反模式转成规则。",
        "control": "argument_patterns、expression_patterns、quality_anti_patterns，且范例只允许影响逻辑与表达。",
    },
    "P-REVISION-PLAN": {
        "agent": "PLANNING_AGENT",
        "failure": "以章节覆盖和篇幅扩张为目标，将源文档全部标题变成同构写作任务。",
        "control": "narrative_architecture、主文页数预算、附件边界和逐章节Section Contract。",
    },
    "P-WRITE-BLUEPRINT": {
        "agent": "SECTION_ARGUMENT_AGENT",
        "failure": "跨章节复用通用段落骨架，段落没有独有命题、证据和新增信息身份。",
        "control": "章节画像、论证角色、primary_claim_id、novel_content_key和前文章节语义摘要。",
    },
    "P-WRITE-CONTENT": {
        "agent": "EVIDENCE_WRITING_AGENT",
        "failure": "把技术标签和系统实现说明扩写为正文，缺少方法实质与证据约束。",
        "control": "段落级claim/evidence/contract绑定、文种校验、信息键唯一性和方法/指标专用画像。",
    },
    "P-WRITE-CRITIC": {
        "agent": "SECTION_QUALITY_CRITIC",
        "failure": "未逐段阅读，只检查结构与Trace存在性，无法识别文种、方法、创新、基础和重复问题。",
        "control": "checked_paragraph_ids全覆盖、十维质量Scorecard和段落级证据。",
    },
    "P-INTEGRATION-CRITIC": {
        "agent": "DOCUMENT_INTEGRATION_CRITIC",
        "failure": "可能只接收部分候选章节，并对无效对象ID给出完整映射；未检查全文重复和页数边界。",
        "control": "候选集合完整性、真实ID映射、命题链、重复信息键、同构表达、文种和页数预算校验。",
    },
}


def load_traces(audit_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    trace_dir = audit_dir / "prompt_traces"
    if not trace_dir.is_dir():
        raise FileNotFoundError(f"missing prompt_traces directory: {trace_dir}")
    result: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(trace_dir.glob("*.json")):
        with path.open("r", encoding="utf-8") as handle:
            result.append((path, json.load(handle)))
    return result


def historical_metrics(traces: list[tuple[Path, dict[str, Any]]]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    by_prompt: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for _, trace in traces:
        by_prompt[str(trace.get("prompt_id"))].append(trace)

    pd_trace = (by_prompt.get("P-PROJECT-DEFINITION-EXTRACT") or [None])[-1]
    if pd_trace:
        pd = ((pd_trace.get("output") or {}).get("result") or {}).get("project_definition") or {}
        metrics["project_definition"] = {
            "item_count": len(pd.get("items") or []),
            "item_type_counts": dict(collections.Counter(str(item.get("item_type")) for item in pd.get("items") or [])),
            "relation_count": len(pd.get("relations") or []),
        }

    readiness = (by_prompt.get("P-PROJECT-READINESS-CRITIC") or [None])[-1]
    if readiness:
        result = ((readiness.get("output") or {}).get("result") or {})
        metrics["readiness"] = {
            "writeable_profile_count": len(result.get("writeable_section_profiles") or []),
            "blocked_profile_count": len(result.get("blocked_section_profiles") or []),
            "missing_input_count": len(result.get("missing_inputs") or []),
        }

    plan_trace = (by_prompt.get("P-REVISION-PLAN") or [None])[-1]
    if plan_trace:
        plan = (((plan_trace.get("output") or {}).get("result") or {}).get("revision_plan") or {})
        metrics["revision_plan"] = {
            "target_section_count": len(plan.get("target_section_ids") or []),
            "task_count": len(plan.get("tasks") or []),
            "has_narrative_architecture": bool(plan.get("narrative_architecture")),
        }

    critic_traces = by_prompt.get("P-WRITE-CRITIC") or []
    if critic_traces:
        checked_counts = [
            len((((trace.get("output") or {}).get("result") or {}).get("checked_paragraph_ids") or []))
            for trace in critic_traces
        ]
        dimension_counts = [
            len((((trace.get("output") or {}).get("result") or {}).get("profile_acceptance_results") or []))
            for trace in critic_traces
        ]
        metrics["section_critic"] = {
            "run_count": len(critic_traces),
            "runs_with_zero_checked_paragraphs": sum(1 for count in checked_counts if count == 0),
            "max_quality_dimension_count": max(dimension_counts, default=0),
        }

    integration = (by_prompt.get("P-INTEGRATION-CRITIC") or [None])[-1]
    if integration:
        payload = (integration.get("input_envelope") or {}).get("payload") or {}
        result = ((integration.get("output") or {}).get("result") or {})
        metrics["integration"] = {
            "candidate_section_count": len(payload.get("candidate_sections") or []),
            "document_map_section_count": len(payload.get("document_section_map") or []),
            "quality_dimension_count": len(result.get("quality_dimensions") or []),
            "mapping_check_count": len(result.get("mapping_checks") or []),
        }
    return metrics


def replay(traces: list[tuple[Path, dict[str, Any]]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    guard = ProposalQualityGuard()
    records: list[dict[str, Any]] = []
    code_counts: collections.Counter[str] = collections.Counter()
    prompt_rejections: collections.Counter[str] = collections.Counter()
    prompt_total: collections.Counter[str] = collections.Counter()
    errors: list[dict[str, str]] = []

    for path, trace in traces:
        prompt_id = str(trace.get("prompt_id") or "")
        if prompt_id not in RESPONSIBILITY:
            continue
        prompt_total[prompt_id] += 1
        output = copy.deepcopy(trace.get("output") or {})
        envelope = trace.get("input_envelope") or {}
        try:
            checked = guard.apply(prompt_id, envelope, output)
            codes = [str(item.get("code")) for item in checked.get("findings") or [] if str(item.get("code", "")).startswith("QG_")]
            for code in codes:
                code_counts[code] += 1
            if checked.get("status") != "PASS":
                prompt_rejections[prompt_id] += 1
            records.append({
                "trace_file": path.name,
                "prompt_id": prompt_id,
                "original_status": trace.get("status"),
                "replayed_status": checked.get("status"),
                "finding_codes": codes,
            })
        except Exception as exc:  # report malformed historical data rather than hiding it
            errors.append({"trace_file": path.name, "prompt_id": prompt_id, "error": f"{type(exc).__name__}: {exc}"})

    summary = {
        "replayed_trace_count": len(records),
        "replayed_rejected_count": sum(1 for item in records if item["replayed_status"] != "PASS"),
        "prompt_totals": dict(prompt_total),
        "prompt_rejections": dict(prompt_rejections),
        "finding_code_counts": dict(code_counts.most_common()),
        "errors": errors,
    }
    return records, summary


def markdown_report(report: dict[str, Any]) -> str:
    metrics = report["historical_metrics"]
    replay_summary = report["replay_summary"]
    lines = [
        "# 历史申请书Trace责任链与v0.6缺陷重放报告",
        "",
        "## 1. 历史运行事实",
        "",
        f"- Trace总数：{report['trace_count']}。",
    ]
    pd = metrics.get("project_definition", {})
    lines += [
        f"- 项目定义对象：{pd.get('item_count', 0)}个；关系：{pd.get('relation_count', 0)}条；类型分布：`{json.dumps(pd.get('item_type_counts', {}), ensure_ascii=False)}`。",
        f"- 修改计划目标章节：{metrics.get('revision_plan', {}).get('target_section_count', 0)}；写作任务：{metrics.get('revision_plan', {}).get('task_count', 0)}。",
        f"- 正文Critic运行：{metrics.get('section_critic', {}).get('run_count', 0)}；其中未记录任何已检查段落的运行：{metrics.get('section_critic', {}).get('runs_with_zero_checked_paragraphs', 0)}。",
        f"- 全篇Critic收到候选章节：{metrics.get('integration', {}).get('candidate_section_count', 0)}；文档映射章节：{metrics.get('integration', {}).get('document_map_section_count', 0)}。",
        "",
        "这些事实证明，低质量结果不是单一写作Agent造成，而是从项目定义、准备度、规划、章节生成、章节审查到全文审查连续失真。",
        "",
        "## 2. Agent责任与结构性修复",
        "",
        "| Prompt | Agent | 历史缺陷 | v0.6结构性控制 |",
        "|---|---|---|---|",
    ]
    for prompt_id, item in RESPONSIBILITY.items():
        lines.append(f"| `{prompt_id}` | {item['agent']} | {item['failure']} | {item['control']} |")
    lines += [
        "",
        "## 3. 使用v0.6确定性质量规则重放",
        "",
        f"- 纳入重放的相关Trace：{replay_summary['replayed_trace_count']}。",
        f"- 被判定需要修订或终止当前阶段：{replay_summary['replayed_rejected_count']}。",
        "",
        "### 各Prompt重放结果",
        "",
        "| Prompt | 历史运行数 | v0.6拒绝数 |",
        "|---|---:|---:|",
    ]
    for prompt_id in RESPONSIBILITY:
        lines.append(f"| `{prompt_id}` | {replay_summary['prompt_totals'].get(prompt_id, 0)} | {replay_summary['prompt_rejections'].get(prompt_id, 0)} |")
    lines += ["", "### 高频缺陷代码", ""]
    for code, count in list(replay_summary["finding_code_counts"].items())[:20]:
        lines.append(f"- `{code}`：{count}次。")
    lines += [
        "",
        "## 4. 结论",
        "",
        "历史链路会把浅项目图谱、同构章节计划、模板化蓝图、无段落覆盖的Critic和不完整全文输入连续判为合格。v0.6不是在末端增加一个总分，而是把项目论证图谱、章节合同、段落语义身份、全文候选完整性和确定性质量校验分别放到其应负责的阶段。历史Trace在这些阶段会被明确判定为不合格，不能再依赖人工空确认继续流转。",
    ]
    if replay_summary["errors"]:
        lines += ["", "## 5. 重放异常", ""]
        for item in replay_summary["errors"]:
            lines.append(f"- `{item['trace_file']}`：{item['error']}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay historical proposal prompt traces through the v0.6 quality guard.")
    parser.add_argument("audit_dir", type=Path)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--md-out", type=Path, required=True)
    args = parser.parse_args()

    traces = load_traces(args.audit_dir)
    records, replay_summary = replay(traces)
    report = {
        "audit_dir": str(args.audit_dir.resolve()),
        "trace_count": len(traces),
        "historical_metrics": historical_metrics(traces),
        "responsibility": RESPONSIBILITY,
        "replay_summary": replay_summary,
        "records": records,
    }
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.md_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    args.md_out.write_text(markdown_report(report), encoding="utf-8")
    print(json.dumps({
        "status": "PASS" if not replay_summary["errors"] else "PARTIAL",
        "trace_count": len(traces),
        "replayed_trace_count": replay_summary["replayed_trace_count"],
        "replayed_rejected_count": replay_summary["replayed_rejected_count"],
        "json_out": str(args.json_out),
        "md_out": str(args.md_out),
        "errors": len(replay_summary["errors"]),
    }, ensure_ascii=False, indent=2))
    return 0 if not replay_summary["errors"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
