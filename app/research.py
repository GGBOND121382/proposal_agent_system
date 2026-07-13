from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urlparse

import httpx

from .util import new_id, sha256_text


class PublicResearchError(RuntimeError):
    pass


class PublicResearchService:
    def __init__(self, settings):
        self.settings = settings

    async def search(self, plan: dict[str, Any]) -> dict[str, Any]:
        provider = self.settings.public_search_provider
        if provider == "disabled":
            raise PublicResearchError("PUBLIC_SEARCH_PROVIDER is disabled")
        if provider != "searxng":
            raise PublicResearchError(f"Unsupported public search provider: {provider}")
        if not self.settings.public_search_base_url:
            raise PublicResearchError("PUBLIC_SEARCH_BASE_URL is empty")

        queries = plan.get("queries", [])
        normalized: list[str] = []
        for q in queries:
            if isinstance(q, str):
                normalized.append(q)
            elif isinstance(q, dict):
                normalized.append(str(q.get("query") or q.get("query_text") or q.get("text") or ""))
        normalized = [q.strip() for q in normalized if q.strip()][:8]
        sources: list[dict[str, Any]] = []
        passages: list[dict[str, Any]] = []
        seen: set[str] = set()
        timeout = httpx.Timeout(30.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            for query in normalized:
                response = await client.get(f"{self.settings.public_search_base_url}/search", params={"q": query, "format": "json", "language": "zh-CN", "safesearch": 1})
                response.raise_for_status()
                payload = response.json()
                for item in payload.get("results", [])[:5]:
                    url = str(item.get("url") or "")
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    title = str(item.get("title") or "")
                    snippet = str(item.get("content") or item.get("snippet") or "")
                    domain = urlparse(url).netloc
                    source_id = new_id("public-src")
                    source_hash = sha256_text(url + "\n" + title + "\n" + snippet)
                    source = {
                        "source_id": source_id,
                        "source_type": "PUBLIC_SOURCE",
                        "document_version_id": None,
                        "section_id": None,
                        "span_start": None,
                        "span_end": None,
                        "quoted_text": f"{title} | {url}",
                        "source_hash": source_hash,
                        "authority_rank": self._authority_rank(domain),
                        "security_level": "PUBLIC",
                    }
                    sources.append(source)
                    passages.append({"passage_id": new_id("passage"), "source_ref": source, "text": snippet or title, "relevance": f"检索词：{query}"})
        return {"sources": sources, "passages": passages, "queries": normalized}

    @staticmethod
    def _authority_rank(domain: str) -> int:
        lowered = domain.lower()
        if lowered.endswith(".gov.cn") or lowered.endswith(".gov"):
            return 90
        if lowered.endswith(".edu.cn") or lowered.endswith(".edu") or "ac.cn" in lowered:
            return 80
        return 50
