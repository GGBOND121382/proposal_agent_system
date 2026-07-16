from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .runtime_policy import CapabilityPolicy
from .skills.research_audit import verify_research_archive
from .util import sha256_bytes, sha256_json, utc_now


FORBIDDEN_MODEL_MARKERS = {"replay", "mock", "simulated", "static"}
LIVE_RESEARCH_MODES = {"LIVE_CROSSREF", "LIVE_SEARXNG"}


@dataclass(frozen=True)
class G3Preflight:
    status: str
    checks: dict[str, bool]
    missing: list[str]
    summary: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checks": self.checks,
            "missing": self.missing,
            "summary": self.summary,
        }


def _configured(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text and text.upper() not in {"CHANGE_ME", "NONE", "NULL"})


def g3_preflight(settings, pack) -> G3Preflight:
    policy = CapabilityPolicy.from_environment()
    endpoints = {str(item.get("endpoint_id")): item for item in pack.endpoints.get("endpoints", [])}
    models = {str(item.get("model_id")): item for item in pack.models.get("models", [])}
    required_env = {
        "OFFLINE_LLM_API_KEY": os.getenv("OFFLINE_LLM_API_KEY"),
        "ONLINE_LLM_API_KEY": os.getenv("ONLINE_LLM_API_KEY"),
    }
    checks = {
        "capability_mode_enabled": policy.enabled,
        "runtime_is_live": str(settings.runtime_mode).upper() == "LIVE",
        "direct_live_research_provider": str(settings.public_search_provider).lower() in {"crossref", "searxng"},
        "offline_endpoint_configured": _configured((endpoints.get("offline-primary") or {}).get("base_url")),
        "online_endpoint_configured": _configured((endpoints.get("online-public-primary") or {}).get("base_url")),
        "offline_general_model_configured": _configured((models.get("offline-general-primary") or {}).get("provider_model_name")),
        "offline_critic_model_configured": _configured((models.get("offline-critic-primary") or {}).get("provider_model_name")),
        "online_model_configured": _configured((models.get("online-public-primary") or {}).get("provider_model_name")),
        "offline_credential_available": _configured(required_env["OFFLINE_LLM_API_KEY"]),
        "online_credential_available": _configured(required_env["ONLINE_LLM_API_KEY"]),
        "libreoffice_available": bool(shutil.which("libreoffice") or shutil.which("soffice")),
        "pdftoppm_available": bool(shutil.which("pdftoppm")),
        "browser_available": bool(
            settings.mermaid_browser_executable
            or shutil.which("chromium")
            or shutil.which("chromium-browser")
            or shutil.which("google-chrome")
        ),
    }
    missing = [name for name, passed in checks.items() if not passed]
    return G3Preflight(
        status="READY" if not missing else "BLOCKED_CONFIGURATION",
        checks=checks,
        missing=missing,
        summary={
            "runtime_mode": settings.runtime_mode,
            "public_search_provider": settings.public_search_provider,
            "offline_endpoint_id": "offline-primary",
            "online_endpoint_id": "online-public-primary",
            "credentials_reported_as_presence_only": True,
        },
    )


def _latest_workflow(db, project_id: str, workflow_type: str) -> dict[str, Any] | None:
    row = db.fetchone(
        "SELECT * FROM workflows WHERE project_id=? AND workflow_type=? ORDER BY created_at DESC LIMIT 1",
        (project_id, workflow_type),
    )
    if not row:
        return None
    value = dict(row)
    value["state"] = json.loads(value.pop("state_json"))
    return value


def _latest_research_manifest(db, project_id: str) -> Path | None:
    rows = db.fetchall(
        "SELECT output_json FROM skill_runs WHERE project_id=? AND skill_id='public_research.archive' AND status='PASS' ORDER BY created_at DESC",
        (project_id,),
    )
    for row in rows:
        output = json.loads(row.get("output_json") or "{}")
        path = Path(str(output.get("archive_manifest") or ""))
        if path.is_file():
            return path
    return None


def evaluate_g3(
    *,
    db,
    project_id: str,
    output_dir: Path,
    post_export: dict[str, Any],
    source_commit: str,
) -> dict[str, Any]:
    workflows = {
        workflow_type: _latest_workflow(db, project_id, workflow_type)
        for workflow_type in (
            "WF-1_PROJECT_INTAKE",
            "WF-2_TEMPLATE_EXTRACTION",
            "WF-3_HYBRID_ONLINE_ASSIST",
            "WF-4_PROPOSAL_AUTHORING",
            "WF-5_SECURITY_REVIEW_AND_EXPORT",
        )
    }
    authoring = workflows["WF-4_PROPOSAL_AUTHORING"] or {"state": {}}
    state = authoring.get("state") or {}
    contract = state.get("full_proposal_contract") or {}
    cross = state.get("g3_cross_chapter_reviews") or {}
    full_reviews = state.get("full_proposal_review_history") or []
    final_review = full_reviews[-1] if full_reviews else {}
    runs = db.fetchall(
        "SELECT id,prompt_id,status,model_id,endpoint_id,input_hash,output_hash FROM prompt_runs WHERE project_id=? ORDER BY created_at,id",
        (project_id,),
    )
    model_ids = {str(row.get("model_id") or "") for row in runs}
    endpoint_ids = {str(row.get("endpoint_id") or "") for row in runs}
    forbidden_runs = [
        row["id"]
        for row in runs
        if any(marker in str(row.get("model_id") or "").lower() for marker in FORBIDDEN_MODEL_MARKERS)
        or any(marker in str(row.get("endpoint_id") or "").lower() for marker in FORBIDDEN_MODEL_MARKERS)
    ]
    error_runs = [row["id"] for row in runs if row.get("status") in {"ERROR", "BLOCK"}]
    call_root = Path(os.getenv("MODEL_CALL_EVIDENCE_DIR", str(output_dir / "model_calls")))
    response_files = sorted(call_root.glob("*/response.json")) if call_root.exists() else []
    request_files = sorted(call_root.glob("*/request.json")) if call_root.exists() else []

    research_path = _latest_research_manifest(db, project_id)
    research_manifest = json.loads(research_path.read_text(encoding="utf-8")) if research_path else {}
    research_verify = verify_research_archive(research_path) if research_path else {"status": "FAIL"}
    quality = db.fetchall(
        "SELECT content_json,status FROM artifacts WHERE project_id=? AND artifact_type='QUALITY_FINDING' ORDER BY version",
        (project_id,),
    )
    open_quality = [
        json.loads(row["content_json"])
        for row in quality
        if row.get("status") not in {"VERIFIED", "CLOSED"}
    ]
    documents = db.fetchall(
        "SELECT filename,role,security_level,document_hash FROM documents WHERE project_id=? ORDER BY created_at,id",
        (project_id,),
    )
    all_public = bool(documents) and all(row.get("security_level") == "PUBLIC" for row in documents)

    checks = {
        "all_five_workflows_completed": all(
            item and item.get("status") == "COMPLETED" for item in workflows.values()
        ),
        "runtime_used_only_live_models": bool(runs) and not forbidden_runs,
        "no_error_or_block_prompt_runs": not error_runs,
        "raw_request_response_evidence_complete": len(request_files) == len(response_files) >= len(runs),
        "complete_public_material_set": all_public and len(documents) >= 5,
        "real_public_research_archived": research_manifest.get("retrieval_mode") in LIVE_RESEARCH_MODES,
        "research_archive_hashes_verified": research_verify.get("status") == "PASS",
        "research_coverage_passed": (research_manifest.get("coverage") or {}).get("status") == "PASS",
        "fourteen_sections_frozen": len(contract.get("sections") or []) == 14,
        "five_authoring_groups_completed": len(state.get("authoring_child_workflow_ids") or []) == 5,
        "three_chapter_reviews_passed": cross.get("status") == "PASS" and int(cross.get("batch_count") or 0) == 5,
        "full_integration_review_passed": final_review.get("status") == "PASS",
        "no_open_p0_p1": not open_quality,
        "post_export_acceptance_passed": post_export.get("status") == "PASS",
        "docx_pdf_package_present": all(
            (post_export.get(key) or {}).get("sha256")
            for key in ("document", "pdf", "package")
        ),
        "page_visual_evidence_present": bool(post_export.get("screenshots")),
    }
    status = "PASS" if all(checks.values()) else "FAIL"
    report = {
        "schema_version": "1.0",
        "gate": "G3_FORMAL_CAPABILITY_ACCEPTANCE",
        "status": status,
        "generated_at": utc_now(),
        "source_commit": source_commit,
        "project_id": project_id,
        "checks": checks,
        "metrics": {
            "document_count": len(documents),
            "prompt_run_count": len(runs),
            "model_call_request_count": len(request_files),
            "model_call_response_count": len(response_files),
            "research_source_count": int(research_manifest.get("source_count") or 0),
            "section_count": len(contract.get("sections") or []),
            "authoring_group_count": len(state.get("authoring_child_workflow_ids") or []),
            "cross_chapter_batch_count": int(cross.get("batch_count") or 0),
            "full_integration_review_count": len(full_reviews),
            "page_count": len(post_export.get("screenshots") or []),
            "open_quality_finding_count": len(open_quality),
        },
        "models": sorted(model_ids),
        "endpoints": sorted(endpoint_ids),
        "research": {
            "manifest": str(research_path) if research_path else None,
            "retrieval_mode": research_manifest.get("retrieval_mode"),
            "source_count": research_manifest.get("source_count"),
            "coverage": research_manifest.get("coverage"),
            "verification": research_verify,
        },
        "workflow_ids": {
            key: value.get("id") if value else None for key, value in workflows.items()
        },
        "cross_chapter_reviews": cross,
        "full_integration_review": final_review,
        "post_export_attempt_id": post_export.get("attempt_id"),
        "material_manifest": documents,
        "evidence_digest": sha256_json(
            {
                "checks": checks,
                "runs": [
                    {
                        "id": row.get("id"),
                        "input_hash": row.get("input_hash"),
                        "output_hash": row.get("output_hash"),
                    }
                    for row in runs
                ],
                "post_export": post_export.get("attempt_hash"),
            }
        ),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "G3_FORMAL_CAPABILITY_ACCEPTANCE.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def file_record(path: Path) -> dict[str, Any]:
    return {
        "path": str(path.resolve()),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_bytes(path.read_bytes()),
    }
