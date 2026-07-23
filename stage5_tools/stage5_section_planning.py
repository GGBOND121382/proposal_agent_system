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

STAGE = "STAGE_5_PROVISIONAL_SECTION_PLANNING"
GENERATOR_CALL_KEY = "stage5-section-plan-generator-001"
REPAIR_CALL_KEY = "stage5-section-plan-repair-001"
CRITIC_CALL_KEY = "stage5-section-plan-critic-001"
GATE_ID = "stage5-section-plan-confirmation-001"


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


def load_schema(name: str) -> dict[str, Any]:
    return read_json(ROOT / "stage5_tools" / name)


def validate_schema(value: Any, schema_value: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for err in sorted(Draft202012Validator(schema_value).iter_errors(value), key=lambda x: list(x.path)):
        loc = "/".join(str(x) for x in err.path) or "$"
        errors.append(f"{loc}: {err.message}")
    return errors


def append_event(run_dir: Path, event_type: str, **details: Any) -> None:
    path = run_dir / "events.jsonl"
    idx = 1
    if path.exists():
        idx = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip()) + 1
    rec = {"index": idx, "recorded_at": utc_now(), "event_type": event_type, **details}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")


def set_state(run_dir: Path, status: str, phase: str, **details: Any) -> None:
    payload = {
        "schema_version": "1.0", "stage": STAGE, "status": status,
        "phase": phase, "updated_at": utc_now(), **details,
    }
    atomic_json(run_dir / "LATEST_STATE.json", payload)
    append_event(run_dir, "STATE_CHANGED", status=status, phase=phase, details=details)


def make_generator_request(inputs: dict[str, Any], hashes: dict[str, str]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "call_key": GENERATOR_CALL_KEY,
        "prompt_id": "P-STAGE5-PROVISIONAL-SECTION-PLANNING",
        "prompt_version": "1.0.0",
        "executor_role": "Provisional Section Planning Agent",
        "model_contract": {
            "model_independent": True,
            "response_format": "JSON",
            "actual_model_id_required": True,
            "endpoint_id_required": True,
            "original_response_immutable": True,
        },
        "system_prompt": (
            "你是阶段5可逆章节规划Agent。只能规划章节、页数、章节生成合同、图表和分批写作顺序，不得生成正文。"
            "必须严格继承阶段1的14个一级章节名称、顺序和页数预算，正文计划页数控制在14至18页，硬上限20页。"
            "规划必须覆盖阶段4的中心命题、三个研究问题、研究内容、方法、实验、指标、创新假设与研究基础，并使用阶段4A的公开来源和证据边界。"
            "正式指南和模板尚未提供，因此所有章节合同均为可逆合同，收到指南、模板、团队证明或场景基线后必须重新校验。"
            "不得把暂定指标写成实测结果，不得把用户陈述或内部Trace写成正式验收成果，不得宣称绝对首创。"
            "创新章必须同时绑定最近工作和新增机制；结论章必须逐一回答RQ-1至RQ-3、回扣中心命题和三项创新假设。"
        ),
        "task_prompt": (
            "生成14章可逆内容计划。对每章给出职责、必须回答的问题、必须包含和禁止声称的内容、引用的论证节点/来源/指标、"
            "子节和段落角色、目标页数与最大页数、预计字数及图表。制定4个正文批次STAGE-6A至STAGE-6D，确保依赖顺序清晰。"
            "给出跨章闭环控制和重新验证触发条件。只允许放行STAGE_6A_PROVISIONAL_DRAFTING，不允许放行最终提交。"
        ),
        "input_envelope": {
            **inputs,
            "upstream_sha256": hashes,
            "stage_boundary": "PROVISIONAL_SECTION_PLANNING_ONLY",
        },
        "output_schema": load_schema("section_plan.schema.json"),
        "requested_at": utc_now(),
    }


def make_repair_request(candidate: Any, report: dict[str, Any], inputs: dict[str, Any], hashes: dict[str, str]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "call_key": REPAIR_CALL_KEY,
        "prompt_id": "P-STAGE5-PROVISIONAL-SECTION-PLANNING-REPAIR",
        "prompt_version": "1.0.0",
        "executor_role": "Targeted Section Plan Repair Agent",
        "model_contract": {
            "model_independent": True,
            "response_format": "JSON",
            "actual_model_id_required": True,
            "endpoint_id_required": True,
            "single_targeted_repair_only": True,
        },
        "system_prompt": (
            "你是阶段5定向修复Agent。只能修复确定性报告指出的字段，不得改变项目题目、中心命题、研究问题、章节名称、章节顺序或上游事实。"
            "输出完整修复候选，修复后仍必须满足可逆规划边界。"
        ),
        "task_prompt": "依据findings逐项修复；不要删除已有有效映射，不要通过放宽约束或删减章节规避问题。",
        "input_envelope": {
            "original_candidate": candidate,
            "deterministic_findings": report.get("findings", []),
            **inputs,
            "upstream_sha256": hashes,
        },
        "output_schema": load_schema("section_plan.schema.json"),
        "requested_at": utc_now(),
    }


def make_critic_request(candidate: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "call_key": CRITIC_CALL_KEY,
        "prompt_id": "P-STAGE5-PROVISIONAL-SECTION-PLANNING-CRITIC",
        "prompt_version": "1.0.0",
        "executor_role": "Independent Section Plan Critic",
        "model_contract": {
            "independent_from_generator": True,
            "response_format": "JSON",
            "actual_model_id_required": True,
            "endpoint_id_required": True,
        },
        "system_prompt": (
            "你是独立章节规划Critic。检查页数是否真实可执行、每章职责是否互斥且完整、论证节点和来源是否覆盖、"
            "创新章与结论章是否闭环、研究基础和指标是否越界、图表是否服务论证、分批依赖是否合理，"
            "以及规划是否保持可逆并保留重新验证条件。不得因正式模板缺失而否定可逆内容规划。"
        ),
        "task_prompt": "逐一检查14个章节、全部图表、4个批次和7项质量维度。",
        "input_envelope": {"candidate": candidate, "deterministic_report": report},
        "output_schema": load_schema("section_plan_critic.schema.json"),
        "requested_at": utc_now(),
    }


def deterministic_validate(candidate: Any, stage1: dict[str, Any], stage3: dict[str, Any], stage4: dict[str, Any], stage4a: dict[str, Any], hashes: dict[str, str]) -> dict[str, Any]:
    findings: list[dict[str, str]] = []

    def add(code: str, severity: str, message: str) -> None:
        findings.append({"code": code, "severity": severity, "message": message})

    errors = validate_schema(candidate, load_schema("section_plan.schema.json"))
    for err in errors:
        add("SCHEMA_ERROR", "BLOCKING", err)
    if errors:
        return {"verdict": "FAIL", "candidate_hash": sha256_json(candidate), "findings": findings}

    if candidate["project_title"] != stage1["project_title"] or candidate["project_title"] != stage4["project_title"]:
        add("PROJECT_TITLE_CHANGED", "BLOCKING", "章节规划改变了冻结项目题目。")

    expected_stages = {
        "STAGE_1_DESIGN_INPUT": hashes["stage1"],
        "STAGE_3_PROJECT_DEFINITION": hashes["stage3"],
        "STAGE_4_ARGUMENT_ARCHITECTURE": hashes["stage4"],
        "STAGE_4A_EVIDENCE_COMPLETION": hashes["stage4a"],
    }
    actual_upstream = {x["stage"]: x["sha256"] for x in candidate["upstream_artifacts"]}
    if actual_upstream != expected_stages:
        add("UPSTREAM_ARTIFACT_MISMATCH", "BLOCKING", "上游工件哈希或阶段集合不一致。")

    expected_budget = {x["section_id"]: x for x in stage1["page_budget"]}
    sections = candidate["sections"]
    section_ids = [x["section_id"] for x in sections]
    expected_ids = [f"SEC-{i:02d}" for i in range(1, 15)]
    if section_ids != expected_ids:
        add("SECTION_SEQUENCE_MISMATCH", "BLOCKING", "必须按SEC-01至SEC-14顺序覆盖14个章节。")
    if [x["order"] for x in sections] != list(range(1, 15)):
        add("SECTION_ORDER_MISMATCH", "BLOCKING", "章节order必须为1至14。")

    node_ids = {x["node_id"] for x in stage4["nodes"]}
    node_ids.add(stage4["central_proposition"]["node_id"])
    node_ids.update(x["node_id"] for x in stage4["research_questions"])
    source_ids = {x["source_id"] for x in stage4a["source_registry"]}
    visual_ids = {x["visual_id"] for x in candidate["visual_plan"]}
    seen_subsections: set[str] = set()
    target_sum = 0.0
    max_sum = 0.0
    sec_map = {x["section_id"]: x for x in sections}
    for sec in sections:
        sid = sec["section_id"]
        exp = expected_budget.get(sid)
        if not exp:
            continue
        if sec["section_name"] != exp["section_name"]:
            add("SECTION_NAME_DRIFT", "BLOCKING", f"{sid}名称与阶段1冻结名称不一致。")
        if abs(float(sec["target_pages"]) - float(exp["target_pages"])) > 1e-9:
            add("SECTION_TARGET_PAGE_DRIFT", "BLOCKING", f"{sid}目标页数改变。")
        if abs(float(sec["max_pages"]) - float(exp["max_pages"])) > 1e-9:
            add("SECTION_MAX_PAGE_DRIFT", "BLOCKING", f"{sid}最大页数改变。")
        if sec["target_pages"] > sec["max_pages"]:
            add("SECTION_PAGE_RANGE_INVALID", "BLOCKING", f"{sid}目标页数超过最大页数。")
        if sec["expected_words"]["min"] >= sec["expected_words"]["max"]:
            add("WORD_RANGE_INVALID", "BLOCKING", f"{sid}预计字数区间无效。")
        unknown_nodes = set(sec["required_node_ids"]) - node_ids
        if unknown_nodes:
            add("SECTION_UNKNOWN_NODE", "BLOCKING", f"{sid}引用未知论证节点{sorted(unknown_nodes)}。")
        unknown_sources = set(sec["required_source_ids"]) - source_ids
        if unknown_sources:
            add("SECTION_UNKNOWN_SOURCE", "BLOCKING", f"{sid}引用未知来源{sorted(unknown_sources)}。")
        if set(sec["required_metric_ids"]) - {f"MET-{i}" for i in range(1, 9)}:
            add("SECTION_UNKNOWN_METRIC", "BLOCKING", f"{sid}引用未知指标。")
        if set(sec["visual_ids"]) - visual_ids:
            add("SECTION_UNKNOWN_VISUAL", "BLOCKING", f"{sid}引用未知图表。")
        for sub in sec["subsections"]:
            if not sub["subsection_id"].startswith(sid + "-"):
                add("SUBSECTION_PARENT_MISMATCH", "BLOCKING", f"{sub['subsection_id']}不属于{sid}。")
            if sub["subsection_id"] in seen_subsections:
                add("DUPLICATE_SUBSECTION_ID", "BLOCKING", f"子节ID重复：{sub['subsection_id']}。")
            seen_subsections.add(sub["subsection_id"])
        target_sum += float(sec["target_pages"])
        max_sum += float(sec["max_pages"])

    contract = candidate["document_contract"]
    if abs(target_sum - 16.9) > 1e-9 or abs(float(contract["planned_body_pages"]) - target_sum) > 1e-9:
        add("TARGET_PAGE_TOTAL_MISMATCH", "BLOCKING", f"目标正文页数应为16.9，当前为{target_sum:.2f}。")
    if abs(max_sum - 20.0) > 1e-9 or abs(float(contract["max_body_pages"]) - max_sum) > 1e-9:
        add("MAX_PAGE_TOTAL_MISMATCH", "BLOCKING", f"最大正文页数应为20，当前为{max_sum:.2f}。")
    if contract["body_page_limit"] != 20 or not contract["references_outside_body_limit"]:
        add("DOCUMENT_LIMIT_DRIFT", "BLOCKING", "正文硬上限或参考文献计页规则改变。")

    visuals = candidate["visual_plan"]
    if len(visual_ids) != len(visuals):
        add("DUPLICATE_VISUAL_ID", "BLOCKING", "图表ID重复。")
    for vis in visuals:
        if vis["section_id"] not in sec_map:
            add("VISUAL_UNKNOWN_SECTION", "BLOCKING", f"{vis['visual_id']}绑定未知章节。")
        elif vis["visual_id"] not in sec_map[vis["section_id"]]["visual_ids"]:
            add("VISUAL_SECTION_BACKLINK_MISSING", "BLOCKING", f"{vis['visual_id']}未在所属章节反向登记。")
        if set(vis["required_node_ids"]) - node_ids:
            add("VISUAL_UNKNOWN_NODE", "BLOCKING", f"{vis['visual_id']}引用未知节点。")

    required_specific = {
        "SEC-03": {"PRIOR-1", "PRIOR-2", "PRIOR-3"},
        "SEC-05": {"CP-1", "RQ-1", "RQ-2", "RQ-3"},
        "SEC-06": {"RC-1", "RC-2", "RC-3", "RC-4", "WP-1", "WP-2", "WP-3", "WP-4", "WP-5"},
        "SEC-09": {"PRIOR-1", "PRIOR-2", "PRIOR-3", "INNO-H1", "INNO-H2", "INNO-H3"},
        "SEC-10": {f"MET-{i}" for i in range(1, 9)},
        "SEC-11": {"FOUND-1", "FOUND-2", "FOUND-3"},
        "SEC-14": {"CP-1", "RQ-1", "RQ-2", "RQ-3", "INNO-H1", "INNO-H2", "INNO-H3"},
    }
    for sid, required in required_specific.items():
        missing = required - set(sec_map[sid]["required_node_ids"])
        if missing:
            add("SECTION_REQUIRED_BINDING_MISSING", "BLOCKING", f"{sid}缺少冻结绑定{sorted(missing)}。")

    if set(sec_map["SEC-10"]["required_metric_ids"]) != {f"MET-{i}" for i in range(1, 9)}:
        add("METRIC_SECTION_INCOMPLETE", "BLOCKING", "SEC-10必须完整覆盖MET-1至MET-8。")
    if not sec_map["SEC-03"]["required_source_ids"]:
        add("PRIOR_WORK_SOURCE_MISSING", "BLOCKING", "SEC-03必须绑定公开研究来源。")
    if not sec_map["SEC-11"]["required_source_ids"]:
        add("FOUNDATION_SOURCE_BOUNDARY_MISSING", "BLOCKING", "SEC-11必须绑定用户陈述或内部Trace来源以限定证明范围。")

    batches = candidate["draft_batches"]
    batch_ids = [x["batch_id"] for x in batches]
    if batch_ids != ["STAGE-6A", "STAGE-6B", "STAGE-6C", "STAGE-6D"]:
        add("BATCH_SEQUENCE_MISMATCH", "BLOCKING", "正文批次必须依次为STAGE-6A至STAGE-6D。")
    flattened = [sid for batch in batches for sid in batch["section_ids"]]
    if sorted(flattened) != sorted(expected_ids) or len(flattened) != len(set(flattened)):
        add("BATCH_SECTION_PARTITION_INVALID", "BLOCKING", "四个批次必须无重叠完整划分14章。")
    for batch in batches:
        expected_total = sum(sec_map[sid]["target_pages"] for sid in batch["section_ids"] if sid in sec_map)
        if abs(float(batch["total_target_pages"]) - float(expected_total)) > 1e-9:
            add("BATCH_PAGE_SUM_MISMATCH", "BLOCKING", f"{batch['batch_id']}页数合计不一致。")

    controls = candidate["cross_section_controls"]
    if not {"SEC-01", "SEC-05", "SEC-14"}.issubset(set(controls["central_proposition_sections"])):
        add("CENTRAL_PROPOSITION_CLOSURE_MISSING", "BLOCKING", "中心命题至少应在概览、问题目标和结论中闭环。")
    for rq in ["RQ-1", "RQ-2", "RQ-3"]:
        listed = set(controls["rq_coverage"][rq])
        actual = {s["section_id"] for s in sections if rq in s["required_rq_ids"]}
        if listed != actual:
            add("RQ_COVERAGE_INDEX_MISMATCH", "BLOCKING", f"{rq}跨章索引与章节合同不一致。")
    if candidate["open_items_inherited"] != stage4a["open_items_remaining"]:
        add("OPEN_ITEM_INHERITANCE_MISMATCH", "BLOCKING", "开放事项必须原样继承阶段4A。")
    required_triggers = {"OFFICIAL_GUIDE_RECEIVED", "OFFICIAL_TEMPLATE_RECEIVED", "TEAM_EVIDENCE_RECEIVED", "SCENARIO_BASELINE_RECEIVED"}
    if not required_triggers.issubset(set(candidate["revalidation_triggers"])):
        add("REVALIDATION_TRIGGER_MISSING", "BLOCKING", "缺少正式指南、模板、团队材料或场景基线重新验证触发器。")
    readiness = candidate["readiness"]
    if not readiness["ready_for_provisional_drafting"] or readiness["ready_for_final_submission"] or readiness["next_stage"] != "STAGE_6A_PROVISIONAL_DRAFTING":
        add("READINESS_CLASSIFICATION_INVALID", "BLOCKING", "阶段5只能放行暂定正文批次6A，不能放行最终提交。")

    blocking = [x for x in findings if x["severity"] == "BLOCKING"]
    return {
        "verdict": "PASS" if not blocking else "FAIL",
        "candidate_hash": sha256_json(candidate),
        "statistics": {
            "sections": len(sections), "subsections": len(seen_subsections), "visuals": len(visuals),
            "batches": len(batches), "target_pages": round(target_sum, 2), "max_pages": round(max_sum, 2),
            "open_items": len(candidate["open_items_inherited"]),
        },
        "checked_dimensions": [
            "JSON_SCHEMA", "UPSTREAM_HASHES", "SECTION_IDENTITY", "PAGE_BUDGET", "NODE_AND_SOURCE_BINDING",
            "INNOVATION_BASELINE_BINDING", "CONCLUSION_CLOSURE", "VISUAL_BACKLINKS", "BATCH_PARTITION",
            "RQ_COVERAGE_INDEX", "EVIDENCE_BOUNDARIES", "REVALIDATION_TRIGGERS", "READINESS_CLASSIFICATION",
        ],
        "findings": findings,
    }


def load_inputs(run_dir: Path) -> tuple[dict[str, Any], dict[str, str]]:
    snap = run_dir / "source_snapshots"
    inputs = {
        "stage1_design_input": read_json(snap / "stage1_design_input.json"),
        "stage3_project_definition": read_json(snap / "stage3_project_definition.json"),
        "stage4_argument_architecture": read_json(snap / "stage4_argument_architecture.json"),
        "stage4a_evidence_completion": read_json(snap / "stage4a_evidence_completion.json"),
    }
    meta = read_json(run_dir / "RUN_METADATA.json")
    return inputs, meta["upstream_sha256"]


def init_cmd(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    if run_dir.exists() and any(run_dir.iterdir()):
        raise SystemExit("run directory must be empty")
    for name in ["requests", "responses", "schemas", "intermediate", "quality", "human_gate", "outputs", "source_snapshots", "repairs"]:
        (run_dir / name).mkdir(parents=True, exist_ok=True)
    source_args = {
        "stage1": Path(args.design_input).resolve(),
        "stage3": Path(args.project_definition).resolve(),
        "stage4": Path(args.argument_architecture).resolve(),
        "stage4a": Path(args.evidence_completion).resolve(),
    }
    expected_stage = {
        "stage1": "STAGE_1_DESIGN_INPUT",
        "stage3": "STAGE_3_PROJECT_DEFINITION",
        "stage4": "STAGE_4_ARGUMENT_ARCHITECTURE",
        "stage4a": "STAGE_4A_EVIDENCE_COMPLETION",
    }
    snapshot_names = {
        "stage1": "stage1_design_input.json", "stage3": "stage3_project_definition.json",
        "stage4": "stage4_argument_architecture.json", "stage4a": "stage4a_evidence_completion.json",
    }
    inputs: dict[str, Any] = {}
    hashes: dict[str, str] = {}
    for key, path in source_args.items():
        data = read_json(path)
        if data.get("stage") != expected_stage[key]:
            raise SystemExit(f"invalid {key} artifact")
        target = run_dir / "source_snapshots" / snapshot_names[key]
        target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        hashes[key] = sha256_file(target)
        inputs[{"stage1": "stage1_design_input", "stage3": "stage3_project_definition", "stage4": "stage4_argument_architecture", "stage4a": "stage4a_evidence_completion"}[key]] = data
    for name in ["section_plan.schema.json", "section_plan_critic.schema.json"]:
        (run_dir / "schemas" / name).write_text((ROOT / "stage5_tools" / name).read_text(encoding="utf-8"), encoding="utf-8")
    meta = {
        "schema_version": "1.0", "stage": STAGE, "project_title": inputs["stage1_design_input"]["project_title"],
        "created_at": utc_now(), "run_dir": str(run_dir), "stage_boundary": "PROVISIONAL_SECTION_PLANNING_ONLY",
        "model_bridge": "CHAT_FILE_BRIDGE", "upstream_sha256": hashes, "repair_attempt_limit": 1,
    }
    atomic_json(run_dir / "RUN_METADATA.json", meta)
    req = make_generator_request(inputs, hashes)
    atomic_json(run_dir / "requests" / "001_section_plan_generator.json", req)
    append_event(run_dir, "RUN_INITIALIZED", upstream_sha256=hashes)
    append_event(run_dir, "MODEL_REQUEST_CREATED", call_key=GENERATOR_CALL_KEY, prompt_id=req["prompt_id"])
    set_state(run_dir, "WAITING_MODEL", "SECTION_PLAN_GENERATOR")
    print(json.dumps({"status": "WAITING_MODEL", "request": str(run_dir / "requests" / "001_section_plan_generator.json")}, ensure_ascii=False, indent=2))


def validate_envelope(env: dict[str, Any], call_key: str, prompt_id: str) -> None:
    if env.get("call_key") != call_key or env.get("prompt_id") != prompt_id:
        raise SystemExit("response envelope mismatch")
    if not env.get("model_id") or not env.get("endpoint_id"):
        raise SystemExit("missing actual model or endpoint")


def write_active_candidate(run_dir: Path, candidate: Any, source: str) -> None:
    atomic_json(run_dir / "intermediate" / "active_section_plan_candidate.json", candidate)
    atomic_json(run_dir / "intermediate" / "active_candidate_pointer.json", {"source": source, "candidate_hash": sha256_json(candidate), "updated_at": utc_now()})


def issue_critic(run_dir: Path, candidate: dict[str, Any], report: dict[str, Any]) -> None:
    req = make_critic_request(candidate, report)
    atomic_json(run_dir / "requests" / "003_section_plan_critic.json", req)
    append_event(run_dir, "MODEL_REQUEST_CREATED", call_key=CRITIC_CALL_KEY, prompt_id=req["prompt_id"])
    set_state(run_dir, "WAITING_MODEL", "SECTION_PLAN_CRITIC", candidate_hash=report["candidate_hash"])


def ingest_generator_cmd(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    env = read_json(Path(args.response_file).resolve())
    validate_envelope(env, GENERATOR_CALL_KEY, "P-STAGE5-PROVISIONAL-SECTION-PLANNING")
    candidate = env.get("output")
    inputs, hashes = load_inputs(run_dir)
    report = deterministic_validate(candidate, inputs["stage1_design_input"], inputs["stage3_project_definition"], inputs["stage4_argument_architecture"], inputs["stage4a_evidence_completion"], hashes)
    atomic_json(run_dir / "responses" / "001_section_plan_generator.json", env)
    atomic_json(run_dir / "intermediate" / "original_section_plan_candidate.json", candidate)
    atomic_json(run_dir / "quality" / "deterministic_section_plan_report_original.json", report)
    append_event(run_dir, "MODEL_RESPONSE_INGESTED", call_key=GENERATOR_CALL_KEY, model_id=env["model_id"], endpoint_id=env["endpoint_id"], verdict=report["verdict"], candidate_hash=report["candidate_hash"])
    if report["verdict"] == "PASS":
        write_active_candidate(run_dir, candidate, "GENERATOR")
        atomic_json(run_dir / "quality" / "deterministic_section_plan_report.json", report)
        issue_critic(run_dir, candidate, report)
        return
    req = make_repair_request(candidate, report, inputs, hashes)
    atomic_json(run_dir / "requests" / "002_section_plan_repair.json", req)
    atomic_json(run_dir / "repairs" / "repair_scope.json", {"attempt": 1, "limit": 1, "original_candidate_hash": report["candidate_hash"], "findings": report.get("findings", []), "created_at": utc_now()})
    append_event(run_dir, "TARGETED_REPAIR_REQUEST_CREATED", call_key=REPAIR_CALL_KEY, finding_count=len(report.get("findings", [])))
    set_state(run_dir, "WAITING_MODEL", "SECTION_PLAN_TARGETED_REPAIR")


def ingest_repair_cmd(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    if (run_dir / "responses" / "002_section_plan_repair.json").exists():
        raise SystemExit("repair attempt already consumed")
    env = read_json(Path(args.response_file).resolve())
    validate_envelope(env, REPAIR_CALL_KEY, "P-STAGE5-PROVISIONAL-SECTION-PLANNING-REPAIR")
    candidate = env.get("output")
    inputs, hashes = load_inputs(run_dir)
    report = deterministic_validate(candidate, inputs["stage1_design_input"], inputs["stage3_project_definition"], inputs["stage4_argument_architecture"], inputs["stage4a_evidence_completion"], hashes)
    atomic_json(run_dir / "responses" / "002_section_plan_repair.json", env)
    atomic_json(run_dir / "intermediate" / "repaired_section_plan_candidate.json", candidate)
    atomic_json(run_dir / "quality" / "deterministic_section_plan_report_repaired.json", report)
    append_event(run_dir, "MODEL_RESPONSE_INGESTED", call_key=REPAIR_CALL_KEY, model_id=env["model_id"], endpoint_id=env["endpoint_id"], verdict=report["verdict"], candidate_hash=report["candidate_hash"])
    if report["verdict"] != "PASS":
        set_state(run_dir, "BLOCKED", "SECTION_PLAN_REPAIR_EXHAUSTED", candidate_hash=report["candidate_hash"])
        raise SystemExit(2)
    write_active_candidate(run_dir, candidate, "TARGETED_REPAIR")
    atomic_json(run_dir / "quality" / "deterministic_section_plan_report.json", report)
    issue_critic(run_dir, candidate, report)



def revalidate_original_cmd(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    if not (run_dir / "intermediate" / "original_section_plan_candidate.json").exists():
        raise SystemExit("original candidate missing")
    if (run_dir / "responses" / "002_section_plan_repair.json").exists():
        raise SystemExit("repair response already consumed; original revalidation is no longer allowed")
    candidate = read_json(run_dir / "intermediate" / "original_section_plan_candidate.json")
    inputs, hashes = load_inputs(run_dir)
    report = deterministic_validate(candidate, inputs["stage1_design_input"], inputs["stage3_project_definition"], inputs["stage4_argument_architecture"], inputs["stage4a_evidence_completion"], hashes)
    atomic_json(run_dir / "quality" / "deterministic_section_plan_report_after_validator_fix.json", report)
    append_event(run_dir, "ORIGINAL_CANDIDATE_REVALIDATED", verdict=report["verdict"], candidate_hash=report["candidate_hash"])
    if report["verdict"] != "PASS":
        set_state(run_dir, "WAITING_MODEL", "SECTION_PLAN_TARGETED_REPAIR")
        raise SystemExit(2)
    atomic_json(run_dir / "repairs" / "repair_request_invalidated.json", {
        "reason": "DETERMINISTIC_VALIDATOR_DEFECT_FIXED",
        "invalidated_request": "requests/002_section_plan_repair.json",
        "original_candidate_hash": report["candidate_hash"],
        "invalidated_at": utc_now(),
    })
    write_active_candidate(run_dir, candidate, "GENERATOR_REVALIDATED_AFTER_VALIDATOR_FIX")
    atomic_json(run_dir / "quality" / "deterministic_section_plan_report.json", report)
    issue_critic(run_dir, candidate, report)

def ingest_critic_cmd(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    env = read_json(Path(args.response_file).resolve())
    validate_envelope(env, CRITIC_CALL_KEY, "P-STAGE5-PROVISIONAL-SECTION-PLANNING-CRITIC")
    output = env.get("output")
    errors = validate_schema(output, load_schema("section_plan_critic.schema.json"))
    if errors:
        raise SystemExit("critic schema errors: " + " | ".join(errors))
    candidate = read_json(run_dir / "intermediate" / "active_section_plan_candidate.json")
    ch = sha256_json(candidate)
    if output["approved_candidate_hash"] != ch:
        raise SystemExit("approved candidate hash mismatch")
    if set(output["checked_section_ids"]) != {f"SEC-{i:02d}" for i in range(1, 15)}:
        raise SystemExit("critic did not check all sections")
    expected_visuals = {x["visual_id"] for x in candidate["visual_plan"]}
    if set(output["checked_visual_ids"]) != expected_visuals:
        raise SystemExit("critic did not check all visuals")
    if set(output["checked_batch_ids"]) != {"STAGE-6A", "STAGE-6B", "STAGE-6C", "STAGE-6D"}:
        raise SystemExit("critic did not check all batches")
    dims = {"PAGE_BUDGET", "ARGUMENT_COVERAGE", "SECTION_RESPONSIBILITY", "SOURCE_AND_EVIDENCE_BOUNDARY", "VISUAL_PLAN", "BATCH_DEPENDENCIES", "REVERSIBILITY_AND_REVALIDATION"}
    if {x["dimension"] for x in output["quality_dimensions"]} != dims:
        raise SystemExit("critic quality dimension coverage incomplete")
    atomic_json(run_dir / "responses" / "003_section_plan_critic.json", env)
    atomic_json(run_dir / "quality" / "independent_critic_report.json", output)
    append_event(run_dir, "MODEL_RESPONSE_INGESTED", call_key=CRITIC_CALL_KEY, model_id=env["model_id"], endpoint_id=env["endpoint_id"], verdict=output["verdict"], candidate_hash=ch)
    if output["verdict"] != "ACCEPT" or output["next_stage_decision"] != "ALLOW_STAGE_6A_PROVISIONAL_DRAFTING" or any(x["severity"] in {"BLOCKING", "MAJOR"} for x in output["findings"]):
        set_state(run_dir, "BLOCKED", "SECTION_PLAN_CRITIC", candidate_hash=ch)
        raise SystemExit(2)
    gate = {
        "schema_version": "1.0", "gate_id": GATE_ID, "gate_type": "PROVISIONAL_SECTION_PLAN_CONFIRMATION",
        "required_role": "PROJECT_OWNER", "allowed_actions": ["CONFIRM", "REVISE"], "context_hash": ch,
        "summary": {
            "sections": len(candidate["sections"]),
            "target_body_pages": candidate["document_contract"]["planned_body_pages"],
            "max_body_pages": candidate["document_contract"]["max_body_pages"],
            "visuals": len(candidate["visual_plan"]),
            "draft_batches": [x["batch_id"] for x in candidate["draft_batches"]],
            "ready_for_provisional_drafting": candidate["readiness"]["ready_for_provisional_drafting"],
            "ready_for_final_submission": candidate["readiness"]["ready_for_final_submission"],
            "open_items": candidate["open_items_inherited"],
        },
        "requested_at": utc_now(),
    }
    atomic_json(run_dir / "human_gate" / "section_plan_request.json", gate)
    append_event(run_dir, "HUMAN_GATE_REQUEST_CREATED", gate_id=GATE_ID, context_hash=ch)
    set_state(run_dir, "WAITING_HUMAN", "SECTION_PLAN_CONFIRMATION", candidate_hash=ch)


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            rendered = {}
            for key in fields:
                val = row.get(key, "")
                if isinstance(val, list):
                    val = "|".join(str(x) for x in val)
                elif isinstance(val, dict):
                    val = json.dumps(val, ensure_ascii=False, sort_keys=True)
                rendered[key] = val
            w.writerow(rendered)


def write_outputs(candidate: dict[str, Any], out: Path) -> None:
    atomic_json(out / "stage5_section_plan.json", candidate)
    (out / "stage5_section_plan.yaml").write_text(yaml.safe_dump(candidate, allow_unicode=True, sort_keys=False), encoding="utf-8")
    lines = [f"# {candidate['project_title']}：阶段5可逆章节规划", "", f"正文目标：{candidate['document_contract']['planned_body_pages']}页；硬上限：{candidate['document_contract']['body_page_limit']}页。", "", "## 章节计划"]
    for sec in candidate["sections"]:
        lines += ["", f"### {sec['section_id']} {sec['section_name']}（{sec['target_pages']}页，最多{sec['max_pages']}页）", "", sec["purpose"], "", f"- 必须回答：{'；'.join(sec['must_answer'])}", f"- 必须包含：{'；'.join(sec['must_include'])}", f"- 禁止声称：{'；'.join(sec['must_not_claim'])}", f"- 论证节点：{', '.join(sec['required_node_ids']) or '无'}", f"- 来源：{', '.join(sec['required_source_ids']) or '无'}", f"- 图表：{', '.join(sec['visual_ids']) or '无'}"]
        for sub in sec["subsections"]:
            lines += [f"  - **{sub['subsection_id']} {sub['title']}**：{'；'.join(sub['paragraph_roles'])}"]
    lines += ["", "## 分批写作"]
    for batch in candidate["draft_batches"]:
        lines += [f"- **{batch['batch_id']}**：{', '.join(batch['section_ids'])}，目标{batch['total_target_pages']}页。{batch['purpose']}"]
    lines += ["", "## 放行结论", "", candidate["readiness"]["rationale"], ""]
    (out / "stage5_section_plan.md").write_text("\n".join(lines), encoding="utf-8")
    write_csv(out / "stage5_section_plan.csv", candidate["sections"], ["section_id", "order", "section_name", "target_pages", "max_pages", "expected_words", "purpose", "required_rq_ids", "required_node_ids", "required_source_ids", "required_metric_ids", "visual_ids", "contract_status"])
    contracts = []
    for sec in candidate["sections"]:
        contracts.append({
            "section_id": sec["section_id"], "section_name": sec["section_name"], "must_answer": sec["must_answer"],
            "must_include": sec["must_include"], "must_not_claim": sec["must_not_claim"], "subsections": sec["subsections"],
            "required_node_ids": sec["required_node_ids"], "required_source_ids": sec["required_source_ids"],
            "required_metric_ids": sec["required_metric_ids"], "expected_words": sec["expected_words"], "contract_status": sec["contract_status"],
        })
    atomic_json(out / "stage5_generation_contracts.json", contracts)
    write_csv(out / "stage5_visual_plan.csv", candidate["visual_plan"], ["visual_id", "visual_type", "title", "section_id", "purpose", "required_node_ids", "content_spec", "status"])
    write_csv(out / "stage5_draft_batches.csv", candidate["draft_batches"], ["batch_id", "section_ids", "total_target_pages", "purpose", "dependencies", "completion_gate"])
    write_csv(out / "stage5_page_budget.csv", candidate["sections"], ["section_id", "section_name", "target_pages", "max_pages", "expected_words"])


def build_manifest(run_dir: Path) -> dict[str, Any]:
    excluded = {"TRACE_MANIFEST.json"}
    files = []
    for p in sorted(run_dir.rglob("*")):
        if p.is_file() and p.name not in excluded:
            files.append({"path": str(p.relative_to(run_dir)), "size_bytes": p.stat().st_size, "sha256": sha256_file(p)})
    manifest = {"schema_version": "1.0", "root": str(run_dir), "file_count": len(files), "files": files, "generated_at": utc_now()}
    atomic_json(run_dir / "TRACE_MANIFEST.json", manifest)
    return manifest


def package_trace(run_dir: Path) -> tuple[Path, Path]:
    manifest = build_manifest(run_dir)
    zpath = run_dir.with_suffix(".zip")
    if zpath.exists():
        zpath.unlink()
    with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p in sorted(run_dir.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(run_dir.parent))
    record_path = zpath.with_suffix(".archive.json")
    atomic_json(record_path, {
        "schema_version": "1.0", "archive_path": str(zpath), "size_bytes": zpath.stat().st_size,
        "sha256": sha256_file(zpath), "manifest_sha256": sha256_file(run_dir / "TRACE_MANIFEST.json"),
        "manifest_file_count": manifest["file_count"], "created_at": utc_now(),
    })
    return zpath, record_path


def finalize_cmd(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    decision = read_json(Path(args.gate_response).resolve())
    req = read_json(run_dir / "human_gate" / "section_plan_request.json")
    if decision.get("gate_id") != GATE_ID or decision.get("context_hash") != req["context_hash"]:
        raise SystemExit("gate mismatch")
    atomic_json(run_dir / "human_gate" / "section_plan_response.json", decision)
    if decision.get("action") != "CONFIRM":
        set_state(run_dir, "BLOCKED", "SECTION_PLAN_CONFIRMATION")
        raise SystemExit(2)
    candidate = read_json(run_dir / "intermediate" / "active_section_plan_candidate.json")
    inputs, hashes = load_inputs(run_dir)
    report = deterministic_validate(candidate, inputs["stage1_design_input"], inputs["stage3_project_definition"], inputs["stage4_argument_architecture"], inputs["stage4a_evidence_completion"], hashes)
    atomic_json(run_dir / "quality" / "final_revalidation.json", report)
    if report["verdict"] != "PASS":
        set_state(run_dir, "BLOCKED", "FINAL_REVALIDATION")
        raise SystemExit(2)
    out = run_dir / "outputs"
    write_outputs(candidate, out)
    generator = read_json(run_dir / "responses" / "001_section_plan_generator.json")
    critic = read_json(run_dir / "responses" / "003_section_plan_critic.json")
    pointer = read_json(run_dir / "intermediate" / "active_candidate_pointer.json")
    acceptance = {
        "schema_version": "1.0", "stage": STAGE, "result": "PASS", "candidate_hash": sha256_json(candidate),
        "upstream_sha256": hashes, "active_candidate_source": pointer["source"],
        "generator": {"model_id": generator["model_id"], "endpoint_id": generator["endpoint_id"]},
        "critic": {"model_id": critic["model_id"], "endpoint_id": critic["endpoint_id"], "verdict": critic["output"]["verdict"]},
        "human_gate": {"action": "CONFIRM", "decided_by": decision.get("decided_by"), "decided_role": decision.get("decided_role")},
        "statistics": report["statistics"], "ready_for_provisional_drafting": True, "ready_for_final_submission": False,
        "next_stage": "STAGE_6A_PROVISIONAL_DRAFTING", "completed_at": utc_now(),
    }
    atomic_json(out / "STAGE5_ACCEPTANCE_REPORT.json", acceptance)
    append_event(run_dir, "HUMAN_GATE_CONSUMED", gate_id=GATE_ID, action="CONFIRM")
    set_state(run_dir, "COMPLETED", "STAGE_5_COMPLETE", candidate_hash=acceptance["candidate_hash"], next_stage=acceptance["next_stage"])
    zpath, record = package_trace(run_dir)
    print(json.dumps({"status": "COMPLETED", "run_dir": str(run_dir), "trace_zip": str(zpath), "archive_record": str(record), "candidate_hash": acceptance["candidate_hash"], "next_stage": acceptance["next_stage"]}, ensure_ascii=False, indent=2))


def validate_cmd(args: argparse.Namespace) -> None:
    candidate = read_json(Path(args.candidate))
    stage_paths = {
        "stage1": Path(args.design_input), "stage3": Path(args.project_definition),
        "stage4": Path(args.argument_architecture), "stage4a": Path(args.evidence_completion),
    }
    data = {k: read_json(v) for k, v in stage_paths.items()}
    hashes = {k: sha256_file(v) for k, v in stage_paths.items()}
    print(json.dumps(deterministic_validate(candidate, data["stage1"], data["stage3"], data["stage4"], data["stage4a"], hashes), ensure_ascii=False, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("--run-dir", required=True); p.add_argument("--design-input", required=True); p.add_argument("--project-definition", required=True); p.add_argument("--argument-architecture", required=True); p.add_argument("--evidence-completion", required=True); p.set_defaults(fn=init_cmd)
    p = sub.add_parser("ingest-generator")
    p.add_argument("--run-dir", required=True); p.add_argument("--response-file", required=True); p.set_defaults(fn=ingest_generator_cmd)
    p = sub.add_parser("ingest-repair")
    p.add_argument("--run-dir", required=True); p.add_argument("--response-file", required=True); p.set_defaults(fn=ingest_repair_cmd)
    p = sub.add_parser("revalidate-original")
    p.add_argument("--run-dir", required=True); p.set_defaults(fn=revalidate_original_cmd)
    p = sub.add_parser("ingest-critic")
    p.add_argument("--run-dir", required=True); p.add_argument("--response-file", required=True); p.set_defaults(fn=ingest_critic_cmd)
    p = sub.add_parser("finalize")
    p.add_argument("--run-dir", required=True); p.add_argument("--gate-response", required=True); p.set_defaults(fn=finalize_cmd)
    p = sub.add_parser("validate")
    p.add_argument("--candidate", required=True); p.add_argument("--design-input", required=True); p.add_argument("--project-definition", required=True); p.add_argument("--argument-architecture", required=True); p.add_argument("--evidence-completion", required=True); p.set_defaults(fn=validate_cmd)
    args = ap.parse_args(); args.fn(args)


if __name__ == "__main__":
    main()
