from __future__ import annotations

import csv
import hashlib
import ipaddress
import json
import mimetypes
import re
import socket
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader

from .base import SkillContext, SkillResult
from ..util import new_id, safe_filename, sha256_bytes, sha256_text, utc_now, write_json


class PublicResearchArchiveError(RuntimeError):
    pass


TYPE_PRIORITY = {
    "OFFICIAL_STANDARD": 0, "GOVERNMENT": 1, "PEER_REVIEWED_PAPER": 2,
    "PREPRINT": 3, "OFFICIAL_PROJECT_PAGE": 4, "REPOSITORY": 5,
    "INSTITUTIONAL_PAGE": 6, "NEWS": 7, "OTHER": 8,
}
TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src", "source"}
INTENT_TERMS = {
    "RECENT_WORK": ("recent", "latest", "state of the art", "近年", "近期", "最新", "前沿", "现状"),
    "LIMITATION_MECHANISM": ("limitation", "weakness", "challenge", "gap", "局限", "不足", "挑战", "差距", "瓶颈"),
    "COMPARABLE_BASELINE": ("baseline", "benchmark", "comparison", "state-of-the-art", "基线", "对比", "比较", "基准"),
    "INNOVATION": ("innovation", "novel", "contribution", "创新", "新颖", "贡献"),
}


class PublicResearchArchiveSkill:
    skill_id = "public_research.archive"
    version = "2.0.0"
    description = "Plan-driven public research with snapshots, normalized sources, evidence ledgers, and claim binding."

    def __init__(self, settings):
        self.settings = settings

    def run(self, payload: dict[str, Any], context: SkillContext) -> SkillResult:
        provider = str(payload.get("provider") or self.settings.public_search_provider).lower()
        plan = self._validate_plan(payload.get("plan") or {})
        queries = plan["queries"]
        limit = max(1, min(int(payload.get("max_results") or self.settings.public_search_max_results), 100))
        session_id = new_id("research")
        root = Path(context.data_dir) / "research_archive" / safe_filename(context.project_id) / session_id
        raw_dir, text_dir, meta_dir, connector_dir = (root / x for x in ("raw", "text", "metadata", "connector"))
        for directory in (raw_dir, text_dir, meta_dir, connector_dir):
            directory.mkdir(parents=True, exist_ok=True)

        capture: dict[str, Any] | None = None
        if provider == "recorded":
            candidates = self._load_recorded(payload.get("record_file") or self.settings.public_research_record_file)
            mode = "RECORDED_VERIFIED_SOURCE_SET"
        elif provider == "connector":
            candidates, capture = self._load_connector(
                payload.get("connector_file") or self.settings.public_research_connector_file, queries
            )
            mode = "LIVE_CONNECTOR_ARCHIVE"
        elif provider == "searxng":
            candidates, capture = self._search_searxng(queries, limit)
            mode = "LIVE_SEARXNG"
        else:
            raise PublicResearchArchiveError(f"Unsupported PUBLIC_SEARCH_PROVIDER: {provider}")

        capture_path = None
        if capture is not None:
            capture_path = connector_dir / ("connector_response.json" if provider == "connector" else "searxng_response.json")
            write_json(capture_path, capture)

        records: list[dict[str, Any]] = []
        issues: list[dict[str, Any]] = []
        by_url: dict[str, dict[str, Any]] = {}
        by_content: dict[str, dict[str, Any]] = {}
        for candidate in sorted(candidates, key=self._candidate_sort_key):
            if len(records) >= limit:
                break
            url = str(candidate.get("url") or "").strip()
            if not url:
                issues.append(self._issue("SOURCE_URL_MISSING", "BLOCKING", "检索结果缺少 URL。", query=candidate.get("matched_query")))
                continue
            canonical = self._canonical_url(url)
            if canonical in by_url:
                self._merge_query(by_url[canonical], candidate)
                issues.append(self._issue("DUPLICATE_CANONICAL_URL", "INFO", "规范化 URL 重复，已合并查询命中。", url=url, duplicate_of=by_url[canonical]["source_id"]))
                continue
            try:
                record = self._archive_candidate(candidate, raw_dir, text_dir, meta_dir, provider, canonical)
            except Exception as exc:
                issues.append(self._issue("SOURCE_ARCHIVE_FAILED", "WARNING", str(exc), url=url, query=candidate.get("matched_query")))
                continue
            content_key = record["text_sha256"] if record["text_length"] else record["snapshot_sha256"]
            if content_key in by_content:
                self._merge_query(by_content[content_key], candidate)
                issues.append(self._issue("DUPLICATE_CONTENT", "INFO", "来源正文哈希重复，已合并查询命中。", url=url, duplicate_of=by_content[content_key]["source_id"]))
                continue
            by_url[canonical] = record
            by_content[content_key] = record
            records.append(record)

        if not records:
            write_json(root / "issues.json", {"schema_version": "1.0", "issues": issues})
            raise PublicResearchArchiveError("No public source could be archived")
        records.sort(key=self._record_sort_key)

        query_coverage = self._query_coverage(queries, records)
        for item in query_coverage:
            if not item["source_ids"]:
                issues.append(self._issue("QUERY_WITHOUT_SOURCE", "BLOCKING", "计划查询没有形成可核验来源。", query=item["query"]))
        evidence_coverage = self._evidence_coverage(plan, records)
        for item in evidence_coverage:
            if item["required"] and not item["source_ids"]:
                issues.append(self._issue("EVIDENCE_REQUIREMENT_UNMET", "BLOCKING", "证据要求没有对应来源。", evidence_requirement=item["requirement"], intent=item["intent"]))

        sources, passages, ledger = self._build_evidence(records, queries)
        issue_path, ledger_path = root / "issues.json", root / "evidence_ledger.json"
        write_json(issue_path, {"schema_version": "1.0", "issues": issues})
        write_json(ledger_path, {
            "schema_version": "1.0", "session_id": session_id,
            "claim_binding_contract": {
                "claim_type": "PUBLIC_CLAIM",
                "allowed_evidence_modes": ["DIRECT_QUOTE", "SOURCE_SUMMARY", "MULTI_SOURCE_SYNTHESIS"],
                "requirements": ["source_id 必须来自本次归档", "source_hash 必须匹配快照", "原文、摘要和综合必须区分"],
            },
            "entries": ledger,
        })
        blocking = [item for item in issues if item["severity"] == "BLOCKING"]
        manifest = {
            "schema_version": "2.0", "session_id": session_id, "project_id": context.project_id,
            "workflow_id": context.workflow_id, "retrieval_mode": mode, "provider": provider,
            "created_at": utc_now(), "plan": plan, "queries": queries,
            "query_coverage": query_coverage, "evidence_coverage": evidence_coverage,
            "source_count": len(records), "issue_count": len(issues), "blocking_issue_count": len(blocking),
            "issues": issues, "records": records,
            "retrieval_capture": str(capture_path) if capture_path else None,
            "evidence_ledger": str(ledger_path),
        }
        manifest_path = root / "manifest.json"
        write_json(manifest_path, manifest)
        self._write_csv(root / "source_index.csv", records)
        artifacts = [str(manifest_path), str(root / "source_index.csv"), str(issue_path), str(ledger_path)]
        if capture_path:
            artifacts.append(str(capture_path))
        output = {
            "sources": sources, "passages": passages, "queries": queries,
            "query_coverage": query_coverage, "evidence_coverage": evidence_coverage,
            "mode": mode, "archive_session_id": session_id, "archive_root": str(root),
            "archive_manifest": str(manifest_path), "source_index": str(root / "source_index.csv"),
            "evidence_ledger": str(ledger_path), "issues": issues, "blocking_issues": blocking,
        }
        warnings = [item["message"] for item in issues if item["severity"] == "WARNING"]
        return SkillResult(status="BLOCK" if blocking else "PASS", output=output, warnings=warnings, artifacts=artifacts)

    def _validate_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(plan, dict):
            raise PublicResearchArchiveError("Research plan must be an object")
        questions = self._string_list(plan.get("research_questions"))
        queries = self._queries(plan)
        priorities = self._string_list(plan.get("source_priorities"))
        requirements = self._string_list(plan.get("evidence_requirements"))
        time_scope = str(plan.get("time_scope") or "").strip()
        if not questions or not queries or not priorities:
            raise PublicResearchArchiveError("Research plan requires questions, queries, and source priorities")
        if str(plan.get("task_type") or "PUBLIC_RESEARCH") == "PUBLIC_RESEARCH" and not time_scope:
            raise PublicResearchArchiveError("PUBLIC_RESEARCH requires a non-empty time_scope")
        broad = [query for query in queries if self._is_broad_query(query)]
        if broad:
            raise PublicResearchArchiveError(f"Research queries are too broad: {broad}")
        links = []
        for query in queries:
            scores = [(self._term_overlap(query, question), question) for question in questions]
            score, question = max(scores, default=(0.0, ""))
            links.append({"query": query, "research_question": question, "overlap": round(score, 4)})
        normalized = dict(plan)
        normalized.update({
            "research_questions": questions, "queries": queries, "source_priorities": priorities,
            "time_scope": time_scope, "evidence_requirements": requirements,
            "prohibited_inferences": self._string_list(plan.get("prohibited_inferences")),
            "query_question_links": links,
        })
        return normalized

    @staticmethod
    def _queries(plan: dict[str, Any]) -> list[str]:
        result = []
        for item in plan.get("queries", []):
            value = item if isinstance(item, str) else (item.get("query") or item.get("query_text") or item.get("text") or "") if isinstance(item, dict) else ""
            value = str(value).strip()
            if value and value not in result:
                result.append(value)
        return result[:12]

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        result = []
        for item in value:
            text = str(item).strip()
            if text and text not in result:
                result.append(text)
        return result

    @staticmethod
    def _is_broad_query(query: str) -> bool:
        compact = " ".join(query.split())
        tokens = re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]{2,}", compact)
        return len(compact) < 4 or len(tokens) < 2

    @classmethod
    def _term_overlap(cls, left: str, right: str) -> float:
        a, b = cls._terms(left), cls._terms(right)
        return len(a & b) / max(1, len(a | b))

    @staticmethod
    def _terms(text: str) -> set[str]:
        return {x.lower() for x in re.findall(r"[A-Za-z0-9]{2,}|[\u4e00-\u9fff]{2,}", text)}

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
        if not isinstance(payload, dict) or not isinstance(payload.get("responses"), list):
            raise PublicResearchArchiveError("Connector file must be an object with responses")
        connector_queries, candidates = [], []
        for response in payload["responses"]:
            if not isinstance(response, dict):
                continue
            query = str(response.get("query") or "").strip()
            if query:
                connector_queries.append(query)
            for item in response.get("results") or []:
                if not isinstance(item, dict):
                    continue
                candidate = dict(item)
                candidate.setdefault("matched_query", query)
                candidate.setdefault("retrieved_at", response.get("retrieved_at") or payload.get("created_at") or utc_now())
                candidate["verification"] = {
                    **(candidate.get("verification") or {}), "connector_run_id": payload.get("run_id"),
                    "connector": payload.get("connector"), "query": query,
                    "status": (candidate.get("verification") or {}).get("status") or "CONNECTOR_RETURNED",
                }
                candidates.append(candidate)
        missing = [query for query in planned_queries if query not in connector_queries]
        if missing:
            raise PublicResearchArchiveError(f"Connector responses do not cover planned queries: {missing}")
        if not candidates:
            raise PublicResearchArchiveError("Connector research file contains no result records")
        capture = {**payload, "ingested_at": utc_now(), "planned_queries": planned_queries,
                   "connector_queries": connector_queries, "result_count": len(candidates),
                   "source_file": str(path), "source_file_sha256": sha256_bytes(path.read_bytes())}
        return candidates, capture

    def _search_searxng(self, queries: list[str], max_results: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if not self.settings.public_search_base_url:
            raise PublicResearchArchiveError("PUBLIC_SEARCH_BASE_URL is empty")
        candidates, responses = [], []
        with httpx.Client(timeout=self.settings.research_fetch_timeout_seconds, follow_redirects=True) as client:
            for query in queries:
                retrieved_at = utc_now()
                response = client.get(f"{self.settings.public_search_base_url}/search", params={"q": query, "format": "json", "language": "zh-CN", "safesearch": 1})
                response.raise_for_status()
                payload = response.json()
                responses.append({"query": query, "retrieved_at": retrieved_at, "url": str(response.url),
                                  "status_code": response.status_code, "response_sha256": sha256_bytes(response.content),
                                  "response": payload})
                for item in payload.get("results", [])[: min(10, max_results)]:
                    candidates.append({"title": str(item.get("title") or "").strip(), "url": str(item.get("url") or "").strip(),
                                       "excerpt": str(item.get("content") or item.get("snippet") or "").strip(),
                                       "matched_query": query, "retrieved_at": retrieved_at, "engine": item.get("engine")})
        return candidates, {"provider": "searxng", "created_at": utc_now(), "planned_queries": queries, "responses": responses}

    def _archive_candidate(self, candidate: dict[str, Any], raw_dir: Path, text_dir: Path, meta_dir: Path, provider: str, canonical_url: str) -> dict[str, Any]:
        url = str(candidate.get("url") or "").strip()
        self._validate_public_url(url, resolve_dns=provider == "searxng")
        source_id = str(candidate.get("source_id") or self._source_id(canonical_url))
        title = str(candidate.get("title") or url).strip()
        retrieved_at = str(candidate.get("retrieved_at") or utc_now())
        matched_query = str(candidate.get("matched_query") or "").strip()
        if provider in {"recorded", "connector"}:
            body_text = str(candidate.get("content_text") or candidate.get("page_text") or candidate.get("excerpt") or candidate.get("abstract") or "").strip()
            raw_payload = {"title": title, "url": url, "retrieved_at": retrieved_at,
                           "published_at": candidate.get("published_at"), "authors": candidate.get("authors") or [],
                           "publisher": candidate.get("publisher"), "doi": candidate.get("doi"),
                           "content_text": body_text, "verification": candidate.get("verification") or {},
                           "raw_connector_result": candidate.get("raw_connector_result") or candidate.get("raw_result")}
            raw_bytes, suffix, content_type, final_url, http_status = (
                json.dumps(raw_payload, ensure_ascii=False, indent=2).encode("utf-8"), ".json", "application/json", url, None
            )
        else:
            raw_bytes, content_type, final_url, http_status = self._fetch_url(url)
            body_text = self._extract_text(raw_bytes, content_type, final_url) or str(candidate.get("excerpt") or title)
            suffix = self._suffix(content_type, final_url)
        excerpt = self._compact_text(body_text)[:12000] or title
        raw_path, text_path, meta_path = raw_dir / f"{safe_filename(source_id)}{suffix}", text_dir / f"{safe_filename(source_id)}.txt", meta_dir / f"{safe_filename(source_id)}.json"
        raw_path.write_bytes(raw_bytes)
        text_path.write_text(body_text, encoding="utf-8")
        parsed = urlparse(final_url)
        source_type = self._source_type(parsed.netloc, final_url, candidate)
        record = {
            "source_id": source_id, "title": title, "url": url, "canonical_url": canonical_url,
            "final_url": final_url, "domain": parsed.netloc.lower(), "source_type": source_type,
            "published_at": candidate.get("published_at"), "authors": candidate.get("authors") or [],
            "publisher": candidate.get("publisher"), "doi": candidate.get("doi"), "retrieved_at": retrieved_at,
            "matched_query": matched_query, "matched_queries": [matched_query] if matched_query else [],
            "retrieval_provider": provider, "http_status": http_status, "content_type": content_type,
            "raw_path": str(raw_path), "text_path": str(text_path), "metadata_path": str(meta_path),
            "snapshot_sha256": sha256_bytes(raw_bytes), "text_sha256": sha256_text(body_text),
            "byte_size": len(raw_bytes), "text_length": len(body_text), "excerpt": excerpt,
            "authority_rank": self._authority_rank(parsed.netloc, candidate, source_type),
            "verification": candidate.get("verification") or {},
        }
        write_json(meta_path, record)
        return record

    def _fetch_url(self, url: str) -> tuple[bytes, str, str, int]:
        headers = {"User-Agent": "ProposalAgentResearchArchiver/2.0", "Accept": "text/html,application/pdf,text/plain,*/*;q=0.1"}
        limit = int(self.settings.research_max_source_bytes)
        with httpx.Client(timeout=self.settings.research_fetch_timeout_seconds, follow_redirects=True, headers=headers) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                final_url = str(response.url)
                self._validate_public_url(final_url, resolve_dns=True)
                chunks, total = [], 0
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
            return "\n\n".join((page.extract_text() or "") for page in PdfReader(BytesIO(raw)).pages[:200])
        if content_type.startswith("text/plain"):
            return raw.decode("utf-8", errors="replace")
        soup = BeautifulSoup(raw.decode("utf-8", errors="replace"), "html.parser")
        for node in soup(["script", "style", "noscript", "svg", "nav", "footer", "header"]):
            node.decompose()
        return (soup.find("main") or soup.find("article") or soup.body or soup).get_text("\n", strip=True)

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
    def _canonical_url(url: str) -> str:
        parsed = urlparse(url.strip())
        scheme, host = parsed.scheme.lower(), (parsed.hostname or "").lower()
        if not scheme or not host:
            return url.strip()
        port = parsed.port
        netloc = host if port is None or (scheme == "http" and port == 80) or (scheme == "https" and port == 443) else f"{host}:{port}"
        path = re.sub(r"/{2,}", "/", parsed.path or "/")
        if path != "/":
            path = path.rstrip("/")
        query = urlencode(sorted((k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() not in TRACKING_KEYS and not k.lower().startswith("utm_")), doseq=True)
        return urlunparse((scheme, netloc, path, "", query, ""))

    @staticmethod
    def _source_id(canonical_url: str) -> str:
        return "public-src-" + hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:20]

    @staticmethod
    def _validate_public_url(url: str, *, resolve_dns: bool) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise PublicResearchArchiveError("Only public HTTP(S) URLs are allowed")
        host = parsed.hostname.lower()
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            raise PublicResearchArchiveError("Local addresses are prohibited")
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            addresses = []
            if resolve_dns:
                try:
                    addresses = [ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, None)]
                except socket.gaierror as exc:
                    raise PublicResearchArchiveError(f"DNS resolution failed for {host}") from exc
        for address in addresses:
            if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_multicast:
                raise PublicResearchArchiveError(f"Private/reserved address is prohibited: {address}")

    @classmethod
    def _source_type(cls, domain: str, url: str, candidate: dict[str, Any]) -> str:
        explicit = str(candidate.get("source_type") or "").upper()
        if explicit in TYPE_PRIORITY:
            return explicit
        d, u = domain.lower(), url.lower()
        if d.endswith(".gov") or d.endswith(".gov.cn"):
            return "GOVERNMENT"
        if any(x in d for x in ("rfc-editor.org", "iso.org", "ietf.org", "w3.org", "standards.")):
            return "OFFICIAL_STANDARD"
        if "arxiv.org" in d or "biorxiv.org" in d or "ssrn.com" in d:
            return "PREPRINT"
        if candidate.get("doi") or any(x in d for x in ("ieeexplore.ieee.org", "dl.acm.org", "springer.com", "sciencedirect.com", "nature.com")):
            return "PEER_REVIEWED_PAPER"
        if "github.com" in d or "gitlab.com" in d:
            return "REPOSITORY"
        if d.endswith(".edu") or d.endswith(".edu.cn") or "ac.cn" in d:
            return "INSTITUTIONAL_PAGE"
        if any(x in u for x in ("/project/", "/projects/", "/product/", "/docs/")):
            return "OFFICIAL_PROJECT_PAGE"
        if any(x in d for x in ("news", "reuters.com", "apnews.com")):
            return "NEWS"
        return "OTHER"

    @staticmethod
    def _authority_rank(domain: str, candidate: dict[str, Any], source_type: str) -> int:
        if candidate.get("authority_rank") is not None:
            return int(candidate["authority_rank"])
        return {"OFFICIAL_STANDARD": 100, "GOVERNMENT": 95, "PEER_REVIEWED_PAPER": 90, "PREPRINT": 80,
                "OFFICIAL_PROJECT_PAGE": 75, "REPOSITORY": 70, "INSTITUTIONAL_PAGE": 68, "NEWS": 50, "OTHER": 40}[source_type]

    @classmethod
    def _candidate_sort_key(cls, candidate: dict[str, Any]) -> tuple[int, int, str]:
        parsed = urlparse(str(candidate.get("url") or ""))
        source_type = cls._source_type(parsed.netloc, str(candidate.get("url") or ""), candidate)
        return TYPE_PRIORITY[source_type], -cls._published_sort_value(candidate.get("published_at")), str(candidate.get("url") or "")

    @staticmethod
    def _published_sort_value(value: Any) -> int:
        digits = re.sub(r"\D", "", str(value or ""))[:8]
        try:
            return int(digits.ljust(8, "0"))
        except ValueError:
            return 0

    @classmethod
    def _record_sort_key(cls, record: dict[str, Any]) -> tuple[int, int, int, str]:
        return TYPE_PRIORITY.get(str(record.get("source_type")), 99), -int(record.get("authority_rank") or 0), -cls._published_sort_value(record.get("published_at")), str(record.get("canonical_url") or "")

    @staticmethod
    def _issue(code: str, severity: str, message: str, **details: Any) -> dict[str, Any]:
        return {"issue_id": new_id("research-issue"), "code": code, "severity": severity, "message": message, "details": details, "recorded_at": utc_now()}

    @staticmethod
    def _merge_query(record: dict[str, Any], candidate: dict[str, Any]) -> None:
        query = str(candidate.get("matched_query") or "").strip()
        if query and query not in record["matched_queries"]:
            record["matched_queries"].append(query)
            write_json(Path(record["metadata_path"]), record)

    @staticmethod
    def _query_coverage(queries: list[str], records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"query": query, "source_ids": [r["source_id"] for r in records if query in (r.get("matched_queries") or [])]} for query in queries]

    @classmethod
    def _intent(cls, text: str) -> str:
        lowered = text.lower()
        for intent, terms in INTENT_TERMS.items():
            if any(term in lowered for term in terms):
                return intent
        return "OTHER"

    @classmethod
    def _evidence_coverage(cls, plan: dict[str, Any], records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        requirements = list(plan.get("evidence_requirements") or [])
        all_plan_text = " ".join([*plan.get("research_questions", []), *requirements])
        if cls._intent(all_plan_text) == "INNOVATION" or any(term in all_plan_text.lower() for term in INTENT_TERMS["INNOVATION"]):
            for requirement in ("recent work", "limitation mechanism", "comparable baseline"):
                if requirement not in requirements:
                    requirements.append(requirement)
        result = []
        for requirement in requirements:
            intent = cls._intent(requirement)
            terms = INTENT_TERMS.get(intent, (requirement.lower(),))
            source_ids = []
            for record in records:
                haystack = f"{record.get('title', '')} {record.get('excerpt', '')}".lower()
                if any(term in haystack for term in terms):
                    source_ids.append(record["source_id"])
            result.append({"requirement": requirement, "intent": intent, "required": True, "source_ids": source_ids})
        return result

    @staticmethod
    def _build_evidence(records: list[dict[str, Any]], queries: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        sources, passages, ledger = [], [], []
        for record in records:
            source_ref = {"source_id": record["source_id"], "source_type": "PUBLIC_SOURCE", "document_version_id": None,
                          "section_id": None, "span_start": None, "span_end": None,
                          "quoted_text": f"{record['title']} | {record['url']}", "source_hash": record["snapshot_sha256"],
                          "authority_rank": record["authority_rank"], "security_level": "PUBLIC"}
            passage_id = "passage-" + record["source_id"].removeprefix("public-src-")
            passage_text = record["excerpt"][:6000]
            passage = {"passage_id": passage_id, "source_ref": source_ref, "text": passage_text,
                       "relevance": record.get("matched_query") or (queries[0] if queries else "公开资料检索")}
            sources.append(source_ref)
            passages.append(passage)
            ledger.append({"evidence_id": "evidence-" + record["source_id"].removeprefix("public-src-"),
                           "source_id": record["source_id"], "passage_id": passage_id,
                           "source_hash": record["snapshot_sha256"], "text_hash": record["text_sha256"],
                           "passage_hash": sha256_text(passage_text), "source_type": record["source_type"],
                           "authority_rank": record["authority_rank"], "matched_queries": record.get("matched_queries") or [],
                           "evidence_kind": "SOURCE_EXCERPT"})
        return sources, passages, ledger

    @staticmethod
    def validate_claim_bindings(synthesis: dict[str, Any], research_output: dict[str, Any]) -> dict[str, Any]:
        claims = synthesis.get("claims") or []
        source_by_id = {str(x.get("source_id")): x for x in research_output.get("sources") or [] if isinstance(x, dict) and x.get("source_id")}
        passages: dict[str, list[dict[str, Any]]] = {}
        for passage in research_output.get("passages") or []:
            source_id = str((passage.get("source_ref") or {}).get("source_id") or "") if isinstance(passage, dict) else ""
            if source_id:
                passages.setdefault(source_id, []).append(passage)
        bindings, findings = [], []
        for claim in claims:
            if not isinstance(claim, dict):
                findings.append({"code": "INVALID_CLAIM_OBJECT", "claim_id": None, "message": "公开研究综合包含非对象命题。"})
                continue
            claim_id, text = str(claim.get("claim_id") or ""), str(claim.get("claim_text") or "").strip()
            if str(claim.get("claim_type") or "PUBLIC_CLAIM") != "PUBLIC_CLAIM":
                findings.append({"code": "NON_PUBLIC_CLAIM_IN_RESEARCH_SYNTHESIS", "claim_id": claim_id, "message": "公开研究综合只能产生 PUBLIC_CLAIM。"})
            valid = []
            for ref in [x for x in claim.get("source_refs", []) if isinstance(x, dict)]:
                source_id, archived = str(ref.get("source_id") or ""), source_by_id.get(str(ref.get("source_id") or ""))
                if archived is None:
                    findings.append({"code": "UNKNOWN_SOURCE_REF", "claim_id": claim_id, "source_id": source_id, "message": "引用的 source_id 不在本次归档。"})
                elif str(ref.get("source_hash") or "") != str(archived.get("source_hash") or ""):
                    findings.append({"code": "SOURCE_HASH_MISMATCH", "claim_id": claim_id, "source_id": source_id, "message": "source_hash 与归档快照不一致。"})
                else:
                    valid.append(ref)
            if not valid:
                findings.append({"code": "UNBOUND_PUBLIC_CLAIM", "claim_id": claim_id, "message": "PUBLIC_CLAIM 未绑定有效归档来源。"})
                continue
            source_ids = sorted({str(x.get("source_id")) for x in valid})
            direct = any(len(text) >= 20 and text in str(p.get("text") or "") for sid in source_ids for p in passages.get(sid, []))
            mode = "DIRECT_QUOTE" if direct else "SOURCE_SUMMARY" if len(source_ids) == 1 else "MULTI_SOURCE_SYNTHESIS"
            bindings.append({"claim_id": claim_id, "claim_hash": sha256_text(text), "source_ids": source_ids, "evidence_mode": mode})
        conflicts = [{"topic": x.get("topic"), "source_ids": x.get("source_ids") or [], "summary": x.get("summary")}
                     for x in synthesis.get("source_comparisons") or [] if isinstance(x, dict) and x.get("agreement") == "CONFLICT"]
        conflicts.extend({"topic": "UNSTRUCTURED_CONFLICT", "summary": str(x)} for x in synthesis.get("conflicts") or [])
        return {"schema_version": "1.0", "status": "PASS" if not findings else "BLOCK", "validated_at": utc_now(),
                "claim_count": len(claims), "binding_count": len(bindings), "bindings": bindings, "findings": findings, "conflicts": conflicts}

    @staticmethod
    def verify_archive(archive_root: str | Path) -> dict[str, Any]:
        root = Path(archive_root)
        manifest_path = root / "manifest.json"
        if not manifest_path.exists():
            raise PublicResearchArchiveError(f"Archive manifest not found: {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        findings, source_ids, urls = [], set(), set()
        for record in manifest.get("records", []):
            source_id, url = str(record.get("source_id") or ""), str(record.get("canonical_url") or "")
            if not source_id or source_id in source_ids:
                findings.append({"code": "DUPLICATE_OR_EMPTY_SOURCE_ID", "source_id": source_id})
            if not url or url in urls:
                findings.append({"code": "DUPLICATE_OR_EMPTY_CANONICAL_URL", "canonical_url": url})
            source_ids.add(source_id); urls.add(url)
            for path_key, hash_key, binary in (("raw_path", "snapshot_sha256", True), ("text_path", "text_sha256", False)):
                path = Path(str(record.get(path_key) or ""))
                if not path.exists():
                    findings.append({"code": "ARCHIVE_FILE_MISSING", "source_id": source_id, "path": str(path)})
                    continue
                actual = sha256_bytes(path.read_bytes()) if binary else sha256_text(path.read_text(encoding="utf-8"))
                if actual != str(record.get(hash_key) or ""):
                    findings.append({"code": "ARCHIVE_HASH_MISMATCH", "source_id": source_id, "path": str(path), "expected": record.get(hash_key), "actual": actual})
        for coverage in manifest.get("query_coverage", []):
            if not coverage.get("source_ids"):
                findings.append({"code": "QUERY_WITHOUT_SOURCE", "query": coverage.get("query")})
        binding_path = root / "claim_bindings.json"
        if binding_path.exists() and json.loads(binding_path.read_text(encoding="utf-8")).get("status") != "PASS":
            findings.append({"code": "CLAIM_BINDING_REPORT_BLOCKED"})
        return {"schema_version": "1.0", "archive_root": str(root), "verified_at": utc_now(),
                "source_count": len(manifest.get("records", [])), "status": "PASS" if not findings else "BLOCK", "findings": findings}

    @staticmethod
    def _write_csv(path: Path, records: list[dict[str, Any]]) -> None:
        fields = ["source_id", "title", "url", "canonical_url", "final_url", "domain", "source_type", "published_at",
                  "publisher", "doi", "retrieved_at", "matched_query", "matched_queries", "retrieval_provider", "http_status",
                  "content_type", "snapshot_sha256", "text_sha256", "byte_size", "text_length", "authority_rank", "raw_path",
                  "text_path", "metadata_path"]
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader(); writer.writerows(records)
