from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..search_gateway import SearchProviderResult, SearchQuery


class SearchProviderError(RuntimeError):
    category = "RETRIEVAL"
    error_code = "SEARCH_PROVIDER_ERROR"

    def __init__(self, message: str, *, provider: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.provider = provider
        self.details = dict(details or {})

    def to_failure(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "category": self.category,
            "error_code": self.error_code,
            "message": str(self),
            "details": dict(self.details),
        }


class SearchProviderConfigurationError(SearchProviderError):
    category = "CONFIGURATION"
    error_code = "SEARCH_PROVIDER_CONFIGURATION_ERROR"


class SearchProviderRetrievalError(SearchProviderError):
    category = "RETRIEVAL"
    error_code = "SEARCH_PROVIDER_RETRIEVAL_ERROR"


class SearchProvider(ABC):
    provider_id: str
    # Retrieval channel used by the Phase 3 execution contract. Discovery manifests
    # only carry provider names, so PROVIDER_CHANNELS below maps every known name
    # (including academic sub-providers) to its channel for audit-time classification.
    channel: str = "WEB_SEARCH"

    @abstractmethod
    def search(
        self,
        queries: list[SearchQuery],
        *,
        per_query_limit: int,
    ) -> SearchProviderResult:
        raise NotImplementedError


CHANNEL_ACADEMIC = "ACADEMIC"
CHANNEL_WEB_SEARCH = "WEB_SEARCH"
CHANNEL_REPLAY = "REPLAY"

PROVIDER_CHANNELS: dict[str, str] = {
    "academic-multi-source": CHANNEL_ACADEMIC,
    "openalex": CHANNEL_ACADEMIC,
    "crossref": CHANNEL_ACADEMIC,
    "semantic_scholar": CHANNEL_ACADEMIC,
    "searxng": CHANNEL_WEB_SEARCH,
    "browser": CHANNEL_WEB_SEARCH,
    "browser_search": CHANNEL_WEB_SEARCH,
    "connector": CHANNEL_REPLAY,
    "recorded": CHANNEL_REPLAY,
}


def provider_channel(name: str) -> str | None:
    return PROVIDER_CHANNELS.get(str(name or "").strip().lower())
