from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .exporter import DocxExporter, ExportDenied
from .quality import QualityLifecycleManager
from .util import new_id, sha256_bytes, sha256_json, utc_now, write_json


class PostExportAcceptanceError(RuntimeError):
    pass


class PostExportQualityLifecycleManager(QualityLifecycleManager):
    """Post-export routing without changing the shared quality baseline."""

    def ingest_delivery_findings(
        self,
        *,
        project_id: str,
        workflow_id: str | None,
        validation_run_id: str,
        findings: Any,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for raw in findings:
            finding = dict(raw)
            finding.setdefault("severity", "P1")
            finding.setdefault("blocking", True)
            finding.setdefault("repairable", True)
            finding.setdefault("evidence_refs", [])
            route = self.route_delivery_finding(finding)
            finding.setdefault(
                "suggested_route",
                "WRITING_AGENT" if route.owner_kind == "AGENT" else "BLOCK",
            )
            section_ids = [
                str(item) for item in finding.get("responsible_section_ids") or [] if item
            ]
            scopes = (
                [f"section:{section_id}" for section_id in section_ids]
                if route.owner_kind == "AGENT" and section_ids
                else [self._delivery_scope(finding)]
            )
            for scope_key in scopes:
                scoped = dict(finding)
                if scope_key.startswith("section:"):
                    scoped["responsible_section_ids"] = [scope_key.removeprefix("section:")]
                    scoped["target_path_or_span"] = scope_key
                records.append(
                    self._open_or_refresh(
                        project_id=project_id,
                        workflow_id=workflow_id,
                        prompt_id="DELIVERY_VALIDATOR",
                        run_id=validation_run_id,
                        finding=scoped,
                        scope_key=scope_key,
                        responsibility=route,
                    )
                )
        return records

    def route_delivery_finding(self, finding: dict[str, Any]):  # type: ignore[no-untyped-def]
        category = str(finding.get("category") or "SYSTEM")
        target_type = str(finding.get("target_type") or "").upper()
        code = str(finding.get("code") or "").upper()
        engineering = category != "CONTENT" and (
            category in {"FORMAT", "SYSTEM"}
            or any(token in target_type for token in {"DOCX", "PDF", "EXPORT", "RENDER", "LAYOUT"})
            or any(token in code for token in {"EXPORT", "RENDER", "DOCX", "PDF", "LAYOUT", "CLIP", "OVERLAP"})
        )
        route = super().route_delivery_finding(finding)
        if not engineering:
            return route
        return type(route)(
            owner_kind="ENGINEERING",
            owner="EXPORT_ENGINEERING",
            workflow_type=None,
            stage_prompt_ids=("EXPORT_ENGINEERING",),
            reviewer_prompt_id="DELIVERY_VALIDATOR",
            reason="导出、渲染、版式或文件结构缺陷属于工程责任，不允许通过改写正文掩盖。",
        )


class PostExportAcceptanceManager:
    """Run and persist DOCX/PDF post-export acceptance attempts.

    Content findings go back to the responsible WF-4 section and cannot be bypassed
    by another export. Delivery-engineering findings may use a controlled re-export
    only when the reviewed candidate set is unchanged; a new validator run must then
    verify the repaired files.
    """

    artifact_type = "POST_EXPORT_ACCEPTANCE"

    def __init__(self, db, settings, exporter: DocxExporter | None = None):
        self.db = db
        self.settings = settings
        self.exporter = exporter or DocxExporter(db, settings)
        self.quality_manager = PostExportQualityLifecycleManager(db)

    def run(
        self,
        project_id: str,
        *,
        workflow_id: str | None = None,
        engineering_repair_id: str | None = None,
        expected_candidate_set_hash: str | None = None,
        reuse_verified: bool = True,
    ) -> dict[str, Any]:
        if reuse_verified and not engineering_repair_id:
            reused = self._reusable_pass(project_id)
            if reused:
                return {**reused, "reused_after_restart": True}

        snapshot = self.exporter.candidate_snapshot(project_id)
        if not snapshot["sections"]:
            raise PostExportAcceptanceError("No final Expression-Critic-approved candidates are available")
        integration_review = self._integration_review_snapshot(project_id, workflow_id, snapshot)
        previous_attempt = self.latest_attempt(project_id)
        if (
            not engineering_repair_id
            and previous_attempt
            and previous_attempt.get("status") == "REVISE_CONTENT"
            and (previous_attempt.get("candidate_snapshot") or {}).get("candidate_set_hash")
            == snapshot.get("candidate_set_hash")
        ):
            raise PostExportAcceptanceError(
                "A post-export content finding requires a new reviewed candidate set before re-export"
            )

        prior_blockers = self.quality_manager.open_blockers(project_id)
        if engineering_repair_id:
            if not expected_candidate_set_hash:
                previous = self.latest_attempt(project_id)
                expected_candidate_set_hash = str(
                    (previous or {}).get("candidate_snapshot", {}).get("candidate_set_hash") or ""
                )
            document_path = self.exporter.export_delivery_repair(
                project_id,
                expected_candidate_set_hash=expected_candidate_set_hash or "",
                engineering_repair_id=engineering_repair_id,
            )
        else:
            document_path = self.exporter.export(project_id)

        pdf_path = self.exporter.export_pdf(project_id, document_path)
        validation_run_id = new_id("delivery-validation")
        delivery = self.exporter.inspect_delivery(
            project_id,
            document_path,
            pdf_path,
            validation_run_id=validation_run_id,
        )
        records = self.quality_manager.ingest_delivery_findings(
            project_id=project_id,
            workflow_id=workflow_id,
            validation_run_id=validation_run_id,
            findings=delivery.get("findings") or [],
        )
        current_finding_ids = {str(item.get("finding_id")) for item in records}

        verified_engineering: list[str] = []
        if engineering_repair_id:
            for blocker in prior_blockers:
                responsibility = blocker.get("responsibility") or {}
                if (
                    responsibility.get("owner") != "EXPORT_ENGINEERING"
                    or responsibility.get("owner_kind") != "ENGINEERING"
                ):
                    continue
                finding_id = str(blocker.get("finding_id") or "")
                if not finding_id or finding_id in current_finding_ids:
                    continue
                self.quality_manager.add_repair_evidence(
                    finding_id,
                    project_id=project_id,
                    prompt_id="EXPORT_ENGINEERING",
                    run_id=engineering_repair_id,
                )
                self.quality_manager.verify_finding(
                    finding_id,
                    project_id=project_id,
                    reviewer="DELIVERY_VALIDATOR",
                    review_run_id=validation_run_id,
                    review_hash=sha256_json(
                        {
                            "validation_run_id": validation_run_id,
                            "docx_sha256": delivery.get("docx_sha256"),
                            "pdf_sha256": delivery.get("pdf_sha256"),
                            "absent_finding_id": finding_id,
                        }
                    ),
                )
                verified_engineering.append(finding_id)

        open_blockers = self.quality_manager.open_blockers(project_id)
        owner_counts: dict[str, int] = {}
        for item in open_blockers:
            owner = str((item.get("responsibility") or {}).get("owner") or "UNROUTED")
            owner_counts[owner] = owner_counts.get(owner, 0) + 1

        package_path: Path | None = None
        if delivery.get("status") == "PASS" and not open_blockers:
            package_path = self.exporter.package_validated_delivery(
                project_id, document_path, pdf_path, delivery
            )
            status = "PASS"
        elif owner_counts.get("WRITING_AGENT"):
            status = "REVISE_CONTENT"
        elif owner_counts.get("EXPORT_ENGINEERING"):
            status = "ENGINEERING_REPAIR_REQUIRED"
        else:
            status = "BLOCK"

        attempt = {
            "schema_version": "1.0",
            "attempt_id": new_id("post-export"),
            "project_id": project_id,
            "workflow_id": workflow_id,
            "created_at": utc_now(),
            "status": status,
            "candidate_snapshot": snapshot,
            "integration_review": integration_review,
            "validation_run_id": validation_run_id,
            "engineering_repair_id": engineering_repair_id,
            "document": self._file_record(document_path),
            "pdf": self._file_record(pdf_path),
            "package": self._file_record(package_path) if package_path else None,
            "delivery_report": self._file_record(Path(delivery["report_path"])),
            "structure_report": self._file_record(Path(delivery["structure_report"])),
            "visual_report": self._file_record(Path(delivery["visual_report"])),
            "screenshots": [
                self._file_record(path)
                for path in sorted(Path(delivery["screenshot_dir"]).glob("page-*.png"))
            ],
            "finding_ids": [str(item.get("finding_id")) for item in records],
            "verified_engineering_finding_ids": verified_engineering,
            "open_blocker_count": len(open_blockers),
            "open_blockers_by_owner": owner_counts,
            "routing": [
                {
                    "finding_id": item.get("finding_id"),
                    "code": (item.get("finding") or {}).get("code"),
                    "owner": (item.get("responsibility") or {}).get("owner"),
                    "owner_kind": (item.get("responsibility") or {}).get("owner_kind"),
                    "responsible_section_ids": (item.get("finding") or {}).get(
                        "responsible_section_ids", []
                    ),
                }
                for item in records
            ],
            "checks": {
                "candidate_snapshot_present": bool(snapshot["sections"]),
                "matches_latest_full_integration_review": integration_review.get("status") in {"PASS", "NOT_APPLICABLE"},
                "docx_pdf_hashes_recorded": bool(
                    delivery.get("docx_sha256") and delivery.get("pdf_sha256")
                ),
                "structure_report_recorded": Path(delivery["structure_report"]).is_file(),
                "visual_report_recorded": Path(delivery["visual_report"]).is_file(),
                "page_screenshots_recorded": bool(
                    list(Path(delivery["screenshot_dir"]).glob("page-*.png"))
                ),
                "no_open_blockers": not open_blockers,
                "package_created_only_after_pass": (package_path is not None) == (
                    delivery.get("status") == "PASS" and not open_blockers
                ),
            },
        }
        attempt["attempt_hash"] = sha256_json(
            {key: value for key, value in attempt.items() if key != "attempt_hash"}
        )
        report_path = document_path.with_suffix(".post-export-acceptance.json")
        write_json(report_path, attempt)
        attempt["report_path"] = str(report_path)
        self._persist(attempt)
        self.db.audit(
            "POST_EXPORT_ACCEPTANCE_COMPLETED",
            project_id=project_id,
            object_id=attempt["attempt_id"],
            metadata={
                "status": status,
                "candidate_set_hash": snapshot["candidate_set_hash"],
                "validation_run_id": validation_run_id,
                "open_blocker_count": len(open_blockers),
                "package": package_path.name if package_path else None,
            },
        )
        return attempt

    def _integration_review_snapshot(
        self,
        project_id: str,
        workflow_id: str | None,
        candidate_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        if not workflow_id:
            return {"status": "NOT_APPLICABLE", "reason": "workflow_id not supplied"}
        row = self.db.fetchone(
            "SELECT workflow_type,state_json FROM workflows WHERE id=? AND project_id=?",
            (workflow_id, project_id),
        )
        if not row:
            raise PostExportAcceptanceError(f"Workflow not found for post-export acceptance: {workflow_id}")
        state = json.loads(row.get("state_json") or "{}")
        reviews = [
            item for item in state.get("full_proposal_review_history") or []
            if isinstance(item, dict) and item.get("status") == "PASS"
        ]
        if not reviews:
            if state.get("full_proposal_contract"):
                raise PostExportAcceptanceError(
                    "Full-proposal export requires a persisted PASS Full Integration Critic review"
                )
            return {"status": "NOT_APPLICABLE", "reason": "workflow is not a full-proposal run"}
        review = reviews[-1]
        expected = [
            {
                "section_id": str(item.get("section_id") or ""),
                "candidate_id": str(item.get("candidate_id") or ""),
                "polish_run_id": str(item.get("polish_run_id") or ""),
                "expression_critic_run_id": str(item.get("expression_critic_run_id") or ""),
            }
            for item in review.get("section_manifest") or []
        ]
        actual = [
            {key: str(item.get(key) or "") for key in (
                "section_id", "candidate_id", "polish_run_id", "expression_critic_run_id"
            )}
            for item in candidate_snapshot.get("sections") or []
        ]
        if expected != actual:
            raise PostExportAcceptanceError(
                "Export candidate snapshot differs from the latest PASS Full Integration Critic snapshot"
            )
        return {
            "status": "PASS",
            "review_run_id": review.get("run_id"),
            "review_candidate_set_hash": review.get("candidate_set_hash"),
            "section_count": len(expected),
            "section_manifest_hash": sha256_json(expected),
        }

    def latest_attempt(self, project_id: str) -> dict[str, Any] | None:
        row = self.db.fetchone(
            """SELECT content_json FROM artifacts
               WHERE project_id=? AND artifact_type=?
               ORDER BY version DESC, created_at DESC LIMIT 1""",
            (project_id, self.artifact_type),
        )
        return json.loads(row["content_json"]) if row else None

    def _reusable_pass(self, project_id: str) -> dict[str, Any] | None:
        row = self.db.fetchone(
            """SELECT content_json FROM artifacts
               WHERE project_id=? AND artifact_type=? AND status='PASS'
               ORDER BY version DESC, created_at DESC LIMIT 1""",
            (project_id, self.artifact_type),
        )
        if not row:
            return None
        record = json.loads(row["content_json"])
        current_hash = self.exporter.candidate_snapshot(project_id).get("candidate_set_hash")
        if current_hash != (record.get("candidate_snapshot") or {}).get("candidate_set_hash"):
            return None
        for key in ("document", "pdf", "package", "delivery_report", "structure_report", "visual_report"):
            item = record.get(key)
            if not item or not self._verify_file_record(item):
                return None
        if not record.get("screenshots") or not all(
            self._verify_file_record(item) for item in record["screenshots"]
        ):
            return None
        if self.quality_manager.open_blockers(project_id):
            return None
        return record

    def _persist(self, attempt: dict[str, Any]) -> None:
        row = self.db.fetchone(
            "SELECT MAX(version) AS version FROM artifacts WHERE project_id=? AND artifact_type=?",
            (attempt["project_id"], self.artifact_type),
        )
        version = int((row or {}).get("version") or 0) + 1
        project = self.db.fetchone(
            "SELECT security_level FROM projects WHERE id=?", (attempt["project_id"],)
        )
        self.db.execute(
            """INSERT INTO artifacts(
                   id,project_id,workflow_id,artifact_type,prompt_id,version,status,
                   security_level,context_hash,content_json,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                new_id("artifact"),
                attempt["project_id"],
                attempt.get("workflow_id"),
                self.artifact_type,
                None,
                version,
                attempt["status"],
                str((project or {}).get("security_level") or "INTERNAL"),
                str((attempt.get("candidate_snapshot") or {}).get("candidate_set_hash") or ""),
                json.dumps(attempt, ensure_ascii=False),
                utc_now(),
            ),
        )

    @staticmethod
    def _file_record(path: Path | None) -> dict[str, Any] | None:
        if path is None:
            return None
        resolved = path.resolve()
        if not resolved.is_file():
            raise PostExportAcceptanceError(f"Required evidence file is missing: {resolved}")
        return {
            "path": str(resolved),
            "filename": resolved.name,
            "size_bytes": resolved.stat().st_size,
            "sha256": sha256_bytes(resolved.read_bytes()),
        }

    @staticmethod
    def _verify_file_record(record: dict[str, Any]) -> bool:
        path = Path(str(record.get("path") or ""))
        return bool(
            path.is_file()
            and int(record.get("size_bytes") or -1) == path.stat().st_size
            and str(record.get("sha256") or "") == sha256_bytes(path.read_bytes())
        )
