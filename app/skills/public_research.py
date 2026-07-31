from __future__ import annotations

import csv
import hashlib
import ipaddress
import json
import mimetypes
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader

from .base import SkillContext, SkillResult
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

    def run(self, payload: dict[str, Any], context: SkillContext) -> SkillResult:
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
        path = Path(record_file)
        if not path.exists():
            raise PublicResearchConfigurationError(f"Recorded research file not found: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PublicResearchConfigurationError(
                f"Recorded research file is not readable JSON: {path}: {exc}"
            ) from exc
        sources = payload.get("sources") if isinstance(payload, dict) else payload
        if not isinstance(sources, list):
            raise PublicResearchConfigurationError("Recorded research file must contain a sources array")
        return [item for item in sources if isinstance(item, dict)]


    def _load_connector(self, connector_file: str | Path, planned_queries: list[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        path = Path(connector_file)
        if not path.exists():
            raise PublicResearchConfigurationError(f"Connector research file not found: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PublicResearchConfigurationError(
                f"Connector research file is not readable JSON: {path}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise PublicResearchConfigurationError("Connector research file must be a JSON object")
        responses = payload.get("responses")
        if not isinstance(responses, list):
            raise PublicResearchConfigurationError("Connector research file must contain a responses array")
        connector_queries = []
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
        missing = [q for q in planned_queries if q not in connector_queries]
        if missing:
            raise PublicResearchConfigurationError(f"Connector responses do not cover planned queries: {missing}", details={"missing_queries": missing})
        if not candidates:
            raise PublicResearchConfigurationError("Connector research file contains no result records")
        manifest = {
            **payload,
            "ingested_at": utc_now(),
            "planned_queries": planned_queries,
            "connector_queries": connector_queries,
            "result_count": len(candidates),
            "source_file": str(path),
            "source_file_sha256": sha256_bytes(path.read_bytes()),
        }
        return candidates, manifest

    def _search_searxng_sequential(
        self,
        queries: list[str],
        max_results: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not self.settings.public_search_base_url:
            raise PublicResearchConfigurationError("PUBLIC_SEARCH_BASE_URL is empty")
        endpoint = f"{self.settings.public_search_base_url.rstrip('/')}/search"
        engines = str(getattr(self.settings, "public_search_engines", "") or "").strip()
        results_by_query: list[list[dict[str, Any]]] = []
        query_failures: list[dict[str, Any]] = []
        with httpx.Client(
            timeout=self.settings.research_fetch_timeout_seconds,
            follow_redirects=True,
            # The SearXNG endpoint is commonly loopback/LAN.  Windows system
            # proxy discovery can otherwise route 127.0.0.1 through a proxy.
            trust_env=False,
        ) as client:
            for query in queries:
                params = {
                    "q": query,
                    "format": "json",
                    "language": "all",
                    "safesearch": 1,
                }
                if engines:
                    params["engines"] = engines
                query_candidates: list[dict[str, Any]] = []
                try:
                    response = client.get(
                        endpoint,
                        params=params,
                    )
                    response.raise_for_status()
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        raise PublicResearchConfigurationError(
                            f"SearXNG JSON API returned invalid JSON: {endpoint}",
                            details={"endpoint": endpoint, "query": query},
                        ) from exc
                    results = payload.get("results") if isinstance(payload, dict) else None
                    if not isinstance(results, list):
                        raise PublicResearchConfigurationError(
                            f"SearXNG JSON API response has no results array: {endpoint}",
                            details={"endpoint": endpoint, "query": query},
                        )
                    for item in results[: min(10, max_results)]:
                        if not isinstance(item, dict):
                            continue
                        query_candidates.append(
                            {
                                "title": str(item.get("title") or "").strip(),
                                "url": str(item.get("url") or "").strip(),
                                "excerpt": str(item.get("content") or item.get("snippet") or "").strip(),
                                "matched_query": query,
                                "engine": item.get("engine"),
                            }
                        )
                except PublicResearchConfigurationError:
                    raise
                except (httpx.TimeoutException, httpx.RequestError, httpx.HTTPStatusError) as exc:
                    query_failures.append(
                        {
                            "query": query,
                            "category": "RETRIEVAL",
                            "error_code": "PUBLIC_RESEARCH_QUERY_RETRIEVAL_ERROR",
                            "message": f"SearXNG query failed: {exc}",
                            "details": {
                                "endpoint": endpoint,
                                "exception_type": type(exc).__name__,
                            },
                        }
                    )
                    query_candidates = []
                results_by_query.append(query_candidates)

        candidates = self._round_robin(results_by_query)
        if not candidates and query_failures:
            raise PublicResearchRetrievalError(
                "All SearXNG queries failed",
                details={
                    "endpoint": endpoint,
                    "query_failures": query_failures,
                },
            )
        return candidates, query_failures

    def _search_searxng(
        self,
        queries: list[str],
        max_results: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Run independent SearXNG queries concurrently and preserve query order."""
        if not queries:
            return [], []
        results_by_query: list[list[dict[str, Any]]] = [[] for _ in queries]
        query_failures: list[dict[str, Any]] = []

        def search_one(index: int, query: str):
            try:
                candidates, failures = self._search_searxng_sequential([query], max_results)
                return index, candidates, failures
            except PublicResearchRetrievalError as exc:
                failures = list(exc.details.get("query_failures") or [])
                if not failures:
                    failures = [
                        {
                            "query": query,
                            "category": "RETRIEVAL",
                            "error_code": exc.error_code,
                            "message": str(exc),
                            "details": dict(exc.details),
                        }
                    ]
                return index, [], failures

        with ThreadPoolExecutor(
            max_workers=min(4, len(queries)),
            thread_name_prefix="searxng-query",
        ) as pool:
            futures = [
                pool.submit(search_one, index, query)
                for index, query in enumerate(queries)
            ]
            for future in as_completed(futures):
                index, candidates, failures = future.result()
                results_by_query[index] = candidates
                query_failures.extend(failures)

        query_order = {query: index for index, query in enumerate(queries)}
        query_failures.sort(
            key=lambda item: query_order.get(str(item.get("query") or ""), len(queries))
        )
        candidates = self._round_robin(results_by_query)
        if not candidates and query_failures:
            raise PublicResearchRetrievalError(
                "All SearXNG queries failed",
                details={
                    "endpoint": f"{self.settings.public_search_base_url.rstrip('/')}/search",
                    "query_failures": query_failures,
                },
            )
        return candidates, query_failures

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
        else:
            raw_bytes, content_type, final_url, http_status = self._fetch_url(url)
            body_text = self._extract_text(raw_bytes, content_type, final_url)
            if not body_text:
                body_text = str(candidate.get("excerpt") or title)
            suffix = self._suffix(content_type, final_url)

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
            "retrieved_at": retrieved_at,
            "matched_query": matched_query,
            "retrieval_provider": provider,
            "http_status": http_status,
            "content_type": content_type,
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
        headers = {
            "User-Agent": "ProposalAgentResearchArchiver/1.0 (+public-source-verification)",
            "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain;q=0.9,*/*;q=0.1",
        }
        limit = int(self.settings.research_max_source_bytes)
        with httpx.Client(timeout=self.settings.research_fetch_timeout_seconds, follow_redirects=True, headers=headers) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                final_url = str(response.url)
                self._validate_public_url(final_url, resolve_dns=True)
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > limit:
                        raise PublicResearchRetrievalError(f"Source exceeds {limit} bytes")
                    chunks.append(chunk)
                content_type = response.headers.get("content-type", "application/octet-stream").split(";", 1)[0].lower()
                return b"".join(chunks), content_type, final_url, response.status_code

    @staticmethod
    def _extract_text(raw: bytes, content_type: str, url: str) -> str:
        if content_type == "application/pdf" or url.lower().endswith(".pdf"):
            reader = PdfReader(BytesIO(raw))
            return "\n\n".join((page.extract_text() or "") for page in reader.pages[:200])
        if content_type.startswith("text/plain"):
            return raw.decode("utf-8", errors="replace")
        text = raw.decode("utf-8", errors="replace")
        soup = BeautifulSoup(text, "html.parser")
        for node in soup(["script", "style", "noscript", "svg", "nav", "footer", "header"]):
            node.decompose()
        main = soup.find("main") or soup.find("article") or soup.body or soup
        return main.get_text("\n", strip=True)

    @staticmethod
    def _compact_text(text: str) -> str:
        return "\n".join(line.strip() for line in text.splitlines() if line.strip())

    @staticmethod
    def _suffix(content_type: str, url: str) -> str:
        if content_type == "application/pdf" or url.lower().endswith(".pdf"):
            return ".pdf"
        if content_type.startswith("text/plain"):
            return ".txt"
        if "html" in content_type:
            return ".html"
        return mimetypes.guess_extension(content_type) or ".bin"

    @staticmethod
    def _validate_public_url(url: str, *, resolve_dns: bool) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise PublicResearchSecurityError("Only public HTTP(S) URLs are allowed")
        host = parsed.hostname.lower()
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            raise PublicResearchSecurityError("Local addresses are prohibited")
        try:
            ip = ipaddress.ip_address(host)
            addresses = [ip]
        except ValueError:
            addresses = []
            if resolve_dns:
                try:
                    addresses = [ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, None)]
                except socket.gaierror as exc:
                    raise PublicResearchRetrievalError(f"DNS resolution failed for {host}") from exc
        for ip in addresses:
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                raise PublicResearchSecurityError(f"Private/reserved address is prohibited: {ip}")

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
            "snapshot_sha256", "text_sha256", "byte_size", "text_length", "authority_rank",
            "raw_path", "text_path", "metadata_path",
        ]
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(records)
