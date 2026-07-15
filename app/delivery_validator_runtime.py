from __future__ import annotations

from pathlib import Path
from typing import Any

from pypdf import PdfReader

from .delivery_validator import DeliveryValidator as BaseDeliveryValidator


class DeliveryValidator(BaseDeliveryValidator):
    """Runtime validator with conservative coordinate-based text-overlap detection.

    PyPDF frequently splits one normal visual line into adjacent text spans. Bounding
    boxes estimated from string length therefore overlap even when the rendered page
    is correct. A blocking overlap finding is emitted only when different spans are
    painted at effectively the same anchor at least twice on one page.
    """

    def _pdf_overlap_findings(self, pdf_path: Path) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        reader = PdfReader(str(pdf_path))
        for page_number, page in enumerate(reader.pages, 1):
            anchors: list[tuple[float, float, str]] = []

            def visitor(text, cm, tm, font_dict, font_size):  # type: ignore[no-untyped-def]
                value = (text or "").strip()
                if not value or not font_size:
                    return
                anchors.append((float(tm[4]), float(tm[5]), value[:80]))

            try:
                page.extract_text(visitor_text=visitor)
            except Exception:
                continue

            overlap_signals = 0
            for index, left in enumerate(anchors):
                for right in anchors[index + 1 :]:
                    if left[2] == right[2]:
                        continue
                    if abs(left[0] - right[0]) <= 1.5 and abs(left[1] - right[1]) <= 1.5:
                        overlap_signals += 1
                        if overlap_signals >= 2:
                            break
                if overlap_signals >= 2:
                    break
            if overlap_signals >= 2:
                findings.append(
                    self._finding(
                        "D6_TEXT_OVERLAP_RISK",
                        "P1",
                        f"第 {page_number} 页检测到多个不同文本片段在同一坐标重复绘制，存在叠字风险",
                        location=f"PDF page {page_number}",
                        blocking=True,
                    )
                )
        return findings
