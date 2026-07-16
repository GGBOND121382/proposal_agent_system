from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from docx import Document
from pypdf import PdfWriter

from app.post_export_acceptance import PostExportQualityLifecycleManager
from app.post_export_validator import PostExportDeliveryValidator


def _blank_pdf(path: Path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    with path.open("wb") as handle:
        writer.write(handle)


def test_content_issue_routes_to_responsible_writer():
    manager = PostExportQualityLifecycleManager(None)
    route = manager.route_delivery_finding(
        {
            "code": "D5_CONTENT_PLACEHOLDER_WORD",
            "category": "CONTENT",
            "target_type": "SECTION_CANDIDATE",
            "responsible_section_ids": ["section-method"],
        }
    )
    assert route.owner == "WRITING_AGENT"
    assert route.owner_kind == "AGENT"
    assert route.reviewer_prompt_id == "P-INTEGRATION-CRITIC"


def test_layout_issue_routes_to_export_engineering():
    manager = PostExportQualityLifecycleManager(None)
    route = manager.route_delivery_finding(
        {
            "code": "D6_TEXT_OVERLAP_RISK",
            "category": "FORMAT",
            "target_type": "PDF_LAYOUT",
        }
    )
    assert route.owner == "EXPORT_ENGINEERING"
    assert route.owner_kind == "ENGINEERING"
    assert route.stage_prompt_ids == ("EXPORT_ENGINEERING",)
    assert route.reviewer_prompt_id == "DELIVERY_VALIDATOR"


def test_reviewed_content_loss_is_detected_and_scoped(tmp_path: Path):
    docx = tmp_path / "delivery.docx"
    document = Document()
    document.add_heading("技术路线", level=1)
    document.add_paragraph("只保留了部分正文。")
    document.save(docx)
    pdf = tmp_path / "delivery.pdf"
    _blank_pdf(pdf)

    validator = PostExportDeliveryValidator(SimpleNamespace())
    report = validator.validate_structure(
        docx,
        pdf,
        expected_sections=["技术路线"],
        expected_candidates=[
            {
                "section_id": "section-method",
                "section_title": "技术路线",
                "candidate_id": "candidate-1",
                "paragraphs": ["只保留了部分正文。", "缺失的已审查方法与验证内容。"],
            }
        ],
    )
    findings = {item["code"]: item for item in report["findings"]}
    assert "D5_REVIEWED_CONTENT_LOSS" in findings
    assert findings["D5_REVIEWED_CONTENT_LOSS"]["owner"] == "EXPORT_ENGINEERING"
    assert findings["D5_REVIEWED_CONTENT_LOSS"]["responsible_section_ids"] == ["section-method"]


def test_visible_manifest_counts_structured_blocks():
    validator = PostExportDeliveryValidator(SimpleNamespace())
    manifest = validator._expected_visible_manifest(
        [
            {
                "section_id": "section-method",
                "paragraphs": [
                    "[[H2]]方法步骤",
                    "[[TABLE]]对象|指标\n算法|误差",
                    "[[FORMULA]]J=Q+T+S",
                    "正文结论。",
                ],
            }
        ]
    )
    assert manifest["table_count"] == 1
    assert manifest["figure_count"] == 0
    assert {item["kind"] for item in manifest["units"]} >= {
        "HEADING",
        "TABLE_CELL",
        "FORMULA",
        "PARAGRAPH",
    }
