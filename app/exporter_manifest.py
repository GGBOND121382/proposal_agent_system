from __future__ import annotations

from pathlib import Path
from typing import Any

from .util import sha256_bytes, utc_now


class ExportManifestMixin:
    @staticmethod
    def _non_target_check(before: list[str], after: list[str], changed_sections: list[dict[str, Any]]) -> dict[str, Any]:
        # Full OOXML equivalence is not claimed. This check records stable prefix/suffix evidence
        # around the targeted paragraph ranges and makes the limitation explicit.
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
        return {"status": "PASS_WITH_LIMITATION", "unchanged_prefix_paragraphs": prefix, "unchanged_suffix_paragraphs": suffix, "limitation": "Paragraph-level check; complex OOXML objects require deployment-specific validation."}

    def _manifest(self, project: dict[str, Any], gates: dict[str, str], candidates: list[dict[str, Any]], path: Path, integrity: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "project_id": project["id"],
            "project_name": project["name"],
            "security_level": project["security_level"],
            "exported_at": utc_now(),
            "document_filename": path.name,
            "document_sha256": sha256_bytes(path.read_bytes()),
            "approval_gate_ids": gates,
            "source_run_ids": [c["run_id"] for c in candidates],
            "integrity_mode": integrity["mode"],
        }
