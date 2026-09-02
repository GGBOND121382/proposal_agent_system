from __future__ import annotations

import mimetypes
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any

from bs4 import BeautifulSoup
from pypdf import PdfReader

from .fetch_gateway import FetchedDocument
from ..util import sha256_text


@dataclass(frozen=True)
class ExtractedDocument:
    text: str
    extractor: str
    quality: str
    character_count: int
    failure_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def text_sha256(self) -> str:
        return sha256_text(self.text)

    def to_dict(self) -> dict[str, Any]:
        return {
            "extractor": self.extractor,
            "quality": self.quality,
            "character_count": self.character_count,
            "failure_reason": self.failure_reason,
            "metadata": dict(self.metadata),
            "text_sha256": self.text_sha256,
        }


class ContentExtractor:
    """Deterministic static HTML/text/PDF extraction boundary."""

    def extract(self, fetched: FetchedDocument) -> ExtractedDocument:
        text, extractor = self.extract_text(
            fetched.raw_bytes,
            fetched.content_type,
            fetched.final_url,
        )
        compact = self.compact_text(text)
        if not compact:
            quality = "EMPTY"
            failure_reason = "NO_EXTRACTABLE_TEXT"
        elif len(compact) < 200:
            quality = "SHORT"
            failure_reason = None
        else:
            quality = "USABLE"
            failure_reason = None
        return ExtractedDocument(
            text=text,
            extractor=extractor,
            quality=quality,
            character_count=len(text),
            failure_reason=failure_reason,
        )

    @staticmethod
    def extract_text(raw: bytes, content_type: str, url: str) -> tuple[str, str]:
        if content_type == "application/pdf" or url.lower().endswith(".pdf"):
            reader = PdfReader(BytesIO(raw))
            return (
                "\n\n".join((page.extract_text() or "") for page in reader.pages[:200]),
                "PYPDF",
            )
        if content_type.startswith("text/plain"):
            return raw.decode("utf-8", errors="replace"), "PLAIN_TEXT"
        text = raw.decode("utf-8", errors="replace")
        soup = BeautifulSoup(text, "html.parser")
        for node in soup(["script", "style", "noscript", "svg", "nav", "footer", "header"]):
            node.decompose()
        main = soup.find("main") or soup.find("article") or soup.body or soup
        return main.get_text("\n", strip=True), "BEAUTIFULSOUP_MAIN_TEXT"

    @staticmethod
    def compact_text(text: str) -> str:
        return "\n".join(line.strip() for line in text.splitlines() if line.strip())

    @staticmethod
    def suffix(content_type: str, url: str) -> str:
        if content_type == "application/pdf" or url.lower().endswith(".pdf"):
            return ".pdf"
        if content_type.startswith("text/plain"):
            return ".txt"
        if "html" in content_type:
            return ".html"
        return mimetypes.guess_extension(content_type) or ".bin"
