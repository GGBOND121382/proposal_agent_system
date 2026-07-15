from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any, Iterable

from .figure_protocol import ARTIFACT_SCHEME, resolve_figure_reference
from .skills.research_audit import verify_research_archive
from .util import sha256_bytes, sha256_json, sha256_text, utc_now, write_json


class S3EvidenceError(RuntimeError):
    """Raised when the Research + Mermaid + Export integration evidence is incomplete."""


_REQUIRED_EXPORT_SUFFIXES = (
    ".docx",
    ".pdf",
    ".integrity.json",
    ".manifest.json",
    ".pdf-conversion.json",
    ".structure-findings.json",
    ".visual-findings.json",
    ".delivery-validation.json",
)


def _artifact_reference(path: Path, data_dir: Path) -> str:
    root = data_dir.resolve()
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise S3EvidenceError(f"S3 artifact is outside APP_DATA_DIR: {resolved.name}") from exc
    return ARTIFACT_SCHEME + relative.as_posix()


def _resolve_artifact(reference: str, data_dir: Path) -> Path:
    if not str(reference).startswith(ARTIFACT_SCHEME):
        raise S3EvidenceError(f"S3 manifest requires portable artifact references: {reference}")
    relative = str(reference).removeprefix(ARTIFACT_SCHEME)
    candidate = (data_dir.resolve() / relative).resolve()
    try:
        candidate.relative_to(data_dir.resolve())
    except ValueError as exc:
        raise S3EvidenceError("S3 artifact reference escapes APP_DATA_DIR") from exc
    return candidate


def _file_record(path: Path, data_dir: Path, *, role: str) -> dict[str, Any]:
    if not path.is_file():
        raise S3EvidenceError(f"Required S3 artifact is missing: {path.name}")
    return {
        "role": role,
        "reference": _artifact_reference(path, data_dir),
        "sha256": sha256_bytes(path.read_bytes()),
        "size_bytes": path.stat().st_size,
    }


def _collect_research_records(research_output: dict[str, Any], data_dir: Path) -> list[dict[str, Any]]:
    manifest_path = Path(str(research_output.get("archive_manifest") or ""))
    verification = verify_research_archive(manifest_path)
    if verification.get("status") != "PASS":
        raise S3EvidenceError("Research archive verification did not pass")
    declared = research_output.get("archive_verification") or {}
    if declared and declared.get("status") != "PASS":
        raise S3EvidenceError("Research output declared a failed archive verification")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = [_file_record(manifest_path, data_dir, role="RESEARCH_MANIFEST")]
    source_index = Path(str(research_output.get("source_index") or ""))
    if source_index.is_file():
        records.append(_file_record(source_index, data_dir, role="RESEARCH_SOURCE_INDEX"))
    for source in manifest.get("records", []):
        source_id = str(source.get("source_id") or "unknown")
        for key, role in (
            ("raw_path", "RESEARCH_RAW_SNAPSHOT"),
            ("text_path", "RESEARCH_TEXT_EXTRACT"),
            ("metadata_path", "RESEARCH_SOURCE_METADATA"),
        ):
            record = _file_record(Path(str(source.get(key) or "")), data_dir, role=role)
            record["source_id"] = source_id
            records.append(record)
    connector = manifest.get("connector_response")
    if connector:
        records.append(_file_record(Path(str(connector)), data_dir, role="RESEARCH_CONNECTOR_RESPONSE"))
    return records


def _validate_claim_bindings(claim_report: dict[str, Any]) -> set[str]:
    if claim_report.get("status") != "PASS":
        raise S3EvidenceError("PUBLIC_CLAIM binding did not pass")
    bindings = claim_report.get("bindings") or []
    if not bindings:
        raise S3EvidenceError("PUBLIC_CLAIM binding report contains no bindings")
    source_ids: set[str] = set()
    for binding in bindings:
        if not binding.get("claim_id") or not binding.get("source_ids"):
            raise S3EvidenceError("PUBLIC_CLAIM binding is missing claim_id or source_ids")
        source_ids.update(str(item) for item in binding.get("source_ids") or [])
    return source_ids


def _collect_mermaid_records(
    diagrams: Iterable[dict[str, Any]],
    data_dir: Path,
    bound_source_ids: set[str],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen = 0
    for diagram in diagrams:
        seen += 1
        evidence_ids = {str(item) for item in diagram.get("evidence_ids") or []}
        if not evidence_ids or not evidence_ids.issubset(bound_source_ids):
            raise S3EvidenceError("Mermaid diagram is not bound to validated PUBLIC_CLAIM evidence")
        for ref_key, hash_key, role in (
            ("source_reference", "source_sha256", "MERMAID_SOURCE"),
            ("svg_reference", "svg_sha256", "MERMAID_SVG"),
            ("png_reference", "png_sha256", "MERMAID_PNG"),
            ("metadata_reference", None, "MERMAID_METADATA"),
        ):
            reference = str(diagram.get(ref_key) or "")
            path = _resolve_artifact(reference, data_dir)
            record = _file_record(path, data_dir, role=role)
            if hash_key:
                if ref_key == "source_reference":
                    declared_actual = sha256_text(path.read_text(encoding="utf-8").strip())
                elif ref_key == "svg_reference":
                    declared_actual = sha256_text(path.read_text(encoding="utf-8"))
                else:
                    declared_actual = sha256_bytes(path.read_bytes())
                if declared_actual != diagram.get(hash_key):
                    raise S3EvidenceError(f"Mermaid artifact hash mismatch: {ref_key}")
                record["declared_sha256"] = declared_actual
            record.update({
                "section_id": diagram.get("section_id"),
                "claim_id": diagram.get("claim_id"),
                "evidence_ids": sorted(evidence_ids),
            })
            records.append(record)
        png = resolve_figure_reference(str(diagram.get("png_reference") or ""), data_dir)
        if png.stat().st_size < 100:
            raise S3EvidenceError("Mermaid PNG is invalid")
    if seen == 0:
        raise S3EvidenceError("S3 chain produced no Mermaid diagram")
    return records


def _validate_export_package(package_path: Path) -> None:
    if not package_path.is_file():
        raise S3EvidenceError("DOCX/PDF export package is missing")
    with zipfile.ZipFile(package_path) as archive:
        names = set(archive.namelist())
    missing = [suffix for suffix in _REQUIRED_EXPORT_SUFFIXES if not any(name.endswith(suffix) for name in names)]
    if missing:
        raise S3EvidenceError(f"Export package is incomplete: {missing}")
    if not any(name.startswith("pages/page-") and name.endswith(".png") for name in names):
        raise S3EvidenceError("Export package contains no page screenshot evidence")


def build_s3_evidence(
    *,
    data_dir: Path,
    run_dir: Path,
    research_output: dict[str, Any],
    claim_report: dict[str, Any],
    claim_report_path: Path,
    diagrams: list[dict[str, Any]],
    document_path: Path,
    pdf_path: Path,
    delivery_report: dict[str, Any],
    export_package_path: Path,
    database_path: Path | None = None,
    semantic_evidence_mode: str,
    source_commit: str | None = None,
) -> dict[str, Any]:
    data_dir = data_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    research_records = _collect_research_records(research_output, data_dir)
    bound_source_ids = _validate_claim_bindings(claim_report)
    claim_record = _file_record(claim_report_path, data_dir, role="PUBLIC_CLAIM_BINDINGS")
    if claim_record["sha256"] != sha256_json(claim_report):
        # write_json uses pretty JSON, so compare the parsed object hash rather than file bytes.
        parsed = json.loads(claim_report_path.read_text(encoding="utf-8"))
        if sha256_json(parsed) != sha256_json(claim_report):
            raise S3EvidenceError("PUBLIC_CLAIM binding report content drift")
    mermaid_records = _collect_mermaid_records(diagrams, data_dir, bound_source_ids)
    if delivery_report.get("status") != "PASS" or int(delivery_report.get("blocking_finding_count") or 0) != 0:
        raise S3EvidenceError("DOCX/PDF delivery validation did not pass")
    _validate_export_package(export_package_path)

    export_records = [
        _file_record(document_path, data_dir, role="DOCX"),
        _file_record(pdf_path, data_dir, role="PDF"),
        _file_record(Path(str(delivery_report.get("report_path") or "")), data_dir, role="DELIVERY_VALIDATION"),
        _file_record(export_package_path, data_dir, role="EXPORT_PACKAGE"),
    ]
    for suffix, role in (
        (".integrity.json", "DOCX_INTEGRITY"),
        (".manifest.json", "DOCX_MANIFEST"),
        (".pdf-conversion.json", "PDF_CONVERSION_LOG"),
        (".structure-findings.json", "STRUCTURE_FINDINGS"),
        (".visual-findings.json", "VISUAL_FINDINGS"),
    ):
        export_records.append(_file_record(document_path.with_suffix(suffix), data_dir, role=role))
    screenshots_dir = Path(str(delivery_report.get("screenshot_dir") or ""))
    screenshots = sorted(screenshots_dir.glob("page-*.png"))
    if not screenshots:
        raise S3EvidenceError("Delivery validation produced no page screenshots")
    export_records.extend(_file_record(path, data_dir, role="PAGE_SCREENSHOT") for path in screenshots)
    if database_path and database_path.is_file():
        export_records.append(_file_record(database_path, data_dir, role="WORKFLOW_CHECKPOINT"))

    manifest = {
        "schema_version": "1.0",
        "chain": "Research + Mermaid + Export",
        "status": "PASS",
        "created_at": utc_now(),
        "semantic_evidence_mode": semantic_evidence_mode,
        "source_commit": source_commit,
        "research": {
            "mode": research_output.get("mode"),
            "archive_session_id": research_output.get("archive_session_id"),
            "source_count": len(research_output.get("source_catalog") or []),
            "coverage_status": (research_output.get("coverage") or {}).get("status"),
            "records": research_records,
        },
        "claim_binding": {
            "status": claim_report.get("status"),
            "claim_count": claim_report.get("claim_count"),
            "bound_source_ids": sorted(bound_source_ids),
            "record": claim_record,
        },
        "mermaid": {
            "diagram_count": len(diagrams),
            "records": mermaid_records,
        },
        "export": {
            "delivery_status": delivery_report.get("status"),
            "blocking_finding_count": delivery_report.get("blocking_finding_count"),
            "records": export_records,
        },
    }
    manifest_path = run_dir / "S3_CHAIN_MANIFEST.json"
    write_json(manifest_path, manifest)

    bundle_path = run_dir / "s3-research-mermaid-export.zip"
    all_records = research_records + [claim_record] + mermaid_records + export_records
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(manifest_path, arcname=manifest_path.name)
        added: set[str] = set()
        for record in all_records:
            reference = str(record["reference"])
            if reference in added:
                continue
            added.add(reference)
            path = _resolve_artifact(reference, data_dir)
            archive.write(path, arcname=f"artifacts/{reference.removeprefix(ARTIFACT_SCHEME)}")

    acceptance = {
        "schema_version": "1.0",
        "status": "PASS",
        "chain": manifest["chain"],
        "semantic_evidence_mode": semantic_evidence_mode,
        "manifest": _artifact_reference(manifest_path, data_dir),
        "manifest_sha256": sha256_bytes(manifest_path.read_bytes()),
        "bundle": _artifact_reference(bundle_path, data_dir),
        "bundle_sha256": sha256_bytes(bundle_path.read_bytes()),
        "source_commit": source_commit,
        "created_at": utc_now(),
    }
    acceptance_path = run_dir / "S3_ACCEPTANCE.json"
    write_json(acceptance_path, acceptance)
    acceptance["report_path"] = str(acceptance_path)
    acceptance["manifest_path"] = str(manifest_path)
    acceptance["bundle_path"] = str(bundle_path)
    return acceptance


def verify_s3_evidence(acceptance_path: Path, data_dir: Path) -> dict[str, Any]:
    acceptance = json.loads(acceptance_path.read_text(encoding="utf-8"))
    failures: list[dict[str, Any]] = []
    for ref_key, hash_key in (("manifest", "manifest_sha256"), ("bundle", "bundle_sha256")):
        try:
            path = _resolve_artifact(str(acceptance.get(ref_key) or ""), data_dir)
        except S3EvidenceError as exc:
            failures.append({"code": "REFERENCE_INVALID", "field": ref_key, "message": str(exc)})
            continue
        if not path.is_file():
            failures.append({"code": "ARTIFACT_MISSING", "field": ref_key, "path": str(path)})
            continue
        actual = sha256_bytes(path.read_bytes())
        if actual != acceptance.get(hash_key):
            failures.append({"code": "ARTIFACT_HASH_MISMATCH", "field": ref_key, "expected": acceptance.get(hash_key), "actual": actual})
    if not failures:
        manifest_path = _resolve_artifact(str(acceptance["manifest"]), data_dir)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for section in ("research", "mermaid", "export"):
            for record in (manifest.get(section) or {}).get("records") or []:
                path = _resolve_artifact(str(record.get("reference") or ""), data_dir)
                if not path.is_file():
                    failures.append({"code": "CHAIN_ARTIFACT_MISSING", "role": record.get("role"), "path": str(path)})
                    continue
                actual = sha256_bytes(path.read_bytes())
                if actual != record.get("sha256"):
                    failures.append({"code": "CHAIN_ARTIFACT_HASH_MISMATCH", "role": record.get("role"), "path": str(path)})
        claim_record = (manifest.get("claim_binding") or {}).get("record") or {}
        if claim_record:
            path = _resolve_artifact(str(claim_record.get("reference") or ""), data_dir)
            if not path.is_file() or sha256_bytes(path.read_bytes()) != claim_record.get("sha256"):
                failures.append({"code": "CLAIM_BINDING_ARTIFACT_INVALID", "path": str(path)})
    return {
        "status": "FAIL" if failures else "PASS",
        "acceptance": str(acceptance_path),
        "verified_at": utc_now(),
        "failures": failures,
    }
