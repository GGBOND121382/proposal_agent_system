from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import os
import re
import sys
import zipfile
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.util import sha256_json, utc_now

STAGE = "STAGE_6B_PROVISIONAL_DRAFTING"
BATCH_ID = "STAGE-6B"
SECTION_IDS = [f"SEC-{i:02d}" for i in range(6, 9)]
MODEL_ID = "gpt-5.6-thinking"
ENDPOINT_ID = "chatgpt-conversation-file-bridge"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_schema(name: str) -> dict[str, Any]:
    return read_json(ROOT / "stage6b_tools" / name)


def validate_schema(value: Any, schema: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for err in sorted(Draft202012Validator(schema).iter_errors(value), key=lambda x: list(x.path)):
        loc = "/".join(str(x) for x in err.path) or "$"
        errors.append(f"{loc}: {err.message}")
    return errors


def append_event(run_dir: Path, event_type: str, **details: Any) -> None:
    path = run_dir / "events.jsonl"
    idx = 1
    if path.exists():
        idx = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip()) + 1
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"index": idx, "recorded_at": utc_now(), "event_type": event_type, **details}, ensure_ascii=False, sort_keys=True) + "\n")


def set_state(run_dir: Path, status: str, phase: str, **details: Any) -> None:
    payload = {"schema_version": "1.0", "stage": STAGE, "batch_id": BATCH_ID, "status": status, "phase": phase, "updated_at": utc_now(), **details}
    atomic_json(run_dir / "LATEST_STATE.json", payload)
    append_event(run_dir, "STATE_CHANGED", status=status, phase=phase, details=details)


def nonspace_chars(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def paragraphs(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for sub in candidate.get("subsections", []) for p in sub.get("paragraphs", [])]


def canonical_markdown(section_name: str, candidate: dict[str, Any]) -> str:
    lines = [f"# {section_name}", ""]
    for sub in candidate["subsections"]:
        lines += [f"## {sub['title']}", ""]
        for p in sub["paragraphs"]:
            lines += [p["text"].strip(), ""]
        for vis in candidate.get("visual_placeholders", []):
            para_ids = {p["paragraph_id"] for p in sub["paragraphs"]}
            if vis["placement_after_paragraph_id"] in para_ids:
                lines += [f"> **{vis['visual_id']}：{vis['caption']}**", ""]
    return "\n".join(lines).rstrip() + "\n"


def valid_argument_ids(stage4: dict[str, Any]) -> set[str]:
    ids = {str(n["node_id"]) for n in stage4.get("nodes", [])}
    ids.add(str(stage4["central_proposition"]["node_id"]))
    ids.update(str(q["node_id"]) for q in stage4.get("research_questions", []))
    return ids


def section_contract(stage5: dict[str, Any], section_id: str) -> dict[str, Any]:
    for sec in stage5["sections"]:
        if sec["section_id"] == section_id:
            return sec
    raise KeyError(section_id)


def source_subset(stage4a: dict[str, Any], source_ids: list[str]) -> list[dict[str, Any]]:
    wanted = set(source_ids)
    return [x for x in stage4a["source_registry"] if x["source_id"] in wanted]


def argument_subset(stage4: dict[str, Any], node_ids: list[str], rq_ids: list[str]) -> dict[str, Any]:
    wanted = set(node_ids)
    return {
        "central_proposition": stage4["central_proposition"] if stage4["central_proposition"]["node_id"] in wanted else None,
        "research_questions": [q for q in stage4["research_questions"] if q["node_id"] in wanted or q["node_id"] in rq_ids],
        "nodes": [n for n in stage4["nodes"] if n["node_id"] in wanted],
        "argument_chains": [c for c in stage4["argument_chains"] if c["rq_id"] in rq_ids],
    }


def completed_digest(run_dir: Path) -> list[dict[str, Any]]:
    """Return frozen stage-6A contributions plus completed sections in this batch."""
    result: list[dict[str, Any]] = []
    upstream = run_dir / "source_snapshots" / "stage6a_batch_draft.json"
    if upstream.exists():
        batch = read_json(upstream)
        for item in batch.get("sections", []):
            c = item.get("candidate", {})
            result.append({
                "section_id": item.get("section_id"),
                "section_name": item.get("section_name"),
                "section_contribution": c.get("section_contribution", ""),
                "new_information_keys": [p["novel_content_key"] for p in paragraphs(c)],
            })
    for sid in SECTION_IDS:
        path = run_dir / "intermediate" / sid / "polished_candidate.json"
        if not path.exists():
            continue
        c = read_json(path)
        result.append({
            "section_id": sid,
            "section_name": c["section_name"],
            "section_contribution": c["section_contribution"],
            "new_information_keys": [p["novel_content_key"] for p in paragraphs(c)],
        })
    return result


def request_number(section_id: str, phase: str) -> int:
    base = SECTION_IDS.index(section_id) * 4
    return base + {"writer": 1, "critic": 2, "polish": 3, "expression_critic": 4}[phase]


def write_request(run_dir: Path, number: int, name: str, payload: dict[str, Any]) -> Path:
    path = run_dir / "requests" / f"{number:03d}_{name}.json"
    atomic_json(path, payload)
    append_event(run_dir, "MODEL_REQUEST_CREATED", request_file=str(path.relative_to(run_dir)), call_key=payload["call_key"], prompt_id=payload["prompt_id"])
    return path


def make_writer_request(run_dir: Path, section_id: str) -> dict[str, Any]:
    stage1 = read_json(run_dir / "source_snapshots" / "stage1_design_input.json")
    stage3 = read_json(run_dir / "source_snapshots" / "stage3_project_definition.json")
    stage4 = read_json(run_dir / "source_snapshots" / "stage4_argument_architecture.json")
    stage4a = read_json(run_dir / "source_snapshots" / "stage4a_evidence_completion.json")
    stage5 = read_json(run_dir / "source_snapshots" / "stage5_section_plan.json")
    stage6a = read_json(run_dir / "source_snapshots" / "stage6a_batch_draft.json")
    sec = section_contract(stage5, section_id)
    return {
        "schema_version": "1.0",
        "call_key": f"stage6b-{section_id.lower()}-writer-001",
        "prompt_id": "P-STAGE6B-WRITE-SECTION",
        "prompt_version": "1.0.0",
        "executor_role": "Evidence-grounded Proposal Writing Agent",
        "model_contract": {"model_independent": True, "response_format": "JSON", "actual_model_id_required": True, "endpoint_id_required": True, "original_response_immutable": True},
        "system_prompt": (
            "你是科研项目申请书核心方法章节写作Agent。严格按当前章节合同和子节段落角色写作，不得扩展项目范围。"
            "区分已冻结项目设计、拟开展工作、边界条件和未知事项；不得把计划写成成果，不得把原型功能清单替代科学问题。"
            "每段必须绑定真实存在的论证节点和研究问题，并形成该章独有的信息键；元数据必须与正文语义一致。"
            "研究内容章要写清输入、机制、输出、依赖、知识贡献和验证证据；关键问题章要写清内在矛盾、形式化对象和反证现象；"
            "技术路线章要写清数据、模型、确定性工具、人工决定、可替换模型接口以及停止、降级和回滚闭环。"
            "人类保持最终确认权，语言模型输出不得直接转化为执行决定。正文不得讨论本次写作所用Prompt、Gate或文件桥。只返回符合Schema的JSON。"
        ),
        "task_prompt": (
            f"撰写{section_id}《{sec['section_name']}》。覆盖全部must_answer、must_include、required节点和研究问题，"
            "严格遵守must_not_claim。按冻结子节顺序组织段落，正文自然连贯，避免重复阶段6A的概念定义与背景论证。"
            "段落元数据必须准确反映正文，不得为了通过校验机械挂接无关ID。"
        ),
        "input_envelope": {
            "batch_id": BATCH_ID,
            "section_contract": sec,
            "project_positioning": stage1["project_positioning"],
            "concept_definition": stage1["concept_definition"],
            "application_scenarios": stage1["application_scenarios"],
            "method_system": stage1["method_system"],
            "human_ai_collaboration": stage1["human_ai_collaboration"],
            "problem_definition": stage3["problem_definition"],
            "central_proposition": stage3["central_proposition"],
            "research_gaps": stage3["research_gaps"],
            "research_questions": stage3["research_questions"],
            "objectives": stage3["objectives"],
            "research_contents": stage3["research_contents"],
            "scope": stage3["scope"],
            "argument_subset": argument_subset(stage4, sec["required_node_ids"], sec["required_rq_ids"]),
            "source_records": source_subset(stage4a, sec["required_source_ids"]),
            "foundation_boundaries": stage4a["foundation_updates"],
            "metric_boundaries": stage4a["metric_justification"],
            "open_items": stage4a["open_items_remaining"],
            "stage6a_frozen_draft": stage6a,
            "prior_section_digest": completed_digest(run_dir),
            "stage_boundary": "STAGE_6B_SECTIONS_06_TO_08_ONLY",
        },
        "output_schema": load_schema("section_draft.schema.json"),
        "requested_at": utc_now(),
    }


def make_critic_request(run_dir: Path, section_id: str, candidate: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    stage5 = read_json(run_dir / "source_snapshots" / "stage5_section_plan.json")
    return {
        "schema_version": "1.0", "call_key": f"stage6b-{section_id.lower()}-content-critic-001",
        "prompt_id": "P-STAGE6B-SECTION-CONTENT-CRITIC", "prompt_version": "1.0.0",
        "executor_role": "Independent Section Content Critic",
        "model_contract": {"independent_from_writer": True, "response_format": "JSON", "actual_model_id_required": True, "endpoint_id_required": True},
        "system_prompt": (
            "你是独立正文内容Critic。逐段检查章节合同覆盖、论证推进、证据状态、研究实质、章节独有性、声称边界和篇幅密度。"
            "不得因文字流畅而忽略空泛、无来源、把计划写成成果或机械挂接ID。若需修改，必须给出最小可执行范围。"
        ),
        "task_prompt": f"审查{section_id}全部段落和全部7项质量维度。",
        "input_envelope": {"section_contract": section_contract(stage5, section_id), "candidate": candidate, "deterministic_report": report, "prior_section_digest": completed_digest(run_dir)},
        "output_schema": load_schema("section_critic.schema.json"), "requested_at": utc_now(),
    }


def make_polish_request(run_dir: Path, section_id: str, candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1.0", "call_key": f"stage6b-{section_id.lower()}-expression-polish-001",
        "prompt_id": "P-STAGE6B-EXPRESSION-POLISH", "prompt_version": "1.0.0",
        "executor_role": "Proposal Expression Editor",
        "model_contract": {"semantic_identity_immutable": True, "response_format": "JSON", "actual_model_id_required": True, "endpoint_id_required": True},
        "system_prompt": (
            "你是申请书表达编辑。只能改善句间衔接、正式程度、术语一致性和信息密度。"
            "不得改变段落数量、段落ID、角色、节点/来源/研究问题绑定、声称状态、信息键、未决事项、图表位置或章节贡献。"
            "原文已清晰时应保留，不得为了展示修改而改写。"
        ),
        "task_prompt": f"处理{section_id}表达，并逐段说明PRESERVED或POLISHED。",
        "input_envelope": {"candidate": candidate},
        "output_schema": load_schema("expression_polish.schema.json"), "requested_at": utc_now(),
    }


def make_expression_critic_request(section_id: str, original: dict[str, Any], polished: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1.0", "call_key": f"stage6b-{section_id.lower()}-expression-critic-001",
        "prompt_id": "P-STAGE6B-EXPRESSION-CRITIC", "prompt_version": "1.0.0",
        "executor_role": "Independent Expression Critic",
        "model_contract": {"independent_from_editor": True, "response_format": "JSON", "actual_model_id_required": True, "endpoint_id_required": True},
        "system_prompt": "逐段比较编辑前后文本。确认语义身份和证据绑定未改变，并检查正式性、逻辑衔接、术语、密度、重复和声称强度。",
        "task_prompt": f"审查{section_id}全部段落和6项表达维度。",
        "input_envelope": {"original_candidate": original, "polished_candidate": polished},
        "output_schema": load_schema("expression_critic.schema.json"), "requested_at": utc_now(),
    }



def deterministic_validate_batch(run_dir: Path, candidates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    def add(code: str, message: str, target: str = "batch") -> None:
        findings.append({"code": code, "severity": "BLOCKING", "target": target, "message": message})

    if set(candidates) != set(SECTION_IDS):
        add("BATCH_SECTION_SET_MISMATCH", f"批次章节应为{SECTION_IDS}，当前为{sorted(candidates)}。")
        return {"verdict": "FAIL", "findings": findings}

    paragraph_texts: dict[str, str] = {}
    keys: dict[str, str] = {}
    contributions: dict[str, str] = {}
    total_chars = 0
    for sid, candidate in candidates.items():
        contributions[sid] = candidate.get("section_contribution", "").strip()
        for p in paragraphs(candidate):
            text = re.sub(r"\s+", "", p["text"])
            total_chars += len(text)
            if text in paragraph_texts:
                add("CROSS_SECTION_PARAGRAPH_DUPLICATE", f"{p['paragraph_id']}与{paragraph_texts[text]}正文完全重复。", p["paragraph_id"])
            paragraph_texts[text] = p["paragraph_id"]
            key = p["novel_content_key"]
            if key in keys:
                add("CROSS_SECTION_INFORMATION_KEY_DUPLICATE", f"信息键{key}同时用于{keys[key]}和{p['paragraph_id']}。", p["paragraph_id"])
            keys[key] = p["paragraph_id"]

    if len(set(contributions.values())) != len(contributions):
        add("SECTION_CONTRIBUTION_DUPLICATE", "三章的章节贡献描述存在重复。")

    stage6a = read_json(run_dir / "source_snapshots" / "stage6a_batch_draft.json")
    prior_keys = {
        p["novel_content_key"]
        for item in stage6a.get("sections", [])
        for p in paragraphs(item.get("candidate", {}))
    }
    overlap = set(keys) & prior_keys
    if overlap:
        add("STAGE6A_INFORMATION_KEY_REUSE", f"阶段6B复用了阶段6A信息键：{sorted(overlap)}。")

    if not (3000 <= total_chars <= 6000):
        add("BATCH_CHAR_BUDGET_OUT_OF_RANGE", f"三章有效字符数{total_chars}不在阶段性合理区间3000—6000内。")

    required_tokens = {
        "SEC-06": ["知识贡献", "WP-1", "WP-5"],
        "SEC-07": ["矛盾", "反证", "GAP-1", "GAP-3"],
        "SEC-08": ["模型API", "确定性", "降级", "回滚"],
    }
    for sid, tokens in required_tokens.items():
        text = "".join(p["text"] for p in paragraphs(candidates[sid]))
        for token in tokens:
            if token not in text:
                add("BATCH_SECTION_ROLE_INCOMPLETE", f"{sid}缺少体现章节独有职责的“{token}”语义。", sid)

    return {
        "verdict": "PASS" if not findings else "FAIL",
        "section_ids": SECTION_IDS,
        "total_effective_char_count": total_chars,
        "candidate_hashes": {sid: sha256_json(candidates[sid]) for sid in SECTION_IDS},
        "findings": findings,
    }

def make_batch_critic_request(run_dir: Path) -> dict[str, Any]:
    candidates = {sid: read_json(run_dir / "intermediate" / sid / "polished_candidate.json") for sid in SECTION_IDS}
    deterministic_report = deterministic_validate_batch(run_dir, candidates)
    atomic_json(run_dir / "quality" / "batch_deterministic_report.json", deterministic_report)
    if deterministic_report["verdict"] != "PASS":
        raise SystemExit("batch failed deterministic validation")
    stage3 = read_json(run_dir / "source_snapshots" / "stage3_project_definition.json")
    stage5 = read_json(run_dir / "source_snapshots" / "stage5_section_plan.json")
    return {
        "schema_version": "1.0", "call_key": "stage6b-batch-critic-001", "prompt_id": "P-STAGE6B-BATCH-CRITIC", "prompt_version": "1.0.0",
        "executor_role": "Independent Batch Integration Critic",
        "model_contract": {"independent_from_all_section_writers": True, "response_format": "JSON", "actual_model_id_required": True, "endpoint_id_required": True},
        "system_prompt": (
            "你是阶段6B批次Critic。检查研究内容章是否形成任务—机制—依赖—知识贡献闭环，关键问题章是否与工作包区分并具有反证标准，"
            "技术路线章是否闭合输入治理、共享状态、候选竞争、确定性校验、人工交接、停止降级回滚和全过程记录。"
            "同时检查三项研究问题与方法映射、人类最终控制和模型API可替换边界、跨章不重复及篇幅预算。"
        ),
        "task_prompt": "逐章、逐研究问题和逐质量维度检查，只有没有实质问题时才允许进入阶段6C。",
        "input_envelope": {"central_proposition": stage3["central_proposition"], "research_questions": stage3["research_questions"], "section_contracts": [section_contract(stage5, sid) for sid in SECTION_IDS], "stage6a_digest": completed_digest(run_dir), "deterministic_batch_report": deterministic_report, "candidates": candidates},
        "output_schema": load_schema("batch_critic.schema.json"), "requested_at": utc_now(),
    }


def deterministic_validate_section(candidate: Any, contract: dict[str, Any], stage4: dict[str, Any], stage4a: dict[str, Any], prior_digest: list[dict[str, Any]]) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    def add(code: str, message: str, target: str = "candidate", severity: str = "BLOCKING") -> None:
        findings.append({"code": code, "severity": severity, "target": target, "message": message})

    errors = validate_schema(candidate, load_schema("section_draft.schema.json"))
    for err in errors:
        add("SCHEMA_ERROR", err)
    if errors:
        return {"verdict": "FAIL", "candidate_hash": sha256_json(candidate), "findings": findings}

    sid = contract["section_id"]
    if candidate["section_id"] != sid:
        add("SECTION_ID_MISMATCH", "响应章节ID与请求不一致。")
    c = candidate["candidate"]
    if c["section_name"] != contract["section_name"]:
        add("SECTION_NAME_DRIFT", "章节名称改变。")
    expected_subs = [(s["subsection_id"], s["title"]) for s in contract["subsections"]]
    actual_subs = [(s["subsection_id"], s["title"]) for s in c["subsections"]]
    if actual_subs != expected_subs:
        add("SUBSECTION_CONTRACT_DRIFT", "子节ID、标题或顺序与阶段5合同不一致。")

    ps = paragraphs(c)
    pids = [p["paragraph_id"] for p in ps]
    if len(pids) != len(set(pids)):
        add("DUPLICATE_PARAGRAPH_ID", "段落ID重复。")
    roles = [p["role"] for p in ps]
    for sub_contract, sub_actual in zip(contract["subsections"], c["subsections"]):
        actual_roles = [p["role"] for p in sub_actual["paragraphs"]]
        for required_role in sub_contract["paragraph_roles"]:
            if not any(required_role in role or role in required_role for role in actual_roles):
                add("PARAGRAPH_ROLE_MISSING", f"{sub_contract['subsection_id']}缺少段落角色：{required_role}。", sub_contract["subsection_id"])

    node_ids = set().union(*(set(p["node_ids"]) for p in ps)) if ps else set()
    rq_ids = set().union(*(set(p["rq_ids"]) for p in ps)) if ps else set()
    source_ids = set().union(*(set(p["source_ids"]) for p in ps)) if ps else set()
    valid_nodes = valid_argument_ids(stage4)
    valid_sources = {x["source_id"] for x in stage4a["source_registry"]}
    if node_ids - valid_nodes:
        add("UNKNOWN_NODE_REFERENCE", f"引用未知论证节点：{sorted(node_ids-valid_nodes)}。")
    if source_ids - valid_sources:
        add("UNKNOWN_SOURCE_REFERENCE", f"引用未知来源：{sorted(source_ids-valid_sources)}。")
    missing_nodes = set(contract["required_node_ids"]) - node_ids
    missing_rqs = set(contract["required_rq_ids"]) - rq_ids
    missing_sources = set(contract["required_source_ids"]) - source_ids
    if missing_nodes:
        add("REQUIRED_NODE_NOT_COVERED", f"未覆盖必需论证节点：{sorted(missing_nodes)}。")
    if missing_rqs:
        add("REQUIRED_RQ_NOT_COVERED", f"未覆盖必需研究问题：{sorted(missing_rqs)}。")
    if missing_sources:
        add("REQUIRED_SOURCE_NOT_COVERED", f"未覆盖必需来源：{sorted(missing_sources)}。")

    for p in ps:
        if p["claim_status"] == "PUBLIC_RESEARCH_SUMMARY" and not p["source_ids"]:
            add("PUBLIC_CLAIM_WITHOUT_SOURCE", "公开研究归纳段落没有来源。", p["paragraph_id"])
        if p["claim_status"] in {"PROJECT_PLAN", "CONFIRMED_DESIGN"} and any(x in p["text"] for x in ["已经证明", "实验证明本项目", "已达到", "已完成验证", "获得资助"]):
            add("PLAN_WRITTEN_AS_RESULT", "拟开展工作被写成既有结果。", p["paragraph_id"])
        if any(x in p["text"] for x in ["国际首创", "国内首创", "国际领先", "填补空白"]):
            add("ABSOLUTE_NOVELTY_CLAIM", "存在未经证据支持的绝对创新表述。", p["paragraph_id"])
        if "……" in p["text"] or "[中段省略]" in p["text"] or "TODO" in p["text"]:
            add("PLACEHOLDER_TEXT", "正文包含省略或占位文本。", p["paragraph_id"])
        if nonspace_chars(p["text"]) < 55:
            add("PARAGRAPH_TOO_THIN", "段落信息量不足。", p["paragraph_id"])

    canonical = canonical_markdown(contract["section_name"], c)
    if re.sub(r"\s+", "", canonical) != re.sub(r"\s+", "", c["markdown"]):
        add("MARKDOWN_PARAGRAPH_DIVERGENCE", "markdown正文与结构化段落不一致。")

    chars = sum(nonspace_chars(p["text"]) for p in ps)
    minimum = int(contract["expected_words"]["min"] * 0.72)
    maximum = int(contract["expected_words"]["max"] * 1.25)
    if chars < minimum:
        add("SECTION_TOO_SHORT", f"有效字符数{chars}低于阶段性下限{minimum}。")
    if chars > maximum:
        add("SECTION_TOO_LONG", f"有效字符数{chars}超过阶段性上限{maximum}。")

    expected_visuals = set(contract["visual_ids"])
    actual_visuals = {v["visual_id"] for v in c["visual_placeholders"]}
    if expected_visuals != actual_visuals:
        add("VISUAL_PLACEHOLDER_MISMATCH", f"图表占位应为{sorted(expected_visuals)}，当前为{sorted(actual_visuals)}。")
    if any(v["placement_after_paragraph_id"] not in pids for v in c["visual_placeholders"]):
        add("VISUAL_PLACEMENT_UNKNOWN", "图表位置引用未知段落。")

    prior_keys = {k for item in prior_digest for k in item.get("new_information_keys", [])}
    keys = [p["novel_content_key"] for p in ps]
    if len(keys) != len(set(keys)):
        add("DUPLICATE_INFORMATION_KEY", "本章信息键重复。")
    overlap = set(keys) & prior_keys
    if overlap:
        add("CROSS_SECTION_INFORMATION_KEY_REUSE", f"复用了已完成章节信息键：{sorted(overlap)}。")

    if sid == "SEC-03":
        text = "".join(p["text"] for p in ps)
        for token in ["局限", "本项目", "比较"]:
            if token not in text:
                add("LITERATURE_REVIEW_CHAIN_INCOMPLETE", f"相关工作章缺少“{token}”语义。")
    if sid == "SEC-04":
        text = "".join(p["text"] for p in ps)
        for token in ["停止", "降级", "回滚", "最终确认"]:
            if token not in text:
                add("THEORY_BOUNDARY_INCOMPLETE", f"理论框架缺少“{token}”条件。")
    text = "".join(p["text"] for p in ps)
    if sid == "SEC-06":
        for token in ["RC-1", "RC-2", "RC-3", "RC-4", "WP-1", "WP-2", "WP-3", "WP-4", "WP-5"]:
            if token not in text:
                add("RESEARCH_CONTENT_ID_NOT_EXPLICIT", f"研究内容章正文未显式标记{token}。")
        for token in ["知识贡献", "依赖", "交付", "验证"]:
            if token not in text:
                add("RESEARCH_CONTENT_CHAIN_INCOMPLETE", f"研究内容章缺少“{token}”语义。")
    if sid == "SEC-07":
        for token in ["GAP-1", "GAP-2", "GAP-3", "RQ-1", "RQ-2", "RQ-3", "FM-1", "FM-2", "FM-3"]:
            if token not in text:
                add("KEY_PROBLEM_ID_NOT_EXPLICIT", f"关键问题章正文未显式标记{token}。")
        for token in ["矛盾", "失败", "反证"]:
            if token not in text:
                add("KEY_PROBLEM_FALSIFICATION_INCOMPLETE", f"关键问题章缺少“{token}”语义。")
    if sid == "SEC-08":
        for token in ["输入治理", "共享态势", "候选", "确定性", "人工", "停止", "降级", "回滚", "模型API", "当前最好可行"]:
            if token not in text:
                add("TECHNICAL_ROUTE_CLOSURE_INCOMPLETE", f"技术路线章缺少“{token}”语义。")
        if "直接执行" not in text and "不得直接" not in text:
            add("MODEL_OUTPUT_EXECUTION_BOUNDARY_MISSING", "技术路线章未明确模型输出不得直接转化为执行决定。")

    return {"verdict": "PASS" if not findings else "FAIL", "candidate_hash": sha256_json(c), "effective_char_count": chars, "paragraph_count": len(ps), "findings": findings}


def semantic_identity_errors(original: dict[str, Any], polished: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in ["candidate_id", "section_name", "visual_placeholders", "unresolved_open_item_ids", "key_terms", "section_contribution"]:
        if original.get(field) != polished.get(field):
            errors.append(f"candidate.{field} changed")
    op = paragraphs(original); pp = paragraphs(polished)
    if len(op) != len(pp):
        errors.append("paragraph count changed")
        return errors
    immutable = ["paragraph_id", "role", "node_ids", "rq_ids", "source_ids", "claim_status", "novel_content_key"]
    for a, b in zip(op, pp):
        for field in immutable:
            if a.get(field) != b.get(field):
                errors.append(f"{a.get('paragraph_id')}.{field} changed")
    if [(s["subsection_id"], s["title"]) for s in original["subsections"]] != [(s["subsection_id"], s["title"]) for s in polished["subsections"]]:
        errors.append("subsection identity changed")
    return errors


def init_cmd(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    if run_dir.exists() and any(run_dir.iterdir()):
        raise SystemExit("run directory must be empty")
    run_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "stage1": Path(args.design_input).resolve(), "stage3": Path(args.project_definition).resolve(),
        "stage4": Path(args.argument_architecture).resolve(), "stage4a": Path(args.evidence_completion).resolve(),
        "stage5": Path(args.section_plan).resolve(), "stage6a": Path(args.stage6a_draft).resolve(),
    }
    values = {k: read_json(p) for k, p in paths.items()}
    stage5 = values["stage5"]
    batch = next(b for b in stage5["draft_batches"] if b["batch_id"] == BATCH_ID)
    if batch["section_ids"] != SECTION_IDS:
        raise SystemExit("stage5 batch 6B section IDs mismatch")
    hashes = {k: sha256_file(p) for k, p in paths.items()}
    for key, p in paths.items():
        atomic_json(run_dir / "source_snapshots" / f"{key}_{p.name}", values[key])
    # Stable names used by request builders.
    atomic_json(run_dir / "source_snapshots" / "stage1_design_input.json", values["stage1"])
    atomic_json(run_dir / "source_snapshots" / "stage3_project_definition.json", values["stage3"])
    atomic_json(run_dir / "source_snapshots" / "stage4_argument_architecture.json", values["stage4"])
    atomic_json(run_dir / "source_snapshots" / "stage4a_evidence_completion.json", values["stage4a"])
    atomic_json(run_dir / "source_snapshots" / "stage5_section_plan.json", values["stage5"])
    atomic_json(run_dir / "source_snapshots" / "stage6a_batch_draft.json", values["stage6a"])
    metadata = {"schema_version": "1.0", "stage": STAGE, "batch_id": BATCH_ID, "created_at": utc_now(), "upstream_paths": {k: str(p) for k,p in paths.items()}, "upstream_sha256": hashes, "section_ids": SECTION_IDS, "model_bridge": ENDPOINT_ID}
    atomic_json(run_dir / "RUN_METADATA.json", metadata)
    for schema in ["section_draft.schema.json", "section_critic.schema.json", "expression_polish.schema.json", "expression_critic.schema.json", "batch_critic.schema.json"]:
        atomic_json(run_dir / "schemas" / schema, load_schema(schema))
    append_event(run_dir, "RUN_INITIALIZED", upstream_sha256=hashes)
    req = make_writer_request(run_dir, SECTION_IDS[0])
    write_request(run_dir, request_number(SECTION_IDS[0], "writer"), f"{SECTION_IDS[0]}_writer", req)
    set_state(run_dir, "WAITING_MODEL", "SECTION_WRITER", active_section_id=SECTION_IDS[0], completed_section_ids=[])


def ingest_writer_cmd(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve(); sid = args.section_id
    response = read_json(Path(args.response_file).resolve())
    errors = validate_schema(response, load_schema("section_draft.schema.json"))
    if errors:
        atomic_json(run_dir / "responses" / f"{request_number(sid,'writer'):03d}_{sid}_writer_schema_failed.json", response)
        set_state(run_dir, "BLOCKED", "SECTION_WRITER_SCHEMA_FAILED", active_section_id=sid, schema_errors=errors)
        raise SystemExit("; ".join(errors))
    if response["actual_model_id"] == "" or response["endpoint_id"] == "" or response["section_id"] != sid:
        raise SystemExit("response metadata mismatch")
    response_path = run_dir / "responses" / f"{request_number(sid,'writer'):03d}_{sid}_writer.json"
    atomic_json(response_path, response); append_event(run_dir, "MODEL_RESPONSE_INGESTED", response_file=str(response_path.relative_to(run_dir)), actual_model_id=response["actual_model_id"], endpoint_id=response["endpoint_id"])
    stage4 = read_json(run_dir / "source_snapshots" / "stage4_argument_architecture.json")
    stage4a = read_json(run_dir / "source_snapshots" / "stage4a_evidence_completion.json")
    stage5 = read_json(run_dir / "source_snapshots" / "stage5_section_plan.json")
    report = deterministic_validate_section(response, section_contract(stage5, sid), stage4, stage4a, completed_digest(run_dir))
    atomic_json(run_dir / "quality" / sid / "deterministic_content_report.json", report)
    if report["verdict"] != "PASS" or response["status"] != "PASS":
        repair = {"schema_version":"1.0","call_key":f"stage6b-{sid.lower()}-writer-repair-001","prompt_id":"P-STAGE6B-WRITE-SECTION-REPAIR","system_prompt":"只修复确定性报告指出的段落或元数据，不改变章节合同和冻结事实。","input_envelope":{"original_response":response,"findings":report["findings"]},"output_schema":load_schema("section_draft.schema.json"),"requested_at":utc_now()}
        write_request(run_dir, 100 + int(sid[-2:]), f"{sid}_writer_repair", repair)
        set_state(run_dir, "WAITING_MODEL", "SECTION_WRITER_REPAIR", active_section_id=sid, findings=report["findings"])
        raise SystemExit("section draft failed deterministic validation")
    candidate = response["candidate"]
    atomic_json(run_dir / "intermediate" / sid / "original_candidate.json", candidate)
    req = make_critic_request(run_dir, sid, candidate, report)
    write_request(run_dir, request_number(sid, "critic"), f"{sid}_content_critic", req)
    set_state(run_dir, "WAITING_MODEL", "SECTION_CONTENT_CRITIC", active_section_id=sid, completed_section_ids=[x["section_id"] for x in completed_digest(run_dir)])



def ingest_writer_repair_cmd(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve(); sid = args.section_id
    response = read_json(Path(args.response_file).resolve())
    errors = validate_schema(response, load_schema("section_draft.schema.json"))
    if errors:
        raise SystemExit("; ".join(errors))
    if response["section_id"] != sid or response["status"] != "PASS":
        raise SystemExit("repair response metadata or status mismatch")
    original_path = run_dir / "responses" / f"{request_number(sid,'writer'):03d}_{sid}_writer.json"
    if not original_path.exists():
        # A schema-valid but deterministically failed response is still immutable evidence.
        failed_path = run_dir / "responses" / f"{request_number(sid,'writer'):03d}_{sid}_writer.json"
        if not failed_path.exists():
            raise SystemExit("original writer response missing")
    original = read_json(original_path)
    if original["candidate"]["candidate_id"] != response["candidate"]["candidate_id"]:
        raise SystemExit("targeted repair changed candidate identity")
    stage4 = read_json(run_dir / "source_snapshots" / "stage4_argument_architecture.json")
    stage4a = read_json(run_dir / "source_snapshots" / "stage4a_evidence_completion.json")
    stage5 = read_json(run_dir / "source_snapshots" / "stage5_section_plan.json")
    report = deterministic_validate_section(response, section_contract(stage5, sid), stage4, stage4a, completed_digest(run_dir))
    repair_no = 100 + int(sid[-2:])
    response_path = run_dir / "responses" / f"{repair_no:03d}_{sid}_writer_repair.json"
    atomic_json(response_path, response)
    atomic_json(run_dir / "quality" / sid / "deterministic_content_report_after_repair.json", report)
    append_event(run_dir, "MODEL_RESPONSE_INGESTED", response_file=str(response_path.relative_to(run_dir)), actual_model_id=response["actual_model_id"], endpoint_id=response["endpoint_id"], repair=True)
    if report["verdict"] != "PASS":
        set_state(run_dir, "BLOCKED", "SECTION_WRITER_REPAIR_FAILED", active_section_id=sid, findings=report["findings"])
        raise SystemExit("targeted repair failed deterministic validation")
    # Permit text/metadata repair only inside the same frozen section contract.
    atomic_json(run_dir / "intermediate" / sid / "original_candidate_before_repair.json", original["candidate"])
    atomic_json(run_dir / "intermediate" / sid / "original_candidate.json", response["candidate"])
    atomic_json(run_dir / "intermediate" / sid / "active_candidate_pointer.json", {"source": "TARGETED_REPAIR", "response_file": str(response_path.relative_to(run_dir)), "candidate_hash": report["candidate_hash"]})
    req = make_critic_request(run_dir, sid, response["candidate"], report)
    write_request(run_dir, request_number(sid, "critic"), f"{sid}_content_critic", req)
    set_state(run_dir, "WAITING_MODEL", "SECTION_CONTENT_CRITIC", active_section_id=sid, repaired=True)


def ingest_critic_cmd(args: argparse.Namespace) -> None:
    run_dir=Path(args.run_dir).resolve(); sid=args.section_id; response=read_json(Path(args.response_file).resolve())
    errors=validate_schema(response,load_schema("section_critic.schema.json"))
    if errors: raise SystemExit("; ".join(errors))
    candidate=read_json(run_dir/"intermediate"/sid/"original_candidate.json")
    expected={p["paragraph_id"] for p in paragraphs(candidate)}
    if set(response["checked_paragraph_ids"]) != expected: raise SystemExit("critic did not check every paragraph")
    if response["verdict"] != "ACCEPT" or any(d["result"]!="PASS" for d in response["quality_dimensions"]): raise SystemExit("critic did not accept")
    path=run_dir/"responses"/f"{request_number(sid,'critic'):03d}_{sid}_content_critic.json"; atomic_json(path,response)
    atomic_json(run_dir/"quality"/sid/"independent_content_critic.json",response); append_event(run_dir,"MODEL_RESPONSE_INGESTED",response_file=str(path.relative_to(run_dir)),actual_model_id=response["actual_model_id"],endpoint_id=response["endpoint_id"])
    req=make_polish_request(run_dir,sid,candidate); write_request(run_dir,request_number(sid,"polish"),f"{sid}_expression_polish",req)
    set_state(run_dir,"WAITING_MODEL","SECTION_EXPRESSION_POLISH",active_section_id=sid)


def ingest_polish_cmd(args: argparse.Namespace) -> None:
    run_dir=Path(args.run_dir).resolve(); sid=args.section_id; response=read_json(Path(args.response_file).resolve())
    errors=validate_schema(response,load_schema("expression_polish.schema.json"))
    if errors: raise SystemExit("; ".join(errors))
    original=read_json(run_dir/"intermediate"/sid/"original_candidate.json"); polished=response["candidate"]
    identity=semantic_identity_errors(original,polished)
    if identity: raise SystemExit("; ".join(identity))
    stage4=read_json(run_dir/"source_snapshots"/"stage4_argument_architecture.json"); stage4a=read_json(run_dir/"source_snapshots"/"stage4a_evidence_completion.json"); stage5=read_json(run_dir/"source_snapshots"/"stage5_section_plan.json")
    wrapped={"schema_version":"1.0","prompt_id":"P-STAGE6B-WRITE-SECTION","prompt_version":"1.0.0","actual_model_id":response["actual_model_id"],"endpoint_id":response["endpoint_id"],"status":"PASS","section_id":sid,"candidate":polished,"findings":[],"warnings":[]}
    report=deterministic_validate_section(wrapped,section_contract(stage5,sid),stage4,stage4a,completed_digest(run_dir))
    if report["verdict"]!="PASS": raise SystemExit("polished candidate failed deterministic validation")
    path=run_dir/"responses"/f"{request_number(sid,'polish'):03d}_{sid}_expression_polish.json"; atomic_json(path,response)
    atomic_json(run_dir/"intermediate"/sid/"polished_candidate.json",polished); atomic_json(run_dir/"quality"/sid/"post_polish_deterministic_report.json",report)
    append_event(run_dir,"MODEL_RESPONSE_INGESTED",response_file=str(path.relative_to(run_dir)),actual_model_id=response["actual_model_id"],endpoint_id=response["endpoint_id"])
    req=make_expression_critic_request(sid,original,polished); write_request(run_dir,request_number(sid,"expression_critic"),f"{sid}_expression_critic",req)
    set_state(run_dir,"WAITING_MODEL","SECTION_EXPRESSION_CRITIC",active_section_id=sid)


def ingest_expression_critic_cmd(args: argparse.Namespace) -> None:
    run_dir=Path(args.run_dir).resolve(); sid=args.section_id; response=read_json(Path(args.response_file).resolve())
    errors=validate_schema(response,load_schema("expression_critic.schema.json"))
    if errors: raise SystemExit("; ".join(errors))
    polished=read_json(run_dir/"intermediate"/sid/"polished_candidate.json"); expected={p["paragraph_id"] for p in paragraphs(polished)}
    if set(response["checked_paragraph_ids"])!=expected or not response["semantic_identity_preserved"] or response["verdict"]!="ACCEPT" or any(d["result"]!="PASS" for d in response["style_dimensions"]):
        raise SystemExit("expression critic did not accept all paragraphs")
    path=run_dir/"responses"/f"{request_number(sid,'expression_critic'):03d}_{sid}_expression_critic.json"; atomic_json(path,response); atomic_json(run_dir/"quality"/sid/"independent_expression_critic.json",response)
    append_event(run_dir,"MODEL_RESPONSE_INGESTED",response_file=str(path.relative_to(run_dir)),actual_model_id=response["actual_model_id"],endpoint_id=response["endpoint_id"])
    atomic_json(run_dir/"intermediate"/sid/"completion.json",{"section_id":sid,"status":"COMPLETED","completed_at":utc_now(),"candidate_hash":sha256_json(polished)})
    append_event(run_dir,"SECTION_COMPLETED",section_id=sid,candidate_hash=sha256_json(polished))
    idx=SECTION_IDS.index(sid)
    if idx+1 < len(SECTION_IDS):
        next_sid=SECTION_IDS[idx+1]; req=make_writer_request(run_dir,next_sid); write_request(run_dir,request_number(next_sid,"writer"),f"{next_sid}_writer",req)
        set_state(run_dir,"WAITING_MODEL","SECTION_WRITER",active_section_id=next_sid,completed_section_ids=SECTION_IDS[:idx+1])
    else:
        req=make_batch_critic_request(run_dir); write_request(run_dir,13,"stage6b_batch_critic",req)
        set_state(run_dir,"WAITING_MODEL","BATCH_CRITIC",completed_section_ids=SECTION_IDS)


def ingest_batch_critic_cmd(args: argparse.Namespace) -> None:
    run_dir=Path(args.run_dir).resolve(); response=read_json(Path(args.response_file).resolve())
    candidates={sid:read_json(run_dir/"intermediate"/sid/"polished_candidate.json") for sid in SECTION_IDS}
    deterministic_report=deterministic_validate_batch(run_dir,candidates)
    atomic_json(run_dir/"quality"/"batch_deterministic_report.json",deterministic_report)
    if deterministic_report["verdict"]!="PASS":
        set_state(run_dir,"BLOCKED","BATCH_DETERMINISTIC_FAILED",findings=deterministic_report["findings"])
        raise SystemExit("batch failed deterministic validation")
    errors=validate_schema(response,load_schema("batch_critic.schema.json"))
    if errors: raise SystemExit("; ".join(errors))
    if set(response["checked_section_ids"])!=set(SECTION_IDS) or response["verdict"]!="ACCEPT" or response["next_stage_decision"]!="ALLOW_STAGE_6C" or any(d["result"]!="PASS" for d in response["quality_dimensions"]):
        raise SystemExit("batch critic did not accept")
    path=run_dir/"responses"/"013_stage6b_batch_critic.json"; atomic_json(path,response); atomic_json(run_dir/"quality"/"batch_integration_critic.json",response)
    append_event(run_dir,"MODEL_RESPONSE_INGESTED",response_file=str(path.relative_to(run_dir)),actual_model_id=response["actual_model_id"],endpoint_id=response["endpoint_id"])
    gate={"schema_version":"1.0","gate_id":"stage6b-batch-confirmation-001","gate_type":"BATCH_DRAFT_CONFIRMATION","batch_id":BATCH_ID,"candidate_hashes":{sid:sha256_json(read_json(run_dir/"intermediate"/sid/"polished_candidate.json")) for sid in SECTION_IDS},"question":"是否确认阶段6B三章草稿作为后续阶段的冻结上游工件？","allowed_actions":["CONFIRM","REJECT"],"requested_at":utc_now()}
    atomic_json(run_dir/"human_gate"/"stage6b_gate_request.json",gate); append_event(run_dir,"HUMAN_GATE_REQUESTED",gate_id=gate["gate_id"])
    set_state(run_dir,"WAITING_GATE","BATCH_DRAFT_CONFIRMATION",completed_section_ids=SECTION_IDS)


def build_outputs(run_dir: Path) -> dict[str, Any]:
    stage5=read_json(run_dir/"source_snapshots"/"stage5_section_plan.json")
    sections=[]; combined=["# 人机协同决策优势冲刺关键技术研究（阶段6B草稿）",""]
    for sid in SECTION_IDS:
        c=read_json(run_dir/"intermediate"/sid/"polished_candidate.json")
        md=canonical_markdown(c["section_name"],c)
        chars=sum(nonspace_chars(p["text"]) for p in paragraphs(c))
        record={"section_id":sid,"section_name":c["section_name"],"candidate_hash":sha256_json(c),"effective_char_count":chars,"target_pages":section_contract(stage5,sid)["target_pages"],"max_pages":section_contract(stage5,sid)["max_pages"],"candidate":c}
        sections.append(record)
        atomic_text(run_dir/"outputs"/f"{sid}_{c['section_name']}.md",md)
        atomic_json(run_dir/"outputs"/f"{sid}_{c['section_name']}.json",record)
        combined += [md.rstrip(),""]
    total_chars=sum(x["effective_char_count"] for x in sections)
    result={"schema_version":"1.0","stage":STAGE,"batch_id":BATCH_ID,"project_title":"人机协同决策优势冲刺关键技术研究","sections":sections,"total_effective_char_count":total_chars,"target_pages":6.4,"max_pages":7.3,"readiness":{"ready_for_stage6c":True,"ready_for_final_submission":False,"next_stage":"STAGE_6C_PROVISIONAL_DRAFTING"},"open_items_inherited":read_json(run_dir/"source_snapshots"/"stage4a_evidence_completion.json")["open_items_remaining"],"completed_at":utc_now()}
    atomic_text(run_dir/"outputs"/"stage6b_batch_draft.md","\n".join(combined).rstrip()+"\n"); atomic_json(run_dir/"outputs"/"stage6b_batch_draft.json",result)
    with (run_dir/"outputs"/"stage6b_section_summary.csv").open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=["section_id","section_name","effective_char_count","target_pages","max_pages","candidate_hash"]); w.writeheader(); w.writerows([{k:x[k] for k in w.fieldnames} for x in sections])
    return result


def manifest_and_zip(run_dir: Path) -> tuple[Path, dict[str, Any]]:
    manifest_path=run_dir/"TRACE_MANIFEST.json"; zip_path=run_dir.with_suffix(".zip")
    files=[]
    for p in sorted(run_dir.rglob("*")):
        if p.is_file() and p != manifest_path:
            files.append({"path":str(p.relative_to(run_dir)),"size_bytes":p.stat().st_size,"sha256":sha256_file(p)})
    manifest={"schema_version":"1.0","stage":STAGE,"batch_id":BATCH_ID,"generated_at":utc_now(),"file_count":len(files),"files":files}
    atomic_json(manifest_path,manifest)
    with zipfile.ZipFile(zip_path,"w",compression=zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(run_dir.rglob("*")):
            if p.is_file(): zf.write(p,p.relative_to(run_dir.parent))
    archive={"archive_path":str(zip_path),"size_bytes":zip_path.stat().st_size,"sha256":sha256_file(zip_path),"generated_at":utc_now()}
    atomic_json(run_dir.with_suffix(".archive.json"),archive)
    return zip_path,archive


def finalize_cmd(args: argparse.Namespace) -> None:
    run_dir=Path(args.run_dir).resolve(); gate=read_json(Path(args.gate_response).resolve())
    request=read_json(run_dir/"human_gate"/"stage6b_gate_request.json")
    if gate.get("gate_id")!=request["gate_id"] or gate.get("action")!="CONFIRM": raise SystemExit("gate mismatch")
    atomic_json(run_dir/"human_gate"/"stage6b_gate_response.json",gate); append_event(run_dir,"HUMAN_GATE_CONSUMED",gate_id=gate["gate_id"],action=gate["action"])
    result=build_outputs(run_dir)
    acceptance={"schema_version":"1.0","stage":STAGE,"batch_id":BATCH_ID,"status":"PASS","result_hash":sha256_json(result),"section_ids":SECTION_IDS,"model_call_count":len(list((run_dir/"responses").glob("*.json"))),"human_gate_count":1,"batch_critic_verdict":"ACCEPT","next_stage":"STAGE_6C_PROVISIONAL_DRAFTING","final_submission_ready":False,"completed_at":utc_now()}
    atomic_json(run_dir/"outputs"/"STAGE6B_ACCEPTANCE_REPORT.json",acceptance)
    set_state(run_dir,"COMPLETED","STAGE_6B_COMPLETE",completed_section_ids=SECTION_IDS,result_hash=acceptance["result_hash"],next_stage=acceptance["next_stage"])
    zip_path,archive=manifest_and_zip(run_dir)
    print(json.dumps({"result":result,"acceptance":acceptance,"trace_zip":str(zip_path),"archive":archive},ensure_ascii=False,indent=2))


def validate_cmd(args: argparse.Namespace) -> None:
    run_dir=Path(args.run_dir).resolve(); errors=[]
    manifest=read_json(run_dir/"TRACE_MANIFEST.json")
    for item in manifest["files"]:
        p=run_dir/item["path"]
        if not p.exists(): errors.append(f"missing:{item['path']}")
        elif p.stat().st_size!=item["size_bytes"]: errors.append(f"size:{item['path']}")
        elif sha256_file(p)!=item["sha256"]: errors.append(f"hash:{item['path']}")
    required=[run_dir/"outputs"/"stage6b_batch_draft.json",run_dir/"outputs"/"stage6b_batch_draft.md",run_dir/"outputs"/"STAGE6B_ACCEPTANCE_REPORT.json"]
    for p in required:
        if not p.exists(): errors.append(f"missing:{p.name}")
    print(json.dumps({"status":"PASS" if not errors else "FAIL","errors":errors,"manifest_file_count":manifest["file_count"]},ensure_ascii=False,indent=2))
    if errors: raise SystemExit(1)


def main() -> None:
    ap=argparse.ArgumentParser(); subs=ap.add_subparsers(dest="cmd",required=True)
    p=subs.add_parser("init"); p.add_argument("--run-dir",required=True); p.add_argument("--design-input",required=True); p.add_argument("--project-definition",required=True); p.add_argument("--argument-architecture",required=True); p.add_argument("--evidence-completion",required=True); p.add_argument("--section-plan",required=True); p.add_argument("--stage6a-draft",required=True); p.set_defaults(fn=init_cmd)
    for name,fn in [("ingest-writer",ingest_writer_cmd),("ingest-writer-repair",ingest_writer_repair_cmd),("ingest-critic",ingest_critic_cmd),("ingest-polish",ingest_polish_cmd),("ingest-expression-critic",ingest_expression_critic_cmd)]:
        p=subs.add_parser(name); p.add_argument("--run-dir",required=True); p.add_argument("--section-id",required=True,choices=SECTION_IDS); p.add_argument("--response-file",required=True); p.set_defaults(fn=fn)
    p=subs.add_parser("ingest-batch-critic"); p.add_argument("--run-dir",required=True); p.add_argument("--response-file",required=True); p.set_defaults(fn=ingest_batch_critic_cmd)
    p=subs.add_parser("finalize"); p.add_argument("--run-dir",required=True); p.add_argument("--gate-response",required=True); p.set_defaults(fn=finalize_cmd)
    p=subs.add_parser("validate"); p.add_argument("--run-dir",required=True); p.set_defaults(fn=validate_cmd)
    args=ap.parse_args(); args.fn(args)


if __name__ == "__main__":
    main()
