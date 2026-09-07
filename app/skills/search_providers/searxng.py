from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

import httpx

from ...util import utc_now
from ..search_gateway import ProviderRun, SearchHit, SearchProviderResult, SearchQuery
from .base import CHANNEL_WEB_SEARCH, SearchProvider, SearchProviderConfigurationError


class SearxngSearchProvider(SearchProvider):
    provider_id = "searxng"
    channel = CHANNEL_WEB_SEARCH

    def __init__(
        self,
        settings,
        *,
        client_factory: Callable[..., Any] = httpx.Client,
        max_workers: int = 4,
    ):
        self.settings = settings
        self.client_factory = client_factory
        self.max_workers = max(1, int(max_workers))

    @staticmethod
    def _authors(value: Any) -> list[str]:
        if isinstance(value, str):
            return [item.strip() for item in re.split(r"[,;|]", value) if item.strip()]
        if isinstance(value, list):
            result = []
            for item in value:
                name = str(item.get("name") if isinstance(item, dict) else item).strip()
                if name:
                    result.append(name)
            return result
        return []

    @staticmethod
    def _unresponsive_engines(payload: Any) -> list[dict[str, str]]:
        if not isinstance(payload, dict):
            return []
        result: list[dict[str, str]] = []
        for item in payload.get("unresponsive_engines") or []:
            if isinstance(item, (list, tuple)):
                engine = str(item[0] if item else "").strip()
                reason = str(item[1] if len(item) > 1 else "unresponsive").strip()
            elif isinstance(item, dict):
                engine = str(item.get("engine") or item.get("name") or "").strip()
                reason = str(item.get("reason") or item.get("error") or "unresponsive").strip()
            else:
                engine = str(item or "").strip()
                reason = "unresponsive"
            if engine:
                result.append({"engine": engine, "reason": reason or "unresponsive"})
        return result

    def _search_one(
        self,
        query: SearchQuery,
        *,
        per_query_limit: int,
    ) -> tuple[list[SearchHit], ProviderRun, list[dict[str, Any]]]:
        base_url = str(getattr(self.settings, "public_search_base_url", "") or "").rstrip("/")
        if not base_url:
            raise SearchProviderConfigurationError(
                "PUBLIC_SEARCH_BASE_URL is empty",
                provider=self.provider_id,
            )
        endpoint = f"{base_url}/search"
        engines = str(getattr(self.settings, "public_search_engines", "") or "").strip()
        params: dict[str, Any] = {
            "q": query.query,
            "format": "json",
            "language": "all",
            "safesearch": 1,
        }
        if engines:
            params["engines"] = engines
        started_at = utc_now()
        payload: Any = None
        http_status: int | None = None
        hits: list[SearchHit] = []
        failures: list[dict[str, Any]] = []
        try:
            with self.client_factory(
                timeout=getattr(self.settings, "research_fetch_timeout_seconds", 45),
                follow_redirects=True,
                trust_env=False,
            ) as client:
                response = client.get(endpoint, params=params)
                http_status = getattr(response, "status_code", None)
                response.raise_for_status()
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise SearchProviderConfigurationError(
                        f"SearXNG JSON API returned invalid JSON: {endpoint}",
                        provider=self.provider_id,
                        details={"endpoint": endpoint, "query": query.query},
                    ) from exc
                results = payload.get("results") if isinstance(payload, dict) else None
                if not isinstance(results, list):
                    raise SearchProviderConfigurationError(
                        f"SearXNG JSON API response has no results array: {endpoint}",
                        provider=self.provider_id,
                        details={"endpoint": endpoint, "query": query.query},
                    )
                # SearXNG has already paid for this result page.  Its aggregate
                # ranking can interleave irrelevant results from a degraded engine
                # ahead of primary sources from another engine.  Preserve the page
                # for semantic screening; the archive budget belongs after ranking.
                for index, item in enumerate(results):
                    if not isinstance(item, dict):
                        continue
                    candidate = {
                        "title": str(item.get("title") or "").strip(),
                        "url": str(item.get("url") or "").strip(),
                        "excerpt": str(item.get("content") or item.get("snippet") or "").strip(),
                        "matched_query": query.query,
                        "engine": item.get("engine"),
                        "published_at": (
                            item.get("publishedDate")
                            or item.get("published_date")
                            or item.get("pubdate")
                            or item.get("date")
                        ),
                        "authors": self._authors(item.get("authors") or item.get("author") or []),
                        "publisher": item.get("publisher") or item.get("journal") or item.get("source"),
                        "doi": item.get("doi"),
                        "citation_count": item.get("citation_count"),
                        "is_retracted": bool(item.get("is_retracted", False)),
                        "raw_search_result": item,
                    }
                    hits.append(
                        SearchHit.from_candidate(
                            candidate,
                            provider=self.provider_id,
                            query_id=query.query_id,
                            rank=index + 1,
                        )
                    )
                for item in self._unresponsive_engines(payload):
                    failures.append(
                        {
                            "query": query.query,
                            "query_id": query.query_id,
                            "provider": self.provider_id,
                            "engine": item["engine"],
                            "category": "RETRIEVAL",
                            "error_code": "SEARXNG_ENGINE_UNRESPONSIVE",
                            "message": (
                                f"SearXNG engine {item['engine']} was skipped: "
                                f"{item['reason']}"
                            ),
                            "details": {
                                "endpoint": endpoint,
                                "engine": item["engine"],
                                "reason": item["reason"],
                            },
                        }
                    )
        except SearchProviderConfigurationError:
            raise
        except (httpx.TimeoutException, httpx.RequestError, httpx.HTTPStatusError) as exc:
            failures.append(
                {
                    "query": query.query,
                    "query_id": query.query_id,
                    "provider": self.provider_id,
                    "category": "RETRIEVAL",
                    "error_code": "PUBLIC_RESEARCH_QUERY_RETRIEVAL_ERROR",
                    "message": f"SearXNG query failed: {exc}",
                    "details": {
                        "endpoint": endpoint,
                        "exception_type": type(exc).__name__,
                    },
                }
            )
        run_status = "DEGRADED" if hits and failures else ("ERROR" if failures else "PASS")
        run = ProviderRun(
            provider=self.provider_id,
            engine=engines or None,
            query_id=query.query_id,
            query=query.query,
            status=run_status,
            result_count=len(hits),
            http_status=http_status,
            blockage_type=None,
            started_at=started_at,
            completed_at=utc_now(),
            raw_response=payload,
            details={
                "endpoint": endpoint,
                "unresponsive_engines": self._unresponsive_engines(payload),
                "requested_per_query_limit": per_query_limit,
                "raw_result_count": len(payload.get("results", [])) if isinstance(payload, dict) else 0,
                "normalized_result_count": len(hits),
                "truncated_result_count": 0,
            },
        )
        return hits, run, failures

    @staticmethod
    def _round_robin(groups: list[list[SearchHit]]) -> list[SearchHit]:
        result: list[SearchHit] = []
        for index in range(max((len(group) for group in groups), default=0)):
            for group in groups:
                if index < len(group):
                    result.append(group[index])
        return result

    def search(
        self,
        queries: list[SearchQuery],
        *,
        per_query_limit: int,
    ) -> SearchProviderResult:
        if not queries:
            return SearchProviderResult(provider=self.provider_id, providers=[self.provider_id])
        groups: list[list[SearchHit]] = [[] for _ in queries]
        runs: list[ProviderRun | None] = [None for _ in queries]
        failures: list[dict[str, Any]] = []
        with ThreadPoolExecutor(
            max_workers=min(self.max_workers, len(queries)),
            thread_name_prefix="searxng-query",
        ) as pool:
            future_map = {
                pool.submit(self._search_one, query, per_query_limit=per_query_limit): index
                for index, query in enumerate(queries)
            }
            for future in as_completed(future_map):
                index = future_map[future]
                hits, run, query_failures = future.result()
                groups[index] = hits
                runs[index] = run
                failures.extend(query_failures)
        query_order = {query.query: index for index, query in enumerate(queries)}
        failures.sort(key=lambda item: query_order.get(str(item.get("query") or ""), len(queries)))
        return SearchProviderResult(
            provider=self.provider_id,
            hits=self._round_robin(groups),
            runs=[item for item in runs if item is not None],
            failures=failures,
            providers=[self.provider_id],
        )
