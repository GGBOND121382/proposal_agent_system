from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .base import SkillContext, SkillResult
from .content_extraction import ContentExtractor
from .fetch_gateway import (
    FetchGatewayRetrievalError,
    FetchGatewaySecurityError,
    HttpFetchGateway,
    validate_public_url,
)
from .search_gateway import SearchGateway, normalize_search_queries
from .search_providers import (
    ConnectorSearchProvider,
    RecordedSearchProvider,
    SearchProviderConfigurationError,
    SearxngSearchProvider,
)
from ..util import new_id, safe_filename, sha256_bytes, sha256_text, utc_now, write_json


class PublicResearchArchiveError(RuntimeError):
    """Base class for auditable public-research failures.

    ``category`` is intentionally machine-readable so the workflow can distinguish
    configuration dependencies from plan-contract, retrieval, security and archive
    integrity failures without classifying every error raised inside PUBLIC_SEARCH as
    ``WAITING_CONFIGURATION``.
    """

    category = "RUNTIME"
    error_code = "PUBLIC_RESEARCH_RUNTIME_ERROR"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = dict(details or {})


class PublicResearchConfigurationError(PublicResearchArchiveError):
    category = "CONFIGURATION"
    error_code = "PUBLIC_RESEARCH_CONFIGURATION_ERROR"


class PublicResearchPlanContractError(PublicResearchArchiveError):
    category = "PLAN_CONTRACT"
    error_code = "PUBLIC_RESEARCH_PLAN_CONTRACT_ERROR"


class PublicResearchRetrievalError(PublicResearchArchiveError):
    category = "RETRIEVAL"
    error_code = "PUBLIC_RESEARCH_RETRIEVAL_ERROR"


class PublicResearchSecurityError(PublicResearchArchiveError):
    category = "SECURITY"
    error_code = "PUBLIC_RESEARCH_SECURITY_ERROR"


class PublicResearchIntegrityError(PublicResearchArchiveError):
    category = "INTEGRITY"
    error_code = "PUBLIC_RESEARCH_INTEGRITY_ERROR"


class PublicResearchArchiveSkill:
    skill_id = "public_research.archive"
    version = "1.3.0"
    description = "Search public sources, fetch and extract them, and preserve verifiable snapshots with hashes."

    def __init__(self, settings):
        self.settings = settings
        self.fetch_gateway = HttpFetchGateway(settings, client_factory=httpx.Client)
        self.content_extractor = ContentExtractor()
        self._last_search_execution: dict[str, Any] | None = None

    def run(self, payload: dict[str, Any], context: SkillContext) -> SkillResult:
        self._last_search_execution = None
        provider = str(payload.get("provider") or self.settings.public_search_provider).lower()
        plan = payload.get("plan") or {}
        queries = self._queries(plan)
        max_results = max(1, min(int(payload.get("max_results") or self.settings.public_search_max_results), 100))
        session_id = new_id("research")
        root = Path(context.data_dir) / "research_archive" / safe_filename(context.project_id) / session_id
        raw_dir = root / "raw"
        text_dir = root / "text"
        meta_dir = root / "metadata"
        connector_dir = root / "connector"
        for directory in [raw_dir, text_dir, meta_dir, connector_dir]:
            directory.mkdir(parents=True, exist_ok=True)

        connector_manifest: dict[str, Any] | None = None
        retrieval_warnings: list[str] = []
        retrieval_failures: list[dict[str, Any]] = []
        if provider == "recorded":
            candidates = self._load_recorded(payload.get("record_file") or self.settings.public_research_record_file)
            retrieval_mode = "RECORDED_VERIFIED_SOURCE_SET"
        elif provider == "connector":
            connector_path = payload.get("connector_file") or self.settings.public_research_connector_file
            candidates, connector_manifest = self._load_connector(connector_path, queries)
            retrieval_mode = "LIVE_CONNECTOR_ARCHIVE"
        elif provider == "searxng":
            candidates, retrieval_failures = self._search_searxng(queries, max_results)
            retrieval_warnings = [
                f"query={item['query']}: {item['message']}"
                for item in retrieval_failures
            ]
            retrieval_mode = "LIVE_SEARXNG"
        else:
            raise PublicResearchConfigurationError(f"Unsupported PUBLIC_SEARCH_PROVIDER: {provider}")

        records: list[dict[str, Any]] = []
        sources: list[dict[str, Any]] = []
        passages: list[dict[str, Any]] = []
        warnings: list[str] = list(retrieval_warnings)
        candidate_failures: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for candidate in candidates:
            if len(records) >= max_results:
                break
            url = str(candidate.get("url") or "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            try:
                record = self._archive_candidate(candidate, raw_dir, text_dir, meta_dir, provider)
            except PublicResearchArchiveError as exc:
                warning = f"{url}: {exc}"
                warnings.append(warning)
                candidate_failures.append({
                    "url": url,
                    "category": str(getattr(exc, "category", "RUNTIME") or "RUNTIME"),
                    "error_code": str(getattr(exc, "error_code", "PUBLIC_RESEARCH_RUNTIME_ERROR") or "PUBLIC_RESEARCH_RUNTIME_ERROR"),
                    "message": str(exc),
                    "details": dict(getattr(exc, "details", {}) or {}),
                })
                continue
            except Exception as exc:
                warning = f"{url}: {exc}"
                warnings.append(warning)
                candidate_failures.append({
                    "url": url,
                    "category": "RETRIEVAL",
                    "error_code": "PUBLIC_RESEARCH_SOURCE_RETRIEVAL_ERROR",
                    "message": str(exc),
                    "details": {"exception_type": type(exc).__name__},
                })
                continue
            records.append(record)
            source_ref = {
                "source_id": record["source_id"],
                "source_type": "PUBLIC_SOURCE",
                "document_version_id": None,
                "section_id": None,
                "span_start": None,
                "span_end": None,
                "quoted_text": f"{record['title']} | {record['url']}",
                "source_hash": record["snapshot_sha256"],
                "authority_rank": record["authority_rank"],
                "security_level": "PUBLIC",
            }
            sources.append(source_ref)
            passages.append(
                {
                    "passage_id": new_id("passage"),
                    "source_ref": source_ref,
                    "text": record["excerpt"][:6000],
                    "relevance": record.get("matched_query") or (queries[0] if queries else "公开资料检索"),
                }
            )

        if not records:
            failure_categories = {
                str(item.get("category") or "RUNTIME").upper()
                for item in candidate_failures
            }
            details = {
                "warnings": warnings,
                "query_failures": retrieval_failures,
                "candidate_failures": candidate_failures,
                "candidate_count": len(candidates),
            }
            if failure_categories and failure_categories <= {"SECURITY"}:
                raise PublicResearchSecurityError(
                    "All candidate sources were rejected by the public-source security policy",
                    details=details,
                )
            if failure_categories and failure_categories <= {"INTEGRITY"}:
                raise PublicResearchIntegrityError(
                    "All candidate sources failed archive integrity validation",
                    details=details,
                )
            raise PublicResearchRetrievalError(
                "No public source could be archived",
                details=details,
            )

        if connector_manifest is not None:
            write_json(connector_dir / "connector_response.json", connector_manifest)

        manifest = {
            "schema_version": "1.0",
            "session_id": session_id,
            "project_id": context.project_id,
            "workflow_id": context.workflow_id,
            "retrieval_mode": retrieval_mode,
            "provider": provider,
            "queries": queries,
            "created_at": utc_now(),
            "source_count": len(records),
            "warning_count": len(warnings),
            "warnings": warnings,
            "query_failures": retrieval_failures,
            "provider_runs": list((self._last_search_execution or {}).get("provider_runs") or []),
            "records": records,
            "connector_response": str(connector_dir / "connector_response.json") if connector_manifest is not None else None,
        }
        manifest_path = root / "manifest.json"
        write_json(manifest_path, manifest)
        self._write_csv(root / "source_index.csv", records)
        return SkillResult(
            status="PASS",
            output={
                "sources": sources,
                "passages": passages,
                "queries": queries,
                "mode": retrieval_mode,
                "archive_session_id": session_id,
                "archive_root": str(root),
                "archive_manifest": str(manifest_path),
                "source_index": str(root / "source_index.csv"),
                "warnings": warnings,
            },
            warnings=warnings,
            artifacts=[str(manifest_path), str(root / "source_index.csv")] + ([str(connector_dir / "connector_response.json")] if connector_manifest is not None else []),
        )

    def _queries(self, plan: dict[str, Any]) -> list[str]:
        result: list[str] = []
        for item in plan.get("queries", []):
            if isinstance(item, str):
                value = item
            elif isinstance(item, dict):
                value = item.get("query") or item.get("query_text") or item.get("text") or ""
            else:
                value = ""
            value = str(value).strip()
            if value and value not in result:
                result.append(value)
        return result[:12]

    def _load_recorded(self, record_file: str | Path) -> list[dict[str, Any]]:
        try:
            return RecordedSearchProvider(record_file).load_candidates()
        except SearchProviderConfigurationError as exc:
            raise PublicResearchConfigurationError(str(exc), details=exc.details) from exc


    def _load_connector(self, connector_file: str | Path, planned_queries: list[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        try:
            return ConnectorSearchProvider(connector_file).load_candidates(planned_queries)
        except SearchProviderConfigurationError as exc:
            raise PublicResearchConfigurationError(str(exc), details=exc.details) from exc

    def _search_searxng_sequential(
        self,
        queries: list[str],
        max_results: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return self._search_searxng_with_provider(queries, max_results, max_workers=1)

    def _search_searxng(
        self,
        queries: list[str],
        max_results: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Run SearXNG through the provider-neutral search contract."""
        return self._search_searxng_with_provider(queries, max_results, max_workers=4)

    def _search_searxng_with_provider(
        self,
        queries: list[str],
        max_results: int,
        *,
        max_workers: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not queries:
            return [], []
        provider = SearxngSearchProvider(
            self.settings,
            # Resolve the module attribute at call time so existing test and
            # deployment injection points remain compatible.
            client_factory=httpx.Client,
            max_workers=max_workers,
        )
        try:
            batch = SearchGateway([provider]).search(
                normalize_search_queries(queries),
                per_query_limit=max_results,
            )
        except SearchProviderConfigurationError as exc:
            raise PublicResearchConfigurationError(str(exc), details=exc.details) from exc
        self._last_search_execution = {
            "providers": list(batch.providers),
            "provider_runs": [run.to_dict() for run in batch.runs],
            "failures": list(batch.failures),
        }
        candidates = batch.candidates()
        if not candidates and batch.failures:
            endpoint = f"{str(self.settings.public_search_base_url or '').rstrip('/')}/search"
            raise PublicResearchRetrievalError(
                "All SearXNG queries failed",
                details={"endpoint": endpoint, "query_failures": batch.failures},
            )
        return candidates, batch.failures

    @staticmethod
    def _round_robin(groups: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
        """Interleave query results so max_results cannot starve later queries."""
        if not groups:
            return []
        candidates: list[dict[str, Any]] = []
        for index in range(max((len(group) for group in groups), default=0)):
            for group in groups:
                if index < len(group):
                    candidates.append(group[index])
        return candidates

    def _archive_candidate(
        self,
        candidate: dict[str, Any],
        raw_dir: Path,
        text_dir: Path,
        meta_dir: Path,
        provider: str,
    ) -> dict[str, Any]:
        url = str(candidate.get("url") or "").strip()
        self._validate_public_url(url, resolve_dns=provider == "searxng")
        source_id = str(candidate.get("source_id") or new_id("public-src"))
        title = str(candidate.get("title") or url).strip()
        retrieved_at = str(candidate.get("retrieved_at") or utc_now())
        matched_query = str(candidate.get("matched_query") or "")

        if provider in {"recorded", "connector"}:
            body_text = str(candidate.get("content_text") or candidate.get("page_text") or candidate.get("excerpt") or candidate.get("abstract") or "").strip()
            raw_payload = {
                "title": title,
                "url": url,
                "retrieved_at": retrieved_at,
                "published_at": candidate.get("published_at"),
                "authors": candidate.get("authors") or [],
                "publisher": candidate.get("publisher"),
                "doi": candidate.get("doi"),
                "source_type": candidate.get("source_type"),
                "publication_status": candidate.get("publication_status"),
                "publication_kind": candidate.get("publication_kind"),
                "venue": candidate.get("venue"),
                "content_text": body_text,
                "verification": candidate.get("verification") or {},
                "connector": candidate.get("connector"),
                "raw_connector_result": candidate.get("raw_connector_result") or candidate.get("raw_result"),
            }
            raw_bytes = json.dumps(raw_payload, ensure_ascii=False, indent=2).encode("utf-8")
            suffix = ".json"
            content_type = "application/json"
            final_url = url
            http_status = None
            fetch_mode = "PROVIDER_PAYLOAD"
            extractor = "PROVIDER_TEXT"
            extraction_quality = "USABLE" if len(body_text) >= 200 else ("SHORT" if body_text else "EMPTY")
            extraction_failure_reason = "NO_EXTRACTABLE_TEXT" if not body_text else None
        else:
            try:
                fetched = self.fetch_gateway.fetch(url)
            except FetchGatewaySecurityError as exc:
                raise PublicResearchSecurityError(str(exc), details=exc.details) from exc
            except FetchGatewayRetrievalError as exc:
                raise PublicResearchRetrievalError(str(exc), details=exc.details) from exc
            extracted = self.content_extractor.extract(fetched)
            raw_bytes = fetched.raw_bytes
            content_type = fetched.content_type
            final_url = fetched.final_url
            http_status = fetched.http_status
            fetch_mode = fetched.fetch_mode
            extractor = extracted.extractor
            extraction_quality = extracted.quality
            extraction_failure_reason = extracted.failure_reason
            body_text = extracted.text
            if not body_text:
                body_text = str(candidate.get("excerpt") or title)
            suffix = self.content_extractor.suffix(content_type, final_url)

        excerpt = self._compact_text(body_text)[:12000]
        if len(excerpt) < 20:
            excerpt = self._compact_text(str(candidate.get("excerpt") or title))
        raw_path = raw_dir / f"{safe_filename(source_id)}{suffix}"
        text_path = text_dir / f"{safe_filename(source_id)}.txt"
        meta_path = meta_dir / f"{safe_filename(source_id)}.json"
        raw_path.write_bytes(raw_bytes)
        # Write the exact bytes that were hashed.  Path.write_text() performs
        # platform newline translation on Windows, which changes LF to CRLF
        # and makes immediate archive verification fail.
        text_path.write_bytes(body_text.encode("utf-8"))
        snapshot_hash = sha256_bytes(raw_bytes)
        text_hash = sha256_text(body_text)
        parsed = urlparse(final_url)
        record = {
            "source_id": source_id,
            "title": title,
            "url": url,
            "final_url": final_url,
            "domain": parsed.netloc,
            "published_at": candidate.get("published_at"),
            "authors": candidate.get("authors") or [],
            "publisher": candidate.get("publisher"),
            "doi": candidate.get("doi"),
            "source_type": candidate.get("source_type"),
            "publication_status": candidate.get("publication_status"),
            "publication_kind": candidate.get("publication_kind"),
            "venue": candidate.get("venue"),
            "retrieved_at": retrieved_at,
            "matched_query": matched_query,
            "retrieval_provider": provider,
            "http_status": http_status,
            "content_type": content_type,
            "fetch_mode": fetch_mode,
            "extractor": extractor,
            "extraction_quality": extraction_quality,
            "extraction_failure_reason": extraction_failure_reason,
            "raw_path": str(raw_path),
            "text_path": str(text_path),
            "metadata_path": str(meta_path),
            "snapshot_sha256": snapshot_hash,
            "text_sha256": text_hash,
            "byte_size": len(raw_bytes),
            "text_length": len(body_text),
            "excerpt": excerpt,
            "authority_rank": self._authority_rank(parsed.netloc, candidate),
            "verification": candidate.get("verification") or {},
        }
        write_json(meta_path, record)
        return record

    def _fetch_url(self, url: str) -> tuple[bytes, str, str, int]:
        try:
            fetched = HttpFetchGateway(
                self.settings,
                client_factory=httpx.Client,
            ).fetch(url)
        except FetchGatewaySecurityError as exc:
            raise PublicResearchSecurityError(str(exc), details=exc.details) from exc
        except FetchGatewayRetrievalError as exc:
            raise PublicResearchRetrievalError(str(exc), details=exc.details) from exc
        return fetched.raw_bytes, fetched.content_type, fetched.final_url, fetched.http_status

    @staticmethod
    def _extract_text(raw: bytes, content_type: str, url: str) -> str:
        return ContentExtractor.extract_text(raw, content_type, url)[0]

    @staticmethod
    def _compact_text(text: str) -> str:
        return ContentExtractor.compact_text(text)

    @staticmethod
    def _suffix(content_type: str, url: str) -> str:
        return ContentExtractor.suffix(content_type, url)

    @staticmethod
    def _validate_public_url(url: str, *, resolve_dns: bool) -> None:
        try:
            validate_public_url(url, resolve_dns=resolve_dns)
        except FetchGatewaySecurityError as exc:
            raise PublicResearchSecurityError(str(exc), details=exc.details) from exc
        except FetchGatewayRetrievalError as exc:
            raise PublicResearchRetrievalError(str(exc), details=exc.details) from exc

    @staticmethod
    def _authority_rank(domain: str, candidate: dict[str, Any]) -> int:
        if candidate.get("authority_rank") is not None:
            return int(candidate["authority_rank"])
        lowered = domain.lower()
        if lowered.endswith(".gov") or lowered.endswith(".gov.cn") or lowered in {"rfc-editor.org", "www.rfc-editor.org"}:
            return 95
        if lowered.endswith(".edu") or lowered.endswith(".edu.cn") or "ac.cn" in lowered or "ietf.org" in lowered or "iso.org" in lowered:
            return 85
        if "github.com" in lowered or "signal.org" in lowered or "openssl.org" in lowered:
            return 75
        return 60

    @staticmethod
    def _write_csv(path: Path, records: list[dict[str, Any]]) -> None:
        fields = [
            "source_id", "title", "url", "final_url", "domain", "published_at", "publisher",
            "doi", "retrieved_at", "retrieval_provider", "http_status", "content_type",
            "fetch_mode", "extractor", "extraction_quality", "extraction_failure_reason",
            "snapshot_sha256", "text_sha256", "byte_size", "text_length", "authority_rank",
            "raw_path", "text_path", "metadata_path",
        ]
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(records)
