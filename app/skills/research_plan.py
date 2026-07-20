from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

_GENERIC_QUERY_TERMS = {
    "research", "paper", "papers", "study", "studies", "information", "latest", "recent",
    "资料", "研究", "论文", "文献", "最新", "相关", "情况", "现状",
}
_TRACKING_QUERY_PREFIXES = ("utm_", "spm", "from", "source", "ref")


def unique_texts(values: Any) -> list[str]:
    result: list[str] = []
    for item in values or []:
        value = str(item or "").strip()
        if value and value not in result:
            result.append(value)
    return result


def tokens(text: str) -> set[str]:
    lowered = str(text or "").lower()
    latin = {token for token in re.findall(r"[a-z0-9][a-z0-9_-]+", lowered) if len(token) > 1}
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", lowered))
    grams = {chinese[index:index + 2] for index in range(max(0, len(chinese) - 1))}
    # G3 plans may intentionally use Chinese research questions and English
    # executable database queries. Add narrow domain aliases so traceability
    # validation measures semantic binding instead of same-language spelling.
    aliases: set[str] = set()
    bilingual = {
        "需求": {"requirements", "requirement"},
        "追溯": {"traceability", "trace", "tracing"},
        "追踪": {"traceability", "trace", "tracing"},
        "源代码": {"source", "code"},
        "检索": {"retrieval", "search"},
        "链接": {"link", "links", "recovery"},
        "变更": {"change", "changes", "commit"},
        "影响": {"impact", "propagation"},
        "制品": {"artifact", "artifacts"},
        "依赖": {"dependency", "dependencies", "graph"},
        "图": {"graph", "network"},
        "测试": {"test", "testing", "regression"},
        "选择": {"selection", "select"},
        "优先": {"prioritization", "prioritize", "apfd"},
        "缺陷": {"defect", "bug", "bugs", "fault"},
        "风险": {"risk", "prediction"},
        "预测": {"prediction", "predict"},
        "数据集": {"dataset", "datasets", "benchmark"},
        "基准": {"benchmark", "benchmarks"},
        "跨项目": {"cross-project", "transfer"},
        "迁移": {"transfer", "domain", "adaptation"},
        "语义": {"semantic", "language"},
        "历史": {"historical", "history", "co-change"},
        "共变": {"co-change", "logical", "coupling"},
    }
    for phrase, values in bilingual.items():
        if phrase in lowered:
            aliases.update(values)
    return latin | grams | aliases


def canonical_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if not scheme or not host:
        return str(url or "").strip()
    try:
        port = parsed.port
    except ValueError:
        return str(url or "").strip()
    netloc = host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower().startswith(_TRACKING_QUERY_PREFIXES):
            continue
        query.append((key, value))
    return urlunparse((scheme, netloc, path, "", urlencode(sorted(query)), ""))


def normalize_doi(value: Any, url: str = "") -> str | None:
    raw = str(value or "").strip().lower()
    if not raw and "doi.org/" in str(url).lower():
        raw = str(url).lower().split("doi.org/", 1)[1]
    raw = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", raw)
    raw = re.sub(r"^doi:\s*", "", raw).strip().rstrip(".,;)")
    return raw or None


def parse_year(value: Any) -> int | None:
    match = re.search(r"(?:19|20)\d{2}", str(value or ""))
    return int(match.group(0)) if match else None


def title_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value or "").lower())


def candidate_identity(candidate: dict[str, Any]) -> tuple[str, str]:
    url = str(candidate.get("url") or "").strip()
    doi = normalize_doi(candidate.get("doi"), url)
    if doi:
        return "doi", doi
    canonical = canonical_url(url)
    if canonical:
        return "url", canonical
    return "title", f"{title_key(candidate.get('title'))}|{title_key(candidate.get('publisher'))}"


def deduplicate_candidates(candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kept: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], int] = {}
    for candidate in candidates:
        key = candidate_identity(candidate)
        if key[1] and key in seen:
            original = kept[seen[key]]
            conflict_fields: list[str] = []
            if key[0] == "doi":
                first_title = title_key(original.get("title"))
                second_title = title_key(candidate.get("title"))
                if first_title and second_title and first_title != second_title:
                    conflict_fields.append("title")
                first_year = parse_year(original.get("published_at"))
                second_year = parse_year(candidate.get("published_at"))
                if first_year and second_year and first_year != second_year:
                    conflict_fields.append("published_year")
            duplicates.append({
                "reason": f"DUPLICATE_{key[0].upper()}",
                "identity": key[1],
                "kept_url": original.get("url"),
                "duplicate_url": candidate.get("url"),
                "conflict_fields": conflict_fields,
            })
            continue
        seen[key] = len(kept)
        kept.append(candidate)
    return kept, duplicates


def _query_question_score(query: str, question: str) -> float:
    query_tokens = tokens(query)
    question_tokens = tokens(question)
    if not query_tokens or not question_tokens:
        return 0.0
    return len(query_tokens & question_tokens) / max(1, min(len(query_tokens), len(question_tokens)))


def normalize_and_validate_plan(plan: dict[str, Any], *, strict: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(plan, dict):
        raise ValueError("Research plan must be a JSON object")
    questions = unique_texts(plan.get("research_questions"))
    priorities = unique_texts(plan.get("source_priorities"))
    evidence_requirements = unique_texts(plan.get("evidence_requirements"))
    prohibited_inferences = unique_texts(plan.get("prohibited_inferences"))
    time_scope = plan.get("time_scope")
    queries: list[str] = []
    query_items: list[dict[str, Any]] = []
    for index, item in enumerate(plan.get("queries") or []):
        query = item if isinstance(item, str) else (
            item.get("query") or item.get("query_text") or item.get("text") or ""
            if isinstance(item, dict) else ""
        )
        query = str(query).strip()
        if not query or query in queries:
            continue
        queries.append(query)
        linked = [
            question_index for question_index, question in enumerate(questions)
            if _query_question_score(query, question) >= 0.12
        ][:3]
        query_items.append({
            "query_id": f"query-{index + 1}",
            "query": query,
            "linked_question_indexes": linked,
            "token_count": len(tokens(query)),
        })

    findings: list[dict[str, Any]] = []
    warnings: list[str] = []
    if not queries:
        findings.append({"code": "RESEARCH_PLAN_NO_QUERY", "severity": "P0", "message": "No executable query."})
    if not questions:
        if strict:
            findings.append({"code": "RESEARCH_PLAN_NO_QUESTION", "severity": "P1", "message": "Research questions are required."})
        else:
            warnings.append("Research questions are absent; query traceability is unavailable.")
    if strict and not priorities:
        findings.append({"code": "RESEARCH_PLAN_NO_SOURCE_PRIORITY", "severity": "P1", "message": "Source priorities are required."})
    if strict and not str(time_scope or "").strip():
        findings.append({"code": "RESEARCH_PLAN_NO_TIME_SCOPE", "severity": "P1", "message": "A time scope is required."})
    for item in query_items:
        query = item["query"]
        words = {word for word in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2,}", query.lower()) if word}
        broad = len(query) < 8 or (len(words - _GENERIC_QUERY_TERMS) <= 1 and item["token_count"] <= 2)
        if broad:
            issue = {"code": "RESEARCH_PLAN_BROAD_QUERY", "severity": "P1", "query": query, "message": "Query is too broad."}
            findings.append(issue) if strict else warnings.append(f"RESEARCH_PLAN_BROAD_QUERY: {query}")
        if strict and questions and not item["linked_question_indexes"]:
            findings.append({"code": "RESEARCH_PLAN_UNBOUND_QUERY", "severity": "P1", "query": query, "message": "Query is not linked to a research question."})

    normalized = {
        "plan_id": str(plan.get("plan_id") or ""),
        "task_type": str(plan.get("task_type") or "PUBLIC_RESEARCH"),
        "research_questions": questions,
        "queries": queries,
        "query_items": query_items,
        "source_priorities": priorities,
        "time_scope": time_scope,
        "evidence_requirements": evidence_requirements,
        "prohibited_inferences": prohibited_inferences,
    }
    return normalized, {
        "status": "BLOCK" if findings else ("WARN" if warnings else "PASS"),
        "strict": strict,
        "findings": findings,
        "warnings": warnings,
    }
