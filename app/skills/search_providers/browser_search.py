from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

from bs4 import BeautifulSoup

from ...util import new_id, safe_filename, sha256_text, utc_now, write_json
from ..browser_worker import BrowserPageResult, BrowserWorker
from ..fetch_gateway import FetchGatewaySecurityError, validate_public_url
from ..search_gateway import ProviderRun, SearchHit, SearchProviderResult, SearchQuery
from .base import SearchProvider, SearchProviderConfigurationError


class BrowserSearchProvider(SearchProvider):
    provider_id = "browser_search"

    def __init__(
        self,
        settings,
        *,
        worker: BrowserWorker | None = None,
        evidence_dir: str | Path | None = None,
    ):
        self.settings = settings
        self.worker = worker or BrowserWorker(settings)
        self.evidence_dir = Path(
            evidence_dir
            or Path(getattr(settings, "data_dir", Path("data")))
            / "browser_search_evidence"
        )
        self.evidence_dir.mkdir(parents=True, exist_ok=True)

    def search(
        self,
        queries: list[SearchQuery],
        *,
        per_query_limit: int,
    ) -> SearchProviderResult:
        template = str(
            getattr(self.settings, "browser_search_url_template", "") or ""
        ).strip()
        if "{query}" not in template:
            raise SearchProviderConfigurationError(
                "BROWSER_SEARCH_URL_TEMPLATE must contain {query}",
                provider=self.provider_id,
            )
        hits: list[SearchHit] = []
        runs: list[ProviderRun] = []
        failures: list[dict[str, Any]] = []
        for query in queries:
            started_at = utc_now()
            search_url = template.format(query=quote_plus(query.query))
            page = self.worker.fetch(search_url)
            evidence = self._preserve_page(query, search_url, page)
            query_hits: list[SearchHit] = []
            if page.status == "PASS":
                query_hits = self._parse_hits(
                    page,
                    query,
                    limit=max(1, int(per_query_limit)),
                    raw_response_ref=str(evidence["html_path"]),
                )
                hits.extend(query_hits)
            else:
                failures.append(
                    {
                        "provider": self.provider_id,
                        "query": query.query,
                        "query_id": query.query_id,
                        "category": "RETRIEVAL",
                        "error_code": (
                            "BROWSER_SEARCH_PROVIDER_BLOCKED"
                            if page.status == "PROVIDER_BLOCKED"
                            else "BROWSER_SEARCH_NAVIGATION_FAILED"
                        ),
                        "message": page.error
                        or f"Browser search ended with {page.status}",
                        "details": {
                            "search_url": search_url,
                            "blockage_type": page.blockage_type,
                            "evidence_path": str(evidence["receipt_path"]),
                        },
                    }
                )
            runs.append(
                ProviderRun(
                    provider=self.provider_id,
                    engine=urlparse(search_url).hostname,
                    query_id=query.query_id,
                    query=query.query,
                    status=page.status,
                    result_count=len(query_hits),
                    http_status=page.http_status,
                    blockage_type=page.blockage_type,
                    started_at=started_at,
                    completed_at=utc_now(),
                    raw_response={
                        "search_url": search_url,
                        "final_url": page.final_url,
                        "html_path": str(evidence["html_path"]),
                        "html_sha256": page.html_sha256,
                        "receipt_path": str(evidence["receipt_path"]),
                    },
                    details={
                        "cache_hit": page.cache_hit,
                        "page_title": page.title,
                        "text_length": len(page.text),
                    },
                )
            )
        return SearchProviderResult(
            provider=self.provider_id,
            hits=hits,
            runs=runs,
            failures=failures,
            providers=[self.provider_id],
            raw_manifest={
                "provider": self.provider_id,
                "provider_runs": [run.to_dict() for run in runs],
                "failures": failures,
            },
        )

    def _preserve_page(
        self,
        query: SearchQuery,
        search_url: str,
        page: BrowserPageResult,
    ) -> dict[str, Path]:
        run_id = new_id("browser-search")
        stem = safe_filename(f"{query.query_id}-{run_id}")
        html_path = self.evidence_dir / f"{stem}.html"
        receipt_path = self.evidence_dir / f"{stem}.json"
        html_path.write_bytes(page.html.encode("utf-8"))
        receipt = {
            "schema_version": "1.0",
            "provider": self.provider_id,
            "query": query.to_dict(),
            "search_url": search_url,
            "page": page.to_dict(),
            "raw_html_path": str(html_path),
            "raw_html_sha256": sha256_text(page.html),
            "created_at": utc_now(),
        }
        write_json(receipt_path, receipt)
        return {"html_path": html_path, "receipt_path": receipt_path}

    def _parse_hits(
        self,
        page: BrowserPageResult,
        query: SearchQuery,
        *,
        limit: int,
        raw_response_ref: str,
    ) -> list[SearchHit]:
        soup = BeautifulSoup(page.html, "html.parser")
        selectors = (
            "li.b_algo",
            "div.snippet[data-type='web']",
            "article[data-testid='result']",
            "div.result",
            "div.g",
        )
        nodes = []
        for selector in selectors:
            nodes = soup.select(selector)
            if nodes:
                break
        results: list[SearchHit] = []
        seen: set[str] = set()
        for node in nodes:
            anchor = node.select_one("h2 a, a.result__a, h3 a, a[href]")
            if anchor is None:
                continue
            title_node = node.select_one(".title, h2, h3")
            title = (
                title_node.get_text(" ", strip=True)
                if title_node is not None
                else anchor.get_text(" ", strip=True)
            )
            raw_url = str(anchor.get("href") or "").strip()
            url = self._normalize_result_url(raw_url, page.final_url)
            if not title or not url or url in seen:
                continue
            try:
                validate_public_url(url, resolve_dns=False)
            except FetchGatewaySecurityError:
                continue
            seen.add(url)
            snippet_node = node.select_one(
                ".b_caption p, .generic-snippet .content, .result__snippet, [data-result='snippet'], .VwiC3b, p"
            )
            snippet = snippet_node.get_text(" ", strip=True) if snippet_node else ""
            results.append(
                SearchHit(
                    provider=self.provider_id,
                    query_id=query.query_id,
                    matched_query=query.query,
                    rank=len(results) + 1,
                    title=title,
                    url=url,
                    snippet=snippet,
                    engine=urlparse(page.final_url).hostname,
                    raw_response_ref=raw_response_ref,
                    metadata={"search_result_page_sha256": page.html_sha256},
                )
            )
            if len(results) >= limit:
                break
        return results

    @staticmethod
    def _normalize_result_url(raw_url: str, base_url: str) -> str:
        if not raw_url:
            return ""
        absolute = urljoin(base_url, raw_url)
        parsed = urlparse(absolute)
        if parsed.path == "/url":
            wrapped = parse_qs(parsed.query).get("q") or parse_qs(parsed.query).get("url")
            if wrapped:
                absolute = wrapped[0]
        return absolute.strip()
