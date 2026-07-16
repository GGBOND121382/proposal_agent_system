from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

from .delivery_validator import DeliveryValidationError
from .post_export_validator import PostExportDeliveryValidator as DeliveryValidator
from .figure_protocol import FigureProtocolError
from .pdf_exporter import PdfConversionError, PdfConverter
from .quality import QualityGateBlocked, QualityLifecycleManager
from .util import new_id, safe_filename, sha256_bytes, sha256_json, sha256_text, utc_now, write_json


class ExportDenied(RuntimeError):
    pass


class ExportBaseMixin:
    def __init__(self, db, settings):
        self.db = db
        self.settings = settings
        self.pdf_converter = PdfConverter(settings)
        self.delivery_validator = DeliveryValidator(settings)
        self.quality_manager = QualityLifecycleManager(db)

    def export(self, project_id: str) -> Path:
        return self._export_document(project_id, delivery_repair=False)

    def export_delivery_repair(
        self,
        project_id: str,
        *,
        expected_candidate_set_hash: str,
        engineering_repair_id: str,
    ) -> Path:
        if not engineering_repair_id.strip():
            raise ExportDenied("engineering_repair_id is required for a delivery repair export")
        return self._export_document(
            project_id,
            delivery_repair=True,
            expected_candidate_set_hash=expected_candidate_set_hash,
            engineering_repair_id=engineering_repair_id,
        )

    def _export_document(
        self,
        project_id: str,
        *,
        delivery_repair: bool,
        expected_candidate_set_hash: str | None = None,
        engineering_repair_id: str | None = None,
    ) -> Path:
        if delivery_repair:
            project, gates = self._authorized_delivery_repair(
                project_id, expected_candidate_set_hash=expected_candidate_set_hash or ""
            )
        else:
            project, gates = self._authorized_project(project_id)
        candidates = self._candidate_runs(project_id)
        if not candidates:
            raise ExportDenied(
                "No section has a P-EXPRESSION-POLISH candidate approved by a later P-EXPRESSION-CRITIC run"
            )
        template_row = self.db.fetchone(
            "SELECT file_path,filename,parsed_json FROM documents WHERE project_id=? AND role='CURRENT_PROPOSAL' AND filename LIKE '%.docx' ORDER BY created_at DESC LIMIT 1",
            (project_id,),
        )
        integrity: dict[str, Any]
        filename = safe_filename(f"{project['name']}-{new_id('export')}.docx")
        path = self.settings.exports_dir / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if template_row and len(candidates) == 1:
                path, integrity = self._patch_template(Path(template_row["file_path"]), path, candidates)
            else:
                path, integrity = self._generate_document(project, path, candidates)
        except FigureProtocolError as exc:
            raise ExportDenied(f"Figure protocol validation failed: {exc}") from exc
        manifest = self._manifest(project, gates, candidates, path, integrity)
        write_json(path.with_suffix(".integrity.json"), integrity)
        write_json(path.with_suffix(".manifest.json"), manifest)
        self.db.audit(
            "DOCX_EXPORTED",
            project_id=project_id,
            object_id=filename,
            metadata={
                "filename": path.name,
                "sha256": manifest["document_sha256"],
                "candidate_count": len(candidates),
                "mode": integrity["mode"],
                "expression_critic_run_ids": [c["expression_critic_run_id"] for c in candidates],
                "delivery_repair": delivery_repair,
                "engineering_repair_id": engineering_repair_id,
                "candidate_set_hash": self.candidate_snapshot(project_id)["candidate_set_hash"],
            },
        )
        return path

    def export_pdf(self, project_id: str, document_path: Path | None = None) -> Path:
        document_path = document_path or self.export(project_id)
        try:
            pdf_path = self.pdf_converter.convert(document_path)
        except PdfConversionError as exc:
            self.db.audit(
                "PDF_EXPORT_FAILED",
                project_id=project_id,
                object_id=document_path.name,
                metadata={"filename": document_path.name, "error": str(exc)},
            )
            raise ExportDenied(str(exc)) from exc
        self.db.audit(
            "PDF_EXPORTED",
            project_id=project_id,
            object_id=pdf_path.name,
            metadata={"filename": pdf_path.name, "sha256": sha256_bytes(pdf_path.read_bytes())},
        )
        return pdf_path

    def inspect_delivery(
        self,
        project_id: str,
        document_path: Path,
        pdf_path: Path,
        *,
        validation_run_id: str | None = None,
    ) -> dict[str, Any]:
        candidates = self._candidate_runs(project_id)
        expected_sections = [
            str(item.get("section_title") or "").strip()
            for item in candidates
            if str(item.get("section_title") or "").strip()
        ]
        try:
            report = self.delivery_validator.validate(
                document_path,
                pdf_path,
                expected_sections=expected_sections,
                expected_candidates=candidates,
                screenshots_dir=document_path.parent / f"{document_path.stem}-pages",
            )
        except DeliveryValidationError as exc:
            self.db.audit(
                "DELIVERY_VALIDATION_FAILED",
                project_id=project_id,
                object_id=document_path.name,
                metadata={"filename": document_path.name, "error": str(exc)},
            )
            raise ExportDenied(str(exc)) from exc
        report["validation_run_id"] = validation_run_id or new_id("delivery-validation")
        report["candidate_snapshot"] = self.candidate_snapshot(project_id)
        write_json(document_path.with_suffix(".delivery-validation.json"), report)
        report["report_path"] = str(document_path.with_suffix(".delivery-validation.json"))
        self.db.audit(
            "DELIVERY_INSPECTED",
            project_id=project_id,
            object_id=document_path.name,
            metadata={
                "filename": document_path.name,
                "report": report["report_path"],
                "finding_count": report["finding_count"],
            },
        )
        return report

    def validate_delivery(
        self,
        project_id: str,
        document_path: Path,
        pdf_path: Path,
    ) -> dict[str, Any]:
        report = self.inspect_delivery(project_id, document_path, pdf_path)
        try:
            self.delivery_validator.require_pass(report)
        except DeliveryValidationError as exc:
            self.db.audit(
                "DELIVERY_VALIDATION_FAILED",
                project_id=project_id,
                object_id=document_path.name,
                metadata={"filename": document_path.name, "error": str(exc)},
            )
            raise ExportDenied(str(exc)) from exc
        self.db.audit(
            "DELIVERY_VALIDATED",
            project_id=project_id,
            object_id=document_path.name,
            metadata={
                "filename": document_path.name,
                "report": report["report_path"],
                "finding_count": report["finding_count"],
            },
        )
        return report

    def export_package(self, project_id: str, document_path: Path | None = None) -> Path:
        document_path = document_path or self.export(project_id)
        pdf_path = self.export_pdf(project_id, document_path)
        report = self.validate_delivery(project_id, document_path, pdf_path)
        return self.package_validated_delivery(project_id, document_path, pdf_path, report)

    def package_validated_delivery(
        self,
        project_id: str,
        document_path: Path,
        pdf_path: Path,
        report: dict[str, Any],
    ) -> Path:
        self.delivery_validator.require_pass(report)
        package_path = document_path.with_suffix(".zip")
        screenshots_dir = Path(report["screenshot_dir"])
        members = [
            document_path,
            document_path.with_suffix(".integrity.json"),
            document_path.with_suffix(".manifest.json"),
            pdf_path,
            document_path.with_suffix(".pdf-conversion.json"),
            document_path.with_suffix(".structure-findings.json"),
            document_path.with_suffix(".visual-findings.json"),
            document_path.with_suffix(".delivery-validation.json"),
        ]
        with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in members:
                if not path.is_file():
                    raise ExportDenied(f"Required delivery artifact is missing: {path.name}")
                zf.write(path, arcname=path.name)
            screenshots = sorted(screenshots_dir.glob("page-*.png"))
            if not screenshots:
                raise ExportDenied("Required page screenshot evidence is missing")
            for screenshot in screenshots:
                zf.write(screenshot, arcname=f"pages/{screenshot.name}")
        self.db.audit(
            "EXPORT_PACKAGE_CREATED",
            project_id=project_id,
            object_id=package_path.name,
            metadata={
                "filename": package_path.name,
                "sha256": sha256_bytes(package_path.read_bytes()),
                "contains_pdf": True,
                "contains_visual_evidence": True,
                "validation_run_id": report.get("validation_run_id"),
                "candidate_set_hash": (report.get("candidate_snapshot") or {}).get("candidate_set_hash"),
            },
        )
        return package_path

    def _authorized_project(self, project_id: str) -> tuple[dict[str, Any], dict[str, str]]:
        project = self.db.fetchone("SELECT * FROM projects WHERE id=?", (project_id,))
        if not project:
            raise KeyError(project_id)
        try:
            self.quality_manager.assert_no_open_blockers(project_id)
        except QualityGateBlocked as exc:
            raise ExportDenied(
                str(exc) + "。导出必须等待修复证据与独立复审完成，不能通过批准Gate或手工改库绕过。"
            ) from exc
        return project, self._approved_gate_ids(project_id)

    def candidate_snapshot(self, project_id: str) -> dict[str, Any]:
        records = []
        for candidate in self._candidate_runs(project_id):
            paragraphs = [str(item) for item in candidate.get("paragraphs") or []]
            records.append({
                "section_id": str(candidate.get("section_id") or ""),
                "section_title": str(candidate.get("section_title") or ""),
                "candidate_id": str(candidate.get("candidate_id") or ""),
                "polish_run_id": str(candidate.get("run_id") or ""),
                "expression_critic_run_id": str(candidate.get("expression_critic_run_id") or ""),
                "paragraph_hashes": [sha256_text(item) for item in paragraphs],
                "candidate_visible_hash": sha256_json(paragraphs),
            })
        core = {"section_count": len(records), "sections": records}
        return {**core, "candidate_set_hash": sha256_json(core)}

    def _authorized_delivery_repair(
        self,
        project_id: str,
        *,
        expected_candidate_set_hash: str,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        project = self.db.fetchone("SELECT * FROM projects WHERE id=?", (project_id,))
        if not project:
            raise KeyError(project_id)
        blockers = self.quality_manager.open_blockers(project_id)
        invalid = [
            item for item in blockers
            if (item.get("responsibility") or {}).get("owner") != "EXPORT_ENGINEERING"
            or (item.get("responsibility") or {}).get("owner_kind") != "ENGINEERING"
        ]
        if invalid:
            raise ExportDenied(
                "Delivery repair export is allowed only when every open blocker belongs to EXPORT_ENGINEERING"
            )
        actual_hash = self.candidate_snapshot(project_id)["candidate_set_hash"]
        if not expected_candidate_set_hash or actual_hash != expected_candidate_set_hash:
            raise ExportDenied(
                "Delivery engineering repair must preserve the reviewed candidate set; "
                f"expected={expected_candidate_set_hash}, actual={actual_hash}"
            )
        return project, self._approved_gate_ids(project_id)

    def _approved_gate_ids(self, project_id: str) -> dict[str, str]:
        gates: dict[str, str] = {}
        for gate_type in ["FINAL_CONTENT_SECURITY_APPROVAL", "FINAL_EXPORT_APPROVAL"]:
            gate = self.db.fetchone(
                "SELECT id,status FROM gates WHERE project_id=? AND gate_type=? ORDER BY created_at DESC LIMIT 1",
                (project_id, gate_type),
            )
            if not gate or gate["status"] != "APPROVED":
                raise ExportDenied(f"{gate_type} gate has not been approved")
            gates[gate_type] = gate["id"]
        return gates

    def _candidate_runs(self, project_id: str) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            "SELECT id,workflow_id,prompt_id,input_json,output_json,status,created_at "
            "FROM prompt_runs WHERE project_id=? AND prompt_id IN ('P-EXPRESSION-POLISH','P-EXPRESSION-CRITIC') "
            "ORDER BY created_at,id",
            (project_id,),
        )
        polished: dict[tuple[str, str], dict[str, Any]] = {}
        approvals: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            input_data = json.loads(row["input_json"] or "{}")
            output = json.loads(row["output_json"] or "{}")
            payload = input_data.get("payload", {})
            workflow_id = str(row.get("workflow_id") or "")
            if row["prompt_id"] == "P-EXPRESSION-POLISH" and row["status"] == "PASS":
                source_section = payload.get("source_section") or {}
                result = output.get("result") or {}
                candidate_id = str(result.get("candidate_id") or "")
                section_id = str(source_section.get("section_id") or "")
                if not candidate_id or not section_id:
                    continue
                paragraphs = [
                    paragraph.get("text", "")
                    for paragraph in sorted(result.get("paragraphs", []), key=lambda item: item.get("sequence", 0))
                    if isinstance(paragraph, dict)
                ] or [result.get("candidate_text", "")]
                marker_prefixes = ("[[TABLE]]", "[[FIGURE]]", "[[FORMULA]]")
                polished[(workflow_id, candidate_id)] = {
                    "run_id": row["id"],
                    "workflow_id": workflow_id,
                    "created_at": row["created_at"],
                    "section_id": section_id,
                    "section_title": source_section.get("title") or source_section.get("section_key"),
                    "source_section_hash": source_section.get("text_hash"),
                    "contains_complex_content": any(
                        source_section.get(key, False)
                        for key in ["contains_table", "contains_formula", "contains_image", "contains_comment", "contains_revision"]
                    ) or any(str(paragraph).strip().startswith(marker_prefixes) for paragraph in paragraphs),
                    "paragraphs": paragraphs,
                    "candidate_id": candidate_id,
                }
            elif row["prompt_id"] == "P-EXPRESSION-CRITIC" and row["status"] == "PASS":
                target = payload.get("polished_candidate") or payload.get("content_candidate") or {}
                candidate_id = str(target.get("candidate_id") or "")
                if candidate_id:
                    approvals[(workflow_id, candidate_id)] = {
                        "run_id": row["id"],
                        "created_at": row["created_at"],
                    }

        latest: dict[str, dict[str, Any]] = {}
        for key, candidate in polished.items():
            approval = approvals.get(key)
            if not approval or str(approval["created_at"]) < str(candidate["created_at"]):
                continue
            candidate = dict(candidate)
            candidate["expression_critic_run_id"] = approval["run_id"]
            current = latest.get(candidate["section_id"])
            if current is None or str(candidate["created_at"]) >= str(current["created_at"]):
                latest[candidate["section_id"]] = candidate

        order: dict[str, int] = {}
        template_row = self.db.fetchone(
            "SELECT parsed_json FROM documents WHERE project_id=? AND role='CURRENT_PROPOSAL' ORDER BY created_at DESC LIMIT 1",
            (project_id,),
        )
        if template_row:
            parsed = json.loads(template_row["parsed_json"])
            order = {
                str(section.get("section_id")): index
                for index, section in enumerate(parsed.get("sections", []))
            }
        candidates = list(latest.values())
        candidates.sort(
            key=lambda item: (
                order.get(str(item.get("section_id")), 10_000),
                item.get("section_title") or "",
            )
        )
        return candidates
