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
    return latin | grams


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


def _merge_unique_values(*values: Any) -> list[str]:
    merged: list[str] = []
    for raw_values in values:
        if isinstance(raw_values, str):
            raw_values = [raw_values]
        for raw in raw_values or []:
            value = str(raw or "").strip()
            if value and value not in merged:
                merged.append(value)
    return merged


def _merge_duplicate_candidate(original: dict[str, Any], duplicate: dict[str, Any]) -> None:
    """Preserve multi-query/provider evidence when the same work is rediscovered.

    Deduplication must collapse archive identity, not erase retrieval provenance.  A DOI
    returned for several approved queries therefore remains bound to every query, and a
    work seen through several scholarly providers keeps all provider identities.
    """

    matched = _merge_unique_values(
        original.get("matched_queries"),
        [original.get("matched_query")],
        duplicate.get("matched_queries"),
        [duplicate.get("matched_query")],
    )
    if matched:
        original["matched_queries"] = matched
        original["matched_query"] = matched[0]
    providers = _merge_unique_values(
        original.get("discovery_providers"),
        [original.get("academic_provider")],
        duplicate.get("discovery_providers"),
        [duplicate.get("academic_provider")],
    )
    if providers:
        original["discovery_providers"] = providers
    original["authors"] = _merge_unique_values(original.get("authors"), duplicate.get("authors"))
    for field in ("abstract", "excerpt", "content_text"):
        first = str(original.get(field) or "")
        second = str(duplicate.get(field) or "")
        if len(second) > len(first):
            original[field] = duplicate.get(field)
    try:
        original["citation_count"] = max(
            int(original.get("citation_count") or 0),
            int(duplicate.get("citation_count") or 0),
        )
    except (TypeError, ValueError):
        pass
    original["is_retracted"] = bool(original.get("is_retracted")) or bool(duplicate.get("is_retracted"))
    verification = dict(original.get("verification") or {})
    duplicate_verification = dict(duplicate.get("verification") or {})
    verification["matched_queries"] = _merge_unique_values(
        verification.get("matched_queries"),
        duplicate_verification.get("matched_queries"),
        matched,
    )
    verification["discovery_providers"] = _merge_unique_values(
        verification.get("discovery_providers"),
        [verification.get("discovery_provider")],
        duplicate_verification.get("discovery_providers"),
        [duplicate_verification.get("discovery_provider")],
        providers,
    )
    original["verification"] = verification


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
            _merge_duplicate_candidate(original, candidate)
            duplicates.append({
                "reason": f"DUPLICATE_{key[0].upper()}",
                "identity": key[1],
                "kept_url": original.get("url"),
                "duplicate_url": candidate.get("url"),
                "conflict_fields": conflict_fields,
                "merged_query_count": len(original.get("matched_queries") or []),
                "merged_provider_count": len(original.get("discovery_providers") or []),
            })
            continue
        seen[key] = len(kept)
        kept.append(candidate)
    return kept, duplicates


def _query_question_score(query: str, question: str) -> float:
    """Return a same-script lexical signal, never a semantic hard gate.

    Token overlap is useful when both strings are written in the same language, but
    it cannot prove that an English search query is unrelated to a Chinese research
    question.  Callers may use this score as a positive legacy migration signal;
    absence of overlap must not be treated as evidence of no binding.
    """
    query_tokens = tokens(query)
    question_tokens = tokens(question)
    if not query_tokens or not question_tokens:
        return 0.0
    return len(query_tokens & question_tokens) / max(1, min(len(query_tokens), len(question_tokens)))


def _explicit_question_indexes(item: dict[str, Any], question_count: int) -> tuple[list[int], list[Any]]:
    raw = item.get("linked_question_indexes")
    if raw is None:
        raw = item.get("linked_research_question_indexes")
    if raw is None:
        return [], []
    values = raw if isinstance(raw, list) else [raw]
    valid: list[int] = []
    invalid: list[Any] = []
    for value in values:
        if isinstance(value, bool):
            invalid.append(value)
            continue
        if isinstance(value, int):
            index = value
        elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
            index = int(value.strip())
        else:
            # Do not silently coerce 1.5 to 1 or arbitrary objects to strings.
            # New plans are schema-validated as integers; digit strings are accepted
            # only for deterministic migration of older persisted plans.
            invalid.append(value)
            continue
        if index < 0 or index >= question_count:
            invalid.append(value)
            continue
        if index not in valid:
            valid.append(index)
    return valid, invalid


def _query_text(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        return str(item.get("query") or item.get("query_text") or item.get("text") or "").strip()
    return ""


MAX_RESEARCH_QUERIES = 12


def normalize_and_validate_plan(plan: dict[str, Any], *, strict: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(plan, dict):
        raise ValueError("Research plan must be a JSON object")
    questions = [
        str(item or "").strip()
        for item in plan.get("research_questions") or []
        if str(item or "").strip()
    ]
    priorities = unique_texts(plan.get("source_priorities"))
    evidence_requirements = unique_texts(plan.get("evidence_requirements"))
    prohibited_inferences = unique_texts(plan.get("prohibited_inferences"))
    time_scope = plan.get("time_scope")
    binding_contract_version = str(plan.get("binding_contract_version") or "").strip() or None
    explicit_contract = binding_contract_version == "1.0"

    findings: list[dict[str, Any]] = []
    warnings: list[str] = []
    if len(set(questions)) != len(questions):
        issue = {
            "code": "RESEARCH_PLAN_DUPLICATE_QUESTION",
            "severity": "P1",
            "message": "Research questions must be unique so index bindings remain stable.",
        }
        findings.append(issue) if strict else warnings.append(
            "RESEARCH_PLAN_DUPLICATE_QUESTION: duplicate questions make legacy bindings ambiguous."
        )
    queries: list[str] = []
    query_items: list[dict[str, Any]] = []
    query_index_by_text: dict[str, int] = {}
    query_ids: set[str] = set()

    for source_index, raw_item in enumerate(plan.get("queries") or []):
        query = _query_text(raw_item)
        if not query:
            continue
        explicit_links: list[int] = []
        invalid_links: list[Any] = []
        structured = isinstance(raw_item, dict)
        if structured:
            explicit_links, invalid_links = _explicit_question_indexes(raw_item, len(questions))
        if invalid_links:
            findings.append({
                "code": "RESEARCH_PLAN_INVALID_QUERY_BINDING",
                "severity": "P1",
                "query": query,
                "invalid_indexes": invalid_links,
                "message": "Query binding contains an invalid research-question index.",
            })

        if query in query_index_by_text:
            existing = query_items[query_index_by_text[query]]
            for index in explicit_links:
                if index not in existing["linked_question_indexes"]:
                    existing["linked_question_indexes"].append(index)
            continue

        linked = list(explicit_links)
        binding_basis = "EXPLICIT" if linked else ""
        if not linked and questions and not explicit_contract:
            if len(questions) == 1:
                linked = [0]
                binding_basis = "LEGACY_SINGLE_QUESTION"
            else:
                lexical = [
                    question_index for question_index, question in enumerate(questions)
                    if _query_question_score(query, question) >= 0.12
                ][:3]
                if lexical:
                    linked = lexical
                    binding_basis = "LEGACY_LEXICAL_SIGNAL"
                else:
                    # Legacy 2.0 plans exposed only strings.  Cross-language plans
                    # cannot be deterministically assigned to one question from text
                    # overlap alone.  Keep the plan within its already approved task
                    # scope, report the missing fine-grained traceability as a warning,
                    # and require explicit structural bindings for all new 2.1 plans.
                    binding_basis = "LEGACY_PLAN_SCOPE"
                    warnings.append(
                        "RESEARCH_PLAN_LEGACY_BINDING_UNVERIFIED: "
                        f"{query} is bound to the approved plan scope because the legacy plan "
                        "contains no explicit query-to-question mapping."
                    )

        if explicit_contract and questions and not linked:
            findings.append({
                "code": "RESEARCH_PLAN_UNBOUND_QUERY",
                "severity": "P1",
                "query": query,
                "message": "Query lacks an explicit linked_question_indexes binding.",
            })

        query_id = (
            str(raw_item.get("query_id") or "").strip()
            if isinstance(raw_item, dict)
            else ""
        ) or f"query-{len(query_items) + 1:03d}"
        if query_id in query_ids:
            findings.append({
                "code": "RESEARCH_PLAN_DUPLICATE_QUERY_ID",
                "severity": "P1",
                "query_id": query_id,
                "query": query,
                "message": "Each query_id must identify exactly one query.",
            })
        query_ids.add(query_id)
        query_index_by_text[query] = len(query_items)
        queries.append(query)
        query_items.append({
            "query_id": query_id,
            "query": query,
            "linked_question_indexes": linked,
            "binding_basis": binding_basis or "UNBOUND",
            "token_count": len(tokens(query)),
            "source_index": source_index,
        })

    if not queries:
        findings.append({"code": "RESEARCH_PLAN_NO_QUERY", "severity": "P0", "message": "No executable query."})
    if strict and len(queries) > MAX_RESEARCH_QUERIES:
        findings.append({
            "code": "RESEARCH_PLAN_TOO_MANY_QUERIES",
            "severity": "P1",
            "message": (
                f"At most {MAX_RESEARCH_QUERIES} executable queries are supported; "
                f"received {len(queries)}."
            ),
        })
    if not questions:
        if strict:
            findings.append({"code": "RESEARCH_PLAN_NO_QUESTION", "severity": "P1", "message": "Research questions are required."})
        else:
            warnings.append("Research questions are absent; query traceability is unavailable.")
    if strict and not priorities:
        findings.append({"code": "RESEARCH_PLAN_NO_SOURCE_PRIORITY", "severity": "P1", "message": "Source priorities are required."})
    if strict and not str(time_scope or "").strip():
        findings.append({"code": "RESEARCH_PLAN_NO_TIME_SCOPE", "severity": "P1", "message": "A time scope is required."})
    if strict and binding_contract_version not in {None, "1.0"}:
        findings.append({
            "code": "RESEARCH_PLAN_BINDING_CONTRACT_UNSUPPORTED",
            "severity": "P1",
            "message": f"Unsupported binding_contract_version: {binding_contract_version}",
        })

    for item in query_items:
        query = item["query"]
        words = {word for word in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2,}", query.lower()) if word}
        broad = len(query) < 8 or (len(words - _GENERIC_QUERY_TERMS) <= 1 and item["token_count"] <= 2)
        if broad:
            issue = {"code": "RESEARCH_PLAN_BROAD_QUERY", "severity": "P1", "query": query, "message": "Query is too broad."}
            findings.append(issue) if strict else warnings.append(f"RESEARCH_PLAN_BROAD_QUERY: {query}")

    normalized = {
        "plan_id": str(plan.get("plan_id") or ""),
        "task_type": str(plan.get("task_type") or "PUBLIC_RESEARCH"),
        "binding_contract_version": binding_contract_version or "LEGACY-2.0",
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
        "binding_contract_version": normalized["binding_contract_version"],
        "findings": findings,
        "warnings": warnings,
    }
