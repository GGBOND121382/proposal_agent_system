from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.db import Database
from app.research_mermaid_export import ResearchMermaidExportPipeline
from app.util import new_id, sha256_json, utc_now, write_json


PROJECT_ID = "s3-acceptance-project"
WORKFLOW_ID = "s3-acceptance-workflow"


def _research_plan() -> dict[str, Any]:
    return {
        "plan_id": "s3-plan-001",
        "task_type": "PUBLIC_RESEARCH",
        "research_questions": [
            "公开研究证据如何通过可复核归档、基线比较和局限记录支撑申请书论证？",
            "artifact review evidence reproducibility source evaluation process 如何要求工件、来源和评价过程具备可重复核验能力？",
        ],
        "queries": [
            "公开研究证据 可复核归档 基线比较 局限记录 申请书论证",
            "artifact review evidence reproducibility source evaluation process",
        ],
        "source_priorities": ["官方机构页面", "出版组织规范", "同行评议论文"],
        "time_scope": "2021-01-01/2026-12-31",
        "evidence_requirements": ["最近工作", "可比较基线", "局限机制"],
        "prohibited_inferences": ["不得把工程验收夹具表述为实时公开检索结论"],
    }


def _write_connector_fixture(path: Path, plan: dict[str, Any]) -> Path:
    payload = {
        "run_id": "s3-approved-connector-001",
        "connector": "approved-integration-fixture",
        "created_at": "2026-07-15T00:00:00Z",
        "agent_generated_queries": plan["queries"],
        "responses": [
            {
                "query": plan["queries"][0],
                "retrieved_at": "2026-07-15T00:00:00Z",
                "results": [
                    {
                        "source_id": "src-public-evidence",
                        "title": "Reproducibility and Replicability in Science",
                        "url": "https://www.nationalacademies.org/publications/25303",
                        "publisher": "National Academies of Sciences, Engineering, and Medicine",
                        "published_at": "2025-01-01",
                        "content_text": (
                            "This public evidence record is an integration fixture. It describes "
                            "reproducible evidence retention, comparison baselines, documented "
                            "limitations, and review mechanisms used to test source traceability."
                        ),
                        "verification": {"status": "RECORDED_INTEGRATION_FIXTURE"},
                    }
                ],
            },
            {
                "query": plan["queries"][1],
                "retrieved_at": "2026-07-15T00:00:00Z",
                "results": [
                    {
                        "source_id": "src-artifact-policy",
                        "title": "Artifact Review and Badging",
                        "url": "https://www.acm.org/publications/policies/artifact-review-and-badging-current",
                        "publisher": "Association for Computing Machinery",
                        "published_at": "2025-01-01",
                        "content_text": (
                            "This public policy record is an integration fixture. It covers "
                            "artifact availability, repeatable evaluation, evidence review, "
                            "comparison baselines, and explicit limitations."
                        ),
                        "verification": {"status": "RECORDED_INTEGRATION_FIXTURE"},
                    }
                ],
            },
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, payload)
    return path


def _seed_database(db: Database) -> None:
    now = utc_now()
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (
            PROJECT_ID,
            "公开研究证据链验证项目",
            "验证公开来源、论证图与最终交付物之间的可追溯关系。",
            "PUBLIC",
            json.dumps({"acceptance": "S3"}, ensure_ascii=False),
            now,
            now,
        ),
    )
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            WORKFLOW_ID,
            PROJECT_ID,
            "S3_RESEARCH_MERMAID_EXPORT",
            "RUNNING",
            0,
            "{}",
            now,
            now,
        ),
    )


def _build_synthesis(research_output: dict[str, Any]) -> dict[str, Any]:
    refs = {str(item["source_id"]): dict(item) for item in research_output["sources"]}
    return {
        "claims": [
            {
                "claim_id": "claim-evidence-retention",
                "claim_text": "公开研究论证需要保留可复核的来源工件与证据层级。",
                "claim_type": "PUBLIC_CLAIM",
                "subject_id": None,
                "temporal_status": "CURRENT",
                "qualifiers": ["ENGINEERING_INTEGRATION_FIXTURE"],
                "numeric_values": [],
                "source_refs": [refs["src-public-evidence"]],
                "knowledge_status": "DOCUMENT_EXTRACTED",
                "security_level": "PUBLIC",
            },
            {
                "claim_id": "claim-artifact-review",
                "claim_text": "评价过程应绑定可重复核验的工件、基线与局限记录。",
                "claim_type": "PUBLIC_CLAIM",
                "subject_id": None,
                "temporal_status": "CURRENT",
                "qualifiers": ["ENGINEERING_INTEGRATION_FIXTURE"],
                "numeric_values": [],
                "source_refs": [refs["src-artifact-policy"]],
                "knowledge_status": "DOCUMENT_EXTRACTED",
                "security_level": "PUBLIC",
            },
        ],
        "source_comparisons": [
            {
                "comparison_id": "comparison-s3",
                "source_ids": ["src-public-evidence", "src-artifact-policy"],
                "comparison": "两类公开来源分别支撑证据留存和工件复核。",
            }
        ],
        "conflicts": [],
        "limitations": ["本次仅证明工程集成和可恢复性，不替代 LIVE 公开检索能力验收。"],
        "coverage_summary": "覆盖来源留存、基线比较、局限记录和工件复核。",
    }


def _diagram_specs() -> list[dict[str, Any]]:
    return [
        {
            "section_id": "public-evidence-chain",
            "caption": "公开研究证据链",
            "width_cm": 13.5,
            "mermaid_source": (
                "flowchart TB\n"
                "  A[公开来源] --> B[原始快照与哈希]\n"
                "  B --> C[PUBLIC_CLAIM绑定]\n"
                "  C --> D[申请书论证]\n"
                "  D --> E[DOCX与PDF验收]"
            ),
            "argument_purpose": "说明公开来源如何进入可核验论证和最终交付物",
            "claim_ids": ["claim-evidence-retention", "claim-artifact-review"],
            "source_ids": ["src-public-evidence", "src-artifact-policy"],
            "section_contract_id": "section-contract-s3",
        },
        {
            "section_id": "research-delivery-gates",
            "caption": "研究到交付的质量关卡",
            "width_cm": 13.5,
            "mermaid_source": (
                "flowchart TB\n"
                "  R[研究计划] --> S[来源归档]\n"
                "  S --> C[命题绑定]\n"
                "  C --> M[论证图渲染]\n"
                "  M --> Q[表达审查]\n"
                "  Q --> X[结构与视觉验收]"
            ),
            "argument_purpose": "说明研究、图形和导出链的失败闭锁关卡",
            "claim_ids": ["claim-artifact-review"],
            "source_ids": ["src-artifact-policy"],
            "section_contract_id": "section-contract-s3",
        },
    ]


def _approve_content(db: Database, markers: list[str]) -> None:
    polish_id = new_id("prompt-run")
    critic_id = new_id("prompt-run")
    candidate_id = "candidate-s3-001"
    paragraphs = [
        "公开研究材料只有在保留来源快照、访问元数据和内容哈希后，才能进入可复核的论证过程[1]。",
        markers[0],
        "面向最终交付，来源命题、论证图和章节正文需要经过同一质量链约束，避免图文脱节或引用失配[2]。",
        markers[1],
        "[[TABLE]]环节|确定性验收要求\n公开研究|来源、正文和元数据哈希一致\n论证图|命题与来源绑定且图形可重复渲染\n交付物|DOCX、PDF、结构和页面视觉检查全部通过",
        "[[FORMULA]]Q = w_r R + w_m M + w_d D",
        "[[REFERENCE]][1] National Academies of Sciences, Engineering, and Medicine. Reproducibility and Replicability in Science.",
        "[[REFERENCE]][2] Association for Computing Machinery. Artifact Review and Badging.",
    ]
    polish_input = {
        "payload": {
            "source_section": {
                "section_id": "section-public-research",
                "section_key": "public_research",
                "title": "公开研究与可核验交付链",
                "text_hash": sha256_json(paragraphs),
                "contains_table": True,
                "contains_formula": True,
                "contains_image": True,
            }
        }
    }
    polish_output = {
        "result": {
            "candidate_id": candidate_id,
            "candidate_text": "\n".join(paragraphs),
            "paragraphs": [
                {"sequence": index, "text": text}
                for index, text in enumerate(paragraphs, 1)
            ],
        }
    }
    critic_input = {
        "payload": {
            "polished_candidate": {"candidate_id": candidate_id},
        }
    }
    critic_output = {
        "result": {
            "decision": "PASS",
            "candidate_id": candidate_id,
            "findings": [],
        }
    }
    db.execute(
        "INSERT INTO prompt_runs(id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,input_hash,output_hash,input_json,output_json,error,duration_ms,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            polish_id,
            PROJECT_ID,
            WORKFLOW_ID,
            "P-EXPRESSION-POLISH",
            "PASS",
            "acceptance-fixture",
            "local",
            sha256_json(polish_input),
            sha256_json(polish_output),
            json.dumps(polish_input, ensure_ascii=False),
            json.dumps(polish_output, ensure_ascii=False),
            None,
            1,
            "2026-07-15T00:00:01Z",
        ),
    )
    db.execute(
        "INSERT INTO prompt_runs(id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,input_hash,output_hash,input_json,output_json,error,duration_ms,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            critic_id,
            PROJECT_ID,
            WORKFLOW_ID,
            "P-EXPRESSION-CRITIC",
            "PASS",
            "acceptance-fixture",
            "local",
            sha256_json(critic_input),
            sha256_json(critic_output),
            json.dumps(critic_input, ensure_ascii=False),
            json.dumps(critic_output, ensure_ascii=False),
            None,
            1,
            "2026-07-15T00:00:02Z",
        ),
    )
    now = utc_now()
    for gate_type in ("FINAL_CONTENT_SECURITY_APPROVAL", "FINAL_EXPORT_APPROVAL"):
        db.execute(
            "INSERT INTO gates(id,project_id,workflow_id,gate_type,target_id,target_version,context_hash,question_version,required_role,allowed_actions_json,questions_json,security_level,status,decision_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                new_id("gate"),
                PROJECT_ID,
                WORKFLOW_ID,
                gate_type,
                candidate_id,
                1,
                sha256_json({"gate_type": gate_type, "candidate_id": candidate_id}),
                1,
                "APPROVER",
                json.dumps(["APPROVE", "REJECT"], ensure_ascii=False),
                json.dumps([], ensure_ascii=False),
                "PUBLIC",
                "APPROVED",
                json.dumps({"action": "APPROVE", "acceptance_fixture": True}, ensure_ascii=False),
                now,
                now,
            ),
        )


def run(output_dir: Path) -> dict[str, Any]:
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    base = Settings.load()
    settings = replace(
        base,
        data_dir=output_dir,
        db_path=output_dir / "proposal_agents.sqlite3",
        uploads_dir=output_dir / "uploads",
        exports_dir=output_dir / "exports",
        runtime_mode="REPLAY",
        public_search_provider="connector",
        mermaid_browser_executable=(
            base.mermaid_browser_executable
            or shutil.which("chromium")
            or shutil.which("chromium-browser")
            or shutil.which("google-chrome")
            or ""
        ),
    )
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    settings.exports_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.db_path)
    _seed_database(db)
    pipeline = ResearchMermaidExportPipeline(db, settings)

    plan = _research_plan()
    connector = _write_connector_fixture(output_dir / "inputs" / "connector.json", plan)
    request = {
        "provider": "connector",
        "connector_file": str(connector),
        "max_results": 20,
    }
    research_output = pipeline.research(
        project_id=PROJECT_ID,
        workflow_id=WORKFLOW_ID,
        security_level="PUBLIC",
        research_plan=plan,
        research_request=request,
    )
    synthesis = _build_synthesis(research_output)
    prepared = pipeline.prepare(
        project_id=PROJECT_ID,
        workflow_id=WORKFLOW_ID,
        security_level="PUBLIC",
        research_plan=plan,
        research_request=request,
        research_output=research_output,
        synthesis=synthesis,
        diagrams=_diagram_specs(),
        acceptance_mode="RECORDED_CONNECTOR_ENGINEERING_INTEGRATION",
    )
    _approve_content(db, prepared["required_figure_markers"])
    result = pipeline.finalize(project_id=PROJECT_ID, checkpoint=prepared)

    audit_counts = {
        row["event_type"]: row["count"]
        for row in db.fetchall(
            "SELECT event_type, COUNT(*) AS count FROM audit_events GROUP BY event_type"
        )
    }
    report = {
        "schema_version": "1.0",
        "status": result["status"],
        "acceptance_scope": "RECORDED_CONNECTOR_ENGINEERING_INTEGRATION",
        "semantic_capability_proof": False,
        "limitation": (
            "This acceptance uses an approved recorded connector fixture to prove the "
            "Research + Mermaid + DOCX/PDF integration, restart and hash gates. It does "
            "not claim LIVE search or model semantic quality."
        ),
        "source_count": prepared["research"]["source_count"],
        "claim_binding_status": prepared["claim_binding"]["status"],
        "diagram_count": len(prepared["diagrams"]),
        "delivery": result["delivery"],
        "evidence_bundle": result["evidence_bundle"],
        "result_manifest": result["result_manifest"],
        "audit_counts": audit_counts,
        "created_at": utc_now(),
    }
    report_path = output_dir / "S3_ACCEPTANCE_REPORT.json"
    write_json(report_path, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("recovery_evidence/s3/local"),
    )
    args = parser.parse_args()
    report = run(args.output_dir.resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
