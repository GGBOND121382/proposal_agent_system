from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.util import sha256_json, utc_now
from stage2_tools.stage2_guide_fact_base import deterministic_validate as validate_stage2

STAGE = "STAGE_3_PROJECT_DEFINITION"
MODEL_ID = "gpt-5.6-thinking"
ENDPOINT_ID = "chatgpt-conversation-file-bridge"
UPSTREAM_REPAIR_CALL_KEY = "stage3-upstream-fact-repair-001"
UPSTREAM_REPAIR_CRITIC_CALL_KEY = "stage3-upstream-fact-repair-critic-001"
GENERATOR_CALL_KEY = "stage3-project-definition-generator-001"
GENERATOR_REPAIR_CALL_KEY = "stage3-project-definition-repair-001"
CRITIC_CALL_KEY = "stage3-project-definition-critic-001"
UPSTREAM_GATE_ID = "stage3-upstream-fact-repair-confirmation-001"
GATE_ID = "stage3-project-definition-confirmation-001"

REQUIRED_UPSTREAM_BINDINGS = {
    "PD-CONCEPT": "核心概念工作定义",
    "PD-PROBLEM": "问题陈述",
    "PD-GAP": "当前差距定义",
    "CP-1": "中心命题",
    "PD-ATTRIBUTE": "研究属性",
    "PD-MATURITY": "成熟度目标",
    "DOC-TARGET-PAGES": "正文目标页数",
    "DOC-REFERENCE-PAGE": "参考文献页数规则",
}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def schema(name: str) -> dict[str, Any]:
    return read_json(ROOT / "stage3_tools" / name)


def validate_schema(value: Any, schema_value: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for e in sorted(Draft202012Validator(schema_value).iter_errors(value), key=lambda x: list(x.path)):
        path = "/".join(str(p) for p in e.path) or "$"
        errors.append(f"{path}: {e.message}")
    return errors


def append_event(run_dir: Path, event_type: str, **kwargs: Any) -> None:
    path = run_dir / "events.jsonl"
    idx = 1
    if path.exists():
        idx = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip()) + 1
    record = {"index": idx, "recorded_at": utc_now(), "event_type": event_type, **kwargs}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def state(run_dir: Path, status: str, phase: str, **kwargs: Any) -> None:
    payload = {"schema_version": "1.0", "stage": STAGE, "status": status, "phase": phase, "updated_at": utc_now(), **kwargs}
    atomic_json(run_dir / "LATEST_STATE.json", payload)
    append_event(run_dir, "STATE_CHANGED", status=status, phase=phase, details=kwargs)


def upstream_coverage(stage2: dict[str, Any]) -> dict[str, Any]:
    direct = set(stage2.get("writing_permissions", {}).get("direct_fact_ids", []))
    found: dict[str, list[str]] = {k: [] for k in REQUIRED_UPSTREAM_BINDINGS}
    for fact in stage2.get("facts", []):
        if fact.get("fact_id") not in direct:
            continue
        if fact.get("assertion_policy") != "DIRECT":
            continue
        for key in REQUIRED_UPSTREAM_BINDINGS:
            if key in set(fact.get("related_design_ids", [])):
                found[key].append(fact["fact_id"])
    missing = [key for key, ids in found.items() if not ids]
    return {
        "verdict": "PASS" if not missing else "FAIL",
        "required_bindings": REQUIRED_UPSTREAM_BINDINGS,
        "found_fact_ids": found,
        "missing_bindings": missing,
    }


def make_upstream_repair_request(stage1: dict[str, Any], stage2: dict[str, Any], coverage: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "call_key": UPSTREAM_REPAIR_CALL_KEY,
        "prompt_id": "P-STAGE3-UPSTREAM-FACT-REPAIR",
        "prompt_version": "1.0.0",
        "executor_role": "Stage2 Fact Base Repair Agent",
        "model_contract": {"model_independent": True, "response_format": "JSON", "actual_model_id_required": True, "endpoint_id_required": True},
        "system_prompt": "你是阶段2事实底座定向修复Agent。不得重写已经确认的研究设计，也不得生成申请书正文。你只能在现有阶段2候选基础上补充阶段3项目定义必需、且能从阶段1冻结工件逐字或忠实归纳得到的原子事实。新增事实必须标记为CONFIRMED_DESIGN与DIRECT，绑定真实阶段1字段，并更新writing_permissions。未知信息、暂定指标和工作假设的状态不得改变。输出必须是完整阶段2候选JSON。",
        "task_prompt": "补齐核心概念工作定义、问题陈述、当前差距定义、中心命题、研究属性、成熟度目标、正文目标页数和参考文献页数规则的可直接引用事实。当前差距只能表述为项目设计所界定的差距，不得升级为经过文献检索证明的结论。保留全部原有事实、规则、开放事项、来源和权限分区。",
        "input_envelope": {"stage1_design_input": stage1, "current_stage2_candidate": stage2, "coverage_report": coverage},
        "output_schema": read_json(ROOT / "stage2_tools" / "guide_fact_base.schema.json"),
        "requested_at": utc_now(),
    }


def make_generator_request(stage1: dict[str, Any], stage2: dict[str, Any], stage1_hash: str, stage2_hash: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "call_key": GENERATOR_CALL_KEY,
        "prompt_id": "P-STAGE3-PROJECT-DEFINITION",
        "prompt_version": "1.0.0",
        "executor_role": "Project Definition Agent",
        "model_contract": {"model_independent": True, "response_format": "JSON", "actual_model_id_required": True, "endpoint_id_required": True},
        "system_prompt": "你是科研项目定义Agent。当前阶段只冻结项目定义、中心命题、研究问题、目标与研究内容关系，不生成申请书正文、论证架构或章节计划。严格按阶段2写作权限使用事实：DIRECT可直接陈述，QUALIFIED必须带暂定或假设限定，PROHIBITED不得补写。必须把系统和原型定位为验证载体，而不是把工程建设目标冒充研究命题。最接近已有工作尚未调研，因此创新只能写成待验证假设。输出必须是单个JSON对象并严格满足Schema。",
        "task_prompt": "基于已冻结的阶段1设计输入和经修复的阶段2事实底座，形成项目定义。保留3个研究问题，给出唯一中心命题、3类研究差距、4项研究目标、4项研究内容、可证伪条件、范围边界和关系图。项目定义可进入下一阶段论证架构，但正式模板、最近工作、研究基础证据、团队、经费和周期仍未冻结，因此不得放行章节规划或正文生成。",
        "input_envelope": {
            "stage1_design_input": stage1,
            "stage2_guide_fact_base": stage2,
            "upstream_hashes": {"stage1": stage1_hash, "stage2": stage2_hash},
            "stage_boundary": "PROJECT_DEFINITION_ONLY",
        },
        "output_schema": schema("project_definition.schema.json"),
        "requested_at": utc_now(),
    }


def deterministic_validate(candidate: dict[str, Any], stage1: dict[str, Any], stage2: dict[str, Any], stage1_hash: str, stage2_hash: str) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    def add(code: str, severity: str, message: str) -> None:
        findings.append({"code": code, "severity": severity, "message": message})

    errors = validate_schema(candidate, schema("project_definition.schema.json"))
    for err in errors:
        add("SCHEMA_ERROR", "BLOCKING", err)
    if errors:
        return {"verdict": "FAIL", "candidate_hash": sha256_json(candidate), "findings": findings}

    ups = {x["stage"]: x for x in candidate["upstream_artifacts"]}
    if ups.get("STAGE_1_DESIGN_INPUT", {}).get("sha256") != stage1_hash:
        add("STAGE1_HASH_MISMATCH", "BLOCKING", "阶段1哈希与冻结快照不一致。")
    if ups.get("STAGE_2_GUIDE_AND_FACT_BASE", {}).get("sha256") != stage2_hash:
        add("STAGE2_HASH_MISMATCH", "BLOCKING", "阶段2哈希与冻结快照不一致。")

    contract = candidate["document_contract"]
    if contract["body_page_limit"] != stage1["document_contract"]["body_page_limit"]:
        add("PAGE_LIMIT_CHANGED", "BLOCKING", "项目定义改变了阶段1正文页数上限。")
    if contract["target_body_pages"] != stage1["document_contract"]["target_body_pages"]:
        add("PAGE_TARGET_CHANGED", "BLOCKING", "项目定义改变了阶段1目标页数。")

    def unique(items: list[dict[str, Any]], key: str) -> set[str]:
        vals = [x[key] for x in items]
        if len(vals) != len(set(vals)):
            add("DUPLICATE_ID", "BLOCKING", f"{key}存在重复ID。")
        return set(vals)

    rq_ids = unique(candidate["research_questions"], "rq_id")
    gap_ids = unique(candidate["research_gaps"], "gap_id")
    obj_ids = unique(candidate["objectives"], "objective_id")
    content_ids = unique(candidate["research_contents"], "content_id")
    relation_ids = unique(candidate["relationship_graph"], "relation_id")
    expected_rqs = {x["rq_id"] for x in stage1["research_questions"]}
    expected_objs = {x["objective_id"] for x in stage1["objectives"]}
    if rq_ids != expected_rqs:
        add("RQ_SET_CHANGED", "BLOCKING", f"研究问题集合必须保持{sorted(expected_rqs)}，实际为{sorted(rq_ids)}。")
    if obj_ids != expected_objs:
        add("OBJECTIVE_SET_CHANGED", "BLOCKING", f"目标集合必须保持{sorted(expected_objs)}，实际为{sorted(obj_ids)}。")
    if candidate["central_proposition"]["proposition_id"] != "CP-1":
        add("CENTRAL_PROPOSITION_COUNT_INVALID", "BLOCKING", "必须只有一个中心命题CP-1。")

    method_ids = {x["method_id"] for x in stage1["method_system"]}
    wp_ids = {x["wp_id"] for x in stage1["work_packages"]}
    scenario_ids = {x["scenario_id"] for x in stage1["application_scenarios"]}
    metric_ids = {x["metric_id"] for x in stage1["evaluation_design"]["metrics"]}
    for rq in candidate["research_questions"]:
        if set(rq["gap_ids"]) - gap_ids:
            add("RQ_UNKNOWN_GAP", "BLOCKING", f"{rq['rq_id']}引用未知差距。")
        if set(rq["objective_ids"]) - obj_ids:
            add("RQ_UNKNOWN_OBJECTIVE", "BLOCKING", f"{rq['rq_id']}引用未知目标。")
        if set(rq["method_ids"]) - method_ids:
            add("RQ_UNKNOWN_METHOD", "BLOCKING", f"{rq['rq_id']}引用未知方法。")
        if any(word in rq["question"] for word in ["构建平台", "开发系统", "形成原型"]):
            add("ENGINEERING_OBJECTIVE_MASQUERADES_AS_RESEARCH", "BLOCKING", f"{rq['rq_id']}被写成工程建设任务。")
    for gap in candidate["research_gaps"]:
        if set(gap["affected_rq_ids"]) - rq_ids:
            add("GAP_UNKNOWN_RQ", "BLOCKING", f"{gap['gap_id']}引用未知研究问题。")
    for obj in candidate["objectives"]:
        if set(obj["rq_ids"]) - rq_ids or set(obj["content_ids"]) - content_ids:
            add("OBJECTIVE_ALIGNMENT_INVALID", "BLOCKING", f"{obj['objective_id']}关系不闭合。")
    for content in candidate["research_contents"]:
        if set(content["rq_ids"]) - rq_ids or set(content["objective_ids"]) - obj_ids:
            add("CONTENT_ALIGNMENT_INVALID", "BLOCKING", f"{content['content_id']}问题或目标引用无效。")
        if set(content["method_ids"]) - method_ids or set(content["work_package_ids"]) - wp_ids:
            add("CONTENT_METHOD_WP_INVALID", "BLOCKING", f"{content['content_id']}方法或工作包引用无效。")
        if set(content["validation_carrier_ids"]) - scenario_ids:
            add("CONTENT_SCENARIO_INVALID", "BLOCKING", f"{content['content_id']}场景引用无效。")

    cp = candidate["central_proposition"]
    if set(cp["mechanism_ids"]) - method_ids:
        add("CP_UNKNOWN_METHOD", "BLOCKING", "中心命题引用未知方法。")
    if set(cp["outcome_metric_ids"]) - metric_ids:
        add("CP_UNKNOWN_METRIC", "BLOCKING", "中心命题引用未知指标。")
    if len(cp["falsification_conditions"]) < 3:
        add("CP_NOT_FALSIFIABLE", "BLOCKING", "中心命题缺少充分可证伪条件。")

    permissions = stage2["writing_permissions"]
    direct_allowed = set(permissions["direct_fact_ids"])
    qualified_allowed = set(permissions["qualified_fact_ids"])
    prohibited_allowed = set(permissions["prohibited_fact_ids"])
    usage = candidate["fact_usage"]
    if set(usage["direct_fact_ids"]) - direct_allowed:
        add("UNAUTHORIZED_DIRECT_FACT", "BLOCKING", "项目定义直接使用了未授权事实。")
    if set(usage["qualified_fact_ids"]) - qualified_allowed:
        add("UNAUTHORIZED_QUALIFIED_FACT", "BLOCKING", "项目定义限定使用了未授权事实。")
    if set(usage["prohibited_fact_ids_checked"]) != prohibited_allowed:
        add("PROHIBITED_FACT_CHECK_INCOMPLETE", "BLOCKING", "没有完整核对全部禁止事实。")
    open_ids = {x["item_id"] for x in stage2["open_items"]}
    if set(usage["unknown_open_item_ids_inherited"]) != open_ids:
        add("OPEN_ITEM_USAGE_INCOMPLETE", "BLOCKING", "事实使用边界没有继承全部开放事项。")
    if {x["item_id"] for x in candidate["open_items_inherited"]} != open_ids:
        add("OPEN_ITEM_INHERITANCE_INCOMPLETE", "BLOCKING", "项目定义没有继承全部开放事项。")

    if any(x["closest_prior_work_status"] != "NOT_RESEARCHED" or x["novelty_status"] != "TO_BE_VALIDATED" for x in candidate["innovation_hypotheses"]):
        add("NOVELTY_PREMATURELY_CONFIRMED", "BLOCKING", "未完成相关工作调研前不得确认创新性。")
    if not all("OPEN-013" in x["blocked_by_open_item_ids"] for x in candidate["innovation_hypotheses"]):
        add("INNOVATION_NOT_BLOCKED_BY_RESEARCH", "BLOCKING", "创新假设必须受正式参考文献范围开放事项约束。")
    if candidate["readiness"]["ready_for_section_planning"]:
        add("PREMATURE_SECTION_PLANNING", "BLOCKING", "当前不得放行章节规划。")

    nodes = {"CP-1", *rq_ids, *gap_ids, *obj_ids, *content_ids, *method_ids, *metric_ids, *wp_ids, *scenario_ids}
    for rel in candidate["relationship_graph"]:
        if rel["from_id"] not in nodes or rel["to_id"] not in nodes:
            add("RELATION_UNKNOWN_NODE", "BLOCKING", f"{rel['relation_id']}引用未知节点。")
    if len(relation_ids) < 20:
        add("RELATION_GRAPH_TOO_SHALLOW", "BLOCKING", "关系图过浅。")
    outgoing_cp = [x for x in candidate["relationship_graph"] if x["from_id"] == "CP-1"]
    if not outgoing_cp:
        add("CP_NOT_CONNECTED", "BLOCKING", "中心命题没有进入关系图。")

    blocking = [x for x in findings if x["severity"] == "BLOCKING"]
    return {
        "verdict": "PASS" if not blocking else "FAIL",
        "candidate_hash": sha256_json(candidate),
        "statistics": {
            "research_gaps": len(gap_ids), "research_questions": len(rq_ids), "objectives": len(obj_ids),
            "research_contents": len(content_ids), "method_hypotheses": len(candidate["method_hypotheses"]),
            "innovation_hypotheses": len(candidate["innovation_hypotheses"]), "relations": len(relation_ids),
            "open_items": len(candidate["open_items_inherited"]),
        },
        "checked_dimensions": [
            "JSON_SCHEMA", "UPSTREAM_HASH", "DOCUMENT_CONTRACT", "CENTRAL_PROPOSITION",
            "RESEARCH_GAP_RQ_ALIGNMENT", "OBJECTIVE_CONTENT_ALIGNMENT", "METHOD_WP_SCENARIO_ALIGNMENT",
            "FALSIFIABILITY", "FACT_PERMISSION", "OPEN_ITEM_INHERITANCE", "INNOVATION_BOUNDARY",
            "ENGINEERING_RESEARCH_BOUNDARY", "RELATION_GRAPH", "NEXT_STAGE_BOUNDARY",
        ],
        "findings": findings,
    }


def init_cmd(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    stage1_path = Path(args.design_input).resolve()
    stage2_path = Path(args.guide_fact_base).resolve()
    if run_dir.exists() and any(run_dir.iterdir()):
        raise SystemExit("run directory must be empty")
    run_dir.mkdir(parents=True, exist_ok=True)
    for name in ["requests", "responses", "schemas", "intermediate", "quality", "human_gate", "outputs", "source_snapshots", "repairs"]:
        (run_dir / name).mkdir(parents=True, exist_ok=True)
    stage1 = read_json(stage1_path); stage2 = read_json(stage2_path)
    if stage1.get("stage") != "STAGE_1_DESIGN_INPUT" or stage2.get("stage") != "STAGE_2_GUIDE_AND_FACT_BASE":
        raise SystemExit("invalid upstream stage")
    s1 = run_dir / "source_snapshots" / "stage1_design_input.json"
    s2 = run_dir / "source_snapshots" / "stage2_guide_fact_base_original.json"
    s1.write_text(stage1_path.read_text(encoding="utf-8"), encoding="utf-8")
    s2.write_text(stage2_path.read_text(encoding="utf-8"), encoding="utf-8")
    for name in ["project_definition.schema.json", "project_definition_critic.schema.json"]:
        (run_dir / "schemas" / name).write_text((ROOT / "stage3_tools" / name).read_text(encoding="utf-8"), encoding="utf-8")
    metadata = {
        "schema_version": "1.0", "stage": STAGE, "project_title": stage1["project_title"], "created_at": utc_now(),
        "run_dir": str(run_dir), "stage_boundary": "PROJECT_DEFINITION_ONLY", "model_bridge": "CHAT_FILE_BRIDGE",
        "stage1_sha256": sha256_file(s1), "stage2_original_sha256": sha256_file(s2),
    }
    atomic_json(run_dir / "RUN_METADATA.json", metadata)
    coverage = upstream_coverage(stage2)
    atomic_json(run_dir / "quality" / "upstream_project_definition_coverage.json", coverage)
    append_event(run_dir, "RUN_INITIALIZED", stage1_sha256=metadata["stage1_sha256"], stage2_sha256=metadata["stage2_original_sha256"])
    if coverage["verdict"] != "PASS":
        req = make_upstream_repair_request(stage1, stage2, coverage)
        atomic_json(run_dir / "requests" / "001_upstream_fact_repair.json", req)
        append_event(run_dir, "UPSTREAM_GAP_DETECTED", missing_bindings=coverage["missing_bindings"])
        append_event(run_dir, "MODEL_REQUEST_CREATED", call_key=UPSTREAM_REPAIR_CALL_KEY, prompt_id=req["prompt_id"])
        state(run_dir, "WAITING_MODEL", "UPSTREAM_FACT_REPAIR")
        print(json.dumps({"status": "WAITING_MODEL", "request": str(run_dir / "requests" / "001_upstream_fact_repair.json"), "missing_bindings": coverage["missing_bindings"]}, ensure_ascii=False, indent=2))
        return
    _create_stage3_generator_request(run_dir, stage1, stage2, metadata["stage1_sha256"], metadata["stage2_original_sha256"])


def ingest_upstream_repair_cmd(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    env = read_json(Path(args.response_file).resolve())
    if env.get("call_key") != UPSTREAM_REPAIR_CALL_KEY or env.get("prompt_id") != "P-STAGE3-UPSTREAM-FACT-REPAIR":
        raise SystemExit("upstream repair response mismatch")
    if not env.get("model_id") or not env.get("endpoint_id"):
        raise SystemExit("missing actual model or endpoint")
    candidate = env.get("output")
    meta = read_json(run_dir / "RUN_METADATA.json")
    report = validate_stage2(candidate, meta["stage1_sha256"])
    coverage = upstream_coverage(candidate)
    combined = {"stage2_deterministic_report": report, "project_definition_coverage": coverage, "verdict": "PASS" if report["verdict"] == "PASS" and coverage["verdict"] == "PASS" else "FAIL"}
    atomic_json(run_dir / "responses" / "001_upstream_fact_repair.json", env)
    atomic_json(run_dir / "repairs" / "stage2_guide_fact_base_repaired_candidate.json", candidate)
    atomic_json(run_dir / "quality" / "upstream_repair_deterministic_report.json", combined)
    append_event(run_dir, "MODEL_RESPONSE_INGESTED", call_key=UPSTREAM_REPAIR_CALL_KEY, model_id=env["model_id"], endpoint_id=env["endpoint_id"], verdict=combined["verdict"], candidate_hash=sha256_json(candidate))
    if combined["verdict"] != "PASS":
        state(run_dir, "BLOCKED", "UPSTREAM_FACT_REPAIR_REVIEW")
        raise SystemExit(2)
    critic_req = {
        "schema_version": "1.0", "call_key": UPSTREAM_REPAIR_CRITIC_CALL_KEY,
        "prompt_id": "P-STAGE3-UPSTREAM-FACT-REPAIR-CRITIC", "prompt_version": "1.0.0",
        "executor_role": "Independent Stage2 Fact Repair Critic",
        "model_contract": {"independent_from_generator": True, "response_format": "JSON", "actual_model_id_required": True, "endpoint_id_required": True},
        "system_prompt": "你是独立事实底座修复Critic。检查新增事实是否都能从阶段1冻结设计输入直接定位，是否保持原子性和状态边界，是否没有改变原有未知信息、暂定指标、规则或开放事项。若修复仅补足项目定义所需事实且无越权，返回ACCEPT。",
        "task_prompt": "审查阶段2定向修复。重点确认当前差距仍被标为项目设计假设，而不是文献检索结论；中心命题、问题陈述、概念定义、成熟度和页数规则均具有DIRECT事实绑定。",
        "input_envelope": {"original": read_json(run_dir / "source_snapshots" / "stage2_guide_fact_base_original.json"), "repaired": candidate, "deterministic_report": combined},
        "output_schema": read_json(ROOT / "stage2_tools" / "guide_fact_critic.schema.json"), "requested_at": utc_now(),
    }
    atomic_json(run_dir / "requests" / "002_upstream_fact_repair_critic.json", critic_req)
    append_event(run_dir, "MODEL_REQUEST_CREATED", call_key=UPSTREAM_REPAIR_CRITIC_CALL_KEY, prompt_id=critic_req["prompt_id"])
    state(run_dir, "WAITING_MODEL", "UPSTREAM_FACT_REPAIR_CRITIC")


def ingest_upstream_repair_critic_cmd(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve(); env = read_json(Path(args.response_file).resolve())
    if env.get("call_key") != UPSTREAM_REPAIR_CRITIC_CALL_KEY or env.get("prompt_id") != "P-STAGE3-UPSTREAM-FACT-REPAIR-CRITIC":
        raise SystemExit("upstream repair critic mismatch")
    output = env.get("output")
    errors = validate_schema(output, read_json(ROOT / "stage2_tools" / "guide_fact_critic.schema.json"))
    if errors:
        raise SystemExit("critic schema errors: " + " | ".join(errors))
    candidate = read_json(run_dir / "repairs" / "stage2_guide_fact_base_repaired_candidate.json")
    ch = sha256_json(candidate)
    if output["approved_candidate_hash"] != ch:
        raise SystemExit("approved hash mismatch")
    atomic_json(run_dir / "responses" / "002_upstream_fact_repair_critic.json", env)
    atomic_json(run_dir / "quality" / "upstream_repair_independent_critic.json", output)
    append_event(run_dir, "MODEL_RESPONSE_INGESTED", call_key=UPSTREAM_REPAIR_CRITIC_CALL_KEY, model_id=env["model_id"], endpoint_id=env["endpoint_id"], verdict=output["verdict"], candidate_hash=ch)
    if output["verdict"] != "ACCEPT" or any(x["severity"] in {"BLOCKING", "MAJOR"} for x in output["findings"]):
        state(run_dir, "BLOCKED", "UPSTREAM_FACT_REPAIR_CRITIC")
        raise SystemExit(2)
    gate = {
        "schema_version": "1.0", "gate_id": UPSTREAM_GATE_ID, "gate_type": "UPSTREAM_FACT_REPAIR_CONFIRMATION",
        "required_role": "PROJECT_OWNER", "allowed_actions": ["CONFIRM", "REVISE"], "context_hash": ch,
        "summary": {"added_project_definition_bindings": list(REQUIRED_UPSTREAM_BINDINGS), "candidate_hash": ch}, "requested_at": utc_now(),
    }
    atomic_json(run_dir / "human_gate" / "upstream_repair_request.json", gate)
    append_event(run_dir, "HUMAN_GATE_REQUEST_CREATED", gate_id=UPSTREAM_GATE_ID, context_hash=ch)
    state(run_dir, "WAITING_HUMAN", "UPSTREAM_FACT_REPAIR_CONFIRMATION")


def confirm_upstream_repair_cmd(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve(); decision = read_json(Path(args.gate_response).resolve())
    req = read_json(run_dir / "human_gate" / "upstream_repair_request.json")
    if decision.get("gate_id") != UPSTREAM_GATE_ID or decision.get("context_hash") != req["context_hash"]:
        raise SystemExit("upstream gate mismatch")
    atomic_json(run_dir / "human_gate" / "upstream_repair_response.json", decision)
    if decision.get("action") != "CONFIRM":
        state(run_dir, "BLOCKED", "UPSTREAM_FACT_REPAIR_CONFIRMATION")
        raise SystemExit(2)
    candidate = read_json(run_dir / "repairs" / "stage2_guide_fact_base_repaired_candidate.json")
    repaired = run_dir / "source_snapshots" / "stage2_guide_fact_base_repaired.json"
    atomic_json(repaired, candidate)
    meta = read_json(run_dir / "RUN_METADATA.json")
    meta["stage2_repaired_sha256"] = sha256_file(repaired)
    meta["upstream_repair_confirmed_at"] = utc_now()
    atomic_json(run_dir / "RUN_METADATA.json", meta)
    append_event(run_dir, "HUMAN_GATE_CONSUMED", gate_id=UPSTREAM_GATE_ID, action="CONFIRM")
    stage1 = read_json(run_dir / "source_snapshots" / "stage1_design_input.json")
    _create_stage3_generator_request(run_dir, stage1, candidate, meta["stage1_sha256"], meta["stage2_repaired_sha256"])


def _create_stage3_generator_request(run_dir: Path, stage1: dict[str, Any], stage2: dict[str, Any], s1h: str, s2h: str) -> None:
    req = make_generator_request(stage1, stage2, s1h, s2h)
    atomic_json(run_dir / "requests" / "003_project_definition_generator.json", req)
    append_event(run_dir, "MODEL_REQUEST_CREATED", call_key=GENERATOR_CALL_KEY, prompt_id=req["prompt_id"])
    state(run_dir, "WAITING_MODEL", "PROJECT_DEFINITION_GENERATOR")


def _current_stage2_snapshot(run_dir: Path) -> Path:
    repaired = run_dir / "source_snapshots" / "stage2_guide_fact_base_repaired.json"
    return repaired if repaired.exists() else run_dir / "source_snapshots" / "stage2_guide_fact_base_original.json"


def _active_candidate_path(run_dir: Path) -> Path:
    repaired = run_dir / "intermediate" / "project_definition_candidate_repaired.json"
    return repaired if repaired.exists() else run_dir / "intermediate" / "project_definition_candidate.json"


def _generator_response_path(run_dir: Path) -> Path:
    repaired = run_dir / "responses" / "005_project_definition_repair.json"
    return repaired if repaired.exists() else run_dir / "responses" / "003_project_definition_generator.json"


def _create_project_definition_critic_request(run_dir: Path, candidate: dict[str, Any], report: dict[str, Any], stage2: dict[str, Any]) -> None:
    critic_req = {
        "schema_version": "1.0", "call_key": CRITIC_CALL_KEY, "prompt_id": "P-STAGE3-PROJECT-DEFINITION-CRITIC", "prompt_version": "1.0.0",
        "executor_role": "Independent Project Definition Critic",
        "model_contract": {"independent_from_generator": True, "response_format": "JSON", "actual_model_id_required": True, "endpoint_id_required": True},
        "system_prompt": "你是独立项目定义Critic，不撰写申请书正文。逐项检查文种契约、唯一中心命题、差距与问题、目标与研究内容、方法与评价、工程载体边界、事实权限、开放事项和下一阶段边界。当前未做公开资料调研，因此不得把创新假设判定为已证实创新。没有阻断或重大问题时返回ACCEPT。",
        "task_prompt": "审查阶段3项目定义是否形成可验证、可证伪、可供论证架构使用的冻结对象；确认3个研究问题不是系统功能列表，原型只作为验证载体，所有暂定指标保持限定状态，全部开放事项继续继承。",
        "input_envelope": {"candidate": candidate, "deterministic_report": report, "stage2_writing_permissions": stage2["writing_permissions"]},
        "output_schema": schema("project_definition_critic.schema.json"), "requested_at": utc_now(),
    }
    atomic_json(run_dir / "requests" / "004_project_definition_critic.json", critic_req)
    append_event(run_dir, "MODEL_REQUEST_CREATED", call_key=CRITIC_CALL_KEY, prompt_id=critic_req["prompt_id"])
    state(run_dir, "WAITING_MODEL", "PROJECT_DEFINITION_CRITIC")


def _schedule_project_definition_repair(run_dir: Path) -> None:
    candidate = read_json(run_dir / "intermediate" / "project_definition_candidate.json")
    report = read_json(run_dir / "quality" / "deterministic_project_definition_report.json")
    request = {
        "schema_version": "1.0", "call_key": GENERATOR_REPAIR_CALL_KEY,
        "prompt_id": "P-STAGE3-PROJECT-DEFINITION-REPAIR", "prompt_version": "1.0.0",
        "executor_role": "Project Definition Repair Agent",
        "model_contract": {"model_independent": True, "response_format": "JSON", "actual_model_id_required": True, "endpoint_id_required": True, "max_repair_rounds": 1},
        "system_prompt": "你是项目定义定向修复Agent。只能修复确定性报告指出的字段，不得改变已经通过校验的研究问题、目标、事实权限、中心命题、开放事项或阶段边界。返回完整项目定义JSON，不得输出解释文字。",
        "task_prompt": "依据确定性Finding做最小修改。保留原候选全部已合格内容和ID，只修复Schema或明确关系错误。",
        "input_envelope": {"candidate": candidate, "deterministic_report": report},
        "output_schema": schema("project_definition.schema.json"), "requested_at": utc_now(),
    }
    atomic_json(run_dir / "requests" / "005_project_definition_repair.json", request)
    append_event(run_dir, "MODEL_REQUEST_CREATED", call_key=GENERATOR_REPAIR_CALL_KEY, prompt_id=request["prompt_id"], repair_round=1)
    state(run_dir, "WAITING_MODEL", "PROJECT_DEFINITION_REPAIR", repair_round=1)


def schedule_generator_repair_cmd(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    if not (run_dir / "quality" / "deterministic_project_definition_report.json").exists():
        raise SystemExit("no failed generator report to repair")
    if (run_dir / "requests" / "005_project_definition_repair.json").exists():
        raise SystemExit("repair request already exists")
    _schedule_project_definition_repair(run_dir)


def ingest_generator_cmd(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve(); env = read_json(Path(args.response_file).resolve())
    if env.get("call_key") != GENERATOR_CALL_KEY or env.get("prompt_id") != "P-STAGE3-PROJECT-DEFINITION":
        raise SystemExit("generator response mismatch")
    if not env.get("model_id") or not env.get("endpoint_id"):
        raise SystemExit("missing actual model or endpoint")
    stage1 = read_json(run_dir / "source_snapshots" / "stage1_design_input.json")
    stage2_path = _current_stage2_snapshot(run_dir); stage2 = read_json(stage2_path)
    meta = read_json(run_dir / "RUN_METADATA.json")
    s2h = meta.get("stage2_repaired_sha256") or meta["stage2_original_sha256"]
    candidate = env.get("output")
    report = deterministic_validate(candidate, stage1, stage2, meta["stage1_sha256"], s2h)
    atomic_json(run_dir / "responses" / "003_project_definition_generator.json", env)
    atomic_json(run_dir / "intermediate" / "project_definition_candidate.json", candidate)
    atomic_json(run_dir / "quality" / "deterministic_project_definition_report.json", report)
    append_event(run_dir, "MODEL_RESPONSE_INGESTED", call_key=GENERATOR_CALL_KEY, model_id=env["model_id"], endpoint_id=env["endpoint_id"], verdict=report["verdict"], candidate_hash=report["candidate_hash"])
    if report["verdict"] != "PASS":
        _schedule_project_definition_repair(run_dir)
        return
    _create_project_definition_critic_request(run_dir, candidate, report, stage2)


def ingest_generator_repair_cmd(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve(); env = read_json(Path(args.response_file).resolve())
    if env.get("call_key") != GENERATOR_REPAIR_CALL_KEY or env.get("prompt_id") != "P-STAGE3-PROJECT-DEFINITION-REPAIR":
        raise SystemExit("generator repair response mismatch")
    if not env.get("model_id") or not env.get("endpoint_id"):
        raise SystemExit("missing actual model or endpoint")
    stage1 = read_json(run_dir / "source_snapshots" / "stage1_design_input.json")
    stage2 = read_json(_current_stage2_snapshot(run_dir))
    meta = read_json(run_dir / "RUN_METADATA.json")
    s2h = meta.get("stage2_repaired_sha256") or meta["stage2_original_sha256"]
    candidate = env.get("output")
    report = deterministic_validate(candidate, stage1, stage2, meta["stage1_sha256"], s2h)
    atomic_json(run_dir / "responses" / "005_project_definition_repair.json", env)
    atomic_json(run_dir / "intermediate" / "project_definition_candidate_repaired.json", candidate)
    atomic_json(run_dir / "quality" / "deterministic_project_definition_repair_report.json", report)
    append_event(run_dir, "MODEL_RESPONSE_INGESTED", call_key=GENERATOR_REPAIR_CALL_KEY, model_id=env["model_id"], endpoint_id=env["endpoint_id"], verdict=report["verdict"], candidate_hash=report["candidate_hash"], repair_round=1)
    if report["verdict"] != "PASS":
        state(run_dir, "BLOCKED", "PROJECT_DEFINITION_REPAIR_EXHAUSTED", repair_round=1)
        raise SystemExit(2)
    _create_project_definition_critic_request(run_dir, candidate, report, stage2)

def ingest_critic_cmd(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve(); env = read_json(Path(args.response_file).resolve())
    if env.get("call_key") != CRITIC_CALL_KEY or env.get("prompt_id") != "P-STAGE3-PROJECT-DEFINITION-CRITIC":
        raise SystemExit("critic response mismatch")
    output = env.get("output"); errors = validate_schema(output, schema("project_definition_critic.schema.json"))
    if errors:
        raise SystemExit("critic schema errors: " + " | ".join(errors))
    candidate = read_json(_active_candidate_path(run_dir))
    ch = sha256_json(candidate)
    if output["approved_candidate_hash"] != ch:
        raise SystemExit("critic approved hash mismatch")
    dims = [x["dimension"] for x in output["checked_dimensions"]]
    if set(dims) != {"DOCUMENT_CONTRACT","CENTRAL_PROPOSITION","RESEARCH_GAPS","RESEARCH_QUESTIONS","OBJECTIVE_CONTENT_ALIGNMENT","METHOD_AND_EVALUATION","ENGINEERING_RESEARCH_BOUNDARY","FACT_SOURCE_BOUNDARY","OPEN_ITEM_INHERITANCE","NEXT_STAGE_BOUNDARY"}:
        raise SystemExit("critic dimensions incomplete or duplicated")
    atomic_json(run_dir / "responses" / "004_project_definition_critic.json", env)
    atomic_json(run_dir / "quality" / "independent_critic_report.json", output)
    append_event(run_dir, "MODEL_RESPONSE_INGESTED", call_key=CRITIC_CALL_KEY, model_id=env["model_id"], endpoint_id=env["endpoint_id"], verdict=output["verdict"], candidate_hash=ch)
    if output["verdict"] != "ACCEPT" or any(x["severity"] in {"BLOCKING", "MAJOR"} for x in output["findings"]):
        state(run_dir, "BLOCKED", "PROJECT_DEFINITION_CRITIC")
        raise SystemExit(2)
    gate = {
        "schema_version": "1.0", "gate_id": GATE_ID, "gate_type": "PROJECT_DEFINITION_CONFIRMATION", "required_role": "PROJECT_OWNER",
        "allowed_actions": ["CONFIRM", "REVISE"], "context_hash": ch,
        "summary": {"central_proposition": candidate["central_proposition"]["statement"], "research_questions": [x["question"] for x in candidate["research_questions"]], "frozen_elements": candidate["readiness"]["frozen_elements"], "non_frozen_elements": candidate["readiness"]["non_frozen_elements"]},
        "requested_at": utc_now(),
    }
    atomic_json(run_dir / "human_gate" / "project_definition_request.json", gate)
    append_event(run_dir, "HUMAN_GATE_REQUEST_CREATED", gate_id=GATE_ID, context_hash=ch)
    state(run_dir, "WAITING_HUMAN", "PROJECT_DEFINITION_CONFIRMATION")


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fields})


def write_outputs(candidate: dict[str, Any], out: Path) -> None:
    atomic_json(out / "stage3_project_definition.json", candidate)
    (out / "stage3_project_definition.yaml").write_text(yaml.safe_dump(candidate, allow_unicode=True, sort_keys=False), encoding="utf-8")
    lines = [f"# {candidate['project_title']}：阶段3项目定义", "", "## 项目定位", "", candidate["problem_definition"]["problem_statement"], "", "## 中心命题", "", candidate["central_proposition"]["statement"], "", "## 研究差距"]
    for x in candidate["research_gaps"]:
        lines.append(f"- **{x['gap_id']}**：{x['statement']}（状态：{x['claim_status']}）")
    lines += ["", "## 研究问题"]
    for x in candidate["research_questions"]:
        lines.append(f"- **{x['rq_id']}**：{x['question']}")
    lines += ["", "## 研究目标"]
    for x in candidate["objectives"]:
        lines.append(f"- **{x['objective_id']} [{x['objective_type']}]**：{x['statement']}")
    lines += ["", "## 研究内容"]
    for x in candidate["research_contents"]:
        lines.append(f"- **{x['content_id']} {x['name']}**：{x['research_focus']}")
    lines += ["", "## 当前不冻结内容"]
    for x in candidate["readiness"]["non_frozen_elements"]:
        lines.append(f"- {x}")
    lines += ["", "## 阶段放行", "", candidate["readiness"]["rationale"], ""]
    (out / "stage3_project_definition.md").write_text("\n".join(lines), encoding="utf-8")
    write_csv(out / "stage3_rq_matrix.csv", [{
        "rq_id":x["rq_id"], "question":x["question"], "gap_ids":"|".join(x["gap_ids"]), "objective_ids":"|".join(x["objective_ids"]),
        "method_ids":"|".join(x["method_ids"]), "verification_evidence":"|".join(x["verification_evidence"]), "falsification_criteria":"|".join(x["falsification_criteria"]), "boundary":x["boundary"]
    } for x in candidate["research_questions"]], ["rq_id","question","gap_ids","objective_ids","method_ids","verification_evidence","falsification_criteria","boundary"])
    write_csv(out / "stage3_relationship_graph.csv", candidate["relationship_graph"], ["relation_id","from_id","relation","to_id","rationale"])
    write_csv(out / "stage3_open_items.csv", candidate["open_items_inherited"], ["item_id","field","required_before_stage","blocking_now"])


def finalize_cmd(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve(); decision = read_json(Path(args.gate_response).resolve())
    req = read_json(run_dir / "human_gate" / "project_definition_request.json")
    if decision.get("gate_id") != GATE_ID or decision.get("context_hash") != req["context_hash"]:
        raise SystemExit("project definition gate mismatch")
    atomic_json(run_dir / "human_gate" / "project_definition_response.json", decision)
    if decision.get("action") != "CONFIRM":
        state(run_dir, "BLOCKED", "PROJECT_DEFINITION_CONFIRMATION")
        raise SystemExit(2)
    candidate = read_json(_active_candidate_path(run_dir))
    stage1 = read_json(run_dir / "source_snapshots" / "stage1_design_input.json")
    stage2 = read_json(_current_stage2_snapshot(run_dir))
    meta = read_json(run_dir / "RUN_METADATA.json"); s2h = meta.get("stage2_repaired_sha256") or meta["stage2_original_sha256"]
    report = deterministic_validate(candidate, stage1, stage2, meta["stage1_sha256"], s2h)
    atomic_json(run_dir / "quality" / "final_revalidation.json", report)
    if report["verdict"] != "PASS":
        state(run_dir, "BLOCKED", "FINAL_REVALIDATION")
        raise SystemExit(2)
    out = run_dir / "outputs"; write_outputs(candidate, out)
    acceptance = {
        "schema_version":"1.0","stage":STAGE,"result":"PASS","candidate_hash":sha256_json(candidate),
        "upstream_hashes":{"stage1":meta["stage1_sha256"],"stage2":s2h},
        "upstream_repair_performed":bool(meta.get("stage2_repaired_sha256")),
        "generator":{"model_id":read_json(_generator_response_path(run_dir))["model_id"],"endpoint_id":read_json(_generator_response_path(run_dir))["endpoint_id"],"repair_used":_generator_response_path(run_dir).name.startswith("005_")},
        "critic":{"model_id":read_json(run_dir/"responses"/"004_project_definition_critic.json")["model_id"],"endpoint_id":read_json(run_dir/"responses"/"004_project_definition_critic.json")["endpoint_id"],"verdict":"ACCEPT"},
        "human_gate":{"action":"CONFIRM","decided_by":decision.get("decided_by"),"decided_role":decision.get("decided_role")},
        "statistics":report["statistics"],"next_stage":"STAGE_4_ARGUMENT_ARCHITECTURE","completed_at":utc_now(),
    }
    atomic_json(out / "STAGE3_ACCEPTANCE_REPORT.json", acceptance)
    append_event(run_dir,"HUMAN_GATE_CONSUMED",gate_id=GATE_ID,action="CONFIRM")
    state(run_dir,"COMPLETED","STAGE_3_COMPLETE",candidate_hash=acceptance["candidate_hash"],next_stage=acceptance["next_stage"])
    z=package_trace(run_dir)
    print(json.dumps({"status":"COMPLETED","run_dir":str(run_dir),"trace_zip":str(z),"candidate_hash":acceptance["candidate_hash"]},ensure_ascii=False,indent=2))


def build_manifest(run_dir: Path) -> None:
    excluded={"TRACE_MANIFEST.json","TRACE_ARCHIVE.json"}; files=[]
    for p in sorted(run_dir.rglob("*")):
        if p.is_file() and p.name not in excluded:
            files.append({"path":str(p.relative_to(run_dir)),"size_bytes":p.stat().st_size,"sha256":sha256_file(p)})
    atomic_json(run_dir/"TRACE_MANIFEST.json",{"schema_version":"1.0","root":str(run_dir),"file_count":len(files),"files":files,"archive_policy":"TRACE_ARCHIVE.json is external to the archive hash manifest to avoid a self-reference cycle.","generated_at":utc_now()})


def package_trace(run_dir: Path) -> Path:
    build_manifest(run_dir); zpath=run_dir.with_suffix(".zip")
    if zpath.exists(): zpath.unlink()
    with zipfile.ZipFile(zpath,"w",compression=zipfile.ZIP_DEFLATED) as z:
        for p in sorted(run_dir.rglob("*")):
            if p.is_file() and p.name!="TRACE_ARCHIVE.json": z.write(p,p.relative_to(run_dir.parent))
    atomic_json(run_dir/"TRACE_ARCHIVE.json",{"path":str(zpath),"size_bytes":zpath.stat().st_size,"sha256":sha256_file(zpath),"created_at":utc_now()})
    return zpath


def validate_cmd(args: argparse.Namespace) -> None:
    candidate=read_json(Path(args.candidate)); stage1=read_json(Path(args.design_input)); stage2=read_json(Path(args.guide_fact_base))
    print(json.dumps(deterministic_validate(candidate,stage1,stage2,sha256_file(Path(args.design_input)),sha256_file(Path(args.guide_fact_base))),ensure_ascii=False,indent=2))


def main() -> None:
    ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest="cmd",required=True)
    p=sub.add_parser("init"); p.add_argument("--run-dir",required=True); p.add_argument("--design-input",required=True); p.add_argument("--guide-fact-base",required=True); p.set_defaults(fn=init_cmd)
    p=sub.add_parser("ingest-upstream-repair"); p.add_argument("--run-dir",required=True); p.add_argument("--response-file",required=True); p.set_defaults(fn=ingest_upstream_repair_cmd)
    p=sub.add_parser("ingest-upstream-repair-critic"); p.add_argument("--run-dir",required=True); p.add_argument("--response-file",required=True); p.set_defaults(fn=ingest_upstream_repair_critic_cmd)
    p=sub.add_parser("confirm-upstream-repair"); p.add_argument("--run-dir",required=True); p.add_argument("--gate-response",required=True); p.set_defaults(fn=confirm_upstream_repair_cmd)
    p=sub.add_parser("ingest-generator"); p.add_argument("--run-dir",required=True); p.add_argument("--response-file",required=True); p.set_defaults(fn=ingest_generator_cmd)
    p=sub.add_parser("schedule-generator-repair"); p.add_argument("--run-dir",required=True); p.set_defaults(fn=schedule_generator_repair_cmd)
    p=sub.add_parser("ingest-generator-repair"); p.add_argument("--run-dir",required=True); p.add_argument("--response-file",required=True); p.set_defaults(fn=ingest_generator_repair_cmd)
    p=sub.add_parser("ingest-critic"); p.add_argument("--run-dir",required=True); p.add_argument("--response-file",required=True); p.set_defaults(fn=ingest_critic_cmd)
    p=sub.add_parser("finalize"); p.add_argument("--run-dir",required=True); p.add_argument("--gate-response",required=True); p.set_defaults(fn=finalize_cmd)
    p=sub.add_parser("validate"); p.add_argument("--candidate",required=True); p.add_argument("--design-input",required=True); p.add_argument("--guide-fact-base",required=True); p.set_defaults(fn=validate_cmd)
    args=ap.parse_args(); args.fn(args)


if __name__ == "__main__":
    main()
