from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .runtime_policy import CapabilityModeError, CapabilityPolicy
from .util import sha256_bytes, sha256_json, utc_now, write_json


FORBIDDEN_RUNTIME_MARKERS = ("replay", "mock", "simulated", "static", "sample")
LIVE_RESEARCH_MODES = {"LIVE_CROSSREF", "LIVE_SEARXNG", "LIVE_CONNECTOR_ARCHIVE"}
REQUIRED_WORKFLOWS = {
    "WF-1_PROJECT_INTAKE",
    "WF-2_TEMPLATE_EXTRACTION",
    "WF-3_HYBRID_ONLINE_ASSIST",
    "WF-4_PROPOSAL_AUTHORING",
    "WF-5_SECURITY_REVIEW_AND_EXPORT",
}
REQUIRED_PROMPTS = {
    "P-SECURITY-CLASSIFY", "P-SECURITY-CLASSIFY-CRITIC",
    "P-SCHEME-EXTRACT", "P-SCHEME-CRITIC",
    "P-PROJECT-DEFINITION-EXTRACT", "P-PROJECT-DEFINITION-CRITIC",
    "P-FACT-EXTRACT", "P-FACT-CRITIC", "P-PROJECT-READINESS-CRITIC",
    "P-TEMPLATE-EXTRACT", "P-TEMPLATE-CRITIC",
    "P-SAFE-ONLINE-PACKAGE", "P-SAFE-ONLINE-PACKAGE-CRITIC",
    "P-PUBLIC-RESEARCH-PLAN", "P-PUBLIC-RESEARCH-SYNTHESIS",
    "P-PUBLIC-RESEARCH-CRITIC", "P-ONLINE-RESULT-IMPORT-CRITIC",
    "P-ARGUMENT-ARCHITECTURE", "P-ARGUMENT-ARCHITECTURE-CRITIC",
    "P-REVISION-PLAN", "P-REVISION-PLAN-CRITIC",
    "P-WRITE-BLUEPRINT", "P-WRITE-BLUEPRINT-CRITIC",
    "P-WRITE-CONTENT", "P-WRITE-CRITIC",
    "P-EXPRESSION-POLISH", "P-EXPRESSION-CRITIC",
    "P-INTEGRATION-CRITIC", "P-FINAL-CONFIDENTIALITY-REVIEW",
}


class G3AcceptanceError(RuntimeError):
    pass


@dataclass(frozen=True)
class G3Preflight:
    status: str
    checks: dict[str, bool]
    errors: list[str]
    environment: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "stage": "G3_PREFLIGHT",
            "status": self.status,
            "checked_at": utc_now(),
            "checks": self.checks,
            "errors": self.errors,
            "environment": self.environment,
        }


def _configured(name: str) -> bool:
    value = os.getenv(name, "").strip()
    if not value:
        return False
    lowered = value.lower()
    return not any(token in lowered for token in ("change_me", "example", "placeholder", "your_", "<", ">"))


def preflight_environment() -> G3Preflight:
    errors: list[str] = []
    runtime_mode = os.getenv("MODEL_RUNTIME_MODE", "").upper()
    provider = os.getenv("PUBLIC_SEARCH_PROVIDER", "").lower()
    policy = CapabilityPolicy.from_environment()
    try:
        policy.assert_environment(runtime_mode)
        policy_ok = policy.enabled and runtime_mode == "LIVE"
        if not policy.enabled:
            errors.append("CAPABILITY_ACCEPTANCE_MODE is not enabled")
        elif runtime_mode != "LIVE":
            errors.append(f"MODEL_RUNTIME_MODE must be LIVE, received {runtime_mode or '<empty>'}")
    except CapabilityModeError as exc:
        policy_ok = False
        errors.append(str(exc))
    checks = {
        "capability_policy_live": policy_ok,
        "offline_base_url_configured": _configured("OFFLINE_LLM_BASE_URL"),
        "offline_general_model_configured": _configured("OFFLINE_GENERAL_MODEL"),
        "offline_critic_model_configured": _configured("OFFLINE_CRITIC_MODEL"),
        "online_base_url_configured": _configured("ONLINE_LLM_BASE_URL"),
        "online_public_model_configured": _configured("ONLINE_PUBLIC_MODEL"),
        "live_public_research_provider": provider in {"crossref", "searxng"},
        "operator_attested": os.getenv("G3_OPERATOR_ATTESTATION", "").strip().upper() in {"TRUE", "YES", "ATTESTED", "USER_REQUESTED"},
        "operator_identified": bool(os.getenv("G3_OPERATOR_ID", "").strip()),
        "real_model_endpoint_attested": os.getenv("G3_MODEL_PROVENANCE_ATTESTATION", "").strip().upper() == "REAL_MODEL_ENDPOINT",
    }
    for name, passed in checks.items():
        if not passed:
            errors.append(f"G3 preflight failed: {name}")
    return G3Preflight(
        status="PASS" if all(checks.values()) else "BLOCKED_CONFIGURATION",
        checks=checks,
        errors=errors,
        environment={
            "runtime_mode": runtime_mode,
            "public_search_provider": provider,
            "offline_base_url_host": _redacted_host(os.getenv("OFFLINE_LLM_BASE_URL", "")),
            "online_base_url_host": _redacted_host(os.getenv("ONLINE_LLM_BASE_URL", "")),
            "offline_general_model": os.getenv("OFFLINE_GENERAL_MODEL", ""),
            "offline_critic_model": os.getenv("OFFLINE_CRITIC_MODEL", ""),
            "online_public_model": os.getenv("ONLINE_PUBLIC_MODEL", ""),
            "operator_id": os.getenv("G3_OPERATOR_ID", ""),
            "model_provenance_attestation": os.getenv("G3_MODEL_PROVENANCE_ATTESTATION", ""),
        },
    )


def _redacted_host(url: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return parsed.netloc or ""


def _is_real_identifier(value: Any) -> bool:
    normalized = str(value or "").strip().lower()
    return bool(normalized) and not any(marker in normalized for marker in FORBIDDEN_RUNTIME_MARKERS)


def validate_g3_run(
    *,
    db,
    settings,
    project_id: str,
    workflow_ids: dict[str, str],
    cross_chapter_reviews: list[dict[str, Any]],
    post_export_report: dict[str, Any],
    output_dir: Path,
    source_commit: str,
) -> dict[str, Any]:
    errors: list[str] = []
    workflows = db.fetchall(
        "SELECT id,workflow_type,status,state_json FROM workflows WHERE project_id=? ORDER BY created_at,id",
        (project_id,),
    )
    top_level = []
    for row in workflows:
        state = json.loads(row.get("state_json") or "{}")
        if not state.get("parent_workflow_id"):
            top_level.append({**row, "state": state})
    completed_types = {row["workflow_type"] for row in top_level if row["status"] == "COMPLETED"}

    runs = db.fetchall(
        "SELECT id,workflow_id,prompt_id,status,model_id,endpoint_id,input_hash,output_hash,created_at FROM prompt_runs WHERE project_id=? ORDER BY created_at,id",
        (project_id,),
    )
    prompt_ids = {str(row.get("prompt_id") or "") for row in runs if row.get("status") != "ERROR"}
    invalid_runs = [
        row for row in runs
        if row.get("status") == "ERROR"
        or not _is_real_identifier(row.get("model_id"))
        or not _is_real_identifier(row.get("endpoint_id"))
    ]

    parent_id = workflow_ids.get("WF-4_PROPOSAL_AUTHORING")
    parent = next((row for row in top_level if row["id"] == parent_id), None)
    parent_state = (parent or {}).get("state") or {}
    research_id = workflow_ids.get("WF-3_HYBRID_ONLINE_ASSIST")
    research_workflow = next((row for row in top_level if row["id"] == research_id), None)
    research_state = (research_workflow or {}).get("state") or {}
    contract = parent_state.get("full_proposal_contract") or {}
    final_reviews = parent_state.get("full_proposal_review_history") or []
    final_review = final_reviews[-1] if final_reviews else {}

    research_manifests = list(Path(settings.data_dir).glob(f"research_archive/{project_id}/*/manifest.json"))
    research_reports: list[dict[str, Any]] = []
    for path in research_manifests:
        try:
            research_reports.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:
            errors.append(f"research manifest unreadable {path}: {exc}")
    live_research = [item for item in research_reports if item.get("retrieval_mode") in LIVE_RESEARCH_MODES]
    source_count = sum(int(item.get("source_count") or 0) for item in live_research)

    trace_rows = db.fetchall(
        "SELECT content_json FROM artifacts WHERE project_id=? AND artifact_type='PROMPT_TRACE'",
        (project_id,),
    )
    raw_trace_ok = True
    for row in trace_rows:
        payload = json.loads(row.get("content_json") or "{}")
        if not str(payload.get("raw_response_text") or "").strip():
            raw_trace_ok = False
            break
        if not _is_real_identifier(payload.get("model_id")) or not _is_real_identifier(payload.get("endpoint_id")):
            raw_trace_ok = False
            break

    quality_matrix = __import__("app.quality", fromlist=["QualityLifecycleManager"]).QualityLifecycleManager(db).quality_matrix(project_id)
    post_checks = post_export_report.get("checks") or {}
    structure_path = Path(str((post_export_report.get("artifacts") or {}).get("structure_report") or (post_export_report.get("structure_report") or {}).get("path") or ""))
    visual_path = Path(str((post_export_report.get("artifacts") or {}).get("visual_report") or (post_export_report.get("visual_report") or {}).get("path") or ""))
    structure = json.loads(structure_path.read_text(encoding="utf-8")) if structure_path.is_file() else {}
    visual = json.loads(visual_path.read_text(encoding="utf-8")) if visual_path.is_file() else {}
    parity = structure.get("candidate_parity") or {}

    review_windows = [tuple(item.get("section_ids") or []) for item in cross_chapter_reviews]
    flattened = [section_id for window in review_windows for section_id in window]
    contract_ids = [str(item.get("section_id")) for item in contract.get("sections") or []]
    checks = {
        "runtime_is_live": str(settings.runtime_mode).upper() == "LIVE",
        "capability_mode_enabled": CapabilityPolicy.from_environment().enabled,
        "all_five_top_level_workflows_completed": REQUIRED_WORKFLOWS <= completed_types,
        "all_required_prompts_executed": REQUIRED_PROMPTS <= prompt_ids,
        "no_error_or_nonlive_prompt_runs": bool(runs) and not invalid_runs,
        "all_prompt_traces_present": len(trace_rows) == len(runs) and raw_trace_ok,
        "real_public_research_archived": bool(live_research) and source_count >= 8,
        "public_claim_validation_passed": (research_state.get("public_claim_validation") or {}).get("status") == "PASS",
        "fourteen_section_contract": len(contract_ids) == 14,
        "five_concurrent_groups": len(contract.get("groups") or []) == 5,
        "all_sections_completed": len(parent_state.get("section_results") or []) == len(contract_ids) == 14,
        "cross_chapter_windows_complete": len(cross_chapter_reviews) == 5 and flattened == contract_ids,
        "cross_chapter_reviews_passed": bool(cross_chapter_reviews) and all(item.get("status") == "PASS" for item in cross_chapter_reviews),
        "cross_chapter_reviews_independent": len({item.get("run_id") for item in cross_chapter_reviews}) == len(cross_chapter_reviews),
        "full_integration_critic_passed": final_review.get("status") == "PASS" and all((final_review.get("checks") or {}).values()),
        "no_open_p0_p1": int(quality_matrix.get("open_blockers", -1)) == 0,
        "post_export_passed": post_export_report.get("status") == "PASS" and all(post_checks.values()),
        "docx_pdf_candidate_parity": int(parity.get("docx_missing_unit_count", -1)) == 0 and int(parity.get("pdf_missing_unit_count", -1)) == 0,
        "figures_and_tables_complete": int(parity.get("expected_figure_count", 0)) == int(parity.get("actual_figure_count", -1)) and int(parity.get("expected_table_count", 0)) == int(parity.get("actual_table_count", -1)),
        "minimum_editable_figures_present": int(parity.get("actual_figure_count", 0)) >= 3,
        "minimum_structured_tables_present": int(parity.get("actual_table_count", 0)) >= 3,
        "visual_delivery_readable": visual.get("status") == "PASS" and int(visual.get("page_count") or 0) > 0,
        "conclusion_and_document_type_closed": bool((final_review.get("checks") or {}).get("central_proposition_covered")) and bool((final_review.get("checks") or {}).get("document_type_clean")),
        "checkpoint_reusable": bool(post_export_report.get("reused_after_restart") or post_export_report.get("checks", {}).get("restart_reused_verified_attempt", True)),
    }
    for name, passed in checks.items():
        if not passed:
            errors.append(name)

    report = {
        "schema_version": "1.0",
        "gate": "G3",
        "status": "PASS" if all(checks.values()) else "FAIL",
        "source_commit": source_commit,
        "project_id": project_id,
        "workflow_ids": workflow_ids,
        "created_at": utc_now(),
        "checks": checks,
        "metrics": {
            "prompt_run_count": len(runs),
            "trace_count": len(trace_rows),
            "research_archive_count": len(live_research),
            "research_source_count": source_count,
            "section_count": len(contract_ids),
            "concurrent_group_count": len(contract.get("groups") or []),
            "cross_chapter_review_count": len(cross_chapter_reviews),
            "full_integration_review_count": len(final_reviews),
            "repair_run_count": sum(1 for row in runs if row.get("prompt_id") == "P-TARGETED-REPAIR"),
            "pdf_page_count": int(visual.get("page_count") or 0),
            "figure_count": int(parity.get("actual_figure_count") or 0),
            "table_count": int(parity.get("actual_table_count") or 0),
            "open_blocker_count": int(quality_matrix.get("open_blockers", -1)),
        },
        "cross_chapter_reviews": cross_chapter_reviews,
        "full_integration_review": final_review,
        "post_export_report": post_export_report,
        "errors": errors,
    }
    report["report_hash"] = sha256_json({k: v for k, v in report.items() if k != "report_hash"})
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "G3_ACCEPTANCE.json", report)
    (output_dir / "G3_ACCEPTANCE.md").write_text(
        "# G3 正式能力验收\n\n"
        f"- 状态：**{report['status']}**\n"
        f"- Prompt Run：{report['metrics']['prompt_run_count']}\n"
        f"- 真实公开来源：{source_count}\n"
        f"- 完整章节：{report['metrics']['section_count']}\n"
        f"- 每三章审查：{report['metrics']['cross_chapter_review_count']}\n"
        f"- PDF 页数：{report['metrics']['pdf_page_count']}\n"
        f"- 开放 P0/P1：{report['metrics']['open_blocker_count']}\n",
        encoding="utf-8",
    )
    return report


def file_record(path: Path) -> dict[str, Any]:
    return {
        "path": str(path.resolve()),
        "filename": path.name,
        "size_bytes": path.stat().st_size,
        "sha256": sha256_bytes(path.read_bytes()),
    }
