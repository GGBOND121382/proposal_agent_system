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

STAGE = "STAGE_4_ARGUMENT_ARCHITECTURE"
GENERATOR_CALL_KEY = "stage4-argument-architecture-generator-001"
REPAIR_CALL_KEY = "stage4-argument-architecture-repair-001"
CRITIC_CALL_KEY = "stage4-argument-architecture-critic-001"
GATE_ID = "stage4-argument-architecture-confirmation-001"
MODEL_ID = "gpt-5.6-thinking"
ENDPOINT_ID = "chatgpt-conversation-file-bridge"


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
    return read_json(ROOT / "stage4_tools" / name)


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


def make_generator_request(stage1: dict[str, Any], stage2: dict[str, Any], stage3: dict[str, Any], hashes: dict[str, str]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "call_key": GENERATOR_CALL_KEY,
        "prompt_id": "P-STAGE4-ARGUMENT-ARCHITECTURE",
        "prompt_version": "1.0.0",
        "executor_role": "Argument Architecture Agent",
        "model_contract": {
            "model_independent": True,
            "response_format": "JSON",
            "actual_model_id_required": True,
            "endpoint_id_required": True,
        },
        "system_prompt": (
            "你是科研项目论证架构Agent。当前阶段只构建论证图、研究设计矩阵、证据缺口和下一阶段放行判断，"
            "不生成申请书正文，也不决定正式章节结构。必须保持阶段3唯一中心命题、三项研究问题、目标和研究内容不漂移。"
            "最近工作和研究基础证据尚未提供时，必须创建UNKNOWN节点并绑定对应开放事项，不得虚构论文、项目、团队或实验成果。"
            "暂定指标必须保持PROVISIONAL_TARGET。允许论证架构本身完成，但若最近工作、研究基础、评审权重或正式模板仍缺失，"
            "ready_for_section_planning必须为false。输出必须严格满足Schema。"
        ),
        "task_prompt": (
            "为人机协同决策优势冲刺项目构建可审计论证架构。逐一闭合差距—研究问题—目标—研究内容—工作包—形式化模型—"
            "机制—基线—实验—指标—创新假设链，并建立最近工作—新增机制和研究基础—可行性两条证据链。"
            "对未知证据只建立占位节点和解决路径，不得把未知状态改成已支持。形成3行研究设计矩阵、至少3条完整论证链和证据缺口报告。"
        ),
        "input_envelope": {
            "stage1_design_input": stage1,
            "stage2_guide_fact_base": stage2,
            "stage3_project_definition": stage3,
            "upstream_hashes": hashes,
            "stage_boundary": "ARGUMENT_ARCHITECTURE_ONLY",
        },
        "output_schema": schema("argument_architecture.schema.json"),
        "requested_at": utc_now(),
    }


def make_critic_request(candidate: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "call_key": CRITIC_CALL_KEY,
        "prompt_id": "P-STAGE4-ARGUMENT-ARCHITECTURE-CRITIC",
        "prompt_version": "1.0.0",
        "executor_role": "Independent Argument Architecture Critic",
        "model_contract": {
            "independent_from_generator": True,
            "response_format": "JSON",
            "actual_model_id_required": True,
            "endpoint_id_required": True,
        },
        "system_prompt": (
            "你是独立论证架构Critic。逐节点、逐关系和逐研究问题检查中心命题、论证链、方法实质、实验、指标、"
            "创新基线和可行性证据。UNKNOWN节点可以保留，但不能被当作已支持证据。你可以接受一个结构正确、缺口诚实、"
            "但尚不允许章节规划的阶段4工件。必须检查全部节点以及七类核心关系链。"
        ),
        "task_prompt": (
            "审查候选是否保持阶段3冻结内容，是否每个研究问题均具有完整研究设计矩阵，是否将最近工作和研究基础缺口"
            "正确绑定到OPEN-013、OPEN-012与OPEN-009，以及readiness是否与证据缺口一致。"
        ),
        "input_envelope": {"candidate": candidate, "deterministic_report": report},
        "output_schema": schema("argument_architecture_critic.schema.json"),
        "requested_at": utc_now(),
    }


def deterministic_validate(candidate: dict[str, Any], stage1: dict[str, Any], stage2: dict[str, Any], stage3: dict[str, Any], hashes: dict[str, str]) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []

    def add(code: str, severity: str, message: str) -> None:
        findings.append({"code": code, "severity": severity, "message": message})

    errors = validate_schema(candidate, schema("argument_architecture.schema.json"))
    for err in errors:
        add("SCHEMA_ERROR", "BLOCKING", err)
    if errors:
        return {"verdict": "FAIL", "candidate_hash": sha256_json(candidate), "findings": findings}

    ups = {x["stage"]: x for x in candidate["upstream_artifacts"]}
    expected_hashes = {
        "STAGE_1_DESIGN_INPUT": hashes["stage1"],
        "STAGE_2_GUIDE_AND_FACT_BASE": hashes["stage2"],
        "STAGE_3_PROJECT_DEFINITION": hashes["stage3"],
    }
    for key, expected in expected_hashes.items():
        if ups.get(key, {}).get("sha256") != expected:
            add("UPSTREAM_HASH_MISMATCH", "BLOCKING", f"{key}哈希不一致。")

    if candidate["project_title"] != stage3["project_title"]:
        add("TITLE_CHANGED", "BLOCKING", "论证架构改变了项目题目。")
    dc = candidate["document_contract"]
    sdc = stage3["document_contract"]
    for key in ["document_type", "language", "body_page_limit", "target_body_pages", "references_outside_body_limit"]:
        if dc[key] != sdc[key]:
            add("DOCUMENT_CONTRACT_CHANGED", "BLOCKING", f"文种契约字段{key}发生变化。")

    cp = candidate["central_proposition"]
    if cp["statement"] != stage3["central_proposition"]["statement"]:
        add("CENTRAL_PROPOSITION_CHANGED", "BLOCKING", "中心命题文本必须与阶段3冻结结果一致。")
    expected_rqs = {x["rq_id"]: x for x in stage3["research_questions"]}
    actual_rqs = {x["node_id"]: x for x in candidate["research_questions"]}
    if set(actual_rqs) != set(expected_rqs):
        add("RQ_SET_CHANGED", "BLOCKING", "研究问题集合发生变化。")
    else:
        for rqid, src in expected_rqs.items():
            if actual_rqs[rqid]["question"] != src["question"]:
                add("RQ_TEXT_CHANGED", "BLOCKING", f"{rqid}问题文本发生变化。")

    nodes = candidate["nodes"]
    node_ids = [x["node_id"] for x in nodes]
    if len(node_ids) != len(set(node_ids)):
        add("DUPLICATE_NODE_ID", "BLOCKING", "论证节点ID重复。")
    node_map = {x["node_id"]: x for x in nodes}
    relation_ids = [x["relation_id"] for x in candidate["relations"]]
    if len(relation_ids) != len(set(relation_ids)):
        add("DUPLICATE_RELATION_ID", "BLOCKING", "关系ID重复。")
    all_ids = set(node_map) | {"CP-1"} | set(actual_rqs)
    for rel in candidate["relations"]:
        if rel["from_id"] not in all_ids or rel["to_id"] not in all_ids:
            add("UNKNOWN_RELATION_ENDPOINT", "BLOCKING", f"{rel['relation_id']}引用未知节点。")

    required_stage3_ids = (
        {x["gap_id"] for x in stage3["research_gaps"]}
        | {x["objective_id"] for x in stage3["objectives"]}
        | {x["content_id"] for x in stage3["research_contents"]}
        | {x["work_package_ids"][0] for x in stage3["research_contents"] if len(x["work_package_ids"]) == 1}
    )
    missing_core = sorted(x for x in required_stage3_ids if x not in node_map)
    if missing_core:
        add("CORE_NODE_MISSING", "BLOCKING", f"缺少冻结核心节点：{missing_core}")

    method_ids = {x["method_id"] for x in stage1["method_system"]}
    wp_ids = {x["wp_id"] for x in stage1["work_packages"]}
    metric_ids = {x["metric_id"] for x in stage1["evaluation_design"]["metrics"]}
    for expected, label in [(method_ids, "方法"), (wp_ids, "工作包"), (metric_ids, "指标")]:
        missing = sorted(expected - set(node_map))
        if missing:
            add("DESIGN_NODE_MISSING", "BLOCKING", f"缺少{label}节点：{missing}")

    matrix = candidate["research_design_matrix"]
    if {x["rq_id"] for x in matrix} != set(expected_rqs):
        add("MATRIX_COVERAGE", "BLOCKING", "研究设计矩阵未一一覆盖三项研究问题。")
    matrix_fields = [
        "gap_node_ids", "objective_node_ids", "content_node_ids", "work_package_node_ids", "formal_model_node_ids",
        "mechanism_node_ids", "baseline_node_ids", "experiment_node_ids", "metric_node_ids", "innovation_node_ids",
        "closest_prior_work_node_ids", "foundation_node_ids",
    ]
    for row in matrix:
        for field in matrix_fields:
            unknown = sorted(set(row[field]) - set(node_map))
            if unknown:
                add("MATRIX_UNKNOWN_NODE", "BLOCKING", f"{row['rq_id']}.{field}引用未知节点{unknown}")

    prior_nodes = [x for x in nodes if x["node_type"] == "CLOSEST_PRIOR_WORK"]
    if len(prior_nodes) < 3:
        add("PRIOR_WORK_NODE_INSUFFICIENT", "BLOCKING", "每项研究问题至少需要一个最近工作节点。")
    for node in prior_nodes:
        if node["status"] != "UNKNOWN" or "OPEN-013" not in node["blocked_by_open_item_ids"]:
            add("PRIOR_WORK_STATUS_INVALID", "BLOCKING", f"{node['node_id']}必须保持UNKNOWN并绑定OPEN-013。")
        if node["source_fact_ids"]:
            add("PRIOR_WORK_FALSE_SOURCE", "BLOCKING", f"{node['node_id']}不得绑定虚构事实来源。")

    foundation_nodes = [x for x in nodes if x["node_type"] == "TEAM_EVIDENCE"]
    if not foundation_nodes:
        add("FOUNDATION_NODE_MISSING", "BLOCKING", "缺少研究基础证据节点。")
    for node in foundation_nodes:
        if node["status"] not in {"UNKNOWN"}:
            add("FOUNDATION_FALSE_SUPPORT", "BLOCKING", f"{node['node_id']}在证据未提供时不得标记已支持。")
        if not set(node["blocked_by_open_item_ids"]) & {"OPEN-009", "OPEN-012"}:
            add("FOUNDATION_OPEN_ITEM_MISSING", "BLOCKING", f"{node['node_id']}未绑定团队或研究基础开放事项。")

    innovation_nodes = [x for x in nodes if x["node_type"] == "NOVEL_MECHANISM"]
    for node in innovation_nodes:
        if node["status"] != "TO_BE_VALIDATED" or "OPEN-013" not in node["blocked_by_open_item_ids"]:
            add("INNOVATION_PREMATURE", "BLOCKING", f"{node['node_id']}不得提前确认为创新。")

    for node in nodes:
        if node["node_type"] == "EVALUATION_METRIC" and node["status"] != "PROVISIONAL_TARGET":
            add("METRIC_STATUS_CHANGED", "BLOCKING", f"{node['node_id']}必须保持暂定指标状态。")

    gap_ids = {x["gap_id"] for x in candidate["evidence_gap_report"]}
    blocking_gap_ids = {x["gap_id"] for x in candidate["evidence_gap_report"] if x["blocking_for_section_planning"]}
    if set(candidate["readiness"]["blocking_gap_ids"]) != blocking_gap_ids:
        add("READINESS_GAP_MISMATCH", "BLOCKING", "readiness阻断缺口与证据缺口报告不一致。")
    if candidate["readiness"]["ready_for_section_planning"] and blocking_gap_ids:
        add("FALSE_READY", "BLOCKING", "存在阻断性证据缺口却允许章节规划。")
    if not candidate["readiness"]["architecture_complete"]:
        add("ARCHITECTURE_NOT_COMPLETE", "BLOCKING", "阶段4候选未声明论证架构完成。")

    required_open = {x["item_id"] for x in stage3["open_items_inherited"]}
    actual_open = {x["item_id"] for x in candidate["open_items_inherited"]}
    if actual_open != required_open:
        add("OPEN_ITEMS_CHANGED", "BLOCKING", "继承开放事项集合发生变化。")

    # Ensure seven semantic relation classes exist.
    relation_types = {x["relation"] for x in candidate["relations"]}
    required_rel = {"MOTIVATES", "ADDRESSES", "REALIZED_BY", "FORMALIZED_BY", "VERIFIED_BY", "BASELINE_FOR", "SUPPORTS_FEASIBILITY"}
    missing_rel = sorted(required_rel - relation_types)
    if missing_rel:
        add("ARGUMENT_CHAIN_TYPE_MISSING", "BLOCKING", f"缺少核心关系类型：{missing_rel}")

    blocking = [x for x in findings if x["severity"] == "BLOCKING"]
    return {
        "verdict": "PASS" if not blocking else "FAIL",
        "candidate_hash": sha256_json(candidate),
        "statistics": {
            "nodes": len(nodes),
            "relations": len(candidate["relations"]),
            "research_questions": len(actual_rqs),
            "matrix_rows": len(matrix),
            "argument_chains": len(candidate["argument_chains"]),
            "evidence_gaps": len(gap_ids),
            "blocking_gaps": len(blocking_gap_ids),
        },
        "checked_dimensions": [
            "JSON_SCHEMA", "UPSTREAM_HASH", "DOCUMENT_CONTRACT", "CENTRAL_PROPOSITION", "RESEARCH_QUESTION_FREEZE",
            "NODE_IDENTITY", "RELATION_REFERENTIAL_INTEGRITY", "CORE_NODE_COVERAGE", "RESEARCH_DESIGN_MATRIX",
            "PRIOR_WORK_BOUNDARY", "FOUNDATION_BOUNDARY", "INNOVATION_BOUNDARY", "METRIC_STATUS",
            "EVIDENCE_GAP_READINESS", "OPEN_ITEM_INHERITANCE", "SEVEN_CHAIN_TYPES",
        ],
        "findings": findings,
    }


def init_cmd(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    inputs = {
        "stage1": Path(args.design_input).resolve(),
        "stage2": Path(args.guide_fact_base).resolve(),
        "stage3": Path(args.project_definition).resolve(),
    }
    if run_dir.exists() and any(run_dir.iterdir()):
        raise SystemExit("run directory must be empty")
    for name in ["requests", "responses", "schemas", "intermediate", "quality", "human_gate", "outputs", "source_snapshots"]:
        (run_dir / name).mkdir(parents=True, exist_ok=True)
    data = {k: read_json(v) for k, v in inputs.items()}
    if data["stage1"].get("stage") != "STAGE_1_DESIGN_INPUT" or data["stage2"].get("stage") != "STAGE_2_GUIDE_AND_FACT_BASE" or data["stage3"].get("stage") != "STAGE_3_PROJECT_DEFINITION":
        raise SystemExit("invalid upstream stage")
    snapshot_names = {
        "stage1": "stage1_design_input.json",
        "stage2": "stage2_guide_fact_base.json",
        "stage3": "stage3_project_definition.json",
    }
    snapshots: dict[str, Path] = {}
    for k, name in snapshot_names.items():
        p = run_dir / "source_snapshots" / name
        p.write_text(inputs[k].read_text(encoding="utf-8"), encoding="utf-8")
        snapshots[k] = p
    for name in ["argument_architecture.schema.json", "argument_architecture_critic.schema.json"]:
        (run_dir / "schemas" / name).write_text((ROOT / "stage4_tools" / name).read_text(encoding="utf-8"), encoding="utf-8")
    hashes = {k: sha256_file(v) for k, v in snapshots.items()}
    meta = {
        "schema_version": "1.0", "stage": STAGE, "project_title": data["stage3"]["project_title"],
        "created_at": utc_now(), "run_dir": str(run_dir), "stage_boundary": "ARGUMENT_ARCHITECTURE_ONLY",
        "model_bridge": "CHAT_FILE_BRIDGE", "upstream_hashes": hashes,
    }
    atomic_json(run_dir / "RUN_METADATA.json", meta)
    request = make_generator_request(data["stage1"], data["stage2"], data["stage3"], hashes)
    atomic_json(run_dir / "requests" / "001_argument_architecture_generator.json", request)
    append_event(run_dir, "RUN_INITIALIZED", upstream_hashes=hashes)
    append_event(run_dir, "MODEL_REQUEST_CREATED", call_key=GENERATOR_CALL_KEY, prompt_id=request["prompt_id"])
    state(run_dir, "WAITING_MODEL", "ARGUMENT_ARCHITECTURE_GENERATOR")
    print(json.dumps({"status": "WAITING_MODEL", "request": str(run_dir / "requests" / "001_argument_architecture_generator.json")}, ensure_ascii=False, indent=2))



def _active_candidate_path(run_dir: Path) -> Path:
    repaired = run_dir / "intermediate" / "argument_architecture_candidate_repaired.json"
    return repaired if repaired.exists() else run_dir / "intermediate" / "argument_architecture_candidate.json"


def _critic_index(run_dir: Path) -> str:
    return "003" if (run_dir / "responses" / "002_argument_architecture_repair.json").exists() else "002"


def _create_critic_request(run_dir: Path, candidate: dict[str, Any], report: dict[str, Any], index: str) -> None:
    req = make_critic_request(candidate, report)
    atomic_json(run_dir / "requests" / f"{index}_argument_architecture_critic.json", req)
    append_event(run_dir, "MODEL_REQUEST_CREATED", call_key=CRITIC_CALL_KEY, prompt_id=req["prompt_id"])
    state(run_dir, "WAITING_MODEL", "ARGUMENT_ARCHITECTURE_CRITIC")


def schedule_repair_cmd(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    if (run_dir / "responses" / "002_argument_architecture_repair.json").exists() or (run_dir / "requests" / "002_argument_architecture_repair.json").exists():
        raise SystemExit("stage4 generator repair already used")
    candidate = read_json(_active_candidate_path(run_dir))
    report = read_json(run_dir / "quality" / "deterministic_argument_architecture_report.json")
    if report.get("verdict") == "PASS":
        raise SystemExit("repair is not needed")
    req = {
        "schema_version": "1.0",
        "call_key": REPAIR_CALL_KEY,
        "prompt_id": "P-STAGE4-ARGUMENT-ARCHITECTURE-REPAIR",
        "prompt_version": "1.0.0",
        "executor_role": "Argument Architecture Repair Agent",
        "model_contract": {"model_independent": True, "response_format": "JSON", "actual_model_id_required": True, "endpoint_id_required": True},
        "system_prompt": (
            "你是阶段4论证架构定向修复Agent。只能修复确定性报告指出的字段，不得改变阶段3中心命题、研究问题、目标、研究内容、"
            "证据状态或开放事项。必须返回完整阶段4候选JSON。若缺少冻结方法节点，应补充对应节点、必要关系和研究设计矩阵引用；"
            "不得借修复机会补造最近工作、团队基础或实测指标。"
        ),
        "task_prompt": "根据Finding做最小修改，并保持其余候选内容语义和ID稳定。",
        "input_envelope": {"original_candidate": candidate, "deterministic_report": report},
        "output_schema": schema("argument_architecture.schema.json"),
        "requested_at": utc_now(),
    }
    atomic_json(run_dir / "requests" / "002_argument_architecture_repair.json", req)
    append_event(run_dir, "MODEL_REQUEST_CREATED", call_key=REPAIR_CALL_KEY, prompt_id=req["prompt_id"])
    state(run_dir, "WAITING_MODEL", "ARGUMENT_ARCHITECTURE_REPAIR")


def ingest_repair_cmd(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    env = read_json(Path(args.response_file).resolve())
    if env.get("call_key") != REPAIR_CALL_KEY or env.get("prompt_id") != "P-STAGE4-ARGUMENT-ARCHITECTURE-REPAIR":
        raise SystemExit("repair response mismatch")
    if not env.get("model_id") or not env.get("endpoint_id"):
        raise SystemExit("missing actual model or endpoint")
    candidate = env.get("output")
    s1 = read_json(run_dir / "source_snapshots" / "stage1_design_input.json")
    s2 = read_json(run_dir / "source_snapshots" / "stage2_guide_fact_base.json")
    s3 = read_json(run_dir / "source_snapshots" / "stage3_project_definition.json")
    meta = read_json(run_dir / "RUN_METADATA.json")
    report = deterministic_validate(candidate, s1, s2, s3, meta["upstream_hashes"])
    atomic_json(run_dir / "responses" / "002_argument_architecture_repair.json", env)
    atomic_json(run_dir / "intermediate" / "argument_architecture_candidate_repaired.json", candidate)
    atomic_json(run_dir / "quality" / "deterministic_argument_architecture_repair_report.json", report)
    append_event(run_dir, "MODEL_RESPONSE_INGESTED", call_key=REPAIR_CALL_KEY, model_id=env["model_id"], endpoint_id=env["endpoint_id"], verdict=report["verdict"], candidate_hash=report["candidate_hash"])
    if report["verdict"] != "PASS":
        state(run_dir, "BLOCKED", "ARGUMENT_ARCHITECTURE_REPAIR_REVIEW")
        raise SystemExit(2)
    _create_critic_request(run_dir, candidate, report, "003")


def ingest_generator_cmd(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    env = read_json(Path(args.response_file).resolve())
    if env.get("call_key") != GENERATOR_CALL_KEY or env.get("prompt_id") != "P-STAGE4-ARGUMENT-ARCHITECTURE":
        raise SystemExit("generator response mismatch")
    if not env.get("model_id") or not env.get("endpoint_id"):
        raise SystemExit("missing actual model or endpoint")
    candidate = env.get("output")
    s1 = read_json(run_dir / "source_snapshots" / "stage1_design_input.json")
    s2 = read_json(run_dir / "source_snapshots" / "stage2_guide_fact_base.json")
    s3 = read_json(run_dir / "source_snapshots" / "stage3_project_definition.json")
    meta = read_json(run_dir / "RUN_METADATA.json")
    report = deterministic_validate(candidate, s1, s2, s3, meta["upstream_hashes"])
    atomic_json(run_dir / "responses" / "001_argument_architecture_generator.json", env)
    atomic_json(run_dir / "intermediate" / "argument_architecture_candidate.json", candidate)
    atomic_json(run_dir / "quality" / "deterministic_argument_architecture_report.json", report)
    append_event(run_dir, "MODEL_RESPONSE_INGESTED", call_key=GENERATOR_CALL_KEY, model_id=env["model_id"], endpoint_id=env["endpoint_id"], verdict=report["verdict"], candidate_hash=report["candidate_hash"])
    if report["verdict"] != "PASS":
        state(run_dir, "BLOCKED", "ARGUMENT_ARCHITECTURE_DETERMINISTIC_REVIEW")
        raise SystemExit(2)
    _create_critic_request(run_dir, candidate, report, "002")


def ingest_critic_cmd(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    env = read_json(Path(args.response_file).resolve())
    if env.get("call_key") != CRITIC_CALL_KEY or env.get("prompt_id") != "P-STAGE4-ARGUMENT-ARCHITECTURE-CRITIC":
        raise SystemExit("critic response mismatch")
    if not env.get("model_id") or not env.get("endpoint_id"):
        raise SystemExit("missing actual model or endpoint")
    output = env.get("output")
    errors = validate_schema(output, schema("argument_architecture_critic.schema.json"))
    if errors:
        raise SystemExit("critic schema errors: " + " | ".join(errors))
    candidate = read_json(_active_candidate_path(run_dir))
    ch = sha256_json(candidate)
    if output["approved_candidate_hash"] != ch:
        raise SystemExit("approved candidate hash mismatch")
    expected_checked = set(x["node_id"] for x in candidate["nodes"]) | {"CP-1"} | {x["node_id"] for x in candidate["research_questions"]}
    if set(output["checked_node_ids"]) != expected_checked:
        raise SystemExit("critic did not check all nodes")
    required_chains = {"GAP_TO_RQ", "RQ_TO_OBJECTIVE", "OBJECTIVE_TO_CONTENT", "CONTENT_TO_METHOD", "METHOD_TO_EXPERIMENT", "PRIOR_WORK_TO_INNOVATION", "FOUNDATION_TO_FEASIBILITY"}
    if {x["chain_type"] for x in output["chain_checks"]} != required_chains:
        raise SystemExit("critic chain coverage incomplete")
    required_dims = {"CENTRAL_THESIS", "ARGUMENT_CHAIN", "EVIDENCE_SUPPORT", "METHOD_SUBSTANCE", "INNOVATION_BASELINE", "FEASIBILITY_FOUNDATION", "METRIC_JUSTIFICATION"}
    dims = {x["dimension"]: x for x in output["quality_dimensions"]}
    if set(dims) != required_dims:
        raise SystemExit("critic quality dimension coverage incomplete")
    if output["verdict"] != "ACCEPT" or any(x["severity"] in {"BLOCKING", "MAJOR"} for x in output["findings"]):
        atomic_json(run_dir / "responses" / f"{_critic_index(run_dir)}_argument_architecture_critic.json", env)
        atomic_json(run_dir / "quality" / "independent_critic_report.json", output)
        state(run_dir, "BLOCKED", "ARGUMENT_ARCHITECTURE_CRITIC")
        raise SystemExit(2)
    if candidate["readiness"]["ready_for_section_planning"] and output["next_stage_decision"] != "ALLOW_SECTION_PLANNING":
        raise SystemExit("critic next-stage decision inconsistent")
    if not candidate["readiness"]["ready_for_section_planning"] and output["next_stage_decision"] != "HOLD_SECTION_PLANNING":
        raise SystemExit("critic next-stage decision inconsistent")
    atomic_json(run_dir / "responses" / f"{_critic_index(run_dir)}_argument_architecture_critic.json", env)
    atomic_json(run_dir / "quality" / "independent_critic_report.json", output)
    append_event(run_dir, "MODEL_RESPONSE_INGESTED", call_key=CRITIC_CALL_KEY, model_id=env["model_id"], endpoint_id=env["endpoint_id"], verdict=output["verdict"], candidate_hash=ch)
    gate = {
        "schema_version": "1.0", "gate_id": GATE_ID, "gate_type": "ARGUMENT_ARCHITECTURE_CONFIRMATION",
        "required_role": "PROJECT_OWNER", "allowed_actions": ["CONFIRM", "REVISE"], "context_hash": ch,
        "summary": {
            "central_proposition": candidate["central_proposition"]["statement"],
            "research_questions": [x["question"] for x in candidate["research_questions"]],
            "architecture_complete": candidate["readiness"]["architecture_complete"],
            "ready_for_section_planning": candidate["readiness"]["ready_for_section_planning"],
            "blocking_gap_ids": candidate["readiness"]["blocking_gap_ids"],
            "frozen_elements": candidate["readiness"]["frozen_elements"],
            "non_frozen_elements": candidate["readiness"]["non_frozen_elements"],
        },
        "requested_at": utc_now(),
    }
    atomic_json(run_dir / "human_gate" / "argument_architecture_request.json", gate)
    append_event(run_dir, "HUMAN_GATE_REQUEST_CREATED", gate_id=GATE_ID, context_hash=ch)
    state(run_dir, "WAITING_HUMAN", "ARGUMENT_ARCHITECTURE_CONFIRMATION")


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fields})


def write_outputs(candidate: dict[str, Any], out: Path) -> None:
    atomic_json(out / "stage4_argument_architecture.json", candidate)
    (out / "stage4_argument_architecture.yaml").write_text(yaml.safe_dump(candidate, allow_unicode=True, sort_keys=False), encoding="utf-8")
    lines = [f"# {candidate['project_title']}：阶段4论证架构", "", "## 中心命题", "", candidate["central_proposition"]["statement"], "", "## 研究问题与论证闭环"]
    matrix = {x["rq_id"]: x for x in candidate["research_design_matrix"]}
    for rq in candidate["research_questions"]:
        row = matrix[rq["node_id"]]
        lines += ["", f"### {rq['node_id']} {rq['question']}", "", f"- 差距：{', '.join(row['gap_node_ids'])}", f"- 目标：{', '.join(row['objective_node_ids'])}", f"- 研究内容：{', '.join(row['content_node_ids'])}", f"- 形式化模型：{', '.join(row['formal_model_node_ids'])}", f"- 机制：{', '.join(row['mechanism_node_ids'])}", f"- 基线：{', '.join(row['baseline_node_ids'])}", f"- 实验：{', '.join(row['experiment_node_ids'])}", f"- 指标：{', '.join(row['metric_node_ids'])}", f"- 创新假设：{', '.join(row['innovation_node_ids'])}", f"- 比较与反证规则：{row['falsification_or_comparison_rule']}"]
    lines += ["", "## 证据缺口"]
    for x in candidate["evidence_gap_report"]:
        lines.append(f"- **{x['gap_id']}**：{x['reason']}；解决方式：{x['resolution']}；阻断章节规划：{x['blocking_for_section_planning']}")
    lines += ["", "## 阶段放行", "", candidate["readiness"]["rationale"], ""]
    (out / "stage4_argument_architecture.md").write_text("\n".join(lines), encoding="utf-8")
    write_csv(out / "stage4_nodes.csv", candidate["nodes"], ["node_id", "node_type", "label", "statement", "status", "source_stage3_ids", "source_fact_ids", "blocked_by_open_item_ids"])
    write_csv(out / "stage4_relations.csv", candidate["relations"], ["relation_id", "from_id", "relation", "to_id", "rationale"])
    rows = []
    for x in candidate["research_design_matrix"]:
        row = {k: "|".join(v) if isinstance(v, list) else v for k, v in x.items()}
        rows.append(row)
    write_csv(out / "stage4_research_design_matrix.csv", rows, list(candidate["research_design_matrix"][0].keys()))
    write_csv(out / "stage4_evidence_gaps.csv", [{**x, "affected_node_ids": "|".join(x["affected_node_ids"]), "open_item_ids": "|".join(x["open_item_ids"])} for x in candidate["evidence_gap_report"]], ["gap_id", "required_node_type", "affected_node_ids", "open_item_ids", "reason", "blocking_for_section_planning", "resolution"])


def finalize_cmd(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    decision = read_json(Path(args.gate_response).resolve())
    req = read_json(run_dir / "human_gate" / "argument_architecture_request.json")
    if decision.get("gate_id") != GATE_ID or decision.get("context_hash") != req["context_hash"]:
        raise SystemExit("gate mismatch")
    atomic_json(run_dir / "human_gate" / "argument_architecture_response.json", decision)
    if decision.get("action") != "CONFIRM":
        state(run_dir, "BLOCKED", "ARGUMENT_ARCHITECTURE_CONFIRMATION")
        raise SystemExit(2)
    candidate = read_json(_active_candidate_path(run_dir))
    s1 = read_json(run_dir / "source_snapshots" / "stage1_design_input.json")
    s2 = read_json(run_dir / "source_snapshots" / "stage2_guide_fact_base.json")
    s3 = read_json(run_dir / "source_snapshots" / "stage3_project_definition.json")
    meta = read_json(run_dir / "RUN_METADATA.json")
    report = deterministic_validate(candidate, s1, s2, s3, meta["upstream_hashes"])
    atomic_json(run_dir / "quality" / "final_revalidation.json", report)
    if report["verdict"] != "PASS":
        state(run_dir, "BLOCKED", "FINAL_REVALIDATION")
        raise SystemExit(2)
    out = run_dir / "outputs"
    write_outputs(candidate, out)
    critic = read_json(run_dir / "responses" / f"{_critic_index(run_dir)}_argument_architecture_critic.json")
    acceptance = {
        "schema_version": "1.0", "stage": STAGE, "result": "PASS", "candidate_hash": sha256_json(candidate),
        "upstream_hashes": meta["upstream_hashes"],
        "generator": {"model_id": read_json(run_dir / "responses" / "001_argument_architecture_generator.json")["model_id"], "endpoint_id": read_json(run_dir / "responses" / "001_argument_architecture_generator.json")["endpoint_id"], "repair_used": (run_dir / "responses" / "002_argument_architecture_repair.json").exists()},
        "critic": {"model_id": critic["model_id"], "endpoint_id": critic["endpoint_id"], "verdict": critic["output"]["verdict"]},
        "human_gate": {"action": "CONFIRM", "decided_by": decision.get("decided_by"), "decided_role": decision.get("decided_role")},
        "statistics": report["statistics"],
        "architecture_complete": candidate["readiness"]["architecture_complete"],
        "ready_for_section_planning": candidate["readiness"]["ready_for_section_planning"],
        "next_stage": "STAGE_4_EVIDENCE_COMPLETION" if not candidate["readiness"]["ready_for_section_planning"] else "STAGE_5_SECTION_PLANNING",
        "completed_at": utc_now(),
    }
    atomic_json(out / "STAGE4_ACCEPTANCE_REPORT.json", acceptance)
    append_event(run_dir, "HUMAN_GATE_CONSUMED", gate_id=GATE_ID, action="CONFIRM")
    state(run_dir, "COMPLETED", "STAGE_4_COMPLETE", candidate_hash=acceptance["candidate_hash"], next_stage=acceptance["next_stage"])
    z = package_trace(run_dir)
    print(json.dumps({"status": "COMPLETED", "run_dir": str(run_dir), "trace_zip": str(z), "candidate_hash": acceptance["candidate_hash"], "next_stage": acceptance["next_stage"]}, ensure_ascii=False, indent=2))


def build_manifest(run_dir: Path) -> None:
    excluded = {"TRACE_MANIFEST.json", "TRACE_ARCHIVE.json"}
    files = []
    for p in sorted(run_dir.rglob("*")):
        if p.is_file() and p.name not in excluded:
            files.append({"path": str(p.relative_to(run_dir)), "size_bytes": p.stat().st_size, "sha256": sha256_file(p)})
    atomic_json(run_dir / "TRACE_MANIFEST.json", {"schema_version": "1.0", "root": str(run_dir), "file_count": len(files), "files": files, "archive_policy": "TRACE_ARCHIVE.json is external to archive hash manifest.", "generated_at": utc_now()})


def package_trace(run_dir: Path) -> Path:
    build_manifest(run_dir)
    zpath = run_dir.with_suffix(".zip")
    if zpath.exists():
        zpath.unlink()
    with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p in sorted(run_dir.rglob("*")):
            if p.is_file() and p.name != "TRACE_ARCHIVE.json":
                z.write(p, p.relative_to(run_dir.parent))
    atomic_json(run_dir / "TRACE_ARCHIVE.json", {"path": str(zpath), "size_bytes": zpath.stat().st_size, "sha256": sha256_file(zpath), "created_at": utc_now()})
    return zpath


def validate_cmd(args: argparse.Namespace) -> None:
    paths = {"stage1": Path(args.design_input), "stage2": Path(args.guide_fact_base), "stage3": Path(args.project_definition)}
    data = {k: read_json(v) for k, v in paths.items()}
    hashes = {k: sha256_file(v) for k, v in paths.items()}
    candidate = read_json(Path(args.candidate))
    print(json.dumps(deterministic_validate(candidate, data["stage1"], data["stage2"], data["stage3"], hashes), ensure_ascii=False, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("--run-dir", required=True); p.add_argument("--design-input", required=True); p.add_argument("--guide-fact-base", required=True); p.add_argument("--project-definition", required=True); p.set_defaults(fn=init_cmd)
    p = sub.add_parser("ingest-generator")
    p.add_argument("--run-dir", required=True); p.add_argument("--response-file", required=True); p.set_defaults(fn=ingest_generator_cmd)
    p = sub.add_parser("schedule-repair")
    p.add_argument("--run-dir", required=True); p.set_defaults(fn=schedule_repair_cmd)
    p = sub.add_parser("ingest-repair")
    p.add_argument("--run-dir", required=True); p.add_argument("--response-file", required=True); p.set_defaults(fn=ingest_repair_cmd)
    p = sub.add_parser("ingest-critic")
    p.add_argument("--run-dir", required=True); p.add_argument("--response-file", required=True); p.set_defaults(fn=ingest_critic_cmd)
    p = sub.add_parser("finalize")
    p.add_argument("--run-dir", required=True); p.add_argument("--gate-response", required=True); p.set_defaults(fn=finalize_cmd)
    p = sub.add_parser("validate")
    p.add_argument("--candidate", required=True); p.add_argument("--design-input", required=True); p.add_argument("--guide-fact-base", required=True); p.add_argument("--project-definition", required=True); p.set_defaults(fn=validate_cmd)
    args = ap.parse_args(); args.fn(args)


if __name__ == "__main__":
    main()
