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

    @abstractmethod
    def search(
        self,
        queries: list[SearchQuery],
        *,
        per_query_limit: int,
    ) -> SearchProviderResult:
        raise NotImplementedError
