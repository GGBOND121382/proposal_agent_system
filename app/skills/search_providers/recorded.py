from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..search_gateway import SearchHit, SearchProviderResult, SearchQuery
from .base import SearchProvider, SearchProviderConfigurationError


class RecordedSearchProvider(SearchProvider):
    provider_id = "recorded"

    def __init__(self, record_file: str | Path):
        self.record_file = Path(record_file)

    def load_candidates(self) -> list[dict[str, Any]]:
        if not self.record_file.exists():
            raise SearchProviderConfigurationError(
                f"Recorded research file not found: {self.record_file}",
                provider=self.provider_id,
            )
        try:
            payload = json.loads(self.record_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SearchProviderConfigurationError(
                f"Recorded research file is not readable JSON: {self.record_file}: {exc}",
                provider=self.provider_id,
            ) from exc
        sources = payload.get("sources") if isinstance(payload, dict) else payload
        if not isinstance(sources, list):
            raise SearchProviderConfigurationError(
                "Recorded research file must contain a sources array",
                provider=self.provider_id,
            )
        return [item for item in sources if isinstance(item, dict)]

    def search(
        self,
        queries: list[SearchQuery],
        *,
        per_query_limit: int,
    ) -> SearchProviderResult:
        query_id_by_text = {item.query: item.query_id for item in queries}
        hits = []
        for index, candidate in enumerate(self.load_candidates()):
            query = str(candidate.get("matched_query") or (queries[0].query if queries else ""))
            hits.append(
                SearchHit.from_candidate(
                    candidate,
                    provider=self.provider_id,
                    query_id=query_id_by_text.get(query, "query-001"),
                    rank=index + 1,
                )
            )
        return SearchProviderResult(provider=self.provider_id, hits=hits, providers=[self.provider_id])
