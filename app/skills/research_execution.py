from __future__ import annotations

from typing import Any

from ..util import sha256_json
from .research_plan import normalize_and_validate_plan


class ResearchExecutionContractError(ValueError):
    """Raised when retrieval attempts to execute outside the approved research plan."""

    def __init__(self, message: str, *, code: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _query_projection(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "query_id": str(item.get("query_id") or ""),
        "query": str(item.get("query") or "").strip(),
        "linked_question_indexes": list(item.get("linked_question_indexes") or []),
        **({"entity_groups": item["entity_groups"]} if "entity_groups" in item else {}),
    }


def _plan_projection(normalized_plan: dict[str, Any]) -> dict[str, Any]:
    """Project only plan-owned semantics into the execution identity.

    Validator annotations such as token_count/source_index/binding_basis are deliberately
    excluded.  The lock therefore survives deterministic validator upgrades while still
    detecting a changed research question, query text, binding, time scope, or evidence
    requirement.
    """

    return {
        "plan_id": str(normalized_plan.get("plan_id") or ""),
        "task_type": str(normalized_plan.get("task_type") or "PUBLIC_RESEARCH"),
        "binding_contract_version": str(normalized_plan.get("binding_contract_version") or ""),
        "research_questions": list(normalized_plan.get("research_questions") or []),
        "query_items": [
            _query_projection(item)
            for item in normalized_plan.get("query_items") or []
            if isinstance(item, dict)
        ],
        "source_priorities": list(normalized_plan.get("source_priorities") or []),
        "time_scope": normalized_plan.get("time_scope"),
        "evidence_requirements": list(normalized_plan.get("evidence_requirements") or []),
        "prohibited_inferences": list(normalized_plan.get("prohibited_inferences") or []),
        "required_channels": list(normalized_plan.get("required_channels") or []),
        "provider_execution_requirements": {
            "required_providers": list(
                (normalized_plan.get("provider_execution_requirements") or {}).get("required_providers") or []
            ),
            "execute_all_approved_queries": bool(
                (normalized_plan.get("provider_execution_requirements") or {}).get("execute_all_approved_queries", True)
            ),
        },
        "minimum_fulltext_sources_per_query": int(normalized_plan.get("minimum_fulltext_sources_per_query") or 0),
        "allow_snippet_only": bool(normalized_plan.get("allow_snippet_only", True)),
        "require_web_discovery": bool(normalized_plan.get("require_web_discovery", False)),
    }


def build_plan_lock(plan: dict[str, Any]) -> dict[str, Any]:
    if isinstance(plan, dict) and isinstance(plan.get("query_items"), list):
        normalized = dict(plan)
    else:
        normalized, _ = normalize_and_validate_plan(plan or {}, strict=False)
    projection = _plan_projection(normalized)
    return {
        "schema_version": "1.0",
        "plan_id": projection["plan_id"],
        "plan_hash": sha256_json(projection),
        "projection": projection,
    }


def _is_prefix(original: list[Any], candidate: list[Any]) -> bool:
    return len(candidate) >= len(original) and candidate[: len(original)] == original


def _is_superset_sequence(original: list[str], candidate: list[str]) -> bool:
    candidate_set = set(candidate)
    return all(item in candidate_set for item in original)


def validate_plan_transition(
    approved_lock: dict[str, Any],
    candidate_plan: dict[str, Any],
    *,
    allow_additive: bool = True,
    allow_binding_enrichment: bool = False,
) -> dict[str, Any]:
    """Validate retry/research-repair plan continuity and return the new lock.

    A retry may not silently replace or delete an already approved question/query.  When
    ``allow_additive`` is true, later research repair may append questions and query IDs,
    or strengthen source/evidence/prohibited-inference lists.  This is the deterministic
    PlanDelta boundary used by WF-3; destructive changes require a fresh workflow/gate.
    """

    if not isinstance(approved_lock, dict) or not approved_lock.get("projection"):
        raise ResearchExecutionContractError(
            "Approved research plan lock is missing or malformed",
            code="RESEARCH_PLAN_LOCK_INVALID",
        )
    candidate_lock = build_plan_lock(candidate_plan)
    if approved_lock.get("plan_hash") == candidate_lock.get("plan_hash"):
        return candidate_lock

    old = dict(approved_lock.get("projection") or {})
    new = dict(candidate_lock.get("projection") or {})
    immutable_fields = ("task_type", "binding_contract_version", "time_scope")
    changed = [field for field in immutable_fields if old.get(field) != new.get(field)]
    # Phase 3 execution-contract fields are immutable once a lock records them.
    # Locks written before Phase 3 simply lack these keys; absence must not be
    # treated as a mismatch with the new normalization defaults.
    for field in (
        "required_channels",
        "provider_execution_requirements",
        "minimum_fulltext_sources_per_query",
        "allow_snippet_only",
        "require_web_discovery",
    ):
        if field in old and old.get(field) != new.get(field):
            changed.append(field)
    old_plan_id = str(old.get("plan_id") or "")
    new_plan_id = str(new.get("plan_id") or "")
    if old_plan_id and new_plan_id and old_plan_id != new_plan_id:
        changed.append("plan_id")
    if changed:
        raise ResearchExecutionContractError(
            "Approved research plan identity changed during retrieval/retry: " + ", ".join(changed),
            code="RESEARCH_PLAN_LOCK_MISMATCH",
            details={"changed_fields": changed, "approved_plan_hash": approved_lock.get("plan_hash"), "candidate_plan_hash": candidate_lock.get("plan_hash")},
        )

    old_questions = list(old.get("research_questions") or [])
    new_questions = list(new.get("research_questions") or [])
    if (allow_additive and not _is_prefix(old_questions, new_questions)) or (
        not allow_additive and old_questions != new_questions
    ):
        raise ResearchExecutionContractError(
            "Approved research questions were replaced, reordered, or deleted",
            code="RESEARCH_PLAN_DESTRUCTIVE_DELTA",
            details={"field": "research_questions"},
        )

    old_queries = {
        str(item.get("query_id") or ""): item
        for item in old.get("query_items") or []
        if isinstance(item, dict) and str(item.get("query_id") or "")
    }
    new_queries = {
        str(item.get("query_id") or ""): item
        for item in new.get("query_items") or []
        if isinstance(item, dict) and str(item.get("query_id") or "")
    }
    missing_ids = sorted(set(old_queries) - set(new_queries))
    changed_ids: list[str] = []
    for query_id in set(old_queries) & set(new_queries):
        old_query = old_queries[query_id]
        new_query = new_queries[query_id]
        if old_query == new_query:
            continue
        binding_only_enrichment = (
            allow_binding_enrichment
            and old_query.get("query_id") == new_query.get("query_id")
            and old_query.get("query") == new_query.get("query")
            and old_query.get("entity_groups") == new_query.get("entity_groups")
            and set(old_query.get("linked_question_indexes") or []).issubset(
                set(new_query.get("linked_question_indexes") or [])
            )
        )
        if not binding_only_enrichment:
            changed_ids.append(query_id)
    changed_ids.sort()
    added_ids = sorted(set(new_queries) - set(old_queries))
    if missing_ids or changed_ids or (added_ids and not allow_additive):
        raise ResearchExecutionContractError(
            "Approved research queries were replaced, mutated, or deleted",
            code="RESEARCH_PLAN_DESTRUCTIVE_DELTA",
            details={
                "missing_query_ids": missing_ids,
                "changed_query_ids": changed_ids,
                "added_query_ids": added_ids,
            },
        )

    additive_fields = ("source_priorities", "evidence_requirements", "prohibited_inferences")
    bad_fields = []
    for field in additive_fields:
        old_values = [str(item) for item in old.get(field) or []]
        new_values = [str(item) for item in new.get(field) or []]
        valid = _is_superset_sequence(old_values, new_values) if allow_additive else old_values == new_values
        if not valid:
            bad_fields.append(field)
    if bad_fields:
        raise ResearchExecutionContractError(
            "Approved research constraints were weakened or deleted: " + ", ".join(bad_fields),
            code="RESEARCH_PLAN_DESTRUCTIVE_DELTA",
            details={"fields": bad_fields},
        )
    return candidate_lock


def validate_connector_execution(
    manifest: dict[str, Any],
    normalized_plan: dict[str, Any],
    *,
    strict: bool,
) -> dict[str, Any]:
    """Bind an external connector run to the exact approved query set.

    Legacy connector files remain readable when ``strict`` is false.  Strict WF-3 runs
    reject missing, extra, or query-id/text-mutated executions.  If the connector carries
    an approved ``plan_hash`` it is also verified.
    """

    planned_items = [
        _query_projection(item)
        for item in normalized_plan.get("query_items") or []
        if isinstance(item, dict)
    ]
    planned_by_text = {item["query"]: item for item in planned_items if item["query"]}
    planned_by_id = {item["query_id"]: item for item in planned_items if item["query_id"]}
    response_rows = [item for item in manifest.get("responses") or [] if isinstance(item, dict)]
    response_queries = [str(item.get("query") or "").strip() for item in response_rows if str(item.get("query") or "").strip()]
    response_set = set(response_queries)
    planned_set = set(planned_by_text)
    missing = sorted(planned_set - response_set)
    extras = sorted(response_set - planned_set)
    findings: list[dict[str, Any]] = []
    if missing:
        findings.append({"code": "RESEARCH_EXECUTION_MISSING_QUERY", "queries": missing})
    if extras:
        findings.append({"code": "RESEARCH_EXECUTION_UNAPPROVED_QUERY", "queries": extras})

    id_mismatches: list[dict[str, str]] = []
    for row in response_rows:
        query_id = str(row.get("query_id") or "").strip()
        query = str(row.get("query") or "").strip()
        if not query_id:
            continue
        planned = planned_by_id.get(query_id)
        if planned is None or planned.get("query") != query:
            id_mismatches.append({"query_id": query_id, "query": query})
    if id_mismatches:
        findings.append({"code": "RESEARCH_EXECUTION_QUERY_ID_MISMATCH", "items": id_mismatches})

    generated = manifest.get("agent_generated_queries")
    if isinstance(generated, list):
        generated_queries = [str(item or "").strip() for item in generated if str(item or "").strip()]
        if set(generated_queries) != planned_set:
            findings.append(
                {
                    "code": "RESEARCH_EXECUTION_GENERATED_QUERY_SET_MISMATCH",
                    "missing": sorted(planned_set - set(generated_queries)),
                    "extra": sorted(set(generated_queries) - planned_set),
                }
            )

    declared_hash = str(manifest.get("plan_hash") or "").strip()
    expected_hash = build_plan_lock(normalized_plan)["plan_hash"]
    if declared_hash and declared_hash != expected_hash:
        findings.append(
            {
                "code": "RESEARCH_EXECUTION_PLAN_HASH_MISMATCH",
                "expected": expected_hash,
                "actual": declared_hash,
            }
        )

    if strict and findings:
        first = findings[0]
        raise ResearchExecutionContractError(
            "Connector execution does not match the approved research plan: " + str(first.get("code")),
            code=str(first.get("code") or "RESEARCH_EXECUTION_CONTRACT_MISMATCH"),
            details={"findings": findings, "expected_plan_hash": expected_hash},
        )
    return {
        "status": "BLOCK" if strict and findings else ("WARN" if findings else "PASS"),
        "plan_hash": expected_hash,
        "planned_query_count": len(planned_set),
        "executed_query_count": len(response_set),
        "findings": findings,
    }
