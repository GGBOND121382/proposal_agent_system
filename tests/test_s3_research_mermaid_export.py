from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from app.s3_evidence import S3EvidenceError, build_s3_evidence, verify_s3_evidence
from app.util import sha256_bytes, sha256_text, write_json


def _fixture(tmp_path: Path):
    data_dir = tmp_path
    research_root = data_dir / "research_archive" / "project" / "session"
    raw = research_root / "raw" / "source.json"
    text = research_root / "text" / "source.txt"
    metadata = research_root / "metadata" / "source.json"
    for path in (raw, text, metadata):
        path.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text('{"title":"source"}', encoding="utf-8")
    text.write_text("recent benchmark baseline limitations evidence", encoding="utf-8")
    source_record = {
        "source_id": "source-1",
        "raw_path": str(raw),
        "text_path": str(text),
        "metadata_path": str(metadata),
        "snapshot_sha256": sha256_bytes(raw.read_bytes()),
        "text_sha256": sha256_text(text.read_text(encoding="utf-8")),
    }
    write_json(metadata, {
        "source_id": source_record["source_id"],
        "snapshot_sha256": source_record["snapshot_sha256"],
        "text_sha256": source_record["text_sha256"],
    })
    manifest = research_root / "manifest.json"
    write_json(manifest, {"schema_version": "2.0", "records": [source_record]})
    source_index = research_root / "source_index.csv"
    source_index.write_text("source_id\nsource-1\n", encoding="utf-8")
    research_output = {
        "archive_manifest": str(manifest),
        "source_index": str(source_index),
        "archive_verification": {"status": "PASS"},
        "source_catalog": [{"source_id": "source-1"}],
        "coverage": {"status": "PASS"},
        "mode": "TEST_CONNECTOR",
        "archive_session_id": "session",
    }

    claim_report = {
        "status": "PASS",
        "claim_count": 1,
        "bindings": [{"claim_id": "claim-1", "source_ids": ["source-1"]}],
    }
    claim_path = data_dir / "claim_bindings" / "claim.json"
    write_json(claim_path, claim_report)

    diagram_root = data_dir / "diagram_artifacts" / "project" / "section"
    diagram_root.mkdir(parents=True)
    mmd = diagram_root / "diagram.mmd"
    svg = diagram_root / "diagram.svg"
    png = diagram_root / "diagram.png"
    meta = diagram_root / "diagram.json"
    mmd.write_text("flowchart TB\n A --> B\n", encoding="utf-8")
    svg.write_text("<svg xmlns='http://www.w3.org/2000/svg'><rect width='100' height='100'/></svg>", encoding="utf-8")
    Image.new("RGB", (400, 300), "white").save(png)
    write_json(meta, {"status": "PASS"})
    diagrams = [{
        "section_id": "section",
        "claim_id": "claim-1",
        "evidence_ids": ["source-1"],
        "source_reference": "artifact://diagram_artifacts/project/section/diagram.mmd",
        "source_sha256": sha256_text(mmd.read_text(encoding="utf-8").strip()),
        "svg_reference": "artifact://diagram_artifacts/project/section/diagram.svg",
        "svg_sha256": sha256_text(svg.read_text(encoding="utf-8")),
        "png_reference": "artifact://diagram_artifacts/project/section/diagram.png",
        "png_sha256": sha256_bytes(png.read_bytes()),
        "metadata_reference": "artifact://diagram_artifacts/project/section/diagram.json",
    }]

    exports = data_dir / "exports"
    exports.mkdir()
    docx = exports / "delivery.docx"
    pdf = exports / "delivery.pdf"
    docx.write_bytes(b"docx")
    pdf.write_bytes(b"pdf")
    for suffix in (
        ".integrity.json", ".manifest.json", ".pdf-conversion.json",
        ".structure-findings.json", ".visual-findings.json", ".delivery-validation.json",
    ):
        write_json(docx.with_suffix(suffix), {"status": "PASS"})
    pages = exports / "delivery-pages"
    pages.mkdir()
    Image.new("RGB", (300, 400), "white").save(pages / "page-1.png")
    delivery_report = {
        "status": "PASS",
        "blocking_finding_count": 0,
        "report_path": str(docx.with_suffix(".delivery-validation.json")),
        "screenshot_dir": str(pages),
    }
    package = exports / "delivery.zip"
    with zipfile.ZipFile(package, "w") as archive:
        for path in [
            docx, pdf, docx.with_suffix(".integrity.json"), docx.with_suffix(".manifest.json"),
            docx.with_suffix(".pdf-conversion.json"), docx.with_suffix(".structure-findings.json"),
            docx.with_suffix(".visual-findings.json"), docx.with_suffix(".delivery-validation.json"),
        ]:
            archive.write(path, arcname=path.name)
        archive.write(pages / "page-1.png", arcname="pages/page-1.png")
    return {
        "data_dir": data_dir,
        "run_dir": data_dir / "acceptance",
        "research_output": research_output,
        "claim_report": claim_report,
        "claim_report_path": claim_path,
        "diagrams": diagrams,
        "document_path": docx,
        "pdf_path": pdf,
        "delivery_report": delivery_report,
        "export_package_path": package,
        "semantic_evidence_mode": "TEST",
    }


def test_s3_builds_portable_cross_component_evidence_and_restarts(tmp_path):
    values = _fixture(tmp_path)
    result = build_s3_evidence(**values)
    assert result["status"] == "PASS"
    verification = verify_s3_evidence(Path(result["report_path"]), tmp_path)
    assert verification["status"] == "PASS"
    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    references = [
        item["reference"]
        for section in ("research", "mermaid", "export")
        for item in manifest[section]["records"]
    ]
    assert references and all(reference.startswith("artifact://") for reference in references)


def test_s3_blocks_tampered_research_snapshot(tmp_path):
    values = _fixture(tmp_path)
    manifest = json.loads(Path(values["research_output"]["archive_manifest"]).read_text(encoding="utf-8"))
    Path(manifest["records"][0]["text_path"]).write_text("tampered", encoding="utf-8")
    with pytest.raises(S3EvidenceError, match="Research archive verification"):
        build_s3_evidence(**values)


def test_s3_blocks_unvalidated_claim_binding(tmp_path):
    values = _fixture(tmp_path)
    values["claim_report"]["status"] = "BLOCK"
    write_json(values["claim_report_path"], values["claim_report"])
    with pytest.raises(S3EvidenceError, match="PUBLIC_CLAIM binding"):
        build_s3_evidence(**values)


def test_s3_blocks_diagram_without_bound_source_evidence(tmp_path):
    values = _fixture(tmp_path)
    values["diagrams"][0]["evidence_ids"] = ["unknown-source"]
    with pytest.raises(S3EvidenceError, match="not bound"):
        build_s3_evidence(**values)


def test_s3_blocks_incomplete_export_package(tmp_path):
    values = _fixture(tmp_path)
    with zipfile.ZipFile(values["export_package_path"], "w") as archive:
        archive.writestr("delivery.docx", b"docx")
    with pytest.raises(S3EvidenceError, match="incomplete"):
        build_s3_evidence(**values)


def test_s3_restart_verification_detects_post_acceptance_tampering(tmp_path):
    values = _fixture(tmp_path)
    result = build_s3_evidence(**values)
    values["pdf_path"].write_bytes(b"tampered-pdf")
    verification = verify_s3_evidence(Path(result["report_path"]), tmp_path)
    assert verification["status"] == "FAIL"
    assert "CHAIN_ARTIFACT_HASH_MISMATCH" in {item["code"] for item in verification["failures"]}
