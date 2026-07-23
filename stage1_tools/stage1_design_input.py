from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.util import canonical_json, sha256_json, utc_now

MODEL_ID = "gpt-5.6-thinking"
ENDPOINT_ID = "chatgpt-conversation-file-bridge"
GENERATOR_CALL_KEY = "stage1-design-input-generator-001"
CRITIC_CALL_KEY = "stage1-design-input-critic-001"
GATE_ID = "stage1-design-input-confirmation-001"


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
    return read_json(ROOT / "stage1_tools" / name)


def validate_schema(value: Any, schema_value: dict[str, Any]) -> list[str]:
    errors = []
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
    payload = {"schema_version": "1.0", "stage": "STAGE_1_DESIGN_INPUT", "status": status, "phase": phase, "updated_at": utc_now(), **kwargs}
    atomic_json(run_dir / "LATEST_STATE.json", payload)
    append_event(run_dir, "STATE_CHANGED", status=status, phase=phase, details=kwargs)


def generator_request(title: str) -> dict[str, Any]:
    output_schema = schema("design_input.schema.json")
    system_prompt = """你是科研项目设计输入Agent。你的任务不是撰写申请书正文，而是把用户给出的课题意图整理成可供后续多智能体稳定消费的结构化设计输入。\n\n必须遵守：\n1. 只使用输入中明确提供的事实；需要补充的设计选择必须标记为工作假设或暂定指标。\n2. 不得补写申报单位、资助机构、团队名单、经费金额或项目周期。\n3. 将“人机协同决策优势冲刺”定义为通用决策工程概念，验证场景限于生产调度、物流协同、设备维护和大型活动保障等民用场景。\n4. 建立研究问题、目标、工作包、方法、指标、交付物之间的显式ID关联。\n5. 正文硬上限20页，目标16—18页；参考文献不计入正文页数。\n6. 输出必须是单个JSON对象，严格满足给定Schema，不得输出解释性前后缀。"""
    task_prompt = f"""为课题“{title}”生成阶段1设计输入。\n\n用户要求：\n- 使用附件智能体分阶段生成科研项目申请书；\n- 当前阶段只生成设计输入和输入质量审查结果；\n- 申请书正文不超过20页；\n- 后续模型端点可以替换，因此输入应结构化、明确、少依赖隐含常识；\n- 所有模型请求、响应和中间结果需要保留。\n\n请给出一个内容充分但不过度承诺的研究设计。暂定指标必须标注PROVISIONAL_TARGET，缺失信息必须进入unresolved_items。"""
    return {
        "schema_version": "1.0",
        "call_key": GENERATOR_CALL_KEY,
        "prompt_id": "P-STAGE1-DESIGN-INPUT",
        "prompt_version": "1.0.0",
        "executor_role": "Project Design Input Agent",
        "model_contract": {"model_independent": True, "response_format": "JSON", "actual_model_id_required": True, "endpoint_id_required": True},
        "system_prompt": system_prompt,
        "task_prompt": task_prompt,
        "input_envelope": {
            "stage": "STAGE_1_DESIGN_INPUT",
            "project_title": title,
            "user_requirements": {
                "body_page_limit": 20,
                "target_body_pages": [16, 18],
                "references_outside_body_limit": True,
                "stage_boundary": "DESIGN_INPUT_ONLY",
                "trace_required": True
            },
            "known_unknowns": ["申报单位", "资助机构", "团队名单", "经费金额", "项目周期"],
            "allowed_validation_scenarios": ["生产调度", "物流协同", "设备维护", "大型活动保障"]
        },
        "output_schema": output_schema,
        "requested_at": utc_now()
    }


def deterministic_validate(candidate: dict[str, Any]) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    def add(code: str, severity: str, message: str) -> None:
        findings.append({"code": code, "severity": severity, "message": message})

    schema_errors = validate_schema(candidate, schema("design_input.schema.json"))
    for err in schema_errors:
        add("SCHEMA_ERROR", "BLOCKING", err)
    if schema_errors:
        return {"verdict": "FAIL", "findings": findings, "candidate_hash": sha256_json(candidate)}

    contract = candidate["document_contract"]
    tmin, tmax = contract["target_body_pages"]["min"], contract["target_body_pages"]["max"]
    limit = contract["body_page_limit"]
    if not (tmin <= tmax <= limit <= 20):
        add("PAGE_CONTRACT_INVALID", "BLOCKING", "目标页数与硬上限关系不成立。")
    target_sum = round(sum(float(x["target_pages"]) for x in candidate["page_budget"]), 3)
    max_sum = round(sum(float(x["max_pages"]) for x in candidate["page_budget"]), 3)
    if target_sum > tmax:
        add("PAGE_TARGET_SUM_EXCEEDED", "BLOCKING", f"章节目标页数合计{target_sum}超过目标上限{tmax}。")
    if max_sum > limit:
        add("PAGE_MAX_SUM_EXCEEDED", "BLOCKING", f"章节最大页数合计{max_sum}超过硬上限{limit}。")

    def ids(items: list[dict[str, Any]], key: str) -> set[str]:
        values = [str(x[key]) for x in items]
        if len(values) != len(set(values)):
            add("DUPLICATE_ID", "BLOCKING", f"字段{key}存在重复ID。")
        return set(values)

    rq_ids = ids(candidate["research_questions"], "rq_id")
    obj_ids = ids(candidate["objectives"], "objective_id")
    method_ids = ids(candidate["method_system"], "method_id")
    wp_ids = ids(candidate["work_packages"], "wp_id")
    deliv_ids = ids(candidate["deliverables"], "deliverable_id")
    metric_ids = ids(candidate["evaluation_design"]["metrics"], "metric_id")
    scenario_ids = ids(candidate["application_scenarios"], "scenario_id")

    for obj in candidate["objectives"]:
        missing = set(obj["addresses_rq_ids"]) - rq_ids
        if missing:
            add("OBJECTIVE_UNKNOWN_RQ", "BLOCKING", f"{obj['objective_id']}引用未知研究问题{sorted(missing)}。")
    covered_rqs = {rq for obj in candidate["objectives"] for rq in obj["addresses_rq_ids"]}
    if rq_ids - covered_rqs:
        add("RQ_NOT_COVERED", "BLOCKING", f"研究问题未被目标覆盖：{sorted(rq_ids-covered_rqs)}。")

    for method in candidate["method_system"]:
        missing = set(method["applicable_rq_ids"]) - rq_ids
        if missing:
            add("METHOD_UNKNOWN_RQ", "BLOCKING", f"{method['method_id']}引用未知研究问题{sorted(missing)}。")
    for wp in candidate["work_packages"]:
        checks = [
            ("objective_ids", obj_ids, "WP_UNKNOWN_OBJECTIVE"),
            ("method_ids", method_ids, "WP_UNKNOWN_METHOD"),
            ("deliverable_ids", deliv_ids, "WP_UNKNOWN_DELIVERABLE"),
            ("metric_ids", metric_ids, "WP_UNKNOWN_METRIC")
        ]
        for field, universe, code in checks:
            missing = set(wp[field]) - universe
            if missing:
                add(code, "BLOCKING", f"{wp['wp_id']}的{field}引用未知ID{sorted(missing)}。")
    covered_objs = {x for wp in candidate["work_packages"] for x in wp["objective_ids"]}
    if obj_ids - covered_objs:
        add("OBJECTIVE_NOT_IMPLEMENTED", "BLOCKING", f"目标未由工作包落实：{sorted(obj_ids-covered_objs)}。")

    for metric in candidate["evaluation_design"]["metrics"]:
        missing = set(metric["applicable_scenarios"]) - scenario_ids
        if missing:
            add("METRIC_UNKNOWN_SCENARIO", "BLOCKING", f"{metric['metric_id']}引用未知场景{sorted(missing)}。")
    unresolved_fields = {x["field"] for x in candidate["unresolved_items"]}
    for required in ["申报单位", "资助机构", "团队名单", "经费金额", "项目周期"]:
        if required not in unresolved_fields:
            add("MISSING_UNRESOLVED_FIELD", "BLOCKING", f"缺失信息“{required}”未进入unresolved_items。")

    title = candidate["project_title"]
    definition = candidate["concept_definition"]["working_definition"]
    if "优势冲刺" not in title or "有限" not in definition or "人" not in definition or "智能体" not in definition:
        add("CONCEPT_NOT_OPERATIONALIZED", "BLOCKING", "核心概念未被操作化定义。")

    neutral_terms = {x["name"] for x in candidate["application_scenarios"]}
    expected = {"生产调度", "物流协同", "设备维护", "大型活动保障"}
    if len(neutral_terms & expected) < 3:
        add("SCENARIO_COVERAGE_WEAK", "MAJOR", "通用验证场景覆盖不足。")

    blocking = [x for x in findings if x["severity"] == "BLOCKING"]
    return {
        "verdict": "PASS" if not blocking else "FAIL",
        "candidate_hash": sha256_json(candidate),
        "statistics": {
            "research_questions": len(rq_ids), "objectives": len(obj_ids), "work_packages": len(wp_ids),
            "methods": len(method_ids), "metrics": len(metric_ids), "deliverables": len(deliv_ids),
            "scenarios": len(scenario_ids), "page_target_sum": target_sum, "page_max_sum": max_sum
        },
        "findings": findings,
        "checked_dimensions": [
            "JSON_SCHEMA", "PAGE_BUDGET", "ID_UNIQUENESS", "RQ_OBJECTIVE_ALIGNMENT",
            "OBJECTIVE_WORK_PACKAGE_ALIGNMENT", "METHOD_LINKAGE", "METRIC_SCENARIO_LINKAGE",
            "UNKNOWN_INFORMATION_BOUNDARY", "CONCEPT_OPERATIONALIZATION", "SCENARIO_COVERAGE"
        ]
    }


def write_markdown(candidate: dict[str, Any], path: Path) -> None:
    lines = [f"# {candidate['project_title']}：阶段1设计输入", "", "## 核心概念", "", candidate["concept_definition"]["working_definition"], "", "## 中心命题", "", candidate["project_positioning"]["central_proposition"], "", "## 研究问题"]
    for rq in candidate["research_questions"]:
        lines.append(f"- **{rq['rq_id']}**：{rq['question']}")
    lines += ["", "## 研究目标"]
    for obj in candidate["objectives"]:
        lines.append(f"- **{obj['objective_id']}**：{obj['statement']}")
    lines += ["", "## 工作包"]
    for wp in candidate["work_packages"]:
        lines.append(f"- **{wp['wp_id']} {wp['name']}**：{wp['purpose']}")
    lines += ["", "## 暂定评价指标"]
    for met in candidate["evaluation_design"]["metrics"]:
        lines.append(f"- **{met['metric_id']} {met['name']}**：{met['target']}（{met['target_status']}）")
    lines += ["", "## 页数预算", "", "|章节|目标页数|最大页数|", "|---|---:|---:|"]
    for sec in candidate["page_budget"]:
        lines.append(f"|{sec['section_name']}|{sec['target_pages']}|{sec['max_pages']}|")
    lines += ["", "## 待确认信息"]
    for item in candidate["unresolved_items"]:
        lines.append(f"- {item['field']}：{item['status']}，最迟在 {item['required_before_stage']} 前确认。")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def init_cmd(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    if run_dir.exists():
        shutil.rmtree(run_dir)
    (run_dir / "requests").mkdir(parents=True)
    (run_dir / "responses").mkdir(parents=True)
    (run_dir / "schemas").mkdir(parents=True)
    shutil.copy2(ROOT / "stage1_tools" / "design_input.schema.json", run_dir / "schemas" / "design_input.schema.json")
    shutil.copy2(ROOT / "stage1_tools" / "design_input_critic.schema.json", run_dir / "schemas" / "design_input_critic.schema.json")
    metadata = {"schema_version": "1.0", "stage": "STAGE_1_DESIGN_INPUT", "project_title": args.title, "run_dir": str(run_dir), "created_at": utc_now(), "model_bridge": "CHAT_FILE_BRIDGE", "stage_boundary": "DESIGN_INPUT_ONLY"}
    atomic_json(run_dir / "RUN_METADATA.json", metadata)
    atomic_json(run_dir / "requests" / "001_design_input_generator.json", generator_request(args.title))
    append_event(run_dir, "MODEL_REQUEST_CREATED", call_key=GENERATOR_CALL_KEY, prompt_id="P-STAGE1-DESIGN-INPUT")
    state(run_dir, "WAITING_MODEL", "DESIGN_INPUT_GENERATION")


def ingest_generator_cmd(args: argparse.Namespace) -> None:
    run_dir, response_path = Path(args.run_dir).resolve(), Path(args.response_file).resolve()
    envelope = read_json(response_path)
    required = {"call_key", "prompt_id", "model_id", "endpoint_id", "raw_text", "output", "response_author", "responded_at"}
    missing = sorted(required - set(envelope))
    if missing:
        raise SystemExit(f"response envelope missing fields: {missing}")
    if envelope["call_key"] != GENERATOR_CALL_KEY or envelope["prompt_id"] != "P-STAGE1-DESIGN-INPUT":
        raise SystemExit("response envelope does not match generator request")
    candidate = envelope["output"]
    report = deterministic_validate(candidate)
    dest = run_dir / "responses" / "001_design_input_generator.json"
    atomic_json(dest, envelope)
    atomic_json(run_dir / "intermediate" / "design_input_candidate.json", candidate)
    atomic_json(run_dir / "quality" / "deterministic_design_input_report.json", report)
    append_event(run_dir, "MODEL_RESPONSE_INGESTED", call_key=GENERATOR_CALL_KEY, model_id=envelope["model_id"], endpoint_id=envelope["endpoint_id"], candidate_hash=report["candidate_hash"], verdict=report["verdict"])
    if report["verdict"] != "PASS":
        state(run_dir, "BLOCKED", "DESIGN_INPUT_DETERMINISTIC_REVIEW", report_path="quality/deterministic_design_input_report.json")
        raise SystemExit(2)
    critic_request = {
        "schema_version": "1.0", "call_key": CRITIC_CALL_KEY, "prompt_id": "P-STAGE1-DESIGN-INPUT-CRITIC", "prompt_version": "1.0.0", "executor_role": "Independent Design Input Critic",
        "model_contract": {"independent_from_generator": True, "response_format": "JSON", "actual_model_id_required": True, "endpoint_id_required": True},
        "system_prompt": "你是独立的科研项目设计输入Critic。只审查候选是否足以支撑后续项目定义和论证架构，不撰写申请书正文。重点检查概念可操作性、问题目标任务方法指标闭环、适用边界、暂定指标状态、缺失信息边界和20页篇幅约束。若没有阻断或重大问题，返回ACCEPT；不要为了展示审查而强行提出修改。输出必须严格满足Schema。",
        "task_prompt": "审查阶段1设计输入候选。确定性校验已通过，但你必须独立判断其研究逻辑与可执行性。approved_candidate_hash必须等于候选规范JSON的SHA-256。",
        "input_envelope": {"candidate": candidate, "deterministic_report": report},
        "output_schema": schema("design_input_critic.schema.json"), "requested_at": utc_now()
    }
    atomic_json(run_dir / "requests" / "002_design_input_critic.json", critic_request)
    append_event(run_dir, "MODEL_REQUEST_CREATED", call_key=CRITIC_CALL_KEY, prompt_id="P-STAGE1-DESIGN-INPUT-CRITIC")
    state(run_dir, "WAITING_MODEL", "DESIGN_INPUT_CRITIC")


def ingest_critic_cmd(args: argparse.Namespace) -> None:
    run_dir, response_path = Path(args.run_dir).resolve(), Path(args.response_file).resolve()
    envelope = read_json(response_path)
    if envelope.get("call_key") != CRITIC_CALL_KEY or envelope.get("prompt_id") != "P-STAGE1-DESIGN-INPUT-CRITIC":
        raise SystemExit("critic response envelope does not match request")
    output = envelope.get("output")
    errors = validate_schema(output, schema("design_input_critic.schema.json"))
    if errors:
        raise SystemExit("critic schema errors: " + " | ".join(errors))
    candidate = read_json(run_dir / "intermediate" / "design_input_candidate.json")
    candidate_hash = sha256_json(candidate)
    if output["approved_candidate_hash"] != candidate_hash:
        raise SystemExit("critic approved_candidate_hash mismatch")
    atomic_json(run_dir / "responses" / "002_design_input_critic.json", envelope)
    atomic_json(run_dir / "quality" / "independent_critic_report.json", output)
    append_event(run_dir, "MODEL_RESPONSE_INGESTED", call_key=CRITIC_CALL_KEY, model_id=envelope.get("model_id"), endpoint_id=envelope.get("endpoint_id"), verdict=output["verdict"], candidate_hash=candidate_hash)
    if output["verdict"] != "ACCEPT" or any(x["severity"] in {"BLOCKING", "MAJOR"} for x in output["findings"]):
        state(run_dir, "BLOCKED", "DESIGN_INPUT_CRITIC", report_path="quality/independent_critic_report.json")
        raise SystemExit(2)
    gate_request = {
        "schema_version": "1.0", "gate_id": GATE_ID, "gate_type": "DESIGN_INPUT_CONFIRMATION", "required_role": "PROJECT_OWNER",
        "allowed_actions": ["CONFIRM", "REVISE"], "context_hash": candidate_hash,
        "summary": {"project_title": candidate["project_title"], "research_question_count": len(candidate["research_questions"]), "work_package_count": len(candidate["work_packages"]), "metric_count": len(candidate["evaluation_design"]["metrics"]), "body_page_limit": candidate["document_contract"]["body_page_limit"], "unresolved_fields": [x["field"] for x in candidate["unresolved_items"]]},
        "requested_at": utc_now()
    }
    atomic_json(run_dir / "human_gate" / "request.json", gate_request)
    append_event(run_dir, "HUMAN_GATE_REQUEST_CREATED", gate_id=GATE_ID, gate_type="DESIGN_INPUT_CONFIRMATION", context_hash=candidate_hash)
    state(run_dir, "WAITING_HUMAN", "DESIGN_INPUT_CONFIRMATION")


def finalize_cmd(args: argparse.Namespace) -> None:
    run_dir, response_path = Path(args.run_dir).resolve(), Path(args.gate_response).resolve()
    gate = read_json(response_path)
    request = read_json(run_dir / "human_gate" / "request.json")
    if gate.get("gate_id") != GATE_ID or gate.get("context_hash") != request["context_hash"]:
        raise SystemExit("gate response does not match request")
    if gate.get("action") != "CONFIRM":
        atomic_json(run_dir / "human_gate" / "response.json", gate)
        state(run_dir, "BLOCKED", "DESIGN_INPUT_CONFIRMATION", action=gate.get("action"))
        raise SystemExit(2)
    atomic_json(run_dir / "human_gate" / "response.json", gate)
    candidate = read_json(run_dir / "intermediate" / "design_input_candidate.json")
    output_dir = run_dir / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    atomic_json(output_dir / "stage1_design_input.json", candidate)
    (output_dir / "stage1_design_input.yaml").write_text(yaml.safe_dump(candidate, allow_unicode=True, sort_keys=False), encoding="utf-8")
    write_markdown(candidate, output_dir / "stage1_design_input.md")
    acceptance = {
        "schema_version": "1.0", "stage": "STAGE_1_DESIGN_INPUT", "result": "PASS", "candidate_hash": sha256_json(candidate),
        "generator": {"model_id": read_json(run_dir / "responses" / "001_design_input_generator.json")["model_id"], "endpoint_id": read_json(run_dir / "responses" / "001_design_input_generator.json")["endpoint_id"]},
        "critic": {"model_id": read_json(run_dir / "responses" / "002_design_input_critic.json")["model_id"], "endpoint_id": read_json(run_dir / "responses" / "002_design_input_critic.json")["endpoint_id"], "verdict": "ACCEPT"},
        "human_gate": {"action": "CONFIRM", "decided_by": gate.get("decided_by"), "decided_role": gate.get("decided_role")},
        "statistics": read_json(run_dir / "quality" / "deterministic_design_input_report.json")["statistics"],
        "next_stage": "STAGE_2_GUIDE_AND_FACT_BASE", "completed_at": utc_now()
    }
    atomic_json(output_dir / "STAGE1_ACCEPTANCE_REPORT.json", acceptance)
    final_revalidation = deterministic_validate(candidate)
    atomic_json(run_dir / "quality" / "final_revalidation.json", final_revalidation)
    if final_revalidation["verdict"] != "PASS":
        state(run_dir, "BLOCKED", "FINAL_REVALIDATION", report_path="quality/final_revalidation.json")
        raise SystemExit(2)
    append_event(run_dir, "HUMAN_GATE_CONSUMED", gate_id=GATE_ID, action="CONFIRM")
    state(run_dir, "COMPLETED", "STAGE_1_COMPLETE", candidate_hash=acceptance["candidate_hash"], next_stage=acceptance["next_stage"])
    zip_path = package_trace(run_dir)
    print(json.dumps({"status": "COMPLETED", "run_dir": str(run_dir), "trace_zip": str(zip_path), "candidate_hash": acceptance["candidate_hash"]}, ensure_ascii=False, indent=2))


def build_manifest(run_dir: Path) -> None:
    files = []
    excluded = {"TRACE_MANIFEST.json", "TRACE_ARCHIVE.json"}
    for p in sorted(run_dir.rglob("*")):
        if p.is_file() and p.name not in excluded:
            files.append({"path": str(p.relative_to(run_dir)), "size_bytes": p.stat().st_size, "sha256": sha256_file(p)})
    atomic_json(run_dir / "TRACE_MANIFEST.json", {"schema_version": "1.0", "root": str(run_dir), "file_count": len(files), "files": files, "archive_policy": "TRACE_ARCHIVE.json is external to the archive hash manifest to avoid a self-reference cycle.", "generated_at": utc_now()})


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
    candidate_path = run_dir / "outputs" / "stage1_design_input.json"
    if candidate_path.exists():
        atomic_json(run_dir / "quality" / "final_revalidation.json", deterministic_validate(read_json(candidate_path)))
    zip_path = package_trace(run_dir)
    print(json.dumps({"status": "REPACKED", "run_dir": str(run_dir), "trace_zip": str(zip_path), "zip_sha256": sha256_file(zip_path)}, ensure_ascii=False, indent=2))


def validate_cmd(args: argparse.Namespace) -> None:
    candidate = read_json(Path(args.candidate))
    print(json.dumps(deterministic_validate(candidate), ensure_ascii=False, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init"); p.add_argument("--run-dir", required=True); p.add_argument("--title", required=True); p.set_defaults(fn=init_cmd)
    p = sub.add_parser("ingest-generator"); p.add_argument("--run-dir", required=True); p.add_argument("--response-file", required=True); p.set_defaults(fn=ingest_generator_cmd)
    p = sub.add_parser("ingest-critic"); p.add_argument("--run-dir", required=True); p.add_argument("--response-file", required=True); p.set_defaults(fn=ingest_critic_cmd)
    p = sub.add_parser("finalize"); p.add_argument("--run-dir", required=True); p.add_argument("--gate-response", required=True); p.set_defaults(fn=finalize_cmd)
    p = sub.add_parser("repack"); p.add_argument("--run-dir", required=True); p.set_defaults(fn=repack_cmd)
    p = sub.add_parser("validate"); p.add_argument("--candidate", required=True); p.set_defaults(fn=validate_cmd)
    args = ap.parse_args(); args.fn(args)

if __name__ == "__main__":
    main()
