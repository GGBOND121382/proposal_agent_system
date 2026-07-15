from __future__ import annotations

import argparse
import json
import os
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
from app.exporter import DocxExporter
from app.s3_evidence import build_s3_evidence, verify_s3_evidence
from app.skill_setup import build_skill_executor
from app.skills.research_claims import validate_public_claims
from app.util import new_id, sha256_json, utc_now, write_json


def _plan() -> dict[str, Any]:
    return {
        "plan_id": "g2-s3-research-plan",
        "task_type": "PUBLIC_RESEARCH",
        "research_questions": [
            "How do recent work, comparable baselines, and limitation mechanisms support an evidence chain?",
            "How does a public evaluation archive preserve reproducible evidence?",
        ],
        "queries": [
            "recent work comparable baseline limitations evidence chain 2021 2026",
            "public evaluation reproducible evidence archive 2021 2026",
        ],
        "source_priorities": ["同行评议论文", "官方项目页面", "公开技术文档"],
        "time_scope": "2021-01-01/2026-12-31",
        "evidence_requirements": ["最近工作", "可比较基线", "局限机制", "可复核证据"],
        "prohibited_inferences": ["不得将集成夹具表述为真实模型语义能力证明"],
    }


def _write_fixture_connector(path: Path) -> Path:
    plan = _plan()
    payload = {
        "run_id": "g2-s3-connector-fixture",
        "connector": "g2-integration-fixture",
        "created_at": "2026-07-15T00:00:00Z",
        "agent_generated_queries": plan["queries"],
        "responses": [
            {
                "query": plan["queries"][0],
                "retrieved_at": "2026-07-15T00:00:00Z",
                "results": [
                    {
                        "source_id": "g2-source-recent-baseline",
                        "title": "G2 Integration Fixture: Recent Work, Baselines, and Limitations",
                        "url": "https://example.org/g2/recent-baseline-limitations",
                        "publisher": "G2 Integration Fixture",
                        "published_at": "2025-03-01",
                        "content_text": (
                            "This integration fixture represents recent work. It compares a reproducible baseline, "
                            "records evaluation limitations, and preserves evidence needed by the G2 orchestration test."
                        ),
                    }
                ],
            },
            {
                "query": plan["queries"][1],
                "retrieved_at": "2026-07-15T00:00:00Z",
                "results": [
                    {
                        "source_id": "g2-source-public-evidence",
                        "title": "G2 Integration Fixture: Public Evaluation Evidence Archive",
                        "url": "https://example.org/g2/public-evaluation-evidence",
                        "publisher": "G2 Integration Fixture",
                        "published_at": "2024-01-01",
                        "content_text": (
                            "This public integration fixture requires source metadata, original snapshots, hashes, "
                            "comparison baselines, documented limitations, and reproducible evaluation evidence."
                        ),
                    }
                ],
            },
        ],
    }
    write_json(path, payload)
    return path


def _insert_project(db: Database, project_id: str, workflow_id: str) -> None:
    now = utc_now()
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (
            project_id,
            "S3 Research Mermaid Export 验收",
            "G2 小规模集成链交付物",
            "INTERNAL",
            json.dumps({"acceptance_scope": "G2_S3"}, ensure_ascii=False),
            now,
            now,
        ),
    )
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (workflow_id, project_id, "G2_S3_ACCEPTANCE", "COMPLETED", 1, "{}", now, now),
    )


def _insert_approved_export_fixture(
    db: Database,
    *,
    project_id: str,
    workflow_id: str,
    figure_marker: str,
    research_output: dict[str, Any],
) -> None:
    # These rows exist only in the isolated G2 acceptance database. Production Gate logic is not modified.
    now = utc_now()
    for gate_type in ("FINAL_CONTENT_SECURITY_APPROVAL", "FINAL_EXPORT_APPROVAL"):
        db.execute(
            """INSERT INTO gates(
                   id,project_id,workflow_id,gate_type,target_id,target_version,context_hash,
                   question_version,required_role,allowed_actions_json,questions_json,
                   security_level,status,decision_json,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                new_id("gate"), project_id, workflow_id, gate_type, "g2-s3-fixture", 1,
                sha256_json({"project_id": project_id, "gate_type": gate_type}), 1,
                "G2_ACCEPTANCE_FIXTURE", json.dumps(["APPROVE"], ensure_ascii=False),
                json.dumps(["仅验证已批准检查点后的 Research + Mermaid + Export 集成。"], ensure_ascii=False),
                "INTERNAL", "APPROVED",
                json.dumps({"action": "APPROVE", "scope": "ISOLATED_G2_ACCEPTANCE_DB"}, ensure_ascii=False),
                now, now,
            ),
        )

    catalog = research_output["source_catalog"]
    references = []
    citation_paragraphs = []
    for index, source in enumerate(catalog, 1):
        references.append(f"[[REFERENCE]][{index}] {source['title']}. {source['url']}")
        citation_paragraphs.append(f"公开资料[{index}]已归档原始快照、文本提取、元数据与哈希，可回溯至对应来源。")
    paragraphs = [
        "本节验证公开调研、证据绑定、技术路线图和正式交付物在同一审计链中闭合。",
        *citation_paragraphs,
        "[[TABLE]]链路节点|硬性验收结果\nResearch|来源归档与 Claim 绑定通过\nMermaid|源码、SVG、PNG 哈希一致\nExport|DOCX、PDF、结构与页面验证通过",
        figure_marker,
        "[[FORMULA]]H_{chain}=SHA256(H_{research} || H_{claim} || H_{figure} || H_{export})",
        *references,
    ]
    candidate_id = "candidate-g2-s3"
    section = {
        "section_id": "g2-s3-section",
        "section_key": "research_mermaid_export",
        "title": "公开调研、图形与交付链",
        "text_hash": sha256_json(paragraphs),
        "contains_table": True,
        "contains_formula": True,
        "contains_image": True,
        "contains_comment": False,
        "contains_revision": False,
    }
    polish_input = {"payload": {"source_section": section}}
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
    critic_input = {"payload": {"polished_candidate": {"candidate_id": candidate_id}}}
    critic_output = {"result": {"status": "PASS", "findings": []}}
    rows = [
        (
            "run-g2-s3-polish", "P-EXPRESSION-POLISH", polish_input, polish_output,
            "2026-07-15T00:00:00+00:00",
        ),
        (
            "run-g2-s3-expression-critic", "P-EXPRESSION-CRITIC", critic_input, critic_output,
            "2026-07-15T00:00:01+00:00",
        ),
    ]
    for run_id, prompt_id, input_data, output_data, created_at in rows:
        db.execute(
            """INSERT INTO prompt_runs(
                   id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,
                   input_hash,output_hash,input_json,output_json,error,duration_ms,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, project_id, workflow_id, prompt_id, "PASS", "G2_ACCEPTANCE_FIXTURE",
                "LOCAL_INTEGRATION", sha256_json(input_data), sha256_json(output_data),
                json.dumps(input_data, ensure_ascii=False), json.dumps(output_data, ensure_ascii=False),
                None, 0, created_at,
            ),
        )


def run(
    output_dir: Path,
    *,
    connector_file: Path | None,
    fixture: bool,
    source_commit: str | None = None,
) -> dict[str, Any]:
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if fixture:
        connector_file = _write_fixture_connector(output_dir / "inputs" / "connector-fixture.json")
        semantic_evidence_mode = "G2_ORCHESTRATION_FIXTURE_NOT_LIVE_SEMANTIC_PROOF"
    elif connector_file is None:
        raise ValueError("connector_file is required unless fixture=True")
    else:
        connector_file = connector_file.resolve()
        semantic_evidence_mode = "APPROVED_CONNECTOR_ARCHIVE"

    base = Settings.load()
    settings = replace(
        base,
        data_dir=output_dir,
        db_path=output_dir / "workflow_checkpoint.sqlite",
        uploads_dir=output_dir / "uploads",
        exports_dir=output_dir / "exports",
        runtime_mode="LIVE",
        public_search_provider="connector",
        public_research_connector_file=str(connector_file),
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
    project_id = "g2-s3-acceptance"
    workflow_id = "wf-g2-s3-acceptance"
    _insert_project(db, project_id, workflow_id)
    executor = build_skill_executor(db, settings)

    research_payload = {
        "provider": "connector",
        "connector_file": str(connector_file),
        "require_structured_plan": True,
        "plan": _plan(),
        "max_results": 20,
    }
    try:
        research_result = executor.execute(
            "public_research.archive",
            research_payload,
            project_id=project_id,
            workflow_id=workflow_id,
            security_level="PUBLIC",
        )
        research_output = research_result.output
        first_source = research_output["sources"][0]
        synthesis = {
            "claims": [
                {
                    "claim_id": "public-claim-g2-s3",
                    "claim_text": "公开资料要求以可比较基线、局限记录和可复核证据支撑评价结论。",
                    "claim_type": "PUBLIC_CLAIM",
                    "subject_id": "evidence-chain",
                    "temporal_status": "CURRENT",
                    "qualifiers": ["MODEL_SYNTHESIS"],
                    "numeric_values": [],
                    "source_refs": [first_source],
                    "knowledge_status": "DOCUMENT_EXTRACTED",
                    "security_level": "PUBLIC",
                }
            ],
            "source_comparisons": [],
            "conflicts": [],
            "limitations": ["G2 fixture only verifies orchestration and artifact integrity." if fixture else ""],
            "coverage_summary": "最近工作、可比较基线、局限机制和证据归档均已覆盖。",
        }
        claim_report = validate_public_claims(synthesis, research_output)
        claim_report_path = output_dir / "claim_bindings" / "public-claim-bindings.json"
        write_json(claim_report_path, claim_report)
        if claim_report["status"] != "PASS":
            raise RuntimeError(f"PUBLIC_CLAIM binding blocked S3: {claim_report['findings']}")

        bound_ids = claim_report["bindings"][0]["source_ids"]
        mermaid_source = (
            "flowchart TB\n"
            "  R[公开检索与来源归档] --> C[PUBLIC_CLAIM 证据绑定]\n"
            "  C --> M[Mermaid 技术路线图]\n"
            "  M --> D[DOCX 导出]\n"
            "  M --> P[PDF 转换]\n"
            "  D --> V[结构与页面视觉验收]\n"
            "  P --> V"
        )
        diagram_payload = {
            "section_id": "g2-s3-research-route",
            "caption": "图1 Research、Mermaid 与交付物证据链",
            "width_cm": 14.0,
            "mermaid_source": mermaid_source,
            "argument_purpose": "展示公开来源到最终交付物的可追溯链路",
            "claim_id": "public-claim-g2-s3",
            "evidence_ids": bound_ids,
            "section_contract_id": "g2-s3-section-contract",
        }
        first_diagram = executor.execute(
            "mermaid.render",
            diagram_payload,
            project_id=project_id,
            workflow_id=workflow_id,
            security_level="INTERNAL",
        )
        second_diagram = executor.execute(
            "mermaid.render",
            diagram_payload,
            project_id=project_id,
            workflow_id=workflow_id,
            security_level="INTERNAL",
        )
        for key in ("source_sha256", "svg_sha256", "png_sha256"):
            if first_diagram.output[key] != second_diagram.output[key]:
                raise RuntimeError(f"Mermaid repeat render hash drift: {key}")
        if not second_diagram.output.get("cache_hit"):
            raise RuntimeError("Mermaid repeat render did not hit verified cache")

        _insert_approved_export_fixture(
            db,
            project_id=project_id,
            workflow_id=workflow_id,
            figure_marker=second_diagram.output["figure_marker"],
            research_output=research_output,
        )
        exporter = DocxExporter(db, settings)
        document_path = exporter.export(project_id)
        export_package = exporter.export_package(project_id, document_path)
        pdf_path = document_path.with_suffix(".pdf")
        delivery_report_path = document_path.with_suffix(".delivery-validation.json")
        delivery_report = json.loads(delivery_report_path.read_text(encoding="utf-8"))
        delivery_report["report_path"] = str(delivery_report_path)

        report = build_s3_evidence(
            data_dir=output_dir,
            run_dir=output_dir / "acceptance",
            research_output=research_output,
            claim_report=claim_report,
            claim_report_path=claim_report_path,
            diagrams=[second_diagram.output],
            document_path=document_path,
            pdf_path=pdf_path,
            delivery_report=delivery_report,
            export_package_path=export_package,
            database_path=settings.db_path,
            semantic_evidence_mode=semantic_evidence_mode,
            source_commit=source_commit or os.getenv("GITHUB_SHA"),
        )
        restart = verify_s3_evidence(Path(report["report_path"]), output_dir)
        if restart["status"] != "PASS":
            raise RuntimeError(f"S3 restart verification failed: {restart['failures']}")
        restart_path = output_dir / "acceptance" / "S3_RESTART_VERIFY.json"
        write_json(restart_path, restart)
        report["restart_verification"] = restart
        report["restart_report_path"] = str(restart_path)
        return report
    finally:
        mermaid = executor.registry.get("mermaid.render")
        close = getattr(mermaid, "close", None)
        if close:
            close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the G2 S3 Research + Mermaid + Export acceptance chain.")
    parser.add_argument("--output-dir", type=Path, default=Path("recovery_evidence/s3/local"))
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--connector-file", type=Path)
    group.add_argument(
        "--fixture",
        action="store_true",
        help="Use a deterministic G2 integration fixture. This is orchestration evidence, not LIVE semantic proof.",
    )
    parser.add_argument("--source-commit")
    args = parser.parse_args()
    result = run(
        args.output_dir,
        connector_file=args.connector_file,
        fixture=args.fixture,
        source_commit=args.source_commit,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
