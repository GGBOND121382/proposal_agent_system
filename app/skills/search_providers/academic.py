from __future__ import annotations

from typing import Any, Callable

from ..academic_search import AcademicSearchClient
from ..search_gateway import ProviderRun, SearchHit, SearchProviderResult, SearchQuery
from .base import SearchProvider, SearchProviderRetrievalError


class AcademicSearchProvider(SearchProvider):
    provider_id = "academic-multi-source"

    def __init__(
        self,
        settings,
        *,
        client_factory: Callable[[Any], AcademicSearchClient] = AcademicSearchClient,
        time_scope: Any = None,
    ):
        self.settings = settings
        self.client_factory = client_factory
        self.time_scope = time_scope

    def search(
        self,
        queries: list[SearchQuery],
        *,
        per_query_limit: int,
    ) -> SearchProviderResult:
        try:
            manifest = self.client_factory(self.settings).discover(
                [item.query for item in queries],
                time_scope=self.time_scope,
                per_query_limit=per_query_limit,
            )
        except Exception as exc:
            raise SearchProviderRetrievalError(
                f"Academic discovery failed: {exc}",
                provider=self.provider_id,
                details={"exception_type": type(exc).__name__},
            ) from exc

        query_by_text = {item.query: item for item in queries}
        hits: list[SearchHit] = []
        for response in manifest.get("responses") or []:
            if not isinstance(response, dict):
                continue
            query_text = str(response.get("query") or "")
            query = query_by_text.get(query_text)
            if query is None:
                continue
            for index, candidate in enumerate(response.get("results") or []):
                if not isinstance(candidate, dict):
                    continue
                provider = str(candidate.get("academic_provider") or self.provider_id)
                hits.append(
                    SearchHit.from_candidate(
                        candidate,
                        provider=provider,
                        query_id=query.query_id,
                        rank=index + 1,
                    )
                )

        runs: list[ProviderRun] = []
        for raw_run in manifest.get("provider_runs") or []:
            if not isinstance(raw_run, dict):
                continue
            query_text = str(raw_run.get("query") or "")
            query = query_by_text.get(query_text)
            if query is None:
                continue
            provider = str(raw_run.get("provider") or self.provider_id)
            runs.append(
                ProviderRun(
                    provider=provider,
                    query_id=query.query_id,
                    query=query.query,
                    status="PASS",
                    result_count=int(raw_run.get("result_count") or 0),
                    started_at=str(manifest.get("created_at") or ""),
                    completed_at=str(manifest.get("created_at") or ""),
                    raw_response=raw_run.get("raw_response"),
                )
            )
        providers = [str(item) for item in manifest.get("providers") or []]
        return SearchProviderResult(
            provider=self.provider_id,
            hits=hits,
            runs=runs,
            failures=list(manifest.get("failures") or []),
            providers=providers or [self.provider_id],
            raw_manifest=manifest,
        )
