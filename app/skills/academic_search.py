from __future__ import annotations

import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable

import httpx

from ..util import new_id, utc_now
from .research_plan import normalize_doi, parse_time_scope_bounds


ACADEMIC_PROVIDERS = ("openalex", "crossref", "semantic_scholar")


class AcademicDiscoveryError(RuntimeError):
    pass



def _clean_abstract(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _openalex_abstract(inverted: Any) -> str:
    if not isinstance(inverted, dict):
        return ""
    positions: list[tuple[int, str]] = []
    for token, indexes in inverted.items():
        if not isinstance(indexes, list):
            continue
        for index in indexes:
            if isinstance(index, int):
                positions.append((index, str(token)))
    positions.sort(key=lambda item: item[0])
    return " ".join(token for _, token in positions)


def _author_names(values: Any) -> list[str]:
    result: list[str] = []
    for item in values or []:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            author = item.get("author") if isinstance(item.get("author"), dict) else item
            name = str(
                author.get("display_name")
                or author.get("name")
                or " ".join(
                    part for part in (str(author.get("given") or "").strip(), str(author.get("family") or "").strip()) if part
                )
            ).strip()
        else:
            name = ""
        if name and name not in result:
            result.append(name)
    return result[:20]


_PREPRINT_HINTS = ("preprint", "research square", "ssrn", "arxiv")
_EDITORIAL_HINTS = ("decision letter", "editorial", "correction to", "corrigendum", "erratum", "retraction notice")


def _publication_profile(provider: str, raw: dict[str, Any], title: str, publisher: str) -> tuple[str, str, str]:
    """Return (source_type, publication_status, publication_kind)."""

    lowered = f"{title} {publisher}".lower()
    if any(term in lowered for term in _EDITORIAL_HINTS):
        return "EDITORIAL", "EDITORIAL", "editorial"
    if any(term in lowered for term in _PREPRINT_HINTS):
        return "ACADEMIC_PREPRINT", "PREPRINT", "preprint"

    if provider == "openalex":
        work_type = str(raw.get("type") or "").lower()
        primary = raw.get("primary_location") if isinstance(raw.get("primary_location"), dict) else {}
        source = primary.get("source") if isinstance(primary.get("source"), dict) else {}
        source_type = str(source.get("type") or "").lower()
        raw_type = str(primary.get("raw_type") or "").lower()
        version = str(primary.get("version") or "").lower()
        if work_type == "preprint" or "preprint" in raw_type or version in {"submittedversion", "acceptedversion"}:
            return "ACADEMIC_PREPRINT", "PREPRINT", work_type or raw_type or "preprint"
        if work_type in {"book-chapter", "book-section"}:
            return "BOOK_CHAPTER", "PUBLISHED_NON_PEER", work_type
        if work_type in {"book", "monograph"}:
            return "BOOK", "PUBLISHED_NON_PEER", work_type
        if work_type in {"report", "report-component"}:
            return "REPORT", "PUBLISHED_NON_PEER", work_type
        if work_type in {"dataset"}:
            return "DATASET", "NON_ARTICLE", work_type
        if work_type in {"dissertation"}:
            return "THESIS", "PUBLISHED_NON_PEER", work_type
        if work_type in {"editorial", "letter", "paratext"} or bool(raw.get("is_paratext")):
            return "EDITORIAL", "EDITORIAL", work_type or "paratext"
        if source_type in {"conference", "conference-series"} or "proceedings" in raw_type:
            return "CONFERENCE_PAPER", "PEER_REVIEWED", work_type or raw_type or "conference"
        if source_type == "journal" and bool(primary.get("is_published", True)):
            return "PEER_REVIEWED_PAPER", "PEER_REVIEWED", work_type or "article"
        return "SCHOLARLY_PUBLICATION_UNVERIFIED", "UNKNOWN", work_type or source_type or "unknown"

    if provider == "crossref":
        work_type = str(raw.get("type") or "").lower()
        subtype = str(raw.get("subtype") or "").lower()
        if subtype == "preprint" or work_type == "posted-content":
            return "ACADEMIC_PREPRINT", "PREPRINT", subtype or work_type
        if work_type in {"proceedings-article", "proceedings"}:
            return "CONFERENCE_PAPER", "PEER_REVIEWED", work_type
        if work_type == "journal-article":
            return "PEER_REVIEWED_PAPER", "PEER_REVIEWED", work_type
        if work_type in {"book-chapter", "book-section"}:
            return "BOOK_CHAPTER", "PUBLISHED_NON_PEER", work_type
        if work_type in {"report", "report-component"}:
            return "REPORT", "PUBLISHED_NON_PEER", work_type
        if work_type in {"dataset"}:
            return "DATASET", "NON_ARTICLE", work_type
        if work_type in {"dissertation"}:
            return "THESIS", "PUBLISHED_NON_PEER", work_type
        return "SCHOLARLY_PUBLICATION_UNVERIFIED", "UNKNOWN", work_type or subtype or "unknown"

    if provider == "semantic_scholar":
        kinds = {str(item or "").lower() for item in raw.get("publicationTypes") or []}
        if "review" in kinds or "journalarticle" in kinds:
            return "PEER_REVIEWED_PAPER", "PEER_REVIEWED", "journal"
        if "conference" in kinds:
            return "CONFERENCE_PAPER", "PEER_REVIEWED", "conference"
        if "book" in kinds or "bookchapter" in kinds:
            return "BOOK_CHAPTER", "PUBLISHED_NON_PEER", "book"
        if "dataset" in kinds:
            return "DATASET", "NON_ARTICLE", "dataset"
        return "SCHOLARLY_PUBLICATION_UNVERIFIED", "UNKNOWN", ",".join(sorted(kinds)) or "unknown"

    return "SCHOLARLY_PUBLICATION_UNVERIFIED", "UNKNOWN", "unknown"



def _candidate(
    *,
    query: str,
    provider: str,
    title: str,
    url: str,
    abstract: str = "",
    doi: Any = None,
    authors: Any = None,
    publisher: Any = None,
    published_at: Any = None,
    citation_count: Any = None,
    is_retracted: bool = False,
    source_type: str | None = None,
    publication_status: str | None = None,
    publication_kind: str | None = None,
    venue: str | None = None,
    raw: Any = None,
) -> dict[str, Any] | None:
    title = str(title or "").strip()
    doi_value = normalize_doi(doi, str(url or ""))
    url = str(url or "").strip()
    if doi_value:
        url = f"https://doi.org/{doi_value}"
    if not title or not url:
        return None
    try:
        citations = int(citation_count or 0)
    except (TypeError, ValueError):
        citations = 0
    body = _clean_abstract(abstract)
    return {
        "title": title,
        "url": url,
        "doi": doi_value,
        "authors": _author_names(authors),
        "publisher": str(publisher or provider).strip(),
        "published_at": published_at,
        "excerpt": body or title,
        "abstract": body,
        "content_text": body or title,
        "matched_query": query,
        "matched_queries": [query],
        "source_type": str(source_type or "SCHOLARLY_PUBLICATION_UNVERIFIED"),
        "publication_status": str(publication_status or "UNKNOWN"),
        "publication_kind": str(publication_kind or "unknown"),
        "venue": str(venue or publisher or provider).strip(),
        "academic_provider": provider,
        "citation_count": max(0, citations),
        "is_retracted": bool(is_retracted),
        "verification": {
            "status": "ACADEMIC_DISCOVERY_RETURNED",
            "discovery_provider": provider,
            "query": query,
            "citation_count": max(0, citations),
        },
        "raw_connector_result": raw,
    }


class AcademicSearchClient:
    """Small typed adapter over public scholarly discovery APIs.

    The client discovers candidates only.  Archiving, hashes, security checks and claim
    provenance remain owned by ``public_research.archive`` so WF-3 keeps one evidence chain.
    """

    OPENALEX_URL = "https://api.openalex.org/works"
    CROSSREF_URL = "https://api.crossref.org/works"
    SEMANTIC_SCHOLAR_URL = "https://api.semanticscholar.org/graph/v1/paper/search"

    def __init__(self, settings):
        self.settings = settings
        self.timeout = max(1, int(getattr(settings, "research_fetch_timeout_seconds", 45)))
        self.semantic_scholar_min_interval_seconds = self._environment_float(
            "SEMANTIC_SCHOLAR_MIN_INTERVAL_SECONDS", 1.05, minimum=0.0, maximum=60.0
        )
        self.semantic_scholar_max_retries = self._environment_int(
            "SEMANTIC_SCHOLAR_MAX_RETRIES", 3, minimum=0, maximum=10
        )
        self.semantic_scholar_max_retry_after_seconds = self._environment_float(
            "SEMANTIC_SCHOLAR_MAX_RETRY_AFTER_SECONDS", 60.0, minimum=1.0, maximum=600.0
        )
        self._semantic_scholar_request_lock = threading.Lock()
        self._semantic_scholar_next_request_at = 0.0

    @staticmethod
    def _environment_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
        raw = os.getenv(name, "").strip()
        if not raw:
            return default
        try:
            value = float(raw)
        except ValueError as exc:
            raise AcademicDiscoveryError(f"{name} must be a number, got {raw!r}") from exc
        if not minimum <= value <= maximum:
            raise AcademicDiscoveryError(f"{name} must be between {minimum} and {maximum}, got {value}")
        return value

    @staticmethod
    def _environment_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
        raw = os.getenv(name, "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError as exc:
            raise AcademicDiscoveryError(f"{name} must be an integer, got {raw!r}") from exc
        if not minimum <= value <= maximum:
            raise AcademicDiscoveryError(f"{name} must be between {minimum} and {maximum}, got {value}")
        return value

    @staticmethod
    def _retry_after_seconds(value: str | None) -> float | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return max(0.0, float(text))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(text)
            except (TypeError, ValueError, OverflowError):
                return None
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())

    def _get_json(self, url: str, *, params: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
        request_headers = {
            "User-Agent": "ProposalAgentAcademicDiscovery/1.0",
            "Accept": "application/json",
            **(headers or {}),
        }
        with httpx.Client(timeout=self.timeout, follow_redirects=True, headers=request_headers) as client:
            response = client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise AcademicDiscoveryError(f"Academic provider returned non-object JSON: {url}")
        return payload

    def _get_semantic_scholar_json(
        self,
        *,
        params: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Serialize S2 calls and retry only explicit rate-limit responses.

        Semantic Scholar API keys start at one request per second across all
        endpoints.  The lock prevents the multi-provider discovery pool from
        turning one WF-3 batch into a burst of concurrent S2 requests.
        """

        with self._semantic_scholar_request_lock:
            for attempt in range(self.semantic_scholar_max_retries + 1):
                delay = self._semantic_scholar_next_request_at - time.monotonic()
                if delay > 0:
                    time.sleep(delay)

                request_started_at = time.monotonic()
                self._semantic_scholar_next_request_at = (
                    request_started_at + self.semantic_scholar_min_interval_seconds
                )
                try:
                    return self._get_json(
                        self.SEMANTIC_SCHOLAR_URL,
                        params=params,
                        headers=headers,
                    )
                except httpx.HTTPStatusError as exc:
                    response = exc.response
                    if response.status_code != 429 or attempt >= self.semantic_scholar_max_retries:
                        raise
                    retry_after = self._retry_after_seconds(response.headers.get("Retry-After"))
                    exponential_backoff = float(2**attempt)
                    cooldown = min(
                        self.semantic_scholar_max_retry_after_seconds,
                        max(self.semantic_scholar_min_interval_seconds, retry_after or 0.0, exponential_backoff),
                    )
                    self._semantic_scholar_next_request_at = max(
                        self._semantic_scholar_next_request_at,
                        time.monotonic() + cooldown,
                    )

        raise AcademicDiscoveryError("Semantic Scholar retry loop ended unexpectedly")

    def search_openalex(self, query: str, limit: int, time_scope: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        start_date, end_date = parse_time_scope_bounds(time_scope)
        filters: list[str] = []
        if start_date:
            filters.append(f"from_publication_date:{start_date.isoformat()}")
        if end_date:
            filters.append(f"to_publication_date:{end_date.isoformat()}")
        params: dict[str, Any] = {"search": query, "per-page": min(max(1, limit), 50)}
        if filters:
            params["filter"] = ",".join(filters)
        mailto = os.getenv("OPENALEX_MAILTO", "").strip()
        if mailto:
            params["mailto"] = mailto
        payload = self._get_json(self.OPENALEX_URL, params=params)
        values: list[dict[str, Any]] = []
        for item in payload.get("results") or []:
            if not isinstance(item, dict):
                continue
            primary = item.get("primary_location") if isinstance(item.get("primary_location"), dict) else {}
            source = primary.get("source") if isinstance(primary.get("source"), dict) else {}
            publisher = source.get("display_name") or (item.get("host_venue", {}).get("display_name") if isinstance(item.get("host_venue"), dict) else None) or "OpenAlex"
            source_type, publication_status, publication_kind = _publication_profile("openalex", item, str(item.get("display_name") or item.get("title") or ""), str(publisher))
            candidate = _candidate(
                query=query,
                provider="openalex",
                title=str(item.get("display_name") or item.get("title") or ""),
                url=str(primary.get("landing_page_url") or item.get("doi") or item.get("id") or ""),
                abstract=_openalex_abstract(item.get("abstract_inverted_index")),
                doi=item.get("doi"),
                authors=item.get("authorships") or [],
                publisher=publisher,
                published_at=item.get("publication_date") or item.get("publication_year"),
                citation_count=item.get("cited_by_count"),
                is_retracted=bool(item.get("is_retracted")),
                source_type=source_type,
                publication_status=publication_status,
                publication_kind=publication_kind,
                venue=source.get("display_name") or publisher,
                raw=item,
            )
            if candidate:
                values.append(candidate)
        return values, payload

    def search_crossref(self, query: str, limit: int, time_scope: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        start_date, end_date = parse_time_scope_bounds(time_scope)
        filters: list[str] = []
        if start_date:
            filters.append(f"from-pub-date:{start_date.isoformat()}")
        if end_date:
            filters.append(f"until-pub-date:{end_date.isoformat()}")
        params: dict[str, Any] = {
            "query.bibliographic": query,
            "rows": min(max(1, limit), 50),
            "filter": ",".join(filters),
        }
        mailto = os.getenv("CROSSREF_MAILTO", "").strip()
        if mailto:
            params["mailto"] = mailto
        payload = self._get_json(self.CROSSREF_URL, params=params)
        message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
        values: list[dict[str, Any]] = []
        for item in message.get("items") or []:
            if not isinstance(item, dict):
                continue
            title_value = item.get("title") or []
            title = str(title_value[0] if isinstance(title_value, list) and title_value else title_value or "")
            container = item.get("container-title") or []
            publisher = (
                container[0]
                if isinstance(container, list) and container
                else item.get("publisher")
            )
            date_parts = None
            for key in ("published-print", "published-online", "published", "issued"):
                value = item.get(key)
                if isinstance(value, dict) and value.get("date-parts"):
                    date_parts = value.get("date-parts")
                    break
            published_at = None
            if isinstance(date_parts, list) and date_parts and isinstance(date_parts[0], list):
                parts = [str(part) for part in date_parts[0][:3] if isinstance(part, int)]
                published_at = "-".join(part.zfill(2) if index else part for index, part in enumerate(parts))
            url = str(item.get("URL") or "")
            update_type = str(item.get("subtype") or "").lower()
            retracted = "retract" in update_type or "retract" in title.lower()
            source_type, publication_status, publication_kind = _publication_profile("crossref", item, title, str(publisher or ""))
            candidate = _candidate(
                query=query,
                provider="crossref",
                title=title,
                url=url,
                abstract=item.get("abstract"),
                doi=item.get("DOI"),
                authors=item.get("author") or [],
                publisher=publisher,
                published_at=published_at,
                citation_count=item.get("is-referenced-by-count"),
                is_retracted=retracted,
                source_type=source_type,
                publication_status=publication_status,
                publication_kind=publication_kind,
                venue=publisher,
                raw=item,
            )
            if candidate:
                values.append(candidate)
        return values, payload

    def search_semantic_scholar(self, query: str, limit: int, time_scope: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        start_date, end_date = parse_time_scope_bounds(time_scope)
        params: dict[str, Any] = {
            "query": query,
            "limit": min(max(1, limit), 100),
            "fields": "title,url,abstract,year,authors,venue,externalIds,publicationDate,citationCount,isOpenAccess,openAccessPdf,publicationTypes,journal",
        }
        if start_date or end_date:
            lower = start_date.year if start_date else 1900
            upper = end_date.year if end_date else 2100
            params["year"] = f"{lower}-{upper}"
        headers = {}
        api_key = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "").strip()
        if api_key:
            headers["x-api-key"] = api_key
        payload = self._get_semantic_scholar_json(params=params, headers=headers)
        values: list[dict[str, Any]] = []
        for item in payload.get("data") or []:
            if not isinstance(item, dict):
                continue
            external = item.get("externalIds") if isinstance(item.get("externalIds"), dict) else {}
            open_pdf = item.get("openAccessPdf") if isinstance(item.get("openAccessPdf"), dict) else {}
            source_type, publication_status, publication_kind = _publication_profile("semantic_scholar", item, str(item.get("title") or ""), str(item.get("venue") or ""))
            candidate = _candidate(
                query=query,
                provider="semantic_scholar",
                title=str(item.get("title") or ""),
                url=str(open_pdf.get("url") or item.get("url") or ""),
                abstract=item.get("abstract"),
                doi=external.get("DOI"),
                authors=item.get("authors") or [],
                publisher=item.get("venue") or "Semantic Scholar",
                published_at=item.get("publicationDate") or item.get("year"),
                citation_count=item.get("citationCount"),
                source_type=source_type,
                publication_status=publication_status,
                publication_kind=publication_kind,
                venue=item.get("venue") or ((item.get("journal") or {}).get("name") if isinstance(item.get("journal"), dict) else None),
                raw=item,
            )
            if candidate:
                values.append(candidate)
        return values, payload

    def discover(
        self,
        queries: list[str],
        *,
        time_scope: Any,
        per_query_limit: int,
        providers: tuple[str, ...] = ACADEMIC_PROVIDERS,
    ) -> dict[str, Any]:
        provider_methods: dict[str, Callable[[str, int, Any], tuple[list[dict[str, Any]], dict[str, Any]]]] = {
            "openalex": self.search_openalex,
            "crossref": self.search_crossref,
            "semantic_scholar": self.search_semantic_scholar,
        }
        selected = tuple(provider for provider in providers if provider in provider_methods)
        if not selected:
            raise AcademicDiscoveryError("No supported academic discovery provider is enabled")
        grouped: dict[str, list[dict[str, Any]]] = {query: [] for query in queries}
        raw_runs: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []

        def run_one(query_index: int, provider_index: int, query: str, provider: str):
            method = provider_methods[provider]
            try:
                candidates, raw = method(query, per_query_limit, time_scope)
                return query_index, provider_index, query, provider, candidates, raw, None
            except Exception as exc:  # provider isolation is intentional
                return query_index, provider_index, query, provider, [], None, exc

        jobs = []
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(queries) * len(selected))), thread_name_prefix="academic-discovery") as pool:
            for query_index, query in enumerate(queries):
                for provider_index, provider in enumerate(selected):
                    jobs.append(pool.submit(run_one, query_index, provider_index, query, provider))
            ordered: list[tuple[int, int, str, str, list[dict[str, Any]], Any, Exception | None]] = []
            for future in as_completed(jobs):
                ordered.append(future.result())

        ordered.sort(key=lambda item: (item[0], item[1]))
        for _, _, query, provider, candidates, raw, error in ordered:
            if error is not None:
                failures.append(
                    {
                        "query": query,
                        "provider": provider,
                        "category": "RETRIEVAL",
                        "error_code": "ACADEMIC_DISCOVERY_PROVIDER_ERROR",
                        "message": f"{type(error).__name__}: {error}",
                    }
                )
                continue
            grouped[query].extend(candidates)
            raw_runs.append(
                {
                    "query": query,
                    "provider": provider,
                    "result_count": len(candidates),
                    "raw_response": raw,
                }
            )

        responses = [
            {
                "query": query,
                "retrieved_at": utc_now(),
                "results": grouped.get(query) or [],
            }
            for query in queries
        ]
        if not any(response["results"] for response in responses):
            raise AcademicDiscoveryError(
                "All academic discovery providers returned no usable candidate",
            )
        return {
            "schema_version": "1.0",
            "run_id": new_id("academic-discovery"),
            "connector": "wf3-academic-multi-source",
            "created_at": utc_now(),
            "agent_generated_queries": list(queries),
            "providers": list(selected),
            "responses": responses,
            "provider_runs": raw_runs,
            "failures": failures,
            "per_query_limit": int(per_query_limit),
            "time_scope": time_scope,
        }
