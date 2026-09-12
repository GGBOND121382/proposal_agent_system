"""Evidence provenance shared by archiving, coverage and quality reporting."""

from typing import Any
from urllib.parse import urlparse


def is_official_url(url: str) -> bool:
    host = (urlparse(str(url or "")).hostname or "").lower()
    return host.endswith((".gov", ".gov.cn", ".mil"))


def evidence_kind(record: dict[str, Any]) -> str:
    mode = str(record.get("fetch_mode") or "").upper()
    if mode == "SNIPPET_ONLY":
        return "SEARCH_SNIPPET"
    if (
        mode in {"HTTP", "PLAYWRIGHT_RENDERED"}
        and "json" not in str(record.get("content_type") or "").lower()
        and str(record.get("extractor") or "").upper() != "PROVIDER_TEXT"
        and str(record.get("extraction_quality") or "").upper() == "USABLE"
        and not record.get("extraction_failure_reason")
        and int(record.get("text_length") or 0) > 0
    ):
        return "FETCHED_DOCUMENT"
    # Length never upgrades a provider abstract or a search result to full text.
    return "PROVIDER_TEXT" if mode == "PROVIDER_PAYLOAD" else "UNVERIFIED_TEXT"


def is_fulltext(record: dict[str, Any]) -> bool:
    return evidence_kind(record) == "FETCHED_DOCUMENT"
