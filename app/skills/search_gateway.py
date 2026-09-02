from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence, TYPE_CHECKING

from ..util import new_id, sha256_json, utc_now

if TYPE_CHECKING:
    from .search_providers.base import SearchProvider


@dataclass(frozen=True)
class SearchQuery:
    """A normalized query handed to every discovery provider."""

    query_id: str
    query: str
    purpose: str | None = None
    linked_question_indexes: tuple[int, ...] = ()
    constraints: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_value(cls, value: str | dict[str, Any], index: int) -> "SearchQuery":
        if isinstance(value, dict):
            text = str(value.get("query") or value.get("query_text") or value.get("text") or "").strip()
            query_id = str(value.get("query_id") or f"query-{index + 1:03d}").strip()
            purpose = str(value.get("purpose") or value.get("use") or "").strip() or None
            raw_indexes = value.get("linked_question_indexes") or []
            indexes = tuple(int(item) for item in raw_indexes if isinstance(item, int) and item >= 0)
            constraints = {
                key: value[key]
                for key in ("time_scope", "region", "allowed_source_categories")
                if key in value
            }
        else:
            text = str(value or "").strip()
            query_id = f"query-{index + 1:03d}"
            purpose = None
            indexes = ()
            constraints = {}
        if not text:
            raise ValueError("Search query text must not be empty")
        return cls(
            query_id=query_id,
            query=text,
            purpose=purpose,
            linked_question_indexes=indexes,
            constraints=constraints,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "query": self.query,
            "purpose": self.purpose,
            "linked_question_indexes": list(self.linked_question_indexes),
            "constraints": dict(self.constraints),
        }


@dataclass
class SearchHit:
    """Provider-neutral discovery hit; it is not yet archived evidence."""

    provider: str
    query_id: str
    matched_query: str
    rank: int
    title: str
    url: str
    snippet: str = ""
    engine: str | None = None
    raw_response_ref: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_candidate(
        cls,
        candidate: dict[str, Any],
        *,
        provider: str,
        query_id: str,
        rank: int,
        raw_response_ref: str | None = None,
    ) -> "SearchHit":
        metadata = dict(candidate)
        title = str(metadata.pop("title", "") or "").strip()
        url = str(metadata.pop("url", "") or "").strip()
        snippet = str(
            metadata.pop("excerpt", "")
            or metadata.get("abstract")
            or metadata.get("content_text")
            or ""
        ).strip()
        matched_query = str(metadata.pop("matched_query", "") or "").strip()
        engine = metadata.pop("engine", None)
        return cls(
            provider=provider,
            query_id=query_id,
            matched_query=matched_query,
            rank=rank,
            title=title,
            url=url,
            snippet=snippet,
            engine=str(engine).strip() if engine else None,
            raw_response_ref=raw_response_ref,
            metadata=metadata,
        )

    def to_candidate(self) -> dict[str, Any]:
        candidate = dict(self.metadata)
        candidate.update(
            {
                "title": self.title,
                "url": self.url,
                "excerpt": self.snippet,
                "matched_query": self.matched_query,
            }
        )
        if self.engine is not None:
            candidate["engine"] = self.engine
        if self.raw_response_ref is not None:
            candidate["raw_response_ref"] = self.raw_response_ref
        candidate.setdefault("discovery_provider", self.provider)
        candidate.setdefault("provider_rank", self.rank)
        return candidate


@dataclass
class ProviderRun:
    provider: str
    query_id: str
    query: str
    status: str
    result_count: int
    started_at: str
    completed_at: str
    engine: str | None = None
    http_status: int | None = None
    blockage_type: str | None = None
    raw_response: Any = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        raw_hash = sha256_json(self.raw_response) if self.raw_response is not None else None
        return {
            "provider": self.provider,
            "engine": self.engine,
            "query_id": self.query_id,
            "query": self.query,
            "status": self.status,
            "result_count": self.result_count,
            "http_status": self.http_status,
            "blockage_type": self.blockage_type,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "raw_response_sha256": raw_hash,
            "raw_response": self.raw_response,
            "details": dict(self.details),
        }


@dataclass
class SearchProviderResult:
    provider: str
    hits: list[SearchHit] = field(default_factory=list)
    runs: list[ProviderRun] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)
    raw_manifest: dict[str, Any] | None = None


@dataclass
class SearchBatchResult:
    queries: list[SearchQuery]
    hits: list[SearchHit] = field(default_factory=list)
    runs: list[ProviderRun] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)
    provider_manifests: dict[str, dict[str, Any]] = field(default_factory=dict)

    def candidates(self) -> list[dict[str, Any]]:
        return [item.to_candidate() for item in self.hits]

    def to_discovery_manifest(
        self,
        *,
        connector: str,
        retrieval_provider: str,
        per_query_limit: int,
        time_scope: Any = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        grouped: dict[str, list[dict[str, Any]]] = {item.query: [] for item in self.queries}
        query_id_by_text = {item.query: item.query_id for item in self.queries}
        for hit in self.hits:
            grouped.setdefault(hit.matched_query, []).append(hit.to_candidate())
        return {
            "schema_version": "2.0",
            "run_id": run_id or new_id("search-discovery"),
            "connector": connector,
            "created_at": utc_now(),
            "agent_generated_queries": [item.query for item in self.queries],
            "providers": list(dict.fromkeys(self.providers)),
            "responses": [
                {
                    "query_id": query.query_id,
                    "query": query.query,
                    "retrieved_at": utc_now(),
                    "results": grouped.get(query.query, []),
                }
                for query in self.queries
            ],
            "provider_runs": [item.to_dict() for item in self.runs],
            "failures": list(self.failures),
            "per_query_limit": int(per_query_limit),
            "time_scope": time_scope,
            "retrieval_provider": retrieval_provider,
            "query_ids": query_id_by_text,
        }


def normalize_search_queries(values: Sequence[str | dict[str, Any]]) -> list[SearchQuery]:
    result: list[SearchQuery] = []
    seen: set[str] = set()
    for index, value in enumerate(values):
        query = SearchQuery.from_value(value, index)
        if query.query in seen:
            continue
        seen.add(query.query)
        result.append(query)
    return result


class SearchGateway:
    """Execute ordered providers while preserving provider and query receipts."""

    def __init__(self, providers: Iterable["SearchProvider"]):
        self.providers = list(providers)

    def search(
        self,
        queries: Sequence[SearchQuery],
        *,
        per_query_limit: int,
        continue_on_error: bool = False,
    ) -> SearchBatchResult:
        from .search_providers.base import SearchProviderError

        batch = SearchBatchResult(queries=list(queries))
        for provider in self.providers:
            try:
                result = provider.search(list(queries), per_query_limit=per_query_limit)
            except SearchProviderError as exc:
                failure = exc.to_failure()
                batch.failures.append(failure)
                if not continue_on_error:
                    raise
                continue
            batch.hits.extend(result.hits)
            batch.runs.extend(result.runs)
            batch.failures.extend(result.failures)
            batch.providers.extend(result.providers or [result.provider])
            if result.raw_manifest is not None:
                batch.provider_manifests[result.provider] = dict(result.raw_manifest)
        batch.providers = list(dict.fromkeys(batch.providers))
        return batch
