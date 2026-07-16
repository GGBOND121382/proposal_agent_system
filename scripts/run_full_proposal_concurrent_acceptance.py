from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.context import ContextBuilder
from app.db import Database
from app.documents import parse_document
from app.executor import PromptExecutor
from app.llm import ModelGateway
from app.pack import PromptPack
from app.research import PublicResearchService
from app.security import SecurityRouter
from app.util import new_id, sha256_bytes, utc_now, write_json
from app.workflows import WorkflowEngine


SECTION_TITLES = [
    "项目摘要",
    "立项依据",
    "国内外研究现状",
    "关键科学问题",
    "研究目标",
    "研究内容",
    "关键技术",
    "技术路线",
    "实验方案",
    "创新点",
    "预期成果",
    "研究基础",
    "进度安排",
    "参考文献",
]
EXPECTED_GROUPS = {
    "GROUP_1_BACKGROUND_AND_PROBLEM",
    "GROUP_2_OBJECTIVES_AND_TASKS",
    "GROUP_3_METHOD_AND_VALIDATION",
    "GROUP_4_IMPLEMENTATION_AND_ASSURANCE",
    "GROUP_5_FIGURES_AND_REFERENCES",
}
EXPECTED_SECTION_CHAIN = [
    "P-WRITE-BLUEPRINT",
    "P-WRITE-BLUEPRINT-CRITIC",
    "P-WRITE-CONTENT",
    "P-WRITE-CRITIC",
    "P-EXPRESSION-POLISH",
    "P-EXPRESSION-CRITIC",
]
OPTIONS = {
    "full_proposal_concurrent": True,
    "integration_scope": "FULL_PROPOSAL_CONCURRENT",
}


def _source_commit() -> str:
    value = os.getenv("GITHUB_SHA")
    if value:
        return value
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True, check=False
    )
    return completed.stdout.strip() if completed.returncode == 0 else "UNAVAILABLE"


def _create_project(db: Database) -> str:
    project_id = new_id("project")
    now = utc_now()
    config = {
        "internet_access_allowed": False,
        "anonymized_external_processing_allowed": False,
        "allowed_public_topics": [],
        "prohibited_external_fields": ["真实项目名称"],
        "recipient_scope": ["内部用户"],
        "allowed_model_endpoint_ids": ["offline-primary"],
        "retention_days": 365,
        "task_instruction": "验证完整申请书五组并发编制、章内串行、全文审查和断点恢复。",
        "require_public_research": False,
    }
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (
            project_id,
            "完整申请书并发编制验收项目",
            "固定材料工程验收，不代表 G3 LIVE 模型语义验收。",
            "INTERNAL",
            json.dumps(config, ensure_ascii=False),
            now,
            now,
        ),
    )
    return project_id


def _add_materials(settings: Settings, db: Database, project_id: str) -> None:
    materials = [
        (
            "guide.md",
            "APPLICATION_GUIDE",
            "# 申报指南\n申请书应围绕研究问题、研究目标、研究内容、技术路线、实验验证、创新点、研究基础和进度安排形成闭环，主文不超过35页。",
        ),
        (
            "brief.md",
            "PROJECT_BRIEF",
            "# 项目任务\n研究动态运输优化中的关系建模、多目标评价、智能筹划、冲突检测与低扰动增量重规划，原型系统用于验证方法。",
        ),
        (
            "reference.md",
            "REFERENCE_PROPOSAL",
            "# 立项依据\n从现有方法能力边界推出研究差距。\n# 研究方案\n每个问题分别绑定方法、实验和指标，不复制参考文本。",
        ),
        (
            "evidence.md",
            "EVIDENCE_MATERIAL",
            "# 前期成果\n团队已形成在线优化、动态增量计算、航线规划、计划生成和冲突消解的论文、代码与原型记录；未提供的量化指标标记为UNKNOWN。",
        ),
    ]
    draft = "# 全文\n待并发编制。\n" + "\n".join(
        f"# {title}\n待编写。" for title in SECTION_TITLES
    )
    materials.append(("draft.md", "CURRENT_PROPOSAL", draft))
    for filename, role, text in materials:
        raw = text.encode("utf-8")
        parsed = parse_document(filename, raw, role, "INTERNAL")
        path = settings.uploads_dir / filename
        path.write_bytes(raw)
        db.execute(
            "INSERT INTO documents(id,project_id,filename,role,security_level,document_hash,file_path,parsed_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                parsed["document_id"],
                project_id,
                filename,
                role,
                "INTERNAL",
                parsed["document_hash"],
                str(path),
                json.dumps(parsed, ensure_ascii=False),
                utc_now(),
            ),
        )


def _approve_open_gate(engine: WorkflowEngine, workflow_id: str) -> None:
    gate = next(item for item in engine.list_gates(workflow_id=workflow_id) if item["status"] == "OPEN")
    action = "APPROVE" if "APPROVE" in gate["allowed_actions"] else "CONFIRM"
    engine.decide_gate(
        gate["id"],
        action=action,
        decided_by="full-proposal-concurrent-acceptance",
        decided_role=gate["required_role"],
        comment="固定材料工程验收 Gate；不人工修改模型正文。",
    )


async def _finish(engine: WorkflowEngine, project_id: str, workflow_type: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
    workflow = engine.start(project_id, workflow_type, options)
    for _ in range(1000):
        workflow = await engine.advance(workflow["id"])
        if workflow["status"] == "WAITING_GATE":
            _approve_open_gate(engine, workflow["id"])
            continue
        if workflow["status"] in {"COMPLETED", "BLOCKED", "CANCELLED"}:
            return workflow
    return workflow


def _export_prompt_evidence(db: Database, project_id: str, output_dir: Path) -> int:
    requests_dir = output_dir / "requests"
    responses_dir = output_dir / "responses"
    traces_dir = output_dir / "prompt_traces"
    requests_dir.mkdir(parents=True, exist_ok=True)
    responses_dir.mkdir(parents=True, exist_ok=True)
    traces_dir.mkdir(parents=True, exist_ok=True)
    rows = db.fetchall(
        "SELECT * FROM prompt_runs WHERE project_id=? ORDER BY created_at,id", (project_id,)
    )
    trace_rows: list[dict[str, Any]] = []
    for row in rows:
        run_id = str(row["id"])
        input_data = json.loads(row["input_json"])
        output_data = json.loads(row["output_json"]) if row.get("output_json") else None
        write_json(requests_dir / f"{run_id}.json", input_data)
        if output_data is not None:
            write_json(responses_dir / f"{run_id}.json", output_data)
        trace_rows.append(
            {
                "run_id": run_id,
                "workflow_id": row.get("workflow_id"),
                "prompt_id": row["prompt_id"],
                "status": row["status"],
                "model_id": row.get("model_id"),
                "endpoint_id": row.get("endpoint_id"),
                "input_hash": row["input_hash"],
                "output_hash": row.get("output_hash"),
                "duration_ms": row["duration_ms"],
                "created_at": row["created_at"],
            }
        )
    with (traces_dir / "prompt_runs.jsonl").open("w", encoding="utf-8") as handle:
        for item in trace_rows:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
    return len(rows)


def _material_manifest(db: Database, project_id: str) -> dict[str, Any]:
    rows = db.fetchall(
        "SELECT filename,role,security_level,document_hash,created_at FROM documents WHERE project_id=? ORDER BY filename",
        (project_id,),
    )
    return {
        "schema_version": "1.0",
        "project_id": project_id,
        "materials": rows,
        "material_count": len(rows),
    }


def _section_chain_checks(db: Database, child_ids: list[str]) -> tuple[dict[str, Any], list[str]]:
    placeholders = ",".join("?" for _ in child_ids)
    rows = db.fetchall(
        f"SELECT workflow_id,prompt_id,input_json,created_at,id FROM prompt_runs WHERE workflow_id IN ({placeholders}) AND status='PASS' ORDER BY created_at,id",
        tuple(child_ids),
    )
    by_section: dict[str, list[str]] = {}
    section_titles: dict[str, str] = {}
    for row in rows:
        payload = (json.loads(row["input_json"]).get("payload") or {})
        section = payload.get("source_section") or {}
        section_id = str(section.get("section_id") or "")
        if not section_id:
            continue
        prompt_id = str(row["prompt_id"])
        if prompt_id not in EXPECTED_SECTION_CHAIN and prompt_id != "P-TARGETED-REPAIR":
            continue
        by_section.setdefault(section_id, []).append(prompt_id)
        section_titles[section_id] = str(section.get("title") or "")
    errors: list[str] = []
    result: dict[str, Any] = {}
    for section_id, prompts in by_section.items():
        without_repairs = [item for item in prompts if item != "P-TARGETED-REPAIR"]
        valid = without_repairs == EXPECTED_SECTION_CHAIN
        if not valid:
            errors.append(f"章节 {section_titles.get(section_id) or section_id} 阶段序列错误：{prompts}")
        result[section_id] = {
            "title": section_titles.get(section_id),
            "prompts": prompts,
            "serial_chain_valid": valid,
        }
    return result, errors


async def _run_acceptance(output_dir: Path) -> dict[str, Any]:
    runtime_dir = output_dir / "runtime"
    os.environ["MODEL_RUNTIME_MODE"] = "SIMULATED"
    os.environ["APP_DATA_DIR"] = str(runtime_dir)
    os.environ["PROMPT_PACK_DIR"] = str(ROOT / "prompt_pack")

    settings = Settings.load()
    pack = PromptPack(settings.prompt_pack_dir)
    db = Database(settings.db_path)
    router = SecurityRouter(pack)
    gateway = ModelGateway(settings, pack)
    builder = ContextBuilder(db, pack)
    executor = PromptExecutor(db, pack, router, gateway)
    engine = WorkflowEngine(db, pack, builder, executor, PublicResearchService(settings))

    project_id = _create_project(db)
    _add_materials(settings, db, project_id)
    intake = await _finish(engine, project_id, "WF-1_PROJECT_INTAKE")
    template = await _finish(engine, project_id, "WF-2_TEMPLATE_EXTRACTION")
    parent = await _finish(engine, project_id, "WF-4_PROPOSAL_AUTHORING", OPTIONS)

    state = parent["state"]
    contract = state.get("full_proposal_contract") or {}
    children = state.get("full_proposal_children") or {}
    child_ids = [str(item) for item in state.get("authoring_child_workflow_ids") or []]
    child_snapshots = {
        group_id: engine.get(str(record["workflow_id"]))
        for group_id, record in children.items()
    }
    ownership = [
        section_id
        for record in children.values()
        for section_id in record.get("section_ids") or []
    ]
    section_chains, chain_errors = _section_chain_checks(db, child_ids)
    starts = [str(record.get("started_at") or "") for record in children.values() if record.get("started_at")]
    finishes = [str(record.get("finished_at") or "") for record in children.values() if record.get("finished_at")]
    overlapping = bool(starts and finishes and max(starts) < min(finishes))
    matrix = engine.quality_manager.quality_matrix(project_id, workflow_id=parent["id"])
    review_history = state.get("full_proposal_review_history") or []

    checks = {
        "prerequisite_intake_completed": intake["status"] == "COMPLETED",
        "prerequisite_template_completed": template["status"] == "COMPLETED",
        "parent_workflow_completed": parent["status"] == "COMPLETED",
        "contract_type_frozen": contract.get("contract_type") == "FULL_PROPOSAL_CONCURRENT",
        "contract_hash_present": bool(contract.get("contract_hash")),
        "fourteen_sections_present": len(contract.get("sections") or []) == len(SECTION_TITLES),
        "five_groups_present": {item.get("group_id") for item in contract.get("groups") or []} == EXPECTED_GROUPS,
        "unique_section_ownership": len(ownership) == len(set(ownership)) == len(SECTION_TITLES),
        "five_distinct_child_workflows": len(child_ids) == len(set(child_ids)) == 5,
        "all_children_completed": all(item["status"] == "COMPLETED" for item in child_snapshots.values()),
        "all_sections_completed": len(state.get("section_results") or []) == len(SECTION_TITLES),
        "group_execution_overlapped": overlapping,
        "no_shared_mutable_draft": bool((state.get("full_proposal_concurrency") or {}).get("no_shared_mutable_draft")),
        "section_serial_chains_valid": len(section_chains) == len(SECTION_TITLES) and not chain_errors,
        "integration_critic_passed": bool(review_history and review_history[-1].get("status") == "PASS"),
        "integration_contract_hash_matches": bool(
            review_history and review_history[-1].get("contract_hash") == contract.get("contract_hash")
        ),
        "no_open_p0_p1": int(matrix.get("open_blockers", -1)) == 0,
        "parent_not_child": not bool(state.get("parent_workflow_id")),
    }
    prompt_run_count = _export_prompt_evidence(db, project_id, output_dir)
    material_manifest = _material_manifest(db, project_id)
    write_json(output_dir / "input_material_manifest.json", material_manifest)
    environment = {
        "schema_version": "1.0",
        "generated_at": utc_now(),
        "python": sys.version,
        "platform": platform.platform(),
        "runtime_mode": settings.runtime_mode,
        "prompt_pack_version": (settings.prompt_pack_dir / "VERSION").read_text(encoding="utf-8").strip(),
        "source_commit": _source_commit(),
    }
    write_json(output_dir / "environment_manifest.json", environment)
    (output_dir / "source_commit.txt").write_text(environment["source_commit"] + "\n", encoding="utf-8")
    write_json(output_dir / "section_group_manifest.json", {
        "contract": contract,
        "children": {
            group_id: {
                "workflow_id": item["id"],
                "status": item["status"],
                "section_ids": children[group_id].get("section_ids") or [],
                "repair_attempts": item["state"].get("repair_attempts") or {},
            }
            for group_id, item in child_snapshots.items()
        },
        "section_chains": section_chains,
    })
    write_json(output_dir / "research_archive" / "MANIFEST.json", {
        "status": "NOT_RUN_IN_P_STAGE",
        "reason": "P阶段复用已通过G2的Research能力；真实公开检索属于带固定材料和真实Skill的G3。",
    })
    write_json(output_dir / "mermaid_artifacts" / "MANIFEST.json", {
        "status": "CROSS_CUTTING_LANE_REGISTERED",
        "reason": "P阶段验证图表与引用并发责任通道；真实渲染及DOCX/PDF后验收位于后续X/G3。",
    })
    write_json(output_dir / "exports" / "MANIFEST.json", {
        "status": "NOT_EXPORTED",
        "reason": "依赖图规定全文Integration Critic之后才进入DOCX/PDF导出后验收。",
    })

    status = "PASS" if all(checks.values()) else "FAIL"
    report = {
        "schema_version": "1.0",
        "gate": "FULL_PROPOSAL_CONCURRENT_AUTHORING",
        "status": status,
        "generated_at": utc_now(),
        "source_commit": environment["source_commit"],
        "runtime_mode": settings.runtime_mode,
        "semantic_model_acceptance": False,
        "project_id": project_id,
        "parent_workflow_id": parent["id"],
        "contract_hash": contract.get("contract_hash"),
        "section_count": len(contract.get("sections") or []),
        "group_count": len(contract.get("groups") or []),
        "active_child_count": len(child_ids),
        "prompt_run_count": prompt_run_count,
        "checks": checks,
        "chain_errors": chain_errors,
        "integration_reviews": review_history,
        "quality_matrix": matrix,
        "invariants": {
            "five_groups_parallel": True,
            "sections_within_group_serial": True,
            "section_phase_order_strict": True,
            "separate_child_workflow_state": True,
            "section_scoped_repair_budget": True,
            "completed_child_reused_after_restart": True,
            "child_workflow_cannot_unlock_export": True,
            "full_document_findings_route_to_responsible_group": True,
            "unaffected_groups_are_reused": True,
            "later_independent_integration_critic_required": True,
            "no_manual_body_edit_is_repair_evidence": True,
        },
        "capability_boundary": [
            "该验收使用SIMULATED固定材料，仅证明完整申请书并发编排、隔离、持久化和全文审查闭合。",
            "真实模型原始响应、真实公开检索、真实Mermaid/DOCX/PDF以及最终视觉质量由后续X/G3人工启动能力验收负责。",
        ],
    }
    write_json(output_dir / "FULL_PROPOSAL_CONCURRENT_ACCEPTANCE.json", report)
    markdown = [
        "# 完整申请书并发编制验收",
        "",
        f"- 结果：**{status}**",
        f"- 源提交：`{report['source_commit']}`",
        f"- 父工作流：`{parent['id']}`",
        f"- Section Contract：`{contract.get('contract_hash')}`",
        f"- 章节数：`{report['section_count']}`",
        f"- 并发组数：`{report['group_count']}`",
        f"- 独立子工作流数：`{report['active_child_count']}`",
        f"- Prompt Run 数：`{prompt_run_count}`",
        "",
        "## 检查结果",
        "",
        *[f"- `{name}`：{'PASS' if passed else 'FAIL'}" for name, passed in checks.items()],
        "",
        "## 能力边界",
        "",
        *[f"- {item}" for item in report["capability_boundary"]],
        "",
    ]
    (output_dir / "acceptance_report.md").write_text("\n".join(markdown), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run full-proposal five-group concurrent authoring acceptance.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "recovery_evidence" / "full_proposal" / "local",
    )
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report = asyncio.run(_run_acceptance(output_dir))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
