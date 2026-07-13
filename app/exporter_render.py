from __future__ import annotations

import re

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt


class ExportRenderMixin:
    @staticmethod
    def _document_title(name: str) -> str:
        cleaned = re.sub(r"（智能体系统测试）|\(智能体系统测试\)", "", name).strip()
        cleaned = re.sub(r"项目申请书$", "", cleaned).strip()
        return cleaned

    def _append_block(self, document: Document, text: str, list_state: dict[str, int] | None = None) -> None:
        text = (text or "").strip()
        if not text:
            return
        if text.startswith("[[H2]]"):
            if list_state is not None:
                list_state["number"] = 0
            document.add_heading(text.removeprefix("[[H2]]").strip(), level=2)
            return
        if text.startswith("[[H3]]"):
            if list_state is not None:
                list_state["number"] = 0
            document.add_heading(text.removeprefix("[[H3]]").strip(), level=3)
            return
        if text.startswith("[[TABLE]]"):
            self._append_table(document, text.removeprefix("[[TABLE]]").strip())
            return
        if text.startswith("[[BULLET]]"):
            paragraph = document.add_paragraph(style="List Bullet")
            paragraph.paragraph_format.first_line_indent = None
            run = paragraph.add_run(text.removeprefix("[[BULLET]]").strip())
            self._set_run_font(run, "宋体", 12)
            return
        if text.startswith("[[NUMBER]]"):
            if list_state is None:
                list_state = {"number": 0}
            list_state["number"] = list_state.get("number", 0) + 1
            paragraph = document.add_paragraph()
            paragraph.paragraph_format.first_line_indent = Cm(-0.65)
            paragraph.paragraph_format.left_indent = Cm(0.65)
            run = paragraph.add_run(f"{list_state['number']}. {text.removeprefix('[[NUMBER]]').strip()}")
            self._set_run_font(run, "宋体", 12)
            return
        paragraph = document.add_paragraph()
        run = paragraph.add_run(text)
        self._set_run_font(run, "宋体", 12)

    def _append_table(self, document: Document, raw: str) -> None:
        rows = []
        for line in raw.splitlines():
            line = line.strip().strip("|")
            if not line or set(line.replace("|", "").replace("-", "").replace(":", "")) == set():
                continue
            rows.append([cell.strip() for cell in line.split("|")])
        if not rows:
            return
        width = max(len(row) for row in rows)
        rows = [row + [""] * (width - len(row)) for row in rows]
        table = document.add_table(rows=len(rows), cols=width)
        table.style = "Table Grid"
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        table.autofit = True
        for row_index, values in enumerate(rows):
            row = table.rows[row_index]
            tr_pr = row._tr.get_or_add_trPr()
            cant_split = OxmlElement("w:cantSplit")
            tr_pr.append(cant_split)
            if row_index == 0:
                tbl_header = OxmlElement("w:tblHeader")
                tbl_header.set(qn("w:val"), "true")
                tr_pr.append(tbl_header)
            for column_index, value in enumerate(values):
                cell = row.cells[column_index]
                cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
                cell.text = value
                for paragraph in cell.paragraphs:
                    paragraph.paragraph_format.first_line_indent = None
                    paragraph.paragraph_format.space_after = Pt(0)
                    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER if row_index == 0 else WD_ALIGN_PARAGRAPH.LEFT
                    for run in paragraph.runs:
                        self._set_run_font(run, "Noto Serif CJK SC", 9.5, bold=row_index == 0)
        document.add_paragraph().paragraph_format.space_after = Pt(0)

    @staticmethod
    def _set_run_font(run, font_name: str, size: float, bold: bool = False) -> None:
        run.font.name = font_name
        run._element.rPr.rFonts.set(qn("w:eastAsia"), font_name)
        run.font.size = Pt(size)
        run.bold = bold

    def _add_page_numbers(self, document: Document) -> None:
        for section in document.sections:
            paragraph = section.footer.paragraphs[0]
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = paragraph.add_run()
            begin = OxmlElement("w:fldChar")
            begin.set(qn("w:fldCharType"), "begin")
            instr = OxmlElement("w:instrText")
            instr.set(qn("xml:space"), "preserve")
            instr.text = " PAGE "
            end = OxmlElement("w:fldChar")
            end.set(qn("w:fldCharType"), "end")
            run._r.extend([begin, instr, end])
            self._set_run_font(run, "宋体", 10)

