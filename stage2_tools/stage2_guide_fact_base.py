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

MODEL_ID = "gpt-5.6-thinking"
ENDPOINT_ID = "chatgpt-conversation-file-bridge"
GENERATOR_CALL_KEY = "stage2-guide-fact-generator-001"
CRITIC_CALL_KEY = "stage2-guide-fact-critic-001"
GATE_ID = "stage2-guide-fact-confirmation-001"
STAGE = "STAGE_2_GUIDE_AND_FACT_BASE"


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
    return read_json(ROOT / "stage2_tools" / name)


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
    payload = {
        "schema_version": "1.0", "stage": STAGE, "status": status,
        "phase": phase, "updated_at": utc_now(), **kwargs,
    }
    atomic_json(run_dir / "LATEST_STATE.json", payload)
    append_event(run_dir, "STATE_CHANGED", status=status, phase=phase, details=kwargs)


def generator_request(design_input: dict[str, Any], design_path: Path) -> dict[str, Any]:
    source_hash = sha256_file(design_path)
    system_prompt = """你是科研申请书的规则与事实底座Agent。本阶段不写申请书正文，也不开展公开资料调研。你需要把已确认的设计输入拆解为可审计的规则表、原子事实账本、来源注册表、信息缺口清单和写作权限表。\n\n必须遵守：\n1. 官方申报指南没有提供，必须明确标记为NOT_PROVIDED；不得把通用写作习惯伪装成官方要求。\n2. 每条规则和事实都必须绑定来源ID；事实必须原子化，区分USER_ASSERTED、CONFIRMED_DESIGN、PROVISIONAL_TARGET、WORKING_ASSUMPTION和UNKNOWN；包含转折、并列因果或分号的来源描述必须拆成多条事实。\n3. 暂定指标只能以带限定语的方式使用；未知信息禁止写入正文。\n4. 不得补写申报单位、资助机构、团队名单、经费金额、项目周期及任何未提供的申报资格、模板格式、评审权重或截止日期。\n5. 建立规则、事实、来源和开放事项之间的显式ID关系，使后续更换模型端点后仍能确定性校验。\n6. 输出必须是单个JSON对象并严格满足Schema。"""
    task_prompt = """根据已确认的阶段1设计输入，生成阶段2规则与事实底座。\n\n最低要求：\n- 至少12条规则，覆盖篇幅、阶段边界、事实使用、模型接口、人工权限、记录留存和官方指南缺失处理；\n- 至少35条原子事实，覆盖课题名称、核心概念工作定义、问题陈述、当前差距、唯一中心命题、研究属性、成熟度目标、正文页数契约、研究问题、目标、场景、工作包、方法、指标、交付物和假设；\n- 明确哪些事实可直接陈述、哪些必须加限定语、哪些禁止使用；\n- 至少8项开放事项，其中包含官方指南、模板结构、申报资格、时间节点、申报单位、团队、经费和项目周期；\n- 当前只允许进入项目定义，不能据此冻结完整章节规划或正文。"""
    return {
        "schema_version": "1.0",
        "call_key": GENERATOR_CALL_KEY,
        "prompt_id": "P-STAGE2-GUIDE-FACT-BASE",
        "prompt_version": "1.0.0",
        "executor_role": "Guide and Fact Base Agent",
        "model_contract": {
            "model_independent": True, "response_format": "JSON",
            "actual_model_id_required": True, "endpoint_id_required": True,
        },
        "system_prompt": system_prompt,
        "task_prompt": task_prompt,
        "input_envelope": {
            "stage": STAGE,
            "stage_boundary": "GUIDE_AND_FACT_BASE_ONLY",
            "design_input_path": str(design_path),
            "design_input_sha256": source_hash,
            "design_input": design_input,
            "official_guide": {"status": "NOT_PROVIDED"},
            "trace_required": True,
        },
        "output_schema": schema("guide_fact_base.schema.json"),
        "requested_at": utc_now(),
    }


def deterministic_validate(candidate: dict[str, Any], expected_upstream_hash: str | None = None) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []

    def add(code: str, severity: str, message: str) -> None:
        findings.append({"code": code, "severity": severity, "message": message})

    schema_errors = validate_schema(candidate, schema("guide_fact_base.schema.json"))
    for error in schema_errors:
        add("SCHEMA_ERROR", "BLOCKING", error)
    if schema_errors:
        return {"verdict": "FAIL", "candidate_hash": sha256_json(candidate), "findings": findings}

    if expected_upstream_hash and candidate["upstream_artifact"]["sha256"] != expected_upstream_hash:
        add("UPSTREAM_HASH_MISMATCH", "BLOCKING", "候选绑定的阶段1哈希与实际输入文件不一致。")

    source_ids = [x["source_id"] for x in candidate["source_registry"]]
    if len(source_ids) != len(set(source_ids)):
        add("DUPLICATE_SOURCE_ID", "BLOCKING", "来源ID存在重复。")
    source_set = set(source_ids)
    official = [x for x in candidate["source_registry"] if x["source_type"] == "OFFICIAL_GUIDE"]
    if len(official) != 1 or official[0]["availability"] != "MISSING" or official[0]["content_hash"] is not None:
        add("OFFICIAL_GUIDE_STATE_INVALID", "BLOCKING", "官方指南必须有且仅有一个MISSING来源记录，且不得伪造内容哈希。")
    precedence = candidate["authority_policy"]["precedence"]
    expected_precedence = ["OFFICIAL_GUIDE", "USER_REQUIREMENT", "CONFIRMED_DESIGN_INPUT", "WORKING_ASSUMPTION"]
    if precedence != expected_precedence:
        add("AUTHORITY_PRECEDENCE_INVALID", "BLOCKING", "来源优先级必须按官方指南、用户要求、确认设计输入、工作假设排列。")

    rule_ids = [x["rule_id"] for x in candidate["rules"]]
    fact_ids = [x["fact_id"] for x in candidate["facts"]]
    open_ids = [x["item_id"] for x in candidate["open_items"]]
    for code, values, label in [
        ("DUPLICATE_RULE_ID", rule_ids, "规则"),
        ("DUPLICATE_FACT_ID", fact_ids, "事实"),
        ("DUPLICATE_OPEN_ITEM_ID", open_ids, "开放事项"),
    ]:
        if len(values) != len(set(values)):
            add(code, "BLOCKING", f"{label}ID存在重复。")
    rule_set, fact_set, open_set = set(rule_ids), set(fact_ids), set(open_ids)

    for rule in candidate["rules"]:
        missing_sources = set(rule["source_refs"]) - source_set
        if missing_sources:
            add("RULE_UNKNOWN_SOURCE", "BLOCKING", f"{rule['rule_id']}引用未知来源{sorted(missing_sources)}。")
        missing_open = set(rule["depends_on_open_item_ids"]) - open_set
        if missing_open:
            add("RULE_UNKNOWN_OPEN_ITEM", "BLOCKING", f"{rule['rule_id']}引用未知开放事项{sorted(missing_open)}。")
        if rule["status"] == "UNRESOLVED_GUIDE_RULE":
            if rule["modality"] != "UNKNOWN" or not rule["depends_on_open_item_ids"]:
                add("UNRESOLVED_RULE_OVERSTATED", "BLOCKING", f"{rule['rule_id']}是未决指南规则，却被当作确定要求。")
            if rule["blocking"]:
                add("UNRESOLVED_RULE_BLOCKING_NOW", "MAJOR", f"{rule['rule_id']}在当前阶段不应直接阻断项目定义。")

    for fact in candidate["facts"]:
        missing_sources = set(fact["source_refs"]) - source_set
        if missing_sources:
            add("FACT_UNKNOWN_SOURCE", "BLOCKING", f"{fact['fact_id']}引用未知来源{sorted(missing_sources)}。")
        if fact["knowledge_status"] == "UNKNOWN" and fact["assertion_policy"] != "PROHIBITED":
            add("UNKNOWN_FACT_NOT_PROHIBITED", "BLOCKING", f"{fact['fact_id']}为未知事实但未禁止使用。")
        if fact["knowledge_status"] in {"PROVISIONAL_TARGET", "WORKING_ASSUMPTION"}:
            if fact["assertion_policy"] != "QUALIFIED" or not fact["requires_qualification"]:
                add("QUALIFICATION_POLICY_INVALID", "BLOCKING", f"{fact['fact_id']}必须带限定语使用。")
        if fact["knowledge_status"] in {"USER_ASSERTED", "CONFIRMED_DESIGN"} and fact["assertion_policy"] == "PROHIBITED":
            add("CONFIRMED_FACT_PROHIBITED", "MAJOR", f"{fact['fact_id']}为确认事实但被禁止使用。")
        if any(mark in fact["statement"] for mark in ["；", ";"]):
            add("FACT_NOT_ATOMIC", "BLOCKING", f"{fact['fact_id']}包含分号，可能是复合事实。")

    direct = set(candidate["writing_permissions"]["direct_fact_ids"])
    qualified = set(candidate["writing_permissions"]["qualified_fact_ids"])
    prohibited = set(candidate["writing_permissions"]["prohibited_fact_ids"])
    provisional = set(candidate["writing_permissions"]["provisional_target_fact_ids"])
    if (direct | qualified | prohibited) != fact_set:
        add("WRITING_PERMISSION_INCOMPLETE", "BLOCKING", "写作权限表未覆盖全部事实或包含未知事实ID。")
    if direct & qualified or direct & prohibited or qualified & prohibited:
        add("WRITING_PERMISSION_OVERLAP", "BLOCKING", "同一事实被分配到多个互斥写作权限。")
    for fact in candidate["facts"]:
        expected_bucket = {
            "DIRECT": direct, "QUALIFIED": qualified, "PROHIBITED": prohibited,
        }[fact["assertion_policy"]]
        if fact["fact_id"] not in expected_bucket:
            add("WRITING_PERMISSION_MISMATCH", "BLOCKING", f"{fact['fact_id']}的权限字段与汇总表不一致。")
    expected_provisional = {x["fact_id"] for x in candidate["facts"] if x["knowledge_status"] == "PROVISIONAL_TARGET"}
    if provisional != expected_provisional:
        add("PROVISIONAL_TARGET_INDEX_MISMATCH", "BLOCKING", "暂定指标索引与事实账本不一致。")

    open_fields = {x["field"] for x in candidate["open_items"]}
    required_open_fields = {
        "官方申报指南", "申请书模板与必填结构", "申报资格与限制",
        "申报时间节点", "申报单位", "团队名单", "经费金额", "项目周期",
    }
    missing_open_fields = required_open_fields - open_fields
    if missing_open_fields:
        add("OPEN_ITEM_COVERAGE_INCOMPLETE", "BLOCKING", f"缺少开放事项：{sorted(missing_open_fields)}。")
    unknown_fields = set(candidate["writing_permissions"]["unknown_fields"])
    if unknown_fields - open_fields:
        add("UNKNOWN_FIELD_NOT_OPEN", "BLOCKING", "写作禁止字段没有对应开放事项。")
    if unknown_fields != open_fields:
        add("UNKNOWN_FIELD_INDEX_INCOMPLETE", "BLOCKING", "开放事项与写作禁止字段索引不完全一致。")
    unknown_open_links = {
        design_id
        for fact in candidate["facts"]
        if fact["knowledge_status"] == "UNKNOWN"
        for design_id in fact["related_design_ids"]
        if design_id.startswith("OPEN-")
    }
    if open_set - unknown_open_links:
        add("OPEN_ITEM_WITHOUT_UNKNOWN_FACT", "BLOCKING", f"开放事项缺少UNKNOWN事实映射：{sorted(open_set-unknown_open_links)}。")
    if unknown_open_links - open_set:
        add("UNKNOWN_FACT_WITHOUT_OPEN_ITEM", "BLOCKING", f"UNKNOWN事实引用不存在的开放事项：{sorted(unknown_open_links-open_set)}。")

    design_ids = set()
    for fact in candidate["facts"]:
        design_ids.update(fact["related_design_ids"])
    required_prefixes = ["RQ-", "OBJ-", "WP-", "M-", "MET-", "DEL-", "SC-", "ASM-"]
    for prefix in required_prefixes:
        if not any(x.startswith(prefix) for x in design_ids):
            add("DESIGN_COVERAGE_GAP", "BLOCKING", f"事实账本没有覆盖{prefix}类设计对象。")

    # 阶段3项目定义不能绕过事实账本直接读取阶段1正文式字段。
    # 因此核心概念、问题、差距、中心命题、研究属性、成熟度和页数契约必须各有DIRECT事实。
    required_project_definition_bindings = {
        "PD-CONCEPT": "核心概念工作定义",
        "PD-PROBLEM": "问题陈述",
        "PD-GAP": "当前差距定义",
        "CP-1": "中心命题",
        "PD-ATTRIBUTE": "研究属性",
        "PD-MATURITY": "成熟度目标",
        "DOC-TARGET-PAGES": "正文目标页数",
        "DOC-REFERENCE-PAGE": "参考文献页数规则",
    }
    direct_fact_ids = set(candidate["writing_permissions"]["direct_fact_ids"])
    project_definition_links = {
        link
        for fact in candidate["facts"]
        if fact["fact_id"] in direct_fact_ids and fact["assertion_policy"] == "DIRECT"
        for link in fact["related_design_ids"]
    }
    for binding_id, label in required_project_definition_bindings.items():
        if binding_id not in project_definition_links:
            add("PROJECT_DEFINITION_FACT_COVERAGE_GAP", "BLOCKING", f"事实账本缺少可直接引用的{label}绑定：{binding_id}。")

    if candidate["readiness"]["ready_for_section_planning"]:
        add("PREMATURE_SECTION_PLANNING", "BLOCKING", "缺少正式指南和关键组织信息时不得放行完整章节规划。")
    if not candidate["readiness"]["ready_for_project_definition"]:
        add("PROJECT_DEFINITION_NOT_READY", "BLOCKING", "已确认设计输入足以进入项目定义，不应错误阻断。")

    conflict_open = [x for x in candidate["conflicts"] if x["status"] == "OPEN"]
    if conflict_open:
        add("OPEN_CONFLICT", "MAJOR", f"存在{len(conflict_open)}项未解决冲突。")

    blocking = [x for x in findings if x["severity"] == "BLOCKING"]
    return {
        "verdict": "PASS" if not blocking else "FAIL",
        "candidate_hash": sha256_json(candidate),
        "statistics": {
            "sources": len(source_set), "rules": len(rule_set), "facts": len(fact_set),
            "open_items": len(open_set), "conflicts": len(candidate["conflicts"]),
            "direct_facts": len(direct), "qualified_facts": len(qualified),
            "prohibited_facts": len(prohibited), "provisional_targets": len(provisional),
        },
        "checked_dimensions": [
            "JSON_SCHEMA", "UPSTREAM_HASH", "SOURCE_AUTHORITY", "SOURCE_REFERENCE_INTEGRITY",
            "RULE_CERTAINTY", "FACT_ATOMICITY", "FACT_KNOWLEDGE_STATUS", "WRITING_PERMISSION_PARTITION",
            "PROVISIONAL_TARGET_INDEX", "OPEN_ITEM_COVERAGE", "DESIGN_OBJECT_COVERAGE",
            "PROJECT_DEFINITION_FACT_COVERAGE", "READINESS_BOUNDARY", "CONFLICT_REGISTER",
        ],
        "findings": findings,
    }


def init_cmd(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    design_path = Path(args.design_input).resolve()
    if run_dir.exists() and any(run_dir.iterdir()):
        raise SystemExit("run directory must be empty")
    run_dir.mkdir(parents=True, exist_ok=True)
    design = read_json(design_path)
    if design.get("stage") != "STAGE_1_DESIGN_INPUT":
        raise SystemExit("input is not accepted stage1 design input")
    for name in ["requests", "responses", "schemas", "intermediate", "quality", "human_gate", "outputs", "source_snapshots"]:
        (run_dir / name).mkdir(parents=True, exist_ok=True)
    for name in ["guide_fact_base.schema.json", "guide_fact_critic.schema.json"]:
        (run_dir / "schemas" / name).write_text((ROOT / "stage2_tools" / name).read_text(encoding="utf-8"), encoding="utf-8")
    snapshot = run_dir / "source_snapshots" / "stage1_design_input.json"
    snapshot.write_text(design_path.read_text(encoding="utf-8"), encoding="utf-8")
    metadata = {
        "schema_version": "1.0", "stage": STAGE, "project_title": design["project_title"],
        "created_at": utc_now(), "run_dir": str(run_dir), "model_bridge": "CHAT_FILE_BRIDGE",
        "stage_boundary": "GUIDE_AND_FACT_BASE_ONLY", "upstream_sha256": sha256_file(snapshot),
    }
    atomic_json(run_dir / "RUN_METADATA.json", metadata)
    req = generator_request(design, snapshot)
    atomic_json(run_dir / "requests" / "001_guide_fact_generator.json", req)
    append_event(run_dir, "RUN_INITIALIZED", upstream_sha256=metadata["upstream_sha256"])
    append_event(run_dir, "MODEL_REQUEST_CREATED", call_key=GENERATOR_CALL_KEY, prompt_id=req["prompt_id"])
    state(run_dir, "WAITING_MODEL", "GUIDE_FACT_GENERATOR")
    print(json.dumps({"status": "WAITING_MODEL", "request": str(run_dir / "requests" / "001_guide_fact_generator.json")}, ensure_ascii=False, indent=2))


def ingest_generator_cmd(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    envelope = read_json(Path(args.response_file).resolve())
    if envelope.get("call_key") != GENERATOR_CALL_KEY or envelope.get("prompt_id") != "P-STAGE2-GUIDE-FACT-BASE":
        raise SystemExit("generator response envelope does not match request")
    if not envelope.get("model_id") or not envelope.get("endpoint_id"):
        raise SystemExit("generator response missing actual model or endpoint id")
    candidate = envelope.get("output")
    expected_hash = read_json(run_dir / "RUN_METADATA.json")["upstream_sha256"]
    report = deterministic_validate(candidate, expected_hash)
    atomic_json(run_dir / "responses" / "001_guide_fact_generator.json", envelope)
    atomic_json(run_dir / "intermediate" / "guide_fact_candidate.json", candidate)
    atomic_json(run_dir / "quality" / "deterministic_guide_fact_report.json", report)
    append_event(run_dir, "MODEL_RESPONSE_INGESTED", call_key=GENERATOR_CALL_KEY, model_id=envelope["model_id"], endpoint_id=envelope["endpoint_id"], candidate_hash=report["candidate_hash"], verdict=report["verdict"])
    if report["verdict"] != "PASS":
        state(run_dir, "BLOCKED", "GUIDE_FACT_DETERMINISTIC_REVIEW", report_path="quality/deterministic_guide_fact_report.json")
        raise SystemExit(2)
    critic_req = {
        "schema_version": "1.0", "call_key": CRITIC_CALL_KEY,
        "prompt_id": "P-STAGE2-GUIDE-FACT-CRITIC", "prompt_version": "1.0.0",
        "executor_role": "Independent Guide and Fact Base Critic",
        "model_contract": {"independent_from_generator": True, "response_format": "JSON", "actual_model_id_required": True, "endpoint_id_required": True},
        "system_prompt": "你是独立的规则与事实底座Critic。本阶段不写正文。审查候选是否严格区分官方规则、用户要求、确认设计、暂定指标、工作假设和未知信息；检查每条规则与事实的来源绑定、原子性、写作权限、开放事项、冲突登记和阶段放行边界。官方指南未提供时，不得要求候选补造官方条款。若没有阻断或重大问题，返回ACCEPT。输出必须满足Schema。",
        "task_prompt": "独立审查阶段2候选。确定性报告已通过，但你要判断其是否足以安全支持下一阶段项目定义，并明确不应据此冻结预算、团队分工、正式时间表或最终模板。approved_candidate_hash必须等于候选规范JSON的SHA-256。",
        "input_envelope": {"candidate": candidate, "deterministic_report": report},
        "output_schema": schema("guide_fact_critic.schema.json"), "requested_at": utc_now(),
    }
    atomic_json(run_dir / "requests" / "002_guide_fact_critic.json", critic_req)
    append_event(run_dir, "MODEL_REQUEST_CREATED", call_key=CRITIC_CALL_KEY, prompt_id=critic_req["prompt_id"])
    state(run_dir, "WAITING_MODEL", "GUIDE_FACT_CRITIC")


def ingest_critic_cmd(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    envelope = read_json(Path(args.response_file).resolve())
    if envelope.get("call_key") != CRITIC_CALL_KEY or envelope.get("prompt_id") != "P-STAGE2-GUIDE-FACT-CRITIC":
        raise SystemExit("critic response envelope does not match request")
    if not envelope.get("model_id") or not envelope.get("endpoint_id"):
        raise SystemExit("critic response missing actual model or endpoint id")
    output = envelope.get("output")
    errors = validate_schema(output, schema("guide_fact_critic.schema.json"))
    if errors:
        raise SystemExit("critic schema errors: " + " | ".join(errors))
    candidate = read_json(run_dir / "intermediate" / "guide_fact_candidate.json")
    candidate_hash = sha256_json(candidate)
    if output["approved_candidate_hash"] != candidate_hash:
        raise SystemExit("critic approved_candidate_hash mismatch")
    atomic_json(run_dir / "responses" / "002_guide_fact_critic.json", envelope)
    atomic_json(run_dir / "quality" / "independent_critic_report.json", output)
    append_event(run_dir, "MODEL_RESPONSE_INGESTED", call_key=CRITIC_CALL_KEY, model_id=envelope["model_id"], endpoint_id=envelope["endpoint_id"], verdict=output["verdict"], candidate_hash=candidate_hash)
    if output["verdict"] != "ACCEPT" or any(x["severity"] in {"BLOCKING", "MAJOR"} for x in output["findings"]):
        state(run_dir, "BLOCKED", "GUIDE_FACT_CRITIC", report_path="quality/independent_critic_report.json")
        raise SystemExit(2)
    gate_req = {
        "schema_version": "1.0", "gate_id": GATE_ID,
        "gate_type": "GUIDE_AND_FACT_BASE_CONFIRMATION", "required_role": "PROJECT_OWNER",
        "allowed_actions": ["CONFIRM", "REVISE"], "context_hash": candidate_hash,
        "summary": {
            "project_title": candidate["project_title"],
            "source_count": len(candidate["source_registry"]), "rule_count": len(candidate["rules"]),
            "fact_count": len(candidate["facts"]), "open_item_count": len(candidate["open_items"]),
            "official_guide_status": candidate["authority_policy"]["official_guide_status"],
            "ready_for_project_definition": candidate["readiness"]["ready_for_project_definition"],
            "blocked_capabilities": candidate["readiness"]["blocked_capabilities"],
        },
        "requested_at": utc_now(),
    }
    atomic_json(run_dir / "human_gate" / "request.json", gate_req)
    append_event(run_dir, "HUMAN_GATE_REQUEST_CREATED", gate_id=GATE_ID, context_hash=candidate_hash)
    state(run_dir, "WAITING_HUMAN", "GUIDE_FACT_CONFIRMATION")


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


def write_outputs(candidate: dict[str, Any], output_dir: Path) -> None:
    atomic_json(output_dir / "stage2_guide_fact_base.json", candidate)
    (output_dir / "stage2_guide_fact_base.yaml").write_text(yaml.safe_dump(candidate, allow_unicode=True, sort_keys=False), encoding="utf-8")
    lines = [
        f"# {candidate['project_title']}：阶段2规则与事实底座", "",
        "## 权威边界", "", f"- 官方申报指南：{candidate['authority_policy']['official_guide_status']}",
        f"- 缺失指南处理：{candidate['authority_policy']['missing_guide_policy']}", "",
        "## 规则摘要", "",
    ]
    for rule in candidate["rules"]:
        lines.append(f"- **{rule['rule_id']} [{rule['modality']}]**：{rule['statement']}")
    lines += ["", "## 事实账本摘要", ""]
    for fact in candidate["facts"]:
        lines.append(f"- **{fact['fact_id']} [{fact['knowledge_status']}/{fact['assertion_policy']}]**：{fact['statement']}")
    lines += ["", "## 开放事项", ""]
    for item in candidate["open_items"]:
        lines.append(f"- **{item['item_id']} {item['field']}**：{item['reason']}；最迟在“{item['required_before_stage']}”前解决。")
    lines += ["", "## 阶段放行", "", candidate["readiness"]["rationale"], ""]
    (output_dir / "stage2_guide_fact_base.md").write_text("\n".join(lines), encoding="utf-8")

    write_csv(output_dir / "stage2_rule_table.csv", [
        {**x, "source_refs": "|".join(x["source_refs"]), "applies_to_stages": "|".join(x["applies_to_stages"]), "depends_on_open_item_ids": "|".join(x["depends_on_open_item_ids"])}
        for x in candidate["rules"]
    ], ["rule_id", "category", "modality", "statement", "status", "source_refs", "validation_method", "blocking", "applies_to_stages", "depends_on_open_item_ids"])
    write_csv(output_dir / "stage2_fact_ledger.csv", [
        {**x, "source_refs": "|".join(x["source_refs"]), "related_design_ids": "|".join(x["related_design_ids"])}
        for x in candidate["facts"]
    ], ["fact_id", "statement", "subject", "predicate", "object", "knowledge_status", "source_refs", "scope", "assertion_policy", "requires_qualification", "atomic", "related_design_ids"])
    write_csv(output_dir / "stage2_open_items.csv", candidate["open_items"], ["item_id", "field", "status", "reason", "required_before_stage", "blocking_now", "resolution_source"])
    source_rows = []
    for src in candidate["source_registry"]:
        source_rows.append({**src, "rule_ids": "|".join(x["rule_id"] for x in candidate["rules"] if src["source_id"] in x["source_refs"]), "fact_ids": "|".join(x["fact_id"] for x in candidate["facts"] if src["source_id"] in x["source_refs"])})
    write_csv(output_dir / "stage2_source_mapping.csv", source_rows, ["source_id", "source_type", "title", "availability", "authority", "content_hash", "locator", "rule_ids", "fact_ids"])


def finalize_cmd(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    gate = read_json(Path(args.gate_response).resolve())
    request = read_json(run_dir / "human_gate" / "request.json")
    if gate.get("gate_id") != GATE_ID or gate.get("context_hash") != request["context_hash"]:
        raise SystemExit("gate response does not match request")
    atomic_json(run_dir / "human_gate" / "response.json", gate)
    if gate.get("action") != "CONFIRM":
        state(run_dir, "BLOCKED", "GUIDE_FACT_CONFIRMATION", action=gate.get("action"))
        raise SystemExit(2)
    candidate = read_json(run_dir / "intermediate" / "guide_fact_candidate.json")
    expected_hash = read_json(run_dir / "RUN_METADATA.json")["upstream_sha256"]
    final_report = deterministic_validate(candidate, expected_hash)
    atomic_json(run_dir / "quality" / "final_revalidation.json", final_report)
    if final_report["verdict"] != "PASS":
        state(run_dir, "BLOCKED", "FINAL_REVALIDATION", report_path="quality/final_revalidation.json")
        raise SystemExit(2)
    output_dir = run_dir / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    write_outputs(candidate, output_dir)
    acceptance = {
        "schema_version": "1.0", "stage": STAGE, "result": "PASS",
        "candidate_hash": sha256_json(candidate), "upstream_sha256": expected_hash,
        "generator": {"model_id": read_json(run_dir / "responses" / "001_guide_fact_generator.json")["model_id"], "endpoint_id": read_json(run_dir / "responses" / "001_guide_fact_generator.json")["endpoint_id"]},
        "critic": {"model_id": read_json(run_dir / "responses" / "002_guide_fact_critic.json")["model_id"], "endpoint_id": read_json(run_dir / "responses" / "002_guide_fact_critic.json")["endpoint_id"], "verdict": "ACCEPT"},
        "human_gate": {"action": "CONFIRM", "decided_by": gate.get("decided_by"), "decided_role": gate.get("decided_role")},
        "statistics": final_report["statistics"],
        "official_guide_status": candidate["authority_policy"]["official_guide_status"],
        "next_stage": "STAGE_3_PROJECT_DEFINITION", "completed_at": utc_now(),
    }
    atomic_json(output_dir / "STAGE2_ACCEPTANCE_REPORT.json", acceptance)
    append_event(run_dir, "HUMAN_GATE_CONSUMED", gate_id=GATE_ID, action="CONFIRM")
    state(run_dir, "COMPLETED", "STAGE_2_COMPLETE", candidate_hash=acceptance["candidate_hash"], next_stage=acceptance["next_stage"])
    zip_path = package_trace(run_dir)
    print(json.dumps({"status": "COMPLETED", "run_dir": str(run_dir), "trace_zip": str(zip_path), "candidate_hash": acceptance["candidate_hash"]}, ensure_ascii=False, indent=2))


def build_manifest(run_dir: Path) -> None:
    files = []
    excluded = {"TRACE_MANIFEST.json", "TRACE_ARCHIVE.json"}
    for p in sorted(run_dir.rglob("*")):
        if p.is_file() and p.name not in excluded:
            files.append({"path": str(p.relative_to(run_dir)), "size_bytes": p.stat().st_size, "sha256": sha256_file(p)})
    atomic_json(run_dir / "TRACE_MANIFEST.json", {
        "schema_version": "1.0", "root": str(run_dir), "file_count": len(files), "files": files,
        "archive_policy": "TRACE_ARCHIVE.json is external to the archive hash manifest to avoid a self-reference cycle.",
        "generated_at": utc_now(),
    })


def package_trace(run_dir: Path) -> Path:
    build_manifest(run_dir)
    zip_path = run_dir.with_suffix(".zip")
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p in sorted(run_dir.rglob("*")):
            if p.is_file() and p.name != "TRACE_ARCHIVE.json":
                z.write(p, p.relative_to(run_dir.parent))
    atomic_json(run_dir / "TRACE_ARCHIVE.json", {"path": str(zip_path), "size_bytes": zip_path.stat().st_size, "sha256": sha256_file(zip_path), "created_at": utc_now()})
    return zip_path


def repack_cmd(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    candidate_path = run_dir / "outputs" / "stage2_guide_fact_base.json"
    if candidate_path.exists():
        expected_hash = read_json(run_dir / "RUN_METADATA.json")["upstream_sha256"]
        atomic_json(run_dir / "quality" / "final_revalidation.json", deterministic_validate(read_json(candidate_path), expected_hash))
    zip_path = package_trace(run_dir)
    print(json.dumps({"status": "REPACKED", "trace_zip": str(zip_path), "zip_sha256": sha256_file(zip_path)}, ensure_ascii=False, indent=2))


def validate_cmd(args: argparse.Namespace) -> None:
    expected_hash = args.expected_upstream_hash or None
    print(json.dumps(deterministic_validate(read_json(Path(args.candidate)), expected_hash), ensure_ascii=False, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init"); p.add_argument("--run-dir", required=True); p.add_argument("--design-input", required=True); p.set_defaults(fn=init_cmd)
    p = sub.add_parser("ingest-generator"); p.add_argument("--run-dir", required=True); p.add_argument("--response-file", required=True); p.set_defaults(fn=ingest_generator_cmd)
    p = sub.add_parser("ingest-critic"); p.add_argument("--run-dir", required=True); p.add_argument("--response-file", required=True); p.set_defaults(fn=ingest_critic_cmd)
    p = sub.add_parser("finalize"); p.add_argument("--run-dir", required=True); p.add_argument("--gate-response", required=True); p.set_defaults(fn=finalize_cmd)
    p = sub.add_parser("repack"); p.add_argument("--run-dir", required=True); p.set_defaults(fn=repack_cmd)
    p = sub.add_parser("validate"); p.add_argument("--candidate", required=True); p.add_argument("--expected-upstream-hash"); p.set_defaults(fn=validate_cmd)
    args = ap.parse_args(); args.fn(args)


if __name__ == "__main__":
    main()
