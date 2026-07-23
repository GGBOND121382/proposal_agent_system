from __future__ import annotations

import copy
import json
from pathlib import Path

from stage2_tools.stage2_guide_fact_base import deterministic_validate

FIXTURE = Path(__file__).parent / "fixtures" / "stage2_candidate_missing_open_mapping.json"


def load_candidate() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def add_attachment_unknown_fact(candidate: dict) -> None:
    fact = {
        "fact_id": "FACT-063",
        "statement": "附件与证明材料要求内容为未知。",
        "subject": "附件与证明材料要求",
        "predicate": "内容",
        "object": "未知",
        "knowledge_status": "UNKNOWN",
        "source_refs": ["SRC-OFFICIAL-GUIDE-MISSING"],
        "scope": "开放事项",
        "assertion_policy": "PROHIBITED",
        "requires_qualification": False,
        "atomic": True,
        "related_design_ids": ["OPEN-006"],
    }
    candidate["facts"].append(fact)
    candidate["writing_permissions"]["prohibited_fact_ids"].append(fact["fact_id"])


def add_project_definition_facts(candidate: dict) -> None:
    rows = [
        ("FACT-064", "人机协同决策优势冲刺的工作定义已由阶段1确认。", "人机协同决策优势冲刺", "工作定义", "在有限决策窗口内由人类与多个智能体围绕共享态势持续改进可执行方案。", ["PD-CONCEPT"]),
        ("FACT-065", "项目问题陈述已由阶段1确认。", "项目", "问题陈述", "复杂决策任务同时面临信息变化、约束耦合、目标冲突和决策窗口收缩。", ["PD-PROBLEM"]),
        ("FACT-066", "项目设计界定的当前差距已由阶段1确认。", "项目设计", "当前差距", "现有流程缺少贯穿候选生成、批判、修复和停止判断的人机协同机制。", ["PD-GAP"]),
        ("FACT-067", "项目中心命题已由阶段1确认。", "项目", "中心命题", "统一态势、角色分离协同、人工关键干预和边际增益调度可提升有限窗口内的决策效果。", ["CP-1"]),
        ("FACT-068", "项目研究属性已由阶段1确认。", "项目", "研究属性", "面向复杂决策支持的软件方法、智能体协同机制与工程验证研究", ["PD-ATTRIBUTE"]),
        ("FACT-069", "项目成熟度目标已由阶段1确认。", "项目", "成熟度目标", "形成可配置方法体系和原型系统并完成跨场景对照与消融验证", ["PD-MATURITY"]),
        ("FACT-070", "正文目标篇幅为16至18页。", "申请书正文", "目标页数", "16至18页", ["DOC-TARGET-PAGES"]),
        ("FACT-071", "参考文献不计入正文页数上限。", "参考文献", "页数规则", "不计入正文页数上限", ["DOC-REFERENCE-PAGE"]),
    ]
    for fid, statement, subject, predicate, obj, links in rows:
        candidate["facts"].append({
            "fact_id": fid, "statement": statement, "subject": subject, "predicate": predicate,
            "object": obj, "knowledge_status": "CONFIRMED_DESIGN",
            "source_refs": ["SRC-STAGE1-DESIGN-INPUT"], "scope": "项目定义输入",
            "assertion_policy": "DIRECT", "requires_qualification": False, "atomic": True,
            "related_design_ids": links,
        })
        candidate["writing_permissions"]["direct_fact_ids"].append(fid)


def test_open_item_without_unknown_fact_is_blocking() -> None:
    report = deterministic_validate(load_candidate())
    assert report["verdict"] == "FAIL"
    assert any(x["code"] == "OPEN_ITEM_WITHOUT_UNKNOWN_FACT" for x in report["findings"])


def test_all_open_items_with_unknown_fact_pass() -> None:
    candidate = load_candidate()
    add_attachment_unknown_fact(candidate)
    add_project_definition_facts(candidate)
    report = deterministic_validate(candidate)
    assert report["verdict"] == "PASS", report


def test_unknown_fields_must_exactly_match_open_fields() -> None:
    candidate = load_candidate()
    add_attachment_unknown_fact(candidate)
    add_project_definition_facts(candidate)
    candidate["writing_permissions"]["unknown_fields"].pop()
    report = deterministic_validate(candidate)
    assert report["verdict"] == "FAIL"
    assert any(x["code"] == "UNKNOWN_FIELD_INDEX_INCOMPLETE" for x in report["findings"])


def test_project_definition_fact_coverage_is_required() -> None:
    candidate = load_candidate()
    add_attachment_unknown_fact(candidate)
    report = deterministic_validate(candidate)
    assert report["verdict"] == "FAIL"
    assert any(x["code"] == "PROJECT_DEFINITION_FACT_COVERAGE_GAP" for x in report["findings"])
