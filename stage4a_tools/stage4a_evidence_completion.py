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

STAGE = "STAGE_4A_EVIDENCE_COMPLETION"
GENERATOR_CALL_KEY = "stage4a-evidence-completion-generator-001"
CRITIC_CALL_KEY = "stage4a-evidence-completion-critic-001"
GATE_ID = "stage4a-evidence-completion-confirmation-001"
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
    return read_json(ROOT / "stage4a_tools" / name)


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


def set_state(run_dir: Path, status: str, phase: str, **kwargs: Any) -> None:
    payload = {"schema_version": "1.0", "stage": STAGE, "status": status, "phase": phase, "updated_at": utc_now(), **kwargs}
    atomic_json(run_dir / "LATEST_STATE.json", payload)
    append_event(run_dir, "STATE_CHANGED", status=status, phase=phase, details=kwargs)


def make_generator_request(stage4: dict[str, Any], stage4_hash: str, evidence_inputs: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "call_key": GENERATOR_CALL_KEY,
        "prompt_id": "P-STAGE4A-EVIDENCE-COMPLETION",
        "prompt_version": "1.0.0",
        "executor_role": "Evidence Completion Agent",
        "model_contract": {
            "model_independent": True,
            "response_format": "JSON",
            "actual_model_id_required": True,
            "endpoint_id_required": True,
        },
        "system_prompt": (
            "你是阶段4A证据补全Agent。只能补充公开可核验的最近工作、用户明确陈述的研究基础、内部Trace可证明的工程能力，"
            "以及评价指标的测量协议。不得虚构官方指南、正式模板、团队名单、经费、周期、论文成果或实测指标。"
            "必须区分：内容章节可逆规划、最终申报格式冻结、最终提交证据完备。正式指南和模板缺失只能阻断最终章节合同，"
            "不得在研究内容、中心命题、研究问题和比较框架已经稳定时无条件阻断可逆内容规划。"
            "公开文献只能支持其实际研究范围；创新点必须写成相对机制边界，不得宣称绝对首创。"
        ),
        "task_prompt": (
            "对阶段4的五类证据缺口逐项处置。按RQ-1至RQ-3冻结最接近已有工作、机制边界和比较维度；"
            "将用户陈述与内部Trace映射到FOUND-1至FOUND-3并限定证明范围；为MET-1至MET-8冻结测量协议、基线组和统计摘要，"
            "保留数值阈值为暂定目标；把官方指南和模板缺口重分类为最终合规阻断项。"
            "最终仅允许放行REVERSIBLE_CONTENT_PLAN，不允许冻结最终章节合同。"
        ),
        "input_envelope": {
            "stage4_argument_architecture": stage4,
            "stage4_sha256": stage4_hash,
            "evidence_inputs": evidence_inputs,
            "stage_boundary": "EVIDENCE_COMPLETION_ONLY",
        },
        "output_schema": schema("evidence_completion.schema.json"),
        "requested_at": utc_now(),
    }


def make_critic_request(candidate: dict[str, Any], deterministic_report: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "call_key": CRITIC_CALL_KEY,
        "prompt_id": "P-STAGE4A-EVIDENCE-COMPLETION-CRITIC",
        "prompt_version": "1.0.0",
        "executor_role": "Independent Evidence Critic",
        "model_contract": {
            "independent_from_generator": True,
            "response_format": "JSON",
            "actual_model_id_required": True,
            "endpoint_id_required": True,
        },
        "system_prompt": (
            "你是独立证据Critic。逐项核验来源可定位性、最近工作是否真的接近、创新边界是否克制、"
            "研究基础是否区分用户陈述与内部Trace、指标是否只冻结协议而未伪装成实测结果，以及放行分类是否合理。"
            "正式指南和模板缺失可以保留为最终合规阻断项；若研究内容逻辑已稳定，可允许可逆内容规划。"
        ),
        "task_prompt": "检查全部来源ID、五类证据缺口、三个PRIOR节点、三个FOUND节点和八个MET节点。",
        "input_envelope": {"candidate": candidate, "deterministic_report": deterministic_report},
        "output_schema": schema("evidence_completion_critic.schema.json"),
        "requested_at": utc_now(),
    }


def deterministic_validate(candidate: dict[str, Any], stage4: dict[str, Any], stage4_hash: str) -> dict[str, Any]:
    findings: list[dict[str, str]] = []

    def add(code: str, severity: str, message: str) -> None:
        findings.append({"code": code, "severity": severity, "message": message})

    errors = validate_schema(candidate, schema("evidence_completion.schema.json"))
    for err in errors:
        add("SCHEMA_ERROR", "BLOCKING", err)
    if errors:
        return {"verdict": "FAIL", "candidate_hash": sha256_json(candidate), "findings": findings}

    if candidate["project_title"] != stage4["project_title"]:
        add("TITLE_CHANGED", "BLOCKING", "阶段4A改变了项目题目。")
    if candidate["upstream_artifact"]["sha256"] != stage4_hash:
        add("UPSTREAM_HASH_MISMATCH", "BLOCKING", "阶段4哈希不一致。")

    sources = candidate["source_registry"]
    source_ids = [x["source_id"] for x in sources]
    if len(source_ids) != len(set(source_ids)):
        add("DUPLICATE_SOURCE_ID", "BLOCKING", "来源ID重复。")
    source_map = {x["source_id"]: x for x in sources}

    for src in sources:
        if src["source_type"] in {"PEER_REVIEWED_PAPER", "OFFICIAL_STANDARD"} and src["verification_status"] != "VERIFIED_PUBLIC":
            add("PUBLIC_SOURCE_NOT_VERIFIED", "BLOCKING", f"{src['source_id']}公开来源未标记为已核验。")
        if src["source_type"] == "USER_ASSERTED" and src["verification_status"] != "USER_ASSERTED_UNVERIFIED":
            add("USER_ASSERTION_OVERCLAIMED", "BLOCKING", f"{src['source_id']}用户陈述不得升级为文档核验事实。")
        if src["source_type"] == "INTERNAL_TRACE" and src["verification_status"] != "VERIFIED_INTERNAL_TRACE":
            add("TRACE_SOURCE_STATUS_INVALID", "BLOCKING", f"{src['source_id']}内部Trace状态错误。")

    prior = {x["prior_node_id"]: x for x in candidate["prior_work_updates"]}
    expected_prior = {"PRIOR-1": "RQ-1", "PRIOR-2": "RQ-2", "PRIOR-3": "RQ-3"}
    if set(prior) != set(expected_prior):
        add("PRIOR_NODE_COVERAGE", "BLOCKING", "三个最近工作节点未完整覆盖。")
    for pid, rq in expected_prior.items():
        if pid not in prior:
            continue
        row = prior[pid]
        if row["rq_id"] != rq:
            add("PRIOR_RQ_MISMATCH", "BLOCKING", f"{pid}未绑定{rq}。")
        unknown = set(row["closest_work_source_ids"]) - set(source_map)
        if unknown:
            add("PRIOR_UNKNOWN_SOURCE", "BLOCKING", f"{pid}引用未知来源{sorted(unknown)}。")
        for sid in row["closest_work_source_ids"]:
            src = source_map.get(sid)
            if src and src["source_type"] not in {"PEER_REVIEWED_PAPER", "OFFICIAL_STANDARD"}:
                add("PRIOR_NONPUBLIC_SOURCE", "BLOCKING", f"{pid}最近工作引用了非公开研究来源{sid}。")
            if src and rq not in src["supports_rq_ids"]:
                add("PRIOR_SOURCE_SCOPE_MISMATCH", "BLOCKING", f"{sid}未声明支撑{rq}。")

    foundations = {x["foundation_node_id"]: x for x in candidate["foundation_updates"]}
    if set(foundations) != {"FOUND-1", "FOUND-2", "FOUND-3"}:
        add("FOUNDATION_COVERAGE", "BLOCKING", "FOUND-1至FOUND-3未完整覆盖。")
    for fid, row in foundations.items():
        unknown = set(row["source_ids"]) - set(source_map)
        if unknown:
            add("FOUNDATION_UNKNOWN_SOURCE", "BLOCKING", f"{fid}引用未知来源{sorted(unknown)}。")
        if row["status"] == "SUPPORTED_INTERNAL_TRACE":
            if not row["source_ids"] or any(source_map[sid]["source_type"] != "INTERNAL_TRACE" for sid in row["source_ids"] if sid in source_map):
                add("FOUNDATION_TRACE_MISMATCH", "BLOCKING", f"{fid}内部Trace支撑类型不一致。")
        if row["status"] == "PARTIALLY_SUPPORTED_USER_ASSERTED":
            if not any(source_map[sid]["source_type"] == "USER_ASSERTED" for sid in row["source_ids"] if sid in source_map):
                add("FOUNDATION_USER_ASSERTION_MISSING", "BLOCKING", f"{fid}缺少用户陈述来源。")
        if row["status"] == "PLANNED_NOT_YET_SUPPORTED" and row["source_ids"]:
            add("PLANNED_FOUNDATION_HAS_SOURCE", "BLOCKING", f"{fid}标记未支持却绑定来源。")

    metrics = {x["metric_id"]: x for x in candidate["metric_justification"]}
    expected_metrics = {f"MET-{i}" for i in range(1, 9)}
    if set(metrics) != expected_metrics:
        add("METRIC_COVERAGE", "BLOCKING", "MET-1至MET-8未完整覆盖。")
    for mid, row in metrics.items():
        unknown = set(row["source_ids"]) - set(source_map)
        if unknown:
            add("METRIC_UNKNOWN_SOURCE", "BLOCKING", f"{mid}引用未知来源{sorted(unknown)}。")
        if row["threshold_status"] != "PROVISIONAL_TARGET_NOT_EMPIRICAL_RESULT":
            add("METRIC_THRESHOLD_OVERCLAIM", "BLOCKING", f"{mid}把暂定阈值写成了实测结果。")

    gaps = {x["gap_id"]: x for x in candidate["gap_disposition"]}
    expected_gaps = {f"EVID-GAP-0{i}" for i in range(1, 6)}
    if set(gaps) != expected_gaps:
        add("GAP_COVERAGE", "BLOCKING", "五类证据缺口未完整处置。")
    for gid in ["EVID-GAP-01", "EVID-GAP-02"]:
        row = gaps.get(gid)
        if row and (row["content_planning_blocking"] or not row["final_submission_blocking"] or row["disposition"] != "RECLASSIFIED_FINAL_COMPLIANCE"):
            add("COMPLIANCE_GAP_CLASSIFICATION", "BLOCKING", f"{gid}应只阻断最终合规冻结。")
    row = gaps.get("EVID-GAP-03")
    if row and (row["content_planning_blocking"] or row["disposition"] != "RESOLVED_FOR_CONTENT_PLANNING"):
        add("PRIOR_WORK_GAP_NOT_RESOLVED", "BLOCKING", "最近工作缺口未正确解除内容规划阻断。")
    for gid in ["EVID-GAP-04", "EVID-GAP-05"]:
        row = gaps.get(gid)
        if row and (row["content_planning_blocking"] or not row["final_submission_blocking"]):
            add("PARTIAL_GAP_CLASSIFICATION", "BLOCKING", f"{gid}应允许内容规划但继续阻断最终提交。")

    readiness = candidate["readiness"]
    if not readiness["ready_for_provisional_section_planning"] or readiness["ready_for_final_section_contract"]:
        add("READINESS_INVALID", "BLOCKING", "阶段4A只能放行可逆内容规划，不能冻结最终章节合同。")
    if readiness["planning_mode"] != "REVERSIBLE_CONTENT_PLAN":
        add("PLANNING_MODE_INVALID", "BLOCKING", "规划模式必须为REVERSIBLE_CONTENT_PLAN。")
    if not {"OFFICIAL_GUIDE_OR_TEMPLATE_RECEIVED", "TEAM_OR_FOUNDATION_EVIDENCE_RECEIVED"}.issubset(set(readiness["mandatory_revalidation_triggers"])):
        add("REVALIDATION_TRIGGER_MISSING", "BLOCKING", "缺少必要的重新验证触发条件。")

    original_open = {x["item_id"] for x in stage4["open_items_inherited"]}
    remaining = set(candidate["open_items_remaining"])
    if not remaining.issubset(original_open):
        add("UNKNOWN_OPEN_ITEM", "BLOCKING", "阶段4A引入了新的开放事项ID。")
    if "OPEN-013" in remaining:
        add("PRIOR_WORK_OPEN_NOT_CLOSED", "BLOCKING", "最近工作调研完成后OPEN-013不应继续保持开放。")
    for required in ["OPEN-001", "OPEN-002", "OPEN-005", "OPEN-008", "OPEN-009", "OPEN-012"]:
        if required not in remaining:
            add("REQUIRED_OPEN_ITEM_CLOSED", "BLOCKING", f"{required}仍应保留。")

    blocking = [x for x in findings if x["severity"] == "BLOCKING"]
    return {
        "verdict": "PASS" if not blocking else "FAIL",
        "candidate_hash": sha256_json(candidate),
        "statistics": {
            "sources": len(sources),
            "peer_reviewed_or_standard_sources": sum(x["source_type"] in {"PEER_REVIEWED_PAPER", "OFFICIAL_STANDARD"} for x in sources),
            "prior_work_nodes": len(prior),
            "foundation_nodes": len(foundations),
            "metrics": len(metrics),
            "gap_dispositions": len(gaps),
            "remaining_open_items": len(remaining),
        },
        "checked_dimensions": [
            "JSON_SCHEMA", "UPSTREAM_HASH", "SOURCE_IDENTITY", "SOURCE_VERIFICATION_STATUS",
            "PRIOR_WORK_RQ_SCOPE", "FOUNDATION_EVIDENCE_SCOPE", "METRIC_PROTOCOL_BOUNDARY",
            "GAP_DISPOSITION", "PROVISIONAL_PLANNING_READINESS", "OPEN_ITEM_INHERITANCE",
        ],
        "findings": findings,
    }


def init_cmd(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    stage4_path = Path(args.argument_architecture).resolve()
    evidence_path = Path(args.evidence_inputs).resolve()
    if run_dir.exists() and any(run_dir.iterdir()):
        raise SystemExit("run directory must be empty")
    for name in ["requests", "responses", "schemas", "intermediate", "quality", "human_gate", "outputs", "source_snapshots"]:
        (run_dir / name).mkdir(parents=True, exist_ok=True)
    stage4 = read_json(stage4_path)
    evidence = read_json(evidence_path)
    if stage4.get("stage") != "STAGE_4_ARGUMENT_ARCHITECTURE":
        raise SystemExit("invalid stage4 artifact")
    s4_snapshot = run_dir / "source_snapshots" / "stage4_argument_architecture.json"
    s4_snapshot.write_text(stage4_path.read_text(encoding="utf-8"), encoding="utf-8")
    ev_snapshot = run_dir / "source_snapshots" / "stage4a_evidence_inputs.json"
    ev_snapshot.write_text(evidence_path.read_text(encoding="utf-8"), encoding="utf-8")
    for name in ["evidence_completion.schema.json", "evidence_completion_critic.schema.json"]:
        (run_dir / "schemas" / name).write_text((ROOT / "stage4a_tools" / name).read_text(encoding="utf-8"), encoding="utf-8")
    s4_hash = sha256_file(s4_snapshot)
    meta = {
        "schema_version": "1.0", "stage": STAGE, "project_title": stage4["project_title"],
        "created_at": utc_now(), "run_dir": str(run_dir), "stage_boundary": "EVIDENCE_COMPLETION_ONLY",
        "model_bridge": "CHAT_FILE_BRIDGE", "upstream_hash": s4_hash,
    }
    atomic_json(run_dir / "RUN_METADATA.json", meta)
    request = make_generator_request(stage4, s4_hash, evidence)
    atomic_json(run_dir / "requests" / "001_evidence_completion_generator.json", request)
    append_event(run_dir, "RUN_INITIALIZED", upstream_hash=s4_hash)
    append_event(run_dir, "MODEL_REQUEST_CREATED", call_key=GENERATOR_CALL_KEY, prompt_id=request["prompt_id"])
    set_state(run_dir, "WAITING_MODEL", "EVIDENCE_COMPLETION_GENERATOR")
    print(json.dumps({"status": "WAITING_MODEL", "request": str(run_dir / "requests" / "001_evidence_completion_generator.json")}, ensure_ascii=False, indent=2))


def ingest_generator_cmd(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    env = read_json(Path(args.response_file).resolve())
    if env.get("call_key") != GENERATOR_CALL_KEY or env.get("prompt_id") != "P-STAGE4A-EVIDENCE-COMPLETION":
        raise SystemExit("generator response mismatch")
    if not env.get("model_id") or not env.get("endpoint_id"):
        raise SystemExit("missing actual model or endpoint")
    candidate = env.get("output")
    stage4 = read_json(run_dir / "source_snapshots" / "stage4_argument_architecture.json")
    meta = read_json(run_dir / "RUN_METADATA.json")
    report = deterministic_validate(candidate, stage4, meta["upstream_hash"])
    atomic_json(run_dir / "responses" / "001_evidence_completion_generator.json", env)
    atomic_json(run_dir / "intermediate" / "evidence_completion_candidate.json", candidate)
    atomic_json(run_dir / "quality" / "deterministic_evidence_completion_report.json", report)
    append_event(run_dir, "MODEL_RESPONSE_INGESTED", call_key=GENERATOR_CALL_KEY, model_id=env["model_id"], endpoint_id=env["endpoint_id"], verdict=report["verdict"], candidate_hash=report["candidate_hash"])
    if report["verdict"] != "PASS":
        set_state(run_dir, "BLOCKED", "EVIDENCE_COMPLETION_DETERMINISTIC_REVIEW")
        raise SystemExit(2)
    req = make_critic_request(candidate, report)
    atomic_json(run_dir / "requests" / "002_evidence_completion_critic.json", req)
    append_event(run_dir, "MODEL_REQUEST_CREATED", call_key=CRITIC_CALL_KEY, prompt_id=req["prompt_id"])
    set_state(run_dir, "WAITING_MODEL", "EVIDENCE_COMPLETION_CRITIC")


def ingest_critic_cmd(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    env = read_json(Path(args.response_file).resolve())
    if env.get("call_key") != CRITIC_CALL_KEY or env.get("prompt_id") != "P-STAGE4A-EVIDENCE-COMPLETION-CRITIC":
        raise SystemExit("critic response mismatch")
    if not env.get("model_id") or not env.get("endpoint_id"):
        raise SystemExit("missing actual model or endpoint")
    output = env.get("output")
    errors = validate_schema(output, schema("evidence_completion_critic.schema.json"))
    if errors:
        raise SystemExit("critic schema errors: " + " | ".join(errors))
    candidate = read_json(run_dir / "intermediate" / "evidence_completion_candidate.json")
    ch = sha256_json(candidate)
    if output["approved_candidate_hash"] != ch:
        raise SystemExit("approved candidate hash mismatch")
    expected_sources = {x["source_id"] for x in candidate["source_registry"]}
    if set(output["checked_source_ids"]) != expected_sources:
        raise SystemExit("critic did not check all sources")
    if set(output["checked_gap_ids"]) != {f"EVID-GAP-0{i}" for i in range(1, 6)}:
        raise SystemExit("critic did not check all gaps")
    required_dims = {"SOURCE_VERIFIABILITY", "PRIOR_WORK_CLOSENESS", "INNOVATION_BOUNDARY", "FOUNDATION_EVIDENCE_SCOPE", "METRIC_JUSTIFICATION", "READINESS_CLASSIFICATION"}
    if {x["dimension"] for x in output["quality_dimensions"]} != required_dims:
        raise SystemExit("critic quality dimension coverage incomplete")
    atomic_json(run_dir / "responses" / "002_evidence_completion_critic.json", env)
    atomic_json(run_dir / "quality" / "independent_critic_report.json", output)
    if output["verdict"] != "ACCEPT" or output["next_stage_decision"] != "ALLOW_PROVISIONAL_SECTION_PLANNING" or any(x["severity"] in {"BLOCKING", "MAJOR"} for x in output["findings"]):
        set_state(run_dir, "BLOCKED", "EVIDENCE_COMPLETION_CRITIC")
        raise SystemExit(2)
    gate = {
        "schema_version": "1.0", "gate_id": GATE_ID, "gate_type": "EVIDENCE_COMPLETION_CONFIRMATION",
        "required_role": "PROJECT_OWNER", "allowed_actions": ["CONFIRM", "REVISE"], "context_hash": ch,
        "summary": {
            "public_sources": sum(x["source_type"] in {"PEER_REVIEWED_PAPER", "OFFICIAL_STANDARD"} for x in candidate["source_registry"]),
            "prior_work_nodes": [x["prior_node_id"] for x in candidate["prior_work_updates"]],
            "foundation_status": {x["foundation_node_id"]: x["status"] for x in candidate["foundation_updates"]},
            "ready_for_provisional_section_planning": candidate["readiness"]["ready_for_provisional_section_planning"],
            "ready_for_final_section_contract": candidate["readiness"]["ready_for_final_section_contract"],
            "remaining_open_items": candidate["open_items_remaining"],
        },
        "requested_at": utc_now(),
    }
    atomic_json(run_dir / "human_gate" / "evidence_completion_request.json", gate)
    append_event(run_dir, "MODEL_RESPONSE_INGESTED", call_key=CRITIC_CALL_KEY, model_id=env["model_id"], endpoint_id=env["endpoint_id"], verdict=output["verdict"], candidate_hash=ch)
    append_event(run_dir, "HUMAN_GATE_REQUEST_CREATED", gate_id=GATE_ID, context_hash=ch)
    set_state(run_dir, "WAITING_HUMAN", "EVIDENCE_COMPLETION_CONFIRMATION")


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow({k: "|".join(str(v) for v in row.get(k, [])) if isinstance(row.get(k), list) else row.get(k, "") for k in fields})


def write_outputs(candidate: dict[str, Any], out: Path) -> None:
    atomic_json(out / "stage4a_evidence_completion.json", candidate)
    (out / "stage4a_evidence_completion.yaml").write_text(yaml.safe_dump(candidate, allow_unicode=True, sort_keys=False), encoding="utf-8")
    lines = [f"# {candidate['project_title']}：阶段4A证据补全", "", "## 最近工作与创新边界"]
    for x in candidate["prior_work_updates"]:
        lines += ["", f"### {x['rq_id']} / {x['prior_node_id']}", "", f"- 最接近工作：{', '.join(x['closest_work_source_ids'])}", f"- 已有机制：{x['mechanism_summary']}", f"- 机制边界：{x['boundary_summary']}", f"- 比较维度：{'；'.join(x['comparison_dimensions'])}", f"- 本项目边界：{x['innovation_boundary']}"]
    lines += ["", "## 研究基础证据"]
    for x in candidate["foundation_updates"]:
        lines += [f"- **{x['foundation_node_id']} / {x['status']}**：{x['capability_statement']} 证明范围：{x['proof_scope']} 局限：{x['remaining_limitations']}"]
    lines += ["", "## 证据缺口处置"]
    for x in candidate["gap_disposition"]:
        lines += [f"- **{x['gap_id']}**：{x['disposition']}；阻断内容规划={x['content_planning_blocking']}；阻断最终提交={x['final_submission_blocking']}。{x['rationale']}"]
    lines += ["", "## 放行结论", "", candidate["readiness"]["rationale"], ""]
    (out / "stage4a_evidence_completion.md").write_text("\n".join(lines), encoding="utf-8")
    write_csv(out / "stage4a_source_registry.csv", candidate["source_registry"], ["source_id", "source_type", "title", "authors", "year", "venue", "doi", "locator", "verification_status", "supports_rq_ids", "evidence_scope", "limitations"])
    write_csv(out / "stage4a_prior_work_matrix.csv", candidate["prior_work_updates"], ["prior_node_id", "rq_id", "status", "closest_work_source_ids", "mechanism_summary", "boundary_summary", "comparison_dimensions", "innovation_boundary"])
    write_csv(out / "stage4a_foundation_evidence.csv", candidate["foundation_updates"], ["foundation_node_id", "status", "source_ids", "capability_statement", "proof_scope", "remaining_limitations"])
    write_csv(out / "stage4a_metric_protocols.csv", candidate["metric_justification"], ["metric_id", "status", "measurement_protocol", "baseline_groups", "statistical_summary", "threshold_status", "source_ids"])
    write_csv(out / "stage4a_gap_disposition.csv", candidate["gap_disposition"], ["gap_id", "disposition", "content_planning_blocking", "final_submission_blocking", "rationale", "remaining_actions"])


def build_manifest(run_dir: Path) -> None:
    excluded = {"TRACE_MANIFEST.json", "TRACE_ARCHIVE.json"}
    files = []
    for p in sorted(run_dir.rglob("*")):
        if p.is_file() and p.name not in excluded:
            files.append({"path": str(p.relative_to(run_dir)), "size_bytes": p.stat().st_size, "sha256": sha256_file(p)})
    atomic_json(run_dir / "TRACE_MANIFEST.json", {"schema_version": "1.0", "root": str(run_dir), "file_count": len(files), "files": files, "generated_at": utc_now()})


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


def finalize_cmd(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    decision = read_json(Path(args.gate_response).resolve())
    req = read_json(run_dir / "human_gate" / "evidence_completion_request.json")
    if decision.get("gate_id") != GATE_ID or decision.get("context_hash") != req["context_hash"]:
        raise SystemExit("gate mismatch")
    atomic_json(run_dir / "human_gate" / "evidence_completion_response.json", decision)
    if decision.get("action") != "CONFIRM":
        set_state(run_dir, "BLOCKED", "EVIDENCE_COMPLETION_CONFIRMATION")
        raise SystemExit(2)
    candidate = read_json(run_dir / "intermediate" / "evidence_completion_candidate.json")
    stage4 = read_json(run_dir / "source_snapshots" / "stage4_argument_architecture.json")
    meta = read_json(run_dir / "RUN_METADATA.json")
    report = deterministic_validate(candidate, stage4, meta["upstream_hash"])
    atomic_json(run_dir / "quality" / "final_revalidation.json", report)
    if report["verdict"] != "PASS":
        set_state(run_dir, "BLOCKED", "FINAL_REVALIDATION")
        raise SystemExit(2)
    out = run_dir / "outputs"
    write_outputs(candidate, out)
    gen = read_json(run_dir / "responses" / "001_evidence_completion_generator.json")
    crit = read_json(run_dir / "responses" / "002_evidence_completion_critic.json")
    acceptance = {
        "schema_version": "1.0", "stage": STAGE, "result": "PASS", "candidate_hash": sha256_json(candidate),
        "upstream_hash": meta["upstream_hash"],
        "generator": {"model_id": gen["model_id"], "endpoint_id": gen["endpoint_id"]},
        "critic": {"model_id": crit["model_id"], "endpoint_id": crit["endpoint_id"], "verdict": crit["output"]["verdict"]},
        "human_gate": {"action": "CONFIRM", "decided_by": decision.get("decided_by"), "decided_role": decision.get("decided_role")},
        "statistics": report["statistics"],
        "ready_for_provisional_section_planning": True,
        "ready_for_final_section_contract": False,
        "next_stage": "STAGE_5_PROVISIONAL_SECTION_PLANNING",
        "completed_at": utc_now(),
    }
    atomic_json(out / "STAGE4A_ACCEPTANCE_REPORT.json", acceptance)
    append_event(run_dir, "HUMAN_GATE_CONSUMED", gate_id=GATE_ID, action="CONFIRM")
    set_state(run_dir, "COMPLETED", "STAGE_4A_COMPLETE", candidate_hash=acceptance["candidate_hash"], next_stage=acceptance["next_stage"])
    zpath = package_trace(run_dir)
    print(json.dumps({"status": "COMPLETED", "run_dir": str(run_dir), "trace_zip": str(zpath), "candidate_hash": acceptance["candidate_hash"], "next_stage": acceptance["next_stage"]}, ensure_ascii=False, indent=2))


def validate_cmd(args: argparse.Namespace) -> None:
    candidate = read_json(Path(args.candidate))
    stage4_path = Path(args.argument_architecture)
    stage4 = read_json(stage4_path)
    print(json.dumps(deterministic_validate(candidate, stage4, sha256_file(stage4_path)), ensure_ascii=False, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("--run-dir", required=True); p.add_argument("--argument-architecture", required=True); p.add_argument("--evidence-inputs", required=True); p.set_defaults(fn=init_cmd)
    p = sub.add_parser("ingest-generator")
    p.add_argument("--run-dir", required=True); p.add_argument("--response-file", required=True); p.set_defaults(fn=ingest_generator_cmd)
    p = sub.add_parser("ingest-critic")
    p.add_argument("--run-dir", required=True); p.add_argument("--response-file", required=True); p.set_defaults(fn=ingest_critic_cmd)
    p = sub.add_parser("finalize")
    p.add_argument("--run-dir", required=True); p.add_argument("--gate-response", required=True); p.set_defaults(fn=finalize_cmd)
    p = sub.add_parser("validate")
    p.add_argument("--candidate", required=True); p.add_argument("--argument-architecture", required=True); p.set_defaults(fn=validate_cmd)
    args = ap.parse_args(); args.fn(args)


if __name__ == "__main__":
    main()
