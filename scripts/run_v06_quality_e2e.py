from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.context import ContextBuilder
from app.db import Database
from app.documents import parse_document
from app.executor import PromptExecutor
from app.exporter import DocxExporter
from app.llm import ModelGateway
from app.pack import PromptPack
from app.research import PublicResearchService
from app.security import SecurityRouter
from app.util import new_id, utc_now
from app.workflows import WorkflowEngine


def _add_document(settings: Settings, db: Database, project_id: str, filename: str, role: str, text: str) -> None:
    raw = text.encode("utf-8")
    parsed = parse_document(filename, raw, role, "INTERNAL")
    path = settings.uploads_dir / filename
    path.write_bytes(raw)
    db.execute(
        "INSERT INTO documents(id,project_id,filename,role,security_level,document_hash,file_path,parsed_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            parsed["document_id"], project_id, filename, role, "INTERNAL",
            parsed["document_hash"], str(path), json.dumps(parsed, ensure_ascii=False), utc_now(),
        ),
    )


def _create_project(settings: Settings, db: Database) -> str:
    project_id = new_id("project")
    now = utc_now()
    config = {
        "internet_access_allowed": True,
        "anonymized_external_processing_allowed": True,
        "allowed_public_topics": ["动态运输优化", "组合优化", "多智能体协作"],
        "prohibited_external_fields": ["真实人员姓名", "真实组织名称"],
        "recipient_scope": ["内部项目组"],
        "allowed_model_endpoint_ids": ["offline-primary", "online-public-primary"],
        "retention_days": 365,
        "task_instruction": "形成科学问题集中、方法与验证闭环、工程细节附件化的科研项目申请书。",
    }
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (
            project_id,
            "动态运输优化方法研究",
            "研究不确定事件下约束映射、影响范围识别与低扰动增量优化。",
            "INTERNAL",
            json.dumps(config, ensure_ascii=False),
            now,
            now,
        ),
    )
    _add_document(
        settings, db, project_id, "guide.md", "APPLICATION_GUIDE",
        "# 申报要求\n主申请书不超过35页，突出一个中心命题、有限研究问题、方法实质、创新对比、实验验证和研究基础。部署、接口和审计材料放入附件。",
    )
    _add_document(
        settings, db, project_id, "brief.md", "PROJECT_BRIEF",
        "# 项目任务\n针对动态订单和道路状态变化，研究业务语义到优化约束的可验证映射、受影响决策变量识别和低扰动增量重规划。原型系统仅用于验证方法。",
    )
    _add_document(
        settings, db, project_id, "technical.md", "TECHNICAL_DESIGN",
        "# 技术设想\n以带时间窗和容量约束的运输优化为形式化对象，比较全量重算、固定窗口重算和影响范围驱动的增量方法；设计消融实验检验影响范围识别和稳定性代价的作用。",
    )
    _add_document(
        settings, db, project_id, "evidence.md", "EVIDENCE_MATERIAL",
        "# 前期成果\n团队已完成组合优化原型、动态调度实验代码和一组可复现实验记录，具备运输数据处理、求解器建模和对照实验能力。",
    )
    _add_document(
        settings, db, project_id, "reference.md", "REFERENCE_PROPOSAL",
        "# 立项依据\n优秀申请书从代表工作适用边界推出具体差距，再提出可检验问题。\n# 研究方案\n每个问题分别绑定形式化方法、比较基线、数据条件和实验判断。\n# 表达规则\n段落只承担一种论证功能，避免以技术名称、系统模块或篇幅替代研究贡献。",
    )
    titles = [
        "项目摘要", "立项依据", "国内外研究现状", "关键科学问题", "研究目标",
        "研究内容", "研究方案", "技术路线", "实验与评估", "创新点",
        "研究基础", "预期成果", "参考文献", "附录：原型与部署说明",
    ]
    draft = "# 全文\n待根据论证架构重构。\n" + "\n".join(f"# {title}\n待编写。" for title in titles)
    _add_document(settings, db, project_id, "draft.md", "CURRENT_PROPOSAL", draft)
    return project_id


async def _finish(engine: WorkflowEngine, project_id: str, workflow_type: str, max_steps: int = 1000) -> dict:
    workflow = engine.start(project_id, workflow_type)
    for _ in range(max_steps):
        workflow = await engine.advance(workflow["id"])
        if workflow["status"] == "WAITING_GATE":
            gates = [g for g in engine.list_gates(workflow_id=workflow["id"]) if g["status"] == "OPEN"]
            if not gates:
                raise RuntimeError(f"{workflow_type} waiting without open gate")
            gate = gates[0]
            action = "APPROVE" if "APPROVE" in gate["allowed_actions"] else "CONFIRM"
            engine.decide_gate(gate["id"], action=action, decided_by="v06-e2e", decided_role=gate["required_role"])
            continue
        if workflow["status"] in {"COMPLETED", "BLOCKED", "CANCELLED"}:
            return workflow
    raise RuntimeError(f"{workflow_type} exceeded {max_steps} steps")


def _latest_result(db: Database, project_id: str, prompt_id: str) -> dict:
    row = db.fetchone(
        "SELECT output_json FROM prompt_runs WHERE project_id=? AND prompt_id=? AND status='PASS' ORDER BY created_at DESC,id DESC LIMIT 1",
        (project_id, prompt_id),
    )
    if not row:
        return {}
    return (json.loads(row["output_json"]) or {}).get("result") or {}


def _candidate_rows(db: Database, project_id: str) -> list[dict]:
    rows = db.fetchall(
        "SELECT prompt_id,input_json,output_json FROM prompt_runs WHERE project_id=? AND prompt_id IN ('P-WRITE-CONTENT','P-WRITE-CRITIC','P-EXPRESSION-POLISH','P-EXPRESSION-CRITIC') AND status='PASS' ORDER BY created_at,id",
        (project_id,),
    )
    return [
        {"prompt_id": row["prompt_id"], "input": json.loads(row["input_json"]), "output": json.loads(row["output_json"])}
        for row in rows
    ]


def _build_report(db: Database, project_id: str, workflows: list[dict], docx_path: Path) -> dict:
    project_definition = _latest_result(db, project_id, "P-PROJECT-DEFINITION-EXTRACT").get("project_definition") or {}
    argument_result = _latest_result(db, project_id, "P-ARGUMENT-ARCHITECTURE")
    argument = argument_result.get("argument_architecture") or {}
    plan = _latest_result(db, project_id, "P-REVISION-PLAN").get("revision_plan") or {}
    architecture = plan.get("narrative_architecture") or {}
    contracts = architecture.get("section_contracts") or []
    rows = _candidate_rows(db, project_id)

    content_rows = [r for r in rows if r["prompt_id"] == "P-WRITE-CONTENT"]
    critic_rows = [r for r in rows if r["prompt_id"] == "P-WRITE-CRITIC"]
    polish_rows = [r for r in rows if r["prompt_id"] == "P-EXPRESSION-POLISH"]
    expression_critic_rows = [r for r in rows if r["prompt_id"] == "P-EXPRESSION-CRITIC"]

    information_owners: dict[str, set[str]] = defaultdict(set)
    critic_coverage_ok = True
    profile_rules_ok = True
    semantic_identity_ok = True
    for row in content_rows:
        section = row["input"].get("payload", {}).get("source_section") or {}
        candidate = row["output"].get("result") or {}
        for key in (candidate.get("claim_advancement") or {}).get("new_information_keys", []):
            information_owners[str(key)].add(str(section.get("section_id")))
    for row in critic_rows:
        payload = row["input"].get("payload", {})
        candidate = payload.get("content_candidate") or {}
        result = row["output"].get("result") or {}
        expected = {str(p.get("paragraph_id")) for p in candidate.get("paragraphs", []) if p.get("paragraph_id")}
        checked = {str(x) for x in result.get("checked_paragraph_ids", [])}
        critic_coverage_ok &= expected == checked
        required_rules = set((payload.get("section_profile") or {}).get("acceptance_rules") or [])
        checked_rules = {str(x.get("rule")) for x in result.get("profile_acceptance_results", []) if isinstance(x, dict)}
        profile_rules_ok &= required_rules <= checked_rules
    for row in polish_rows:
        original = row["input"].get("payload", {}).get("content_candidate") or {}
        polished = row["output"].get("result") or {}
        original_map = {p["paragraph_id"]: p for p in original.get("paragraphs", [])}
        polished_map = {p["paragraph_id"]: p for p in polished.get("paragraphs", [])}
        if original_map.keys() != polished_map.keys() or original.get("claim_advancement") != polished.get("claim_advancement"):
            semantic_identity_ok = False
            continue
        for paragraph_id in original_map:
            for field in ["blueprint_paragraph_id", "paragraph_role", "primary_claim_id", "evidence_ids", "novel_content_key", "section_contract_id", "trace_link_ids"]:
                semantic_identity_ok &= original_map[paragraph_id].get(field) == polished_map[paragraph_id].get(field)

    integration = _latest_result(db, project_id, "P-INTEGRATION-CRITIC")
    dimensions = {item.get("dimension"): item for item in integration.get("quality_dimensions", []) if isinstance(item, dict)}
    redundancy = integration.get("redundancy_report") or {}
    prompt_counts = Counter(row["prompt_id"] for row in db.fetchall("SELECT prompt_id FROM prompt_runs WHERE project_id=?", (project_id,)))
    main_contracts = [c for c in contracts if c.get("placement") == "MAIN_BODY"]
    all_info_keys = [str(key) for c in contracts for key in c.get("unique_information_keys", [])]

    checks = {
        "all_workflows_completed": all(w["status"] == "COMPLETED" for w in workflows),
        "project_graph_has_core_types": {"GAP", "PROBLEM", "OBJECTIVE", "WORK_PACKAGE", "METHOD", "EXPERIMENT", "INNOVATION", "ACHIEVEMENT"} <= {i.get("item_type") for i in project_definition.get("items", [])},
        "one_testable_central_proposition": bool(argument.get("central_proposition", {}).get("falsifiable_or_comparable")),
        "research_question_count_valid": 1 <= len(argument.get("research_questions", [])) <= 4,
        "section_contract_count_bounded": 1 <= len(main_contracts) <= 18,
        "section_profiles_diverse": len({c.get("profile_id") for c in main_contracts}) >= min(5, len(main_contracts)),
        "section_information_keys_unique": len(all_info_keys) == len(set(all_info_keys)),
        "generated_information_keys_have_one_owner": all(len(owners) == 1 for owners in information_owners.values()),
        "section_critics_read_all_paragraphs": critic_coverage_ok,
        "section_critics_apply_profile_rules": profile_rules_ok,
        "expression_preserves_semantic_identity": semantic_identity_ok,
        "expression_critic_count_matches_sections": len(expression_critic_rows) == len(polish_rows) == len(content_rows),
        "integration_accepts_full_document": integration.get("verdict") == "ACCEPT",
        "integration_all_12_dimensions_pass": len(dimensions) == 12 and all(item.get("passed") and item.get("score", 0) >= 3 for item in dimensions.values()),
        "integration_no_repetition": all(int(redundancy.get(key, 0)) == 0 for key in ["exact_duplicate_groups", "semantic_template_groups", "duplicate_information_key_groups", "claim_overconcentration_groups", "template_skeleton_groups"]),
        "docx_exported": docx_path.exists() and docx_path.stat().st_size > 10000,
    }
    return {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "project_id": project_id,
        "workflow_status": {w["workflow_type"]: w["status"] for w in workflows},
        "prompt_counts": dict(prompt_counts),
        "metrics": {
            "project_item_count": len(project_definition.get("items", [])),
            "project_relation_count": len(project_definition.get("relations", [])),
            "argument_node_count": len(argument.get("nodes", [])) + len(argument.get("research_questions", [])) + 1,
            "research_question_count": len(argument.get("research_questions", [])),
            "section_contract_count": len(contracts),
            "main_body_contract_count": len(main_contracts),
            "profile_count": len({c.get("profile_id") for c in main_contracts}),
            "content_section_count": len(content_rows),
            "critic_section_count": len(critic_rows),
            "information_key_count": len(information_owners),
            "integration_dimension_count": len(dimensions),
        },
        "checks": checks,
        "integration_redundancy_report": redundancy,
        "docx_path": str(docx_path),
    }


async def main(output_dir: Path) -> dict:
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)
    os.environ["MODEL_RUNTIME_MODE"] = "SIMULATED"
    os.environ["APP_DATA_DIR"] = str(output_dir / "data")
    os.environ["PROMPT_PACK_DIR"] = str(ROOT / "prompt_pack")
    os.environ["PUBLIC_SEARCH_PROVIDER"] = "disabled"

    settings = Settings.load()
    pack = PromptPack(settings.prompt_pack_dir)
    db = Database(settings.db_path)
    router = SecurityRouter(pack)
    gateway = ModelGateway(settings, pack)
    builder = ContextBuilder(db, pack)
    executor = PromptExecutor(db, pack, router, gateway)
    research = PublicResearchService(settings)
    engine = WorkflowEngine(db, pack, builder, executor, research)
    exporter = DocxExporter(db, settings)

    project_id = _create_project(settings, db)
    workflows = []
    for workflow_type in [
        "WF-1_PROJECT_INTAKE", "WF-2_TEMPLATE_EXTRACTION", "WF-3_HYBRID_ONLINE_ASSIST",
        "WF-4_PROPOSAL_AUTHORING", "WF-5_SECURITY_REVIEW_AND_EXPORT",
    ]:
        workflow = await _finish(engine, project_id, workflow_type)
        workflows.append(workflow)
        if workflow["status"] != "COMPLETED":
            raise RuntimeError(f"{workflow_type} failed: {workflow['state'].get('last_error')}")
    docx_path = exporter.export(project_id)
    report = _build_report(db, project_id, workflows, docx_path)
    report_path = output_dir / "v06_quality_e2e_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("data/v06_quality_e2e"))
    args = parser.parse_args()
    result = asyncio.run(main(args.output_dir.resolve()))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "PASS" else 1)
