from pathlib import Path
import re

from pypdf import PdfReader

from stage8_tools.export_final import (
    CHAPTER_NUMBERS,
    FIGURE_SPECS,
    TABLE_SPECS,
    build_docx,
    convert_pdf,
    page_locations,
    parse_markdown,
)

FIXTURE = Path(__file__).resolve().parents[1] / "stage8_tools" / "fixtures" / "stage7_integrated_proposal.md"


def test_stage8_markdown_contract_counts():
    title, blocks = parse_markdown(FIXTURE)
    assert title == "人机协同决策优势冲刺关键技术研究"
    assert len([b for b in blocks if b[0] == "h1" and b[1] != "参考文献"]) == 14
    assert len([b for b in blocks if b[0] == "visual"]) == 12
    assert len(TABLE_SPECS) == 8
    assert len(FIGURE_SPECS) == 4


def test_stage8_export_preserves_page_limit_and_removes_markers(tmp_path):
    docx = tmp_path / "proposal.docx"
    pdf = tmp_path / "proposal.pdf"
    meta = build_docx(FIXTURE, docx, tmp_path / "assets")
    meta.update(convert_pdf(docx, pdf))
    meta.update(page_locations(pdf))
    assert meta["chapter_count"] == 14
    assert meta["body_page_count"] <= 20
    assert meta["table_count"] == 8
    assert meta["figure_count"] == 4
    text = "\n".join((p.extract_text() or "") for p in PdfReader(str(pdf)).pages)
    for marker in ("TAB-", "FIG-", "RQ-", "WP-", "SRC-", "[["):
        assert marker not in text
    assert len(re.findall(r"(?m)^\[\d+\]", text)) == 14


def test_stage8_chapter_headings_are_present():
    title, blocks = parse_markdown(FIXTURE)
    headings = [v for k, v in blocks if k == "h1"]
    for chapter in CHAPTER_NUMBERS:
        assert chapter in headings
    assert headings[-1] == "参考文献"
