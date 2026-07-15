from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

from .exporter import DocxExporter
from .research import PublicResearchService
from .skill_setup import build_skill_executor
from .skills.research_audit import verify_research_archive
from .util import new_id, sha256_bytes, sha256_json, utc_now, write_json


ARTIFACT_SCHEME = "artifact://"


class ResearchMermaidExportError(RuntimeError):
    """Fail-closed error for the S3 Research + Mermaid + Export chain."""


class ResearchMermaidExportPipeline:
    """Checkpointed G2/S3 integration of research, Mermaid and delivery.

    The pipeline intentionally has two phases. ``prepare`` archives and validates public
    evidence, binds PUBLIC_CLAIM objects, renders evidence-linked Mermaid artifacts and
    persists a portable checkpoint. Upstream writing agents may then consume only the
    returned figure markers. ``finalize`` resumes from that checkpoint, requires the
    markers to be present in later Expression-Critic-approved candidates, and invokes the
    production DOCX/PDF delivery hard gate.

    No model output is rewritten here. The integration layer only validates, renders,
    verifies hashes and packages evidence.
    """

    schema_version = "1.0"

    def __init__(self, db, settings, *, skill_executor=None, exporter=None):
        self.db = db
        self.settings = settings
        self.skill_executor = skill_executor or build_skill_executor(db, settings)
        self.exporter = exporter or DocxExporter(db, settings)
        self.research_service = PublicResearchService(settings, self.skill_executor)

    def research(
        self,
        *,
        project_id: str,
        workflow_id: str | None,
        security_level: str,
        research_plan: dict[str, Any],
        research_request: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute and hash-verify the public-research stage for later synthesis."""
        payload = {
            **research_request,
            "plan": research_plan,
            "require_structured_plan": True,
        }
        try:
            result = self.skill_executor.execute(
                "public_research.archive",
                payload,
                project_id=project_id,
                workflow_id=workflow_id,
                security_level=security_level,
            )
        except Exception as exc:
            self._audit_failure("S3_RESEARCH_FAILED", project_id, "research", str(exc))
            raise ResearchMermaidExportError(f"Research stage failed: {exc}") from exc
        if result.status != "PASS":
            raise ResearchMermaidExportError(
                f"Research stage did not pass: {result.status}"
            )
        output = result.output
        verification = output.get("archive_verification") or verify_research_archive(
            output.get("archive_manifest", "")
        )
        if verification.get("status") != "PASS":
            raise ResearchMermaidExportError("Research archive hash verification failed")
        self.db.audit(
            "S3_RESEARCH_READY",
            project_id=project_id,
            object_id=str(output.get("archive_session_id") or "research"),
            metadata={
                "archive_manifest": str(output.get("archive_manifest") or ""),
                "source_count": len(output.get("source_catalog", [])),
                "mode": output.get("mode"),
            },
        )
        return output

    def prepare(
        self,
        *,
        project_id: str,
        workflow_id: str | None,
        security_level: str,
        research_plan: dict[str, Any],
        research_request: dict[str, Any],
        synthesis: dict[str, Any],
        diagrams: list[dict[str, Any]],
        research_output: dict[str, Any] | None = None,
        acceptance_mode: str = "ENGINEERING_INTEGRATION",
    ) -> dict[str, Any]:
        run_id = new_id("s3")
        evidence_dir = self._evidence_root(run_id)
        evidence_dir.mkdir(parents=True, exist_ok=False)

        if research_output is None:
            research_output = self.research(
                project_id=project_id,
                workflow_id=workflow_id,
                security_level=security_level,
                research_plan=research_plan,
                research_request=research_request,
            )
        else:
            archive_verification = research_output.get(
                "archive_verification"
            ) or verify_research_archive(research_output.get("archive_manifest", ""))
            if archive_verification.get("status") != "PASS":
                raise ResearchMermaidExportError("Research archive hash verification failed")

        claim_report = self.research_service.validate_synthesis(synthesis, research_output)
        if claim_report.get("status") != "PASS":
            codes = [item.get("code", "UNKNOWN") for item in claim_report.get("findings", [])]
            raise ResearchMermaidExportError(
                "PUBLIC_CLAIM binding failed: " + ", ".join(codes[:20])
            )
        claim_report_path = self._persist_claim_report(claim_report, evidence_dir)
        portable_research_path = self._write_portable_research_manifest(
            Path(str(research_output["archive_manifest"])), evidence_dir
        )

        diagram_records = self._render_diagrams(
            diagrams,
            claim_report,
            project_id=project_id,
            workflow_id=workflow_id,
            security_level=security_level,
        )
        checkpoint = {
            "schema_version": self.schema_version,
            "chain": "RESEARCH_MERMAID_EXPORT",
            "phase": "PREPARED",
            "status": "WAITING_FOR_EXPRESSION_APPROVED_CONTENT",
            "acceptance_mode": acceptance_mode,
            "run_id": run_id,
            "project_id": project_id,
            "workflow_id": workflow_id,
            "security_level": security_level,
            "created_at": utc_now(),
            "input_hashes": {
                "research_plan_sha256": sha256_json(research_plan),
                "research_request_sha256": sha256_json(research_request),
                "research_output_sha256": sha256_json(research_output),
                "synthesis_sha256": sha256_json(synthesis),
                "diagram_specs_sha256": sha256_json(diagrams),
            },
            "research": {
                "mode": research_output.get("mode"),
                "plan_validation": research_output.get("plan_validation"),
                "coverage": research_output.get("coverage"),
                "issues": research_output.get("issues", []),
                "source_count": len(research_output.get("source_catalog", [])),
                "sources": self._portable_sources(research_output),
                "archive_manifest": self._artifact_record(
                    Path(str(research_output["archive_manifest"])), "RESEARCH_ARCHIVE_MANIFEST"
                ),
                "portable_manifest": self._artifact_record(
                    portable_research_path, "PORTABLE_RESEARCH_MANIFEST"
                ),
            },
            "claim_binding": {
                "status": claim_report.get("status"),
                "validation_mode": claim_report.get("validation_mode"),
                "claim_count": claim_report.get("claim_count"),
                "bindings": claim_report.get("bindings", []),
                "report": self._artifact_record(claim_report_path, "PUBLIC_CLAIM_BINDING_REPORT"),
            },
            "diagrams": diagram_records,
            "required_figure_markers": [item["figure_marker"] for item in diagram_records],
        }
        checkpoint_path = evidence_dir / "S3_PREPARED_CHECKPOINT.json"
        write_json(checkpoint_path, checkpoint)
        checkpoint["checkpoint"] = self._artifact_record(checkpoint_path, "S3_PREPARED_CHECKPOINT")

        verification = self.verify_prepared(checkpoint_path)
        if verification.get("status") != "PASS":
            raise ResearchMermaidExportError("Prepared S3 checkpoint failed restart verification")
        verification_path = evidence_dir / "S3_PREPARED_VERIFICATION.json"
        write_json(verification_path, verification)
        checkpoint["verification"] = self._artifact_record(
            verification_path, "S3_PREPARED_VERIFICATION"
        )
        self.db.audit(
            "S3_PREPARED",
            project_id=project_id,
            object_id=run_id,
            metadata={
                "checkpoint": checkpoint["checkpoint"]["reference"],
                "checkpoint_sha256": checkpoint["checkpoint"]["sha256"],
                "source_count": checkpoint["research"]["source_count"],
                "diagram_count": len(diagram_records),
                "acceptance_mode": acceptance_mode,
            },
        )
        return checkpoint

    def finalize(
        self,
        *,
        project_id: str,
        checkpoint: str | Path | dict[str, Any],
    ) -> dict[str, Any]:
        checkpoint_path, prepared = self._load_checkpoint(checkpoint)
        if prepared.get("project_id") != project_id:
            raise ResearchMermaidExportError("Checkpoint project_id does not match finalize target")
        verification = self.verify_prepared(checkpoint_path)
        if verification.get("status") != "PASS":
            codes = [item.get("code", "UNKNOWN") for item in verification.get("findings", [])]
            raise ResearchMermaidExportError(
                "Prepared checkpoint verification failed: " + ", ".join(codes[:20])
            )

        candidates = self.exporter._candidate_runs(project_id)
        if not candidates:
            raise ResearchMermaidExportError(
                "No Expression-Critic-approved content candidate is available for export"
            )
        self._require_markers_in_approved_candidates(
            prepared.get("required_figure_markers", []), candidates
        )

        try:
            docx_path = self.exporter.export(project_id)
            package_path = self.exporter.export_package(project_id, docx_path)
        except Exception as exc:
            self._audit_failure("S3_EXPORT_FAILED", project_id, prepared["run_id"], str(exc))
            raise ResearchMermaidExportError(f"DOCX/PDF delivery stage failed: {exc}") from exc

        pdf_path = docx_path.with_suffix(".pdf")
        delivery_path = docx_path.with_suffix(".delivery-validation.json")
        delivery = json.loads(delivery_path.read_text(encoding="utf-8"))
        if delivery.get("status") != "PASS":
            raise ResearchMermaidExportError("Delivery validator did not pass")

        final = {
            "schema_version": self.schema_version,
            "chain": "RESEARCH_MERMAID_EXPORT",
            "phase": "FINALIZED",
            "status": "PASS",
            "acceptance_mode": prepared.get("acceptance_mode"),
            "run_id": prepared["run_id"],
            "project_id": project_id,
            "workflow_id": prepared.get("workflow_id"),
            "security_level": prepared.get("security_level"),
            "prepared_checkpoint": self._artifact_record(
                checkpoint_path, "S3_PREPARED_CHECKPOINT"
            ),
            "research": prepared["research"],
            "claim_binding": prepared["claim_binding"],
            "diagrams": prepared["diagrams"],
            "approved_content": [
                {
                    "section_id": item.get("section_id"),
                    "section_title": item.get("section_title"),
                    "candidate_id": item.get("candidate_id"),
                    "expression_polish_run_id": item.get("run_id"),
                    "expression_critic_run_id": item.get("expression_critic_run_id"),
                }
                for item in candidates
            ],
            "delivery": {
                "docx": self._artifact_record(docx_path, "DOCX"),
                "pdf": self._artifact_record(pdf_path, "PDF"),
                "package": self._artifact_record(package_path, "DELIVERY_PACKAGE"),
                "validation": self._artifact_record(
                    delivery_path, "DELIVERY_VALIDATION_REPORT"
                ),
                "finding_count": delivery.get("finding_count"),
                "blocking_finding_count": delivery.get("blocking_finding_count"),
            },
            "finalized_at": utc_now(),
        }
        evidence_dir = checkpoint_path.parent
        final_path = evidence_dir / "S3_FINAL_ACCEPTANCE.json"
        write_json(final_path, final)
        final["final_manifest"] = self._artifact_record(final_path, "S3_FINAL_ACCEPTANCE")

        final_verification = self.verify_final(final_path)
        if final_verification.get("status") != "PASS":
            raise ResearchMermaidExportError("Final S3 evidence failed restart verification")
        final_verification_path = evidence_dir / "S3_FINAL_VERIFICATION.json"
        write_json(final_verification_path, final_verification)
        bundle_path = self._build_evidence_bundle(
            evidence_dir,
            prepared,
            final,
            final_verification_path,
        )
        result = {
            **final,
            "verification": self._artifact_record(
                final_verification_path, "S3_FINAL_VERIFICATION"
            ),
            "evidence_bundle": self._artifact_record(bundle_path, "S3_EVIDENCE_BUNDLE"),
        }
        result_path = evidence_dir / "S3_RESULT.json"
        write_json(result_path, result)
        result["result_manifest"] = self._artifact_record(result_path, "S3_RESULT")
        self.db.audit(
            "S3_FINALIZED",
            project_id=project_id,
            object_id=prepared["run_id"],
            metadata={
                "status": "PASS",
                "docx_sha256": final["delivery"]["docx"]["sha256"],
                "pdf_sha256": final["delivery"]["pdf"]["sha256"],
                "package_sha256": final["delivery"]["package"]["sha256"],
                "bundle_sha256": result["evidence_bundle"]["sha256"],
            },
        )
        return result

    def verify_prepared(self, checkpoint: str | Path | dict[str, Any]) -> dict[str, Any]:
        checkpoint_path, prepared = self._load_checkpoint(checkpoint)
        findings: list[dict[str, Any]] = []
        if prepared.get("phase") != "PREPARED":
            findings.append({"code": "S3_CHECKPOINT_PHASE_INVALID"})
        self._verify_record(prepared.get("research", {}).get("portable_manifest"), findings)
        self._verify_record(prepared.get("claim_binding", {}).get("report"), findings)
        for diagram in prepared.get("diagrams", []):
            for record in diagram.get("artifacts", []):
                self._verify_record(record, findings)
        portable = prepared.get("research", {}).get("portable_manifest")
        if portable:
            self._verify_portable_research_manifest(
                self._resolve_reference(portable["reference"]), findings
            )
        if prepared.get("claim_binding", {}).get("status") != "PASS":
            findings.append({"code": "S3_CLAIM_BINDING_NOT_PASS"})
        if not prepared.get("required_figure_markers"):
            findings.append({"code": "S3_NO_REQUIRED_FIGURES"})
        return {
            "schema_version": self.schema_version,
            "status": "FAIL" if findings else "PASS",
            "verified_at": utc_now(),
            "checkpoint": self._portable_reference(checkpoint_path),
            "checkpoint_sha256": sha256_bytes(checkpoint_path.read_bytes()),
            "findings": findings,
        }

    def verify_final(self, final_manifest: str | Path | dict[str, Any]) -> dict[str, Any]:
        final_path, final = self._load_json_artifact(final_manifest)
        findings: list[dict[str, Any]] = []
        if final.get("phase") != "FINALIZED" or final.get("status") != "PASS":
            findings.append({"code": "S3_FINAL_STATUS_INVALID"})
        checkpoint_record = final.get("prepared_checkpoint")
        self._verify_record(checkpoint_record, findings)
        if checkpoint_record:
            try:
                prepared_verification = self.verify_prepared(
                    self._resolve_reference(checkpoint_record["reference"])
                )
            except Exception as exc:
                findings.append(
                    {"code": "S3_PREPARED_REVERIFY_FAILED", "message": str(exc)}
                )
            else:
                for item in prepared_verification.get("findings", []):
                    findings.append(
                        {
                            **item,
                            "code": f"S3_FINAL_{item.get('code', 'PREPARED_FINDING')}",
                        }
                    )
        for diagram in final.get("diagrams", []):
            for record in diagram.get("artifacts", []):
                self._verify_record(record, findings)
        for key in ("docx", "pdf", "package", "validation"):
            self._verify_record(final.get("delivery", {}).get(key), findings)
        validation_record = final.get("delivery", {}).get("validation")
        if validation_record:
            try:
                delivery = json.loads(
                    self._resolve_reference(validation_record["reference"]).read_text(
                        encoding="utf-8"
                    )
                )
                if delivery.get("status") != "PASS" or int(
                    delivery.get("blocking_finding_count") or 0
                ):
                    findings.append({"code": "S3_DELIVERY_VALIDATION_NOT_PASS"})
            except Exception as exc:
                findings.append(
                    {"code": "S3_DELIVERY_VALIDATION_UNREADABLE", "message": str(exc)}
                )
        return {
            "schema_version": self.schema_version,
            "status": "FAIL" if findings else "PASS",
            "verified_at": utc_now(),
            "final_manifest": self._portable_reference(final_path),
            "final_manifest_sha256": sha256_bytes(final_path.read_bytes()),
            "findings": findings,
        }

    def _render_diagrams(
        self,
        diagrams: list[dict[str, Any]],
        claim_report: dict[str, Any],
        *,
        project_id: str,
        workflow_id: str | None,
        security_level: str,
    ) -> list[dict[str, Any]]:
        if not diagrams:
            raise ResearchMermaidExportError("At least one evidence-linked Mermaid diagram is required")
        bindings = {
            str(item.get("claim_id")): set(item.get("source_ids") or [])
            for item in claim_report.get("bindings", [])
        }
        seen_sections: set[str] = set()
        records: list[dict[str, Any]] = []
        for spec in diagrams:
            section_id = str(spec.get("section_id") or "").strip()
            claim_ids = [str(value) for value in spec.get("claim_ids") or []]
            source_ids = [str(value) for value in spec.get("source_ids") or []]
            if not section_id or section_id in seen_sections:
                raise ResearchMermaidExportError("Diagram section_id is missing or duplicated")
            seen_sections.add(section_id)
            if not claim_ids or not source_ids:
                raise ResearchMermaidExportError(
                    f"Diagram {section_id} must declare claim_ids and source_ids"
                )
            unknown_claims = [claim_id for claim_id in claim_ids if claim_id not in bindings]
            if unknown_claims:
                raise ResearchMermaidExportError(
                    f"Diagram {section_id} references unknown claims: {unknown_claims}"
                )
            allowed_sources = set().union(*(bindings[claim_id] for claim_id in claim_ids))
            unbound_sources = sorted(set(source_ids) - allowed_sources)
            if unbound_sources:
                raise ResearchMermaidExportError(
                    f"Diagram {section_id} uses sources not bound to its claims: {unbound_sources}"
                )
            result = self.skill_executor.execute(
                "mermaid.render",
                {
                    "section_id": section_id,
                    "caption": str(spec.get("caption") or "结构图"),
                    "width_cm": float(spec.get("width_cm") or 15.0),
                    "mermaid_source": str(spec.get("mermaid_source") or ""),
                    "argument_purpose": spec.get("argument_purpose"),
                    "claim_id": claim_ids[0] if len(claim_ids) == 1 else None,
                    "evidence_ids": source_ids,
                    "section_contract_id": spec.get("section_contract_id"),
                },
                project_id=project_id,
                workflow_id=workflow_id,
                security_level=security_level,
            )
            if result.status != "PASS":
                raise ResearchMermaidExportError(
                    f"Mermaid render did not pass for {section_id}: {result.status}"
                )
            records.append(
                {
                    "section_id": section_id,
                    "caption": result.output.get("caption"),
                    "claim_ids": claim_ids,
                    "source_ids": source_ids,
                    "figure_marker": result.output["figure_marker"],
                    "source_reference": result.output.get("source_reference"),
                    "svg_reference": result.output.get("svg_reference"),
                    "png_reference": result.output.get("png_reference"),
                    "source_sha256": result.output.get("source_sha256"),
                    "svg_sha256": result.output.get("svg_sha256"),
                    "png_sha256": result.output.get("png_sha256"),
                    "cache_hit": result.output.get("cache_hit"),
                    "artifacts": [
                        self._artifact_record(Path(path), self._diagram_artifact_type(Path(path)))
                        for path in result.artifacts
                    ],
                }
            )
        return records

    @staticmethod
    def _require_markers_in_approved_candidates(
        markers: list[str], candidates: list[dict[str, Any]]
    ) -> None:
        paragraphs = [
            str(paragraph).strip()
            for candidate in candidates
            for paragraph in candidate.get("paragraphs", [])
        ]
        for marker in markers:
            count = sum(1 for paragraph in paragraphs if paragraph == marker)
            if count != 1:
                raise ResearchMermaidExportError(
                    f"Required figure marker must appear exactly once in approved content; found {count}"
                )

    def _write_portable_research_manifest(
        self, archive_manifest_path: Path, evidence_dir: Path
    ) -> Path:
        manifest = json.loads(archive_manifest_path.read_text(encoding="utf-8"))
        portable_records = []
        for record in manifest.get("records", []):
            item = dict(record)
            for key in ("raw_path", "text_path", "metadata_path"):
                item[key] = self._portable_reference(Path(str(record[key])))
            portable_records.append(item)
        portable = {
            **manifest,
            "portable": True,
            "records": portable_records,
            "connector_response": (
                self._portable_reference(Path(str(manifest["connector_response"])))
                if manifest.get("connector_response")
                else None
            ),
        }
        path = evidence_dir / "research-manifest.portable.json"
        write_json(path, portable)
        return path

    def _verify_portable_research_manifest(
        self, manifest_path: Path, findings: list[dict[str, Any]]
    ) -> None:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            findings.append({"code": "S3_PORTABLE_RESEARCH_MANIFEST_INVALID", "message": str(exc)})
            return
        for record in manifest.get("records", []):
            for path_key, hash_key in (
                ("raw_path", "snapshot_sha256"),
                ("text_path", "text_sha256"),
            ):
                path = self._resolve_reference(str(record.get(path_key) or ""))
                if not path.is_file():
                    findings.append(
                        {"code": "S3_RESEARCH_ARTIFACT_MISSING", "reference": record.get(path_key)}
                    )
                    continue
                actual = sha256_bytes(path.read_bytes())
                if path_key == "text_path":
                    actual = sha256_bytes(path.read_text(encoding="utf-8").encode("utf-8"))
                if actual != record.get(hash_key):
                    findings.append(
                        {"code": "S3_RESEARCH_ARTIFACT_HASH_MISMATCH", "reference": record.get(path_key)}
                    )
            metadata = self._resolve_reference(str(record.get("metadata_path") or ""))
            if not metadata.is_file():
                findings.append(
                    {"code": "S3_RESEARCH_METADATA_MISSING", "reference": record.get("metadata_path")}
                )
            else:
                try:
                    metadata_payload = json.loads(metadata.read_text(encoding="utf-8"))
                except Exception as exc:
                    findings.append(
                        {
                            "code": "S3_RESEARCH_METADATA_INVALID",
                            "reference": record.get("metadata_path"),
                            "message": str(exc),
                        }
                    )
                else:
                    for key in ("source_id", "snapshot_sha256", "text_sha256"):
                        if metadata_payload.get(key) != record.get(key):
                            findings.append(
                                {
                                    "code": "S3_RESEARCH_METADATA_MISMATCH",
                                    "reference": record.get("metadata_path"),
                                    "field": key,
                                }
                            )
        connector = manifest.get("connector_response")
        if connector:
            connector_path = self._resolve_reference(str(connector))
            if not connector_path.is_file():
                findings.append({"code": "S3_CONNECTOR_RESPONSE_MISSING", "reference": connector})
            elif manifest.get("connector_response_sha256") and sha256_bytes(
                connector_path.read_bytes()
            ) != manifest.get("connector_response_sha256"):
                findings.append(
                    {"code": "S3_CONNECTOR_RESPONSE_HASH_MISMATCH", "reference": connector}
                )

    @staticmethod
    def _portable_sources(research_output: dict[str, Any]) -> list[dict[str, Any]]:
        result = []
        source_refs = {
            str(item.get("source_id")): item for item in research_output.get("sources", [])
        }
        for item in research_output.get("source_catalog", []):
            source_id = str(item.get("source_id"))
            result.append(
                {
                    "source_id": source_id,
                    "title": item.get("title"),
                    "url": item.get("url"),
                    "published_at": item.get("published_at"),
                    "publisher": item.get("publisher"),
                    "source_type": item.get("source_type"),
                    "authority_rank": item.get("authority_rank"),
                    "snapshot_sha256": item.get("snapshot_sha256"),
                    "text_sha256": item.get("text_sha256"),
                    "source_ref": source_refs.get(source_id),
                }
            )
        return result

    def _persist_claim_report(self, report: dict[str, Any], evidence_dir: Path) -> Path:
        existing = report.get("report_path")
        if existing and Path(str(existing)).is_file():
            return Path(str(existing))
        path = evidence_dir / "claim-binding.json"
        write_json(path, report)
        return path

    def _build_evidence_bundle(
        self,
        evidence_dir: Path,
        prepared: dict[str, Any],
        final: dict[str, Any],
        verification_path: Path,
    ) -> Path:
        records: list[dict[str, Any]] = [
            prepared["research"]["portable_manifest"],
            prepared["claim_binding"]["report"],
            final["prepared_checkpoint"],
            final["delivery"]["package"],
            final["final_manifest"],
            self._artifact_record(verification_path, "S3_FINAL_VERIFICATION"),
        ]
        records.extend(
            self._portable_research_artifact_records(
                prepared["research"]["portable_manifest"]
            )
        )
        for diagram in prepared.get("diagrams", []):
            records.extend(diagram.get("artifacts", []))
        bundle_path = evidence_dir / "s3-research-mermaid-export-evidence.zip"
        seen: set[str] = set()
        with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for record in records:
                reference = str(record.get("reference") or "")
                if not reference or reference in seen:
                    continue
                seen.add(reference)
                path = self._resolve_reference(reference)
                if not path.is_file():
                    raise ResearchMermaidExportError(
                        f"Required S3 bundle artifact is missing: {reference}"
                    )
                archive.write(path, arcname=reference.removeprefix(ARTIFACT_SCHEME))
        return bundle_path


    def _portable_research_artifact_records(
        self, portable_manifest_record: dict[str, Any]
    ) -> list[dict[str, Any]]:
        manifest_path = self._resolve_reference(portable_manifest_record["reference"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        records: list[dict[str, Any]] = []
        artifact_types = {
            "raw_path": "RESEARCH_RAW_SNAPSHOT",
            "text_path": "RESEARCH_EXTRACTED_TEXT",
            "metadata_path": "RESEARCH_SOURCE_METADATA",
        }
        for source in manifest.get("records", []):
            for key, artifact_type in artifact_types.items():
                path = self._resolve_reference(str(source.get(key) or ""))
                records.append(self._artifact_record(path, artifact_type))
        connector = manifest.get("connector_response")
        if connector:
            records.append(
                self._artifact_record(
                    self._resolve_reference(str(connector)),
                    "RESEARCH_CONNECTOR_RESPONSE",
                )
            )
        return records

    def _artifact_record(self, path: Path, artifact_type: str) -> dict[str, Any]:
        path = path.resolve()
        if not path.is_file():
            raise ResearchMermaidExportError(f"Artifact is missing: {path.name}")
        return {
            "artifact_type": artifact_type,
            "reference": self._portable_reference(path),
            "sha256": sha256_bytes(path.read_bytes()),
            "size_bytes": path.stat().st_size,
        }

    def _verify_record(
        self, record: dict[str, Any] | None, findings: list[dict[str, Any]]
    ) -> None:
        if not record:
            findings.append({"code": "S3_ARTIFACT_RECORD_MISSING"})
            return
        try:
            path = self._resolve_reference(str(record.get("reference") or ""))
        except Exception as exc:
            findings.append({"code": "S3_ARTIFACT_REFERENCE_INVALID", "message": str(exc)})
            return
        if not path.is_file():
            findings.append({"code": "S3_ARTIFACT_MISSING", "reference": record.get("reference")})
            return
        if sha256_bytes(path.read_bytes()) != record.get("sha256"):
            findings.append(
                {"code": "S3_ARTIFACT_HASH_MISMATCH", "reference": record.get("reference")}
            )

    def _portable_reference(self, path: Path) -> str:
        root = Path(self.settings.data_dir).resolve()
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(root)
        except ValueError as exc:
            raise ResearchMermaidExportError(
                f"S3 evidence artifact is outside APP_DATA_DIR: {resolved.name}"
            ) from exc
        return ARTIFACT_SCHEME + relative.as_posix()

    def _resolve_reference(self, reference: str) -> Path:
        if not reference.startswith(ARTIFACT_SCHEME):
            raise ResearchMermaidExportError("Only artifact:// references are accepted")
        relative = Path(reference.removeprefix(ARTIFACT_SCHEME))
        if relative.is_absolute() or ".." in relative.parts:
            raise ResearchMermaidExportError("Unsafe artifact reference")
        root = Path(self.settings.data_dir).resolve()
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ResearchMermaidExportError("Artifact reference escapes APP_DATA_DIR") from exc
        return path

    def _load_checkpoint(
        self, checkpoint: str | Path | dict[str, Any]
    ) -> tuple[Path, dict[str, Any]]:
        return self._load_json_artifact(checkpoint, default_name="S3_PREPARED_CHECKPOINT.json")

    def _load_json_artifact(
        self,
        value: str | Path | dict[str, Any],
        *,
        default_name: str = "S3_FINAL_ACCEPTANCE.json",
    ) -> tuple[Path, dict[str, Any]]:
        if isinstance(value, dict):
            record = value.get("checkpoint") or value.get("final_manifest")
            if not record:
                raise ResearchMermaidExportError(f"Dictionary does not reference {default_name}")
            path = self._resolve_reference(record["reference"])
        elif isinstance(value, Path):
            path = value.resolve()
        else:
            raw = str(value)
            path = self._resolve_reference(raw) if raw.startswith(ARTIFACT_SCHEME) else Path(raw).resolve()
        if not path.is_file():
            raise ResearchMermaidExportError(f"JSON artifact is missing: {path.name}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ResearchMermaidExportError(f"JSON artifact is invalid: {path.name}") from exc
        return path, payload

    def _evidence_root(self, run_id: str) -> Path:
        return Path(self.settings.data_dir) / "recovery_evidence" / "s3" / run_id

    @staticmethod
    def _diagram_artifact_type(path: Path) -> str:
        return {
            ".mmd": "MERMAID_SOURCE",
            ".svg": "MERMAID_SVG",
            ".png": "MERMAID_PNG",
            ".json": "MERMAID_METADATA",
        }.get(path.suffix.lower(), "MERMAID_ARTIFACT")

    def _audit_failure(self, event_type: str, project_id: str, run_id: str, error: str) -> None:
        self.db.audit(
            event_type,
            project_id=project_id,
            object_id=run_id,
            metadata={"error": error[:4000]},
        )
