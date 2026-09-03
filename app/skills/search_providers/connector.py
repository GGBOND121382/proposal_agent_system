from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...util import sha256_bytes, utc_now
from ..search_gateway import SearchHit, SearchProviderResult, SearchQuery
from .base import CHANNEL_REPLAY, SearchProvider, SearchProviderConfigurationError


class ConnectorSearchProvider(SearchProvider):
    provider_id = "connector"
    channel = CHANNEL_REPLAY

    def __init__(self, connector_file: str | Path):
        self.connector_file = Path(connector_file)

    def load_candidates(
        self,
        planned_queries: list[str],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if not self.connector_file.exists():
            raise SearchProviderConfigurationError(
                f"Connector research file not found: {self.connector_file}",
                provider=self.provider_id,
            )
        try:
            payload = json.loads(self.connector_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SearchProviderConfigurationError(
                f"Connector research file is not readable JSON: {self.connector_file}: {exc}",
                provider=self.provider_id,
            ) from exc
        if not isinstance(payload, dict):
            raise SearchProviderConfigurationError(
                "Connector research file must be a JSON object",
                provider=self.provider_id,
            )
        responses = payload.get("responses")
        if not isinstance(responses, list):
            raise SearchProviderConfigurationError(
                "Connector research file must contain a responses array",
                provider=self.provider_id,
            )
        connector_queries: list[str] = []
        candidates: list[dict[str, Any]] = []
        for response in responses:
            if not isinstance(response, dict):
                continue
            query = str(response.get("query") or "").strip()
            if query:
                connector_queries.append(query)
            results = response.get("results") or []
            if not isinstance(results, list):
                continue
            for item in results:
                if not isinstance(item, dict):
                    continue
                candidate = dict(item)
                candidate.setdefault("matched_query", query)
                candidate.setdefault("retrieved_at", response.get("retrieved_at") or payload.get("created_at") or utc_now())
                candidate.setdefault("connector", payload.get("connector") or "approved-search-connector")
                candidate.setdefault("verification", {})
                candidate["verification"] = {
                    **candidate["verification"],
                    "connector_run_id": payload.get("run_id"),
                    "connector": payload.get("connector"),
                    "query": query,
                    "status": candidate["verification"].get("status") or "CONNECTOR_RETURNED",
                }
                candidates.append(candidate)
        missing = [query for query in planned_queries if query not in connector_queries]
        if missing:
            raise SearchProviderConfigurationError(
                f"Connector responses do not cover planned queries: {missing}",
                provider=self.provider_id,
                details={"missing_queries": missing},
            )
        if not candidates:
            raise SearchProviderConfigurationError(
                "Connector research file contains no result records",
                provider=self.provider_id,
            )
        manifest = {
            **payload,
            "ingested_at": utc_now(),
            "planned_queries": planned_queries,
            "connector_queries": connector_queries,
            "result_count": len(candidates),
            "source_file": str(self.connector_file),
            "source_file_sha256": sha256_bytes(self.connector_file.read_bytes()),
        }
        return candidates, manifest

    def search(
        self,
        queries: list[SearchQuery],
        *,
        per_query_limit: int,
    ) -> SearchProviderResult:
        candidates, manifest = self.load_candidates([item.query for item in queries])
        query_id_by_text = {item.query: item.query_id for item in queries}
        hits = [
            SearchHit.from_candidate(
                candidate,
                provider=self.provider_id,
                query_id=query_id_by_text.get(str(candidate.get("matched_query") or ""), "query-001"),
                rank=index + 1,
            )
            for index, candidate in enumerate(candidates)
        ]
        return SearchProviderResult(
            provider=self.provider_id,
            hits=hits,
            providers=[self.provider_id],
            raw_manifest=manifest,
        )
