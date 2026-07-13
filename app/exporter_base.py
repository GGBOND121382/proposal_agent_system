from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

from .util import new_id, safe_filename, sha256_bytes, utc_now, write_json


class ExportDenied(RuntimeError):
    pass


class ExportBaseMixin:
    def __init__(self, db, settings):
        self.db = db
        self.settings = settings

    def export(self, project_id: str) -> Path:
        project, gates = self._authorized_project(project_id)
        candidates = self._candidate_runs(project_id)
        template_row = self.db.fetchone(
            "SELECT file_path,filename,parsed_json FROM documents WHERE project_id=? AND role='CURRENT_PROPOSAL' AND filename LIKE '%.docx' ORDER BY created_at DESC LIMIT 1",
            (project_id,),
        )
        integrity: dict[str, Any]
        filename = safe_filename(f"{project['name']}-{new_id('export')}.docx")
        path = self.settings.exports_dir / filename
        # A single-section revision can safely patch the uploaded draft.  A full
        # multi-section authoring run is rendered as a clean document so authoring
        # instructions/placeholders in the source scaffold cannot leak to delivery.
        if template_row and len(candidates) == 1:
            path, integrity = self._patch_template(Path(template_row["file_path"]), path, candidates)
        else:
            path, integrity = self._generate_document(project, path, candidates)
        manifest = self._manifest(project, gates, candidates, path, integrity)
        write_json(path.with_suffix(".integrity.json"), integrity)
        write_json(path.with_suffix(".manifest.json"), manifest)
        self.db.audit("DOCX_EXPORTED", project_id=project_id, object_id=filename, metadata={"filename": path.name, "sha256": manifest["document_sha256"], "candidate_count": len(candidates), "mode": integrity["mode"]})
        return path

    def export_package(self, project_id: str, document_path: Path | None = None) -> Path:
        document_path = document_path or self.export(project_id)
        package_path = document_path.with_suffix(".zip")
        with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in [document_path, document_path.with_suffix(".integrity.json"), document_path.with_suffix(".manifest.json")]:
                zf.write(path, arcname=path.name)
        self.db.audit("EXPORT_PACKAGE_CREATED", project_id=project_id, object_id=package_path.name, metadata={"filename": package_path.name, "sha256": sha256_bytes(package_path.read_bytes())})
        return package_path

    def _authorized_project(self, project_id: str) -> tuple[dict[str, Any], dict[str, str]]:
        project = self.db.fetchone("SELECT * FROM projects WHERE id=?", (project_id,))
        if not project:
            raise KeyError(project_id)
        gates: dict[str, str] = {}
        for gate_type in ["FINAL_CONTENT_SECURITY_APPROVAL", "FINAL_EXPORT_APPROVAL"]:
            gate = self.db.fetchone("SELECT id,status FROM gates WHERE project_id=? AND gate_type=? ORDER BY created_at DESC LIMIT 1", (project_id, gate_type))
            if not gate or gate["status"] != "APPROVED":
                raise ExportDenied(f"{gate_type} gate has not been approved")
            gates[gate_type] = gate["id"]
        return project, gates

    def _candidate_runs(self, project_id: str) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            "SELECT id,input_json,output_json,created_at FROM prompt_runs WHERE project_id=? AND prompt_id='P-WRITE-CONTENT' AND status='PASS' ORDER BY created_at,id",
            (project_id,),
        )
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            input_data = json.loads(row["input_json"])
            output = json.loads(row["output_json"])
            source_section = input_data.get("payload", {}).get("source_section", {})
            result = output.get("result", {})
            key = source_section.get("section_id") or source_section.get("title") or row["id"]
            latest[key] = {
                "run_id": row["id"],
                "section_id": source_section.get("section_id"),
                "section_title": source_section.get("title") or source_section.get("section_key"),
                "source_section_hash": source_section.get("text_hash"),
                "contains_complex_content": any(source_section.get(k, False) for k in ["contains_table", "contains_formula", "contains_image", "contains_comment", "contains_revision"]),
                "paragraphs": [p.get("text", "") for p in sorted(result.get("paragraphs", []), key=lambda x: x.get("sequence", 0))] or [result.get("candidate_text", "")],
                "candidate_id": result.get("candidate_id"),
            }

        order: dict[str, int] = {}
        template_row = self.db.fetchone(
            "SELECT parsed_json FROM documents WHERE project_id=? AND role='CURRENT_PROPOSAL' ORDER BY created_at DESC LIMIT 1",
            (project_id,),
        )
        if template_row:
            parsed = json.loads(template_row["parsed_json"])
            order = {section.get("section_id"): index for index, section in enumerate(parsed.get("sections", []))}
        candidates = list(latest.values())
        candidates.sort(key=lambda item: (order.get(item.get("section_id"), 10_000), item.get("section_title") or ""))
        return candidates

