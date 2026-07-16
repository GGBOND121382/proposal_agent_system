from __future__ import annotations

import argparse
import asyncio
import copy
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
from app.executor import PromptExecutor
from app.llm import ModelGateway
from app.pack import PromptPack
from app.research import PublicResearchService
from app.security import SecurityRouter
from app.util import utc_now, write_json
from app.workflows import WorkflowEngine
from scripts.run_full_proposal_concurrent_acceptance import (
    OPTIONS,
    SECTION_TITLES,
    _add_materials,
    _create_project,
    _export_prompt_evidence,
    _finish,
    _material_manifest,
)


def _source_commit() -> str:
    value = os.getenv("GITHUB_SHA")
    if value:
        return value
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True, check=False
    )
    return completed.stdout.strip() if completed.returncode == 0 else "UNAVAILABLE"


def _install_review_scenario(executor: PromptExecutor) -> dict[str, Any]:
    simulator = executor.gateway.simulator
    original = simulator._handle_integration_critic
    calls: dict[str, Any] = {
        "count": 0,
        "technical_route_section_id": None,
    }

    def injected(base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        output = original(copy.deepcopy(base), envelope)
        calls["count"] += 1
        payload = envelope.get("payload") or {}
        section_by_title = {
            str(item.get("title") or ""): str(item.get("section_id") or "")
            for item in payload.get("document_section_map") or []
            if isinstance(item, dict)
        }
        if calls["count"] == 1:
            target = section_by_title["技术路线"]
            calls["technical_route_section_id"] = target
            output["status"] = "REVISE"
            output["result"]["verdict"] = "REVISE"
            output["result"]["terminology_checks"] = [
                {"term": "低扰动增量优化", "consistent": False, "sections": [target]}
            ]
            output["findings"] = [{
                "code": "FULL_INTEGRATION_TECHNICAL_ROUTE_CONFLICT",
                "severity": "P1",
                "category": "INTEGRATION",
                "target_type": "SECTION_CANDIDATE",
                "target_path_or_span": f"candidate_sections.{target}.paragraphs",
                "description": "技术路线对核心机制的称谓与全文冻结术语不一致。",
                "evidence_refs": [target],
                "repairable": True,
                "repair_instruction": "仅由技术路线责任写作组重写受影响章节。",
                "suggested_route": "WRITING_AGENT",
                "blocking": True,
            }]
        elif calls["count"] == 2:
            output["status"] = "REVISE"
            output["result"]["verdict"] = "REVISE"
            output["findings"] = [{
                "code": "FULL_INTEGRATION_INFORMATION_OWNERSHIP_CONFLICT",
                "severity": "P1",
                "category": "INTEGRATION",
                "target_type": "SECTION_CONTRACT",
                "target_path_or_span": "narrative_architecture.section_contracts.unique_information_keys",
                "description": "多个章节的信息所有权需要重新分配，局部正文改写不能修复。",
                "evidence_refs": [
                    section_by_title.get("研究内容", ""),
                    section_by_title.get("技术路线", ""),
                ],
                "repairable": True,
                "repair_instruction": "返回 Planning Agent 重新冻结唯一信息所有权与章节依赖。",
                "suggested_route": "PLANNING_AGENT",
                "blocking": True,
            }]
        return output

    simulator._handle_integration_critic = injected
    return calls


def _content_counts(db: Database, workflow_ids: list[str]) -> Counter:
    if not workflow_ids:
        return Counter()
    placeholders = ",".join("?" for _ in workflow_ids)
    rows = db.fetchall(
        f"""SELECT input_json FROM prompt_runs
            WHERE workflow_id IN ({placeholders})
              AND prompt_id='P-WRITE-CONTENT' AND status='PASS'""",
        tuple(workflow_ids),
    )
    return Counter(
        str(((json.loads(row["input_json"]).get("payload") or {}).get("source_section") or {}).get("title") or "")
        for row in rows
    )


def _lifecycle_summary(engine: WorkflowEngine, project_id: str, workflow_id: str) -> list[dict[str, Any]]:
    return [
        {
            "finding_id": item.get("finding_id"),
            "code": (item.get("finding") or {}).get("code"),
            "severity": (item.get("finding") or {}).get("severity"),
            "owner": (item.get("responsibility") or {}).get("owner"),
            "state": (item.get("lifecycle") or {}).get("state"),
            "repair_evidence": (item.get("lifecycle") or {}).get("repair_evidence") or [],
            "review_evidence": (item.get("lifecycle") or {}).get("review_evidence") or [],
        }
        for item in engine.quality_manager.list_findings(project_id, workflow_id=workflow_id)
    ]


async def _run(output_dir: Path) -> dict[str, Any]:
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
    scenario = _install_review_scenario(executor)
    parent = await _finish(engine, project_id, "WF-4_PROPOSAL_AUTHORING", OPTIONS)

    state = parent["state"]
    reviews = state.get("full_proposal_review_history") or []
    lifecycle = _lifecycle_summary(engine, project_id, parent["id"])
    generations = state.get("full_proposal_child_generations") or []
    current_children = list(state.get("authoring_child_workflow_ids") or [])
    archived_children = []
    if generations:
        archived_children = [
            str(record.get("workflow_id"))
            for record in (generations[-1].get("children") or {}).values()
            if record.get("workflow_id")
        ]
    archived_counts = _content_counts(db, archived_children)
    current_counts = _content_counts(db, current_children)
    target_title = "技术路线"

    review_hashes = [str(item.get("candidate_set_hash") or "") for item in reviews]
    review_run_ids = [str(item.get("run_id") or "") for item in reviews]
    routes = {
        str(action.get("finding_code")): str(action.get("route"))
        for review in reviews
        for action in review.get("routing_actions") or []
        if isinstance(action, dict)
    }
    blocking_records = [item for item in lifecycle if item.get("severity") in {"P0", "P1"}]
    matrix = engine.quality_manager.quality_matrix(project_id, workflow_id=parent["id"])
    final_review = reviews[-1] if reviews else {}

    checks = {
        "prerequisites_completed": intake["status"] == template["status"] == "COMPLETED",
        "parent_completed": parent["status"] == "COMPLETED",
        "three_independent_full_reviews": len(reviews) == 3 and len(set(review_run_ids)) == 3,
        "review_status_sequence": [item.get("status") for item in reviews] == ["REVISE", "REVISE", "PASS"],
        "candidate_set_changed_after_each_repair": len(review_hashes) == 3 and len(set(review_hashes)) == 3,
        "all_review_input_snapshots_complete": all(
            int(item.get("section_count") or 0) == len(SECTION_TITLES)
            and len(item.get("section_manifest") or []) == len(SECTION_TITLES)
            and len(item.get("child_workflow_ids") or []) == 5
            for item in reviews
        ),
        "all_final_candidates_have_polish_and_expression_review": all(
            section.get("polish_run_id")
            and section.get("expression_critic_run_id")
            and section.get("polish_run_id") != section.get("expression_critic_run_id")
            for section in final_review.get("section_manifest") or []
        ),
        "writing_finding_routed_to_writer": routes.get("FULL_INTEGRATION_TECHNICAL_ROUTE_CONFLICT") == "WRITING_AGENT",
        "planning_finding_routed_to_planner": routes.get("FULL_INTEGRATION_INFORMATION_OWNERSHIP_CONFLICT") == "PLANNING_AGENT",
        "first_repair_only_rewrote_responsible_section": bool(
            archived_counts
            and archived_counts[target_title] == 2
            and all(archived_counts[title] == 1 for title in SECTION_TITLES if title != target_title)
        ),
        "planning_revision_invalidated_old_generation": bool(
            generations and generations[-1].get("reason") == "INTEGRATION_SECTION_CONTRACT_REVISION"
        ),
        "new_generation_rebuilt_all_sections_once": bool(
            current_counts and all(current_counts[title] == 1 for title in SECTION_TITLES)
        ),
        "all_blocking_findings_verified": bool(blocking_records) and all(item.get("state") == "VERIFIED" for item in blocking_records),
        "repair_and_review_evidence_present": all(
            item.get("repair_evidence") and item.get("review_evidence") for item in blocking_records
        ),
        "final_twelve_dimensions_and_six_chains_pass": bool(final_review and all((final_review.get("checks") or {}).values())),
        "no_open_p0_p1": int(matrix.get("open_blockers", -1)) == 0,
        "simulator_executed_three_reviews": scenario.get("count") == 3,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    prompt_run_count = _export_prompt_evidence(db, project_id, output_dir)
    write_json(output_dir / "input_material_manifest.json", _material_manifest(db, project_id))
    environment = {
        "schema_version": "1.0",
        "generated_at": utc_now(),
        "python": sys.version,
        "platform": platform.platform(),
        "runtime_mode": settings.runtime_mode,
        "source_commit": _source_commit(),
    }
    write_json(output_dir / "environment_manifest.json", environment)
    (output_dir / "source_commit.txt").write_text(environment["source_commit"] + "\n", encoding="utf-8")
    write_json(output_dir / "full_integration_reviews.json", reviews)
    write_json(output_dir / "finding_lifecycle.json", lifecycle)
    write_json(output_dir / "generation_reuse.json", {
        "archived_generations": generations,
        "archived_content_counts": dict(archived_counts),
        "current_child_workflow_ids": current_children,
        "current_content_counts": dict(current_counts),
    })

    status = "PASS" if all(checks.values()) else "FAIL"
    report = {
        "schema_version": "1.0",
        "gate": "FULL_INTEGRATION_CRITIC",
        "status": status,
        "generated_at": utc_now(),
        "source_commit": environment["source_commit"],
        "runtime_mode": settings.runtime_mode,
        "semantic_model_acceptance": False,
        "project_id": project_id,
        "workflow_id": parent["id"],
        "section_count": len(SECTION_TITLES),
        "review_count": len(reviews),
        "prompt_run_count": prompt_run_count,
        "checks": checks,
        "review_run_ids": review_run_ids,
        "candidate_set_hashes": review_hashes,
        "routing": routes,
        "quality_matrix": matrix,
        "invariants": {
            "complete_candidate_set_only": True,
            "final_expression_critic_provenance_required": True,
            "six_argument_chains_and_twelve_dimensions_required": True,
            "earliest_responsible_stage_routing": True,
            "unaffected_sections_reused_for_writing_repair": True,
            "upstream_planning_defect_invalidates_downstream_generation": True,
            "later_independent_full_review_required": True,
            "candidate_set_must_change_after_repair": True,
            "manual_gate_or_database_edit_cannot_close_p0_p1": True,
        },
        "capability_boundary": [
            "该验收使用SIMULATED固定材料和故障注入，证明全文审查编排、确定性硬门、责任路由、返修、独立复审和恢复证据。",
            "真实模型科学判断、真实公开检索和最终DOCX/PDF视觉质量仍由后续X/G3正式能力验收证明。",
        ],
    }
    write_json(output_dir / "FULL_INTEGRATION_CRITIC_ACCEPTANCE.json", report)
    markdown = [
        "# 全文 Integration Critic 验收",
        "",
        f"- 结果：**{status}**",
        f"- 源提交：`{report['source_commit']}`",
        f"- 全文章节：`{report['section_count']}`",
        f"- 全文审查运行：`{report['review_count']}`",
        f"- Prompt Run：`{report['prompt_run_count']}`",
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
    parser = argparse.ArgumentParser(description="Run whole-proposal Integration Critic acceptance.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "recovery_evidence" / "full_integration" / "local",
    )
    args = parser.parse_args()
    report = asyncio.run(_run(args.output_dir.resolve()))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
