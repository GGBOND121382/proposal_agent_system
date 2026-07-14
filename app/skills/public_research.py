from __future__ import annotations

import csv
import hashlib
import ipaddress
import json
import mimetypes
import socket
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
    pass


class PublicResearchArchiveSkill:
    skill_id = "public_research.archive"
    version = "1.1.0"
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
        if provider == "recorded":
            candidates = self._load_recorded(payload.get("record_file") or self.settings.public_research_record_file)
            retrieval_mode = "RECORDED_VERIFIED_SOURCE_SET"
        elif provider == "connector":
            connector_path = payload.get("connector_file") or self.settings.public_research_connector_file
            candidates, connector_manifest = self._load_connector(connector_path, queries)
            retrieval_mode = "LIVE_CONNECTOR_ARCHIVE"
        elif provider == "searxng":
            candidates = self._search_searxng(queries, max_results)
            retrieval_mode = "LIVE_SEARXNG"
        else:
            raise PublicResearchArchiveError(f"Unsupported PUBLIC_SEARCH_PROVIDER: {provider}")

        records: list[dict[str, Any]] = []
        sources: list[dict[str, Any]] = []
        passages: list[dict[str, Any]] = []
        warnings: list[str] = []
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
            except Exception as exc:
                warnings.append(f"{url}: {exc}")
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
            raise PublicResearchArchiveError("No public source could be archived")

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
            raise PublicResearchArchiveError(f"Recorded research file not found: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        sources = payload.get("sources") if isinstance(payload, dict) else payload
        if not isinstance(sources, list):
            raise PublicResearchArchiveError("Recorded research file must contain a sources array")
        return [item for item in sources if isinstance(item, dict)]


    def _load_connector(self, connector_file: str | Path, planned_queries: list[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        path = Path(connector_file)
        if not path.exists():
            raise PublicResearchArchiveError(f"Connector research file not found: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise PublicResearchArchiveError("Connector research file must be a JSON object")
        responses = payload.get("responses")
        if not isinstance(responses, list):
            raise PublicResearchArchiveError("Connector research file must contain a responses array")
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
            raise PublicResearchArchiveError(f"Connector responses do not cover planned queries: {missing}")
        if not candidates:
            raise PublicResearchArchiveError("Connector research file contains no result records")
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

    def _search_searxng(self, queries: list[str], max_results: int) -> list[dict[str, Any]]:
        if not self.settings.public_search_base_url:
            raise PublicResearchArchiveError("PUBLIC_SEARCH_BASE_URL is empty")
        candidates: list[dict[str, Any]] = []
        with httpx.Client(timeout=self.settings.research_fetch_timeout_seconds, follow_redirects=True) as client:
            for query in queries:
                response = client.get(
                    f"{self.settings.public_search_base_url}/search",
                    params={"q": query, "format": "json", "language": "zh-CN", "safesearch": 1},
                )
                response.raise_for_status()
                payload = response.json()
                for item in payload.get("results", [])[: min(10, max_results)]:
                    candidates.append(
                        {
                            "title": str(item.get("title") or "").strip(),
                            "url": str(item.get("url") or "").strip(),
                            "excerpt": str(item.get("content") or item.get("snippet") or "").strip(),
                            "matched_query": query,
                            "engine": item.get("engine"),
                        }
                    )
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
        text_path.write_text(body_text, encoding="utf-8")
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
                        raise PublicResearchArchiveError(f"Source exceeds {limit} bytes")
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
            raise PublicResearchArchiveError("Only public HTTP(S) URLs are allowed")
        host = parsed.hostname.lower()
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            raise PublicResearchArchiveError("Local addresses are prohibited")
        try:
            ip = ipaddress.ip_address(host)
            addresses = [ip]
        except ValueError:
            addresses = []
            if resolve_dns:
                try:
                    addresses = [ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, None)]
                except socket.gaierror as exc:
                    raise PublicResearchArchiveError(f"DNS resolution failed for {host}") from exc
        for ip in addresses:
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                raise PublicResearchArchiveError(f"Private/reserved address is prohibited: {ip}")

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
