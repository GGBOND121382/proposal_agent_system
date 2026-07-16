from __future__ import annotations

from pathlib import Path
from typing import Any

from .util import sha256_bytes, sha256_json, sha256_text, utc_now


class ExportManifestMixin:
    @staticmethod
    def _non_target_check(before: list[str], after: list[str], changed_sections: list[dict[str, Any]]) -> dict[str, Any]:
        prefix = 0
        for left, right in zip(before, after):
            if left != right:
                break
            prefix += 1
        suffix = 0
        for left, right in zip(reversed(before), reversed(after)):
            if left != right:
                break
            suffix += 1
        return {
            "status": "PASS_WITH_LIMITATION",
            "unchanged_prefix_paragraphs": prefix,
            "unchanged_suffix_paragraphs": suffix,
            "limitation": "Paragraph-level check; complex OOXML objects require deployment-specific validation.",
        }

    def _manifest(self, project: dict[str, Any], gates: dict[str, str], candidates: list[dict[str, Any]], path: Path, integrity: dict[str, Any]) -> dict[str, Any]:
        candidate_records = [
            {
                "section_id": str(candidate.get("section_id") or ""),
                "section_title": str(candidate.get("section_title") or ""),
                "candidate_id": str(candidate.get("candidate_id") or ""),
                "polish_run_id": str(candidate.get("run_id") or ""),
                "expression_critic_run_id": str(candidate.get("expression_critic_run_id") or ""),
                "paragraph_hashes": [sha256_text(str(item)) for item in candidate.get("paragraphs") or []],
                "candidate_visible_hash": sha256_json([str(item) for item in candidate.get("paragraphs") or []]),
            }
            for candidate in candidates
        ]
        candidate_core = {"section_count": len(candidate_records), "sections": candidate_records}
        return {
            "schema_version": "1.1",
            "project_id": project["id"],
            "project_name": project["name"],
            "security_level": project["security_level"],
            "exported_at": utc_now(),
            "document_filename": path.name,
            "document_sha256": sha256_bytes(path.read_bytes()),
            "approval_gate_ids": gates,
            "source_run_ids": [candidate["run_id"] for candidate in candidates],
            "expression_critic_run_ids": [candidate["expression_critic_run_id"] for candidate in candidates],
            "candidate_ids": [candidate["candidate_id"] for candidate in candidates],
            "candidate_snapshot": {**candidate_core, "candidate_set_hash": sha256_json(candidate_core)},
            "integrity_mode": integrity["mode"],
            "delivery_pipeline": {
                "docx": "GENERATED",
                "pdf": "REQUIRED_FOR_PACKAGE",
                "structure_validation": "REQUIRED_FOR_PACKAGE",
                "visual_validation": "REQUIRED_FOR_PACKAGE",
            },
        }
