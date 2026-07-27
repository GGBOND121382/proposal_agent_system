from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

# One canonical epistemic vocabulary for every field named ``knowledge_status``.
CANONICAL_KNOWLEDGE_STATUSES: tuple[str, ...] = (
    "CONFIRMED",
    "USER_ASSERTED",
    "DOCUMENT_EXTRACTED",
    "ESTIMATED",
    "UNKNOWN",
    "NOT_APPLICABLE",
    "CONFLICTED",
    "SUPERSEDED",
)

CANONICAL_CLAIM_TYPES: tuple[str, ...] = (
    "FACT",
    "PLAN",
    "EXPECTED_RESULT",
    "REQUIREMENT",
    "PUBLIC_CLAIM",
    "MODEL_INFERENCE",
)

CANONICAL_TEMPORAL_STATUSES: tuple[str, ...] = (
    "PAST",
    "CURRENT",
    "PLANNED",
    "EXPECTED",
    "TIME_INDEPENDENT",
    "UNKNOWN",
)

# These values appeared in older staged artifacts or are common model-created
# paraphrases. They are accepted only by the deterministic compatibility layer;
# schemas and prompts expose canonical values only.
LEGACY_CLAIM_TYPE_ALIASES: frozenset[str] = frozenset(
    {
        "PROJECT_DESIGN",
        "CONFIRMED_DESIGN",
        "PROJECT_PLAN",
        "PLANNED",
        "PROVISIONAL_TARGET",
        "WORKING_ASSUMPTION",
    }
)

LEGACY_TEMPORAL_ALIASES: frozenset[str] = frozenset(
    {
        "PROJECT_DESIGN",
        "CONFIRMED_DESIGN",
        "PROJECT_PLAN",
        "PROVISIONAL_TARGET",
        "WORKING_ASSUMPTION",
    }
)

LEGACY_KNOWLEDGE_ALIASES: frozenset[str] = frozenset(
    {
        "PROJECT_DESIGN",
        "CONFIRMED_DESIGN",
        "PROVISIONAL_TARGET",
        "WORKING_ASSUMPTION",
        "PLANNED",
    }
)

STAGE2_FACT_ROLES: tuple[str, ...] = (
    "FACT",
    "DESIGN",
    "TARGET",
    "ASSUMPTION",
    "UNKNOWN",
)


@dataclass(frozen=True)
class KnowledgeStatusDecision:
    original_status: str
    canonical_status: str
    reason: str
    normalized: bool


@dataclass(frozen=True)
class SemanticEnumDecision:
    original_value: str
    canonical_value: str
    reason: str
    normalized: bool


def _iter_source_types(
    source_refs: Iterable[Any] | None,
    source_registry: Mapping[str, Mapping[str, Any]] | None = None,
) -> Iterable[tuple[str, Mapping[str, Any]]]:
    registry = source_registry or {}
    for ref in source_refs or []:
        if isinstance(ref, str):
            metadata = registry.get(ref, {})
            source_type = str(metadata.get("source_type") or "")
            yield source_type, metadata
        elif isinstance(ref, Mapping):
            source_id = str(ref.get("source_id") or "")
            metadata = dict(registry.get(source_id, {}))
            metadata.update(ref)
            source_type = str(metadata.get("source_type") or "")
            yield source_type, metadata


def _source_basis(
    source_refs: Iterable[Any] | None,
    source_registry: Mapping[str, Mapping[str, Any]] | None = None,
) -> str:
    """Return the strongest deterministic provenance basis.

    CONFIRMED means a human-confirmed/accepted artifact, not that a planned
    target has already been achieved. Achievement semantics belong to
    ``fact_role``/``claim_type`` and ``temporal_status``.
    """

    saw_user = False
    saw_document = False
    saw_model = False
    for source_type, metadata in _iter_source_types(source_refs, source_registry):
        availability = str(metadata.get("availability") or "AVAILABLE")
        authority = str(metadata.get("authority") or "")
        accepted = metadata.get("accepted") is True

        if source_type in {"USER_CONFIRMATION"}:
            return "CONFIRMED"
        if source_type == "CONFIRMED_ARTIFACT" and availability != "MISSING":
            return "CONFIRMED"
        if accepted or authority == "CONFIRMED":
            return "CONFIRMED"
        if source_type in {
            "APPLICATION_GUIDE",
            "TASK_BOOK",
            "CONTRACT",
            "CURRENT_PROPOSAL",
            "TECHNICAL_MATERIAL",
            "EVIDENCE_MATERIAL",
            "HISTORICAL_DOCUMENT",
            "REFERENCE_PROPOSAL",
            "PUBLIC_SOURCE",
            "OFFICIAL_GUIDE",
            "STAGE_CONTRACT",
        } and availability != "MISSING":
            saw_document = True
        elif source_type in {"USER_REQUEST", "USER_ASSERTED"}:
            saw_user = True
        elif source_type == "MODEL_INFERENCE":
            saw_model = True

    if saw_document:
        return "DOCUMENT_EXTRACTED"
    if saw_user:
        return "USER_ASSERTED"
    if saw_model:
        return "ESTIMATED"
    return "NONE"


def normalize_knowledge_status(
    raw_status: Any,
    *,
    source_refs: Iterable[Any] | None = None,
    source_registry: Mapping[str, Mapping[str, Any]] | None = None,
) -> KnowledgeStatusDecision:
    """Normalize known semantic aliases without inventing unsupported facts.

    Unknown arbitrary strings are deliberately left unchanged so strict schema
    validation still catches genuinely novel drift. Only known legacy/model
    aliases are converted.
    """

    original = str(raw_status or "").strip().upper()
    if original in CANONICAL_KNOWLEDGE_STATUSES:
        return KnowledgeStatusDecision(original, original, "already canonical", False)
    if original not in LEGACY_KNOWLEDGE_ALIASES:
        return KnowledgeStatusDecision(original, original, "unrecognized status; strict validation required", False)

    basis = _source_basis(source_refs, source_registry)

    if original == "WORKING_ASSUMPTION":
        if basis in {"CONFIRMED", "USER_ASSERTED"}:
            canonical = basis
            reason = "working assumption explicitly supplied or confirmed by a human source"
        else:
            canonical = "ESTIMATED"
            reason = "working assumption is model-derived or lacks direct human confirmation"
    elif original == "PROVISIONAL_TARGET":
        if basis in {"CONFIRMED", "USER_ASSERTED", "DOCUMENT_EXTRACTED"}:
            canonical = basis
            reason = "target statement provenance retained; target semantics move to fact_role/temporal_status"
        else:
            canonical = "ESTIMATED"
            reason = "target was proposed without a direct source"
    else:  # PROJECT_DESIGN / CONFIRMED_DESIGN / misplaced PLANNED
        if basis != "NONE":
            canonical = basis
            reason = "project-design provenance resolved from source references"
        else:
            canonical = "ESTIMATED"
            reason = "project design was inferred without a traceable source"

    return KnowledgeStatusDecision(original, canonical, reason, True)


def normalize_claim_type(raw_value: Any) -> SemanticEnumDecision:
    """Normalize known aliases that drift into the ``claim_type`` field.

    The mapping is semantic, not evidentiary: provenance remains in
    ``knowledge_status``. Unknown values are left untouched so strict schema
    validation still catches genuinely novel drift.
    """

    original = str(raw_value or "").strip().upper()
    if original in CANONICAL_CLAIM_TYPES:
        return SemanticEnumDecision(original, original, "already canonical", False)
    if original not in LEGACY_CLAIM_TYPE_ALIASES:
        return SemanticEnumDecision(
            original, original, "unrecognized claim_type; strict validation required", False
        )

    if original in {"PROJECT_DESIGN", "CONFIRMED_DESIGN", "PROJECT_PLAN", "PLANNED"}:
        canonical = "PLAN"
        reason = "project-design semantics belong to claim_type=PLAN"
    elif original == "PROVISIONAL_TARGET":
        canonical = "EXPECTED_RESULT"
        reason = "provisional-target semantics belong to claim_type=EXPECTED_RESULT"
    else:  # WORKING_ASSUMPTION
        canonical = "MODEL_INFERENCE"
        reason = "working-assumption semantics belong to claim_type=MODEL_INFERENCE"
    return SemanticEnumDecision(original, canonical, reason, True)


def normalize_temporal_status(raw_value: Any) -> SemanticEnumDecision:
    """Normalize known semantic aliases misplaced in ``temporal_status``."""

    original = str(raw_value or "").strip().upper()
    if original in CANONICAL_TEMPORAL_STATUSES:
        return SemanticEnumDecision(original, original, "already canonical", False)
    if original not in LEGACY_TEMPORAL_ALIASES:
        return SemanticEnumDecision(
            original, original, "unrecognized temporal_status; strict validation required", False
        )

    if original in {"PROJECT_DESIGN", "CONFIRMED_DESIGN", "PROJECT_PLAN"}:
        canonical = "PLANNED"
        reason = "project-design time semantics belong to temporal_status=PLANNED"
    elif original == "PROVISIONAL_TARGET":
        canonical = "EXPECTED"
        reason = "provisional-target time semantics belong to temporal_status=EXPECTED"
    else:  # WORKING_ASSUMPTION
        canonical = "UNKNOWN"
        reason = "a working assumption has no established occurrence time"
    return SemanticEnumDecision(original, canonical, reason, True)


def implied_temporal_status_from_claim_alias(raw_value: Any) -> str | None:
    """Return the canonical time dimension implied by a known claim alias."""

    original = str(raw_value or "").strip().upper()
    if original in {"PROJECT_DESIGN", "CONFIRMED_DESIGN", "PROJECT_PLAN", "PLANNED"}:
        return "PLANNED"
    if original == "PROVISIONAL_TARGET":
        return "EXPECTED"
    if original == "WORKING_ASSUMPTION":
        return "UNKNOWN"
    return None


def legacy_fact_role(raw_status: Any) -> str:
    status = str(raw_status or "").strip().upper()
    if status in {"PROJECT_DESIGN", "CONFIRMED_DESIGN", "PLANNED"}:
        return "DESIGN"
    if status == "PROVISIONAL_TARGET":
        return "TARGET"
    if status == "WORKING_ASSUMPTION":
        return "ASSUMPTION"
    if status == "UNKNOWN":
        return "UNKNOWN"
    return "FACT"


def default_temporal_status(fact_role: str) -> str:
    return {
        "DESIGN": "PLANNED",
        "TARGET": "EXPECTED",
        "ASSUMPTION": "UNKNOWN",
        "UNKNOWN": "UNKNOWN",
        "FACT": "TIME_INDEPENDENT",
    }.get(fact_role, "UNKNOWN")


def normalize_stage2_candidate(candidate: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Migrate a Stage-2 candidate to the canonical status contract.

    The input is copied. The report is suitable for trace persistence.
    """

    import copy

    normalized: dict[str, Any] = copy.deepcopy(dict(candidate))
    normalized["schema_version"] = "1.1"
    registry = {
        str(item.get("source_id")): item
        for item in normalized.get("source_registry") or []
        if isinstance(item, Mapping) and item.get("source_id")
    }
    changes: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    permission_changed_fact_ids: set[str] = set()

    for index, fact in enumerate(normalized.get("facts") or []):
        if not isinstance(fact, dict):
            continue
        raw_status = fact.get("knowledge_status")
        raw_role = str(fact.get("fact_role") or "").strip().upper()
        role_aliases = {
            "PROJECT_DESIGN": "DESIGN",
            "CONFIRMED_DESIGN": "DESIGN",
            "PROJECT_PLAN": "DESIGN",
            "PLAN": "DESIGN",
            "PLANNED": "DESIGN",
            "PROVISIONAL_TARGET": "TARGET",
            "EXPECTED_RESULT": "TARGET",
            "WORKING_ASSUMPTION": "ASSUMPTION",
            "MODEL_INFERENCE": "ASSUMPTION",
        }
        role = role_aliases.get(raw_role, raw_role or legacy_fact_role(raw_status))
        if role not in STAGE2_FACT_ROLES:
            unresolved.append({
                "path": f"facts/{index}/fact_role",
                "value": role,
                "reason": "unrecognized fact_role",
            })
        else:
            fact["fact_role"] = role
            if raw_role and raw_role != role:
                changes.append({
                    "path": f"facts/{index}/fact_role",
                    "fact_id": fact.get("fact_id"),
                    "from": raw_role,
                    "to": role,
                    "reason": "legacy/model semantic role mapped to canonical Stage-2 fact_role",
                })

        raw_temporal = fact.get("temporal_status") or default_temporal_status(role)
        temporal_decision = normalize_temporal_status(raw_temporal)
        temporal = temporal_decision.canonical_value
        if temporal_decision.normalized:
            changes.append({
                "path": f"facts/{index}/temporal_status",
                "fact_id": fact.get("fact_id"),
                "from": temporal_decision.original_value,
                "to": temporal,
                "reason": temporal_decision.reason,
            })
        # Stage-2 role semantics deterministically define the time dimension.
        required_temporal = {"DESIGN": "PLANNED", "TARGET": "EXPECTED", "ASSUMPTION": "UNKNOWN"}.get(role)
        if required_temporal and temporal != required_temporal:
            changes.append({
                "path": f"facts/{index}/temporal_status",
                "fact_id": fact.get("fact_id"),
                "from": temporal,
                "to": required_temporal,
                "reason": f"fact_role={role} requires temporal_status={required_temporal}",
            })
            temporal = required_temporal
        if temporal in CANONICAL_TEMPORAL_STATUSES:
            fact["temporal_status"] = temporal
        else:
            unresolved.append({
                "path": f"facts/{index}/temporal_status",
                "value": temporal,
                "reason": "unrecognized temporal_status",
            })

        decision = normalize_knowledge_status(
            raw_status,
            source_refs=fact.get("source_refs"),
            source_registry=registry,
        )
        if decision.normalized:
            fact["knowledge_status"] = decision.canonical_status
            changes.append({
                "path": f"facts/{index}/knowledge_status",
                "fact_id": fact.get("fact_id"),
                "from": decision.original_status,
                "to": decision.canonical_status,
                "reason": decision.reason,
            })
        elif decision.canonical_status not in CANONICAL_KNOWLEDGE_STATUSES:
            unresolved.append({
                "path": f"facts/{index}/knowledge_status",
                "fact_id": fact.get("fact_id"),
                "value": decision.original_status,
                "reason": decision.reason,
            })

        # Preserve semantics that legacy statuses previously encoded.
        if role in {"TARGET", "ASSUMPTION"}:
            if fact.get("assertion_policy") != "QUALIFIED":
                changes.append({
                    "path": f"facts/{index}/assertion_policy",
                    "fact_id": fact.get("fact_id"),
                    "from": fact.get("assertion_policy"),
                    "to": "QUALIFIED",
                    "reason": f"{role} statements must not be written as achieved facts",
                })
                fact["assertion_policy"] = "QUALIFIED"
                if fact.get("fact_id"):
                    permission_changed_fact_ids.add(str(fact["fact_id"]))
            if fact.get("requires_qualification") is not True:
                fact["requires_qualification"] = True
        elif role == "UNKNOWN":
            if fact.get("assertion_policy") != "PROHIBITED":
                changes.append({
                    "path": f"facts/{index}/assertion_policy",
                    "fact_id": fact.get("fact_id"),
                    "from": fact.get("assertion_policy"),
                    "to": "PROHIBITED",
                    "reason": "unknown facts cannot be written as claims",
                })
                fact["assertion_policy"] = "PROHIBITED"
                if fact.get("fact_id"):
                    permission_changed_fact_ids.add(str(fact["fact_id"]))
            fact["requires_qualification"] = False

    # Update only indexes affected by a deterministic assertion-policy migration.
    # Do not rebuild unrelated indexes, so missing/extra IDs still fail validation.
    facts = [fact for fact in normalized.get("facts") or [] if isinstance(fact, dict)]
    permissions = normalized.get("writing_permissions")
    if isinstance(permissions, dict) and permission_changed_fact_ids:
        buckets = {
            "DIRECT": "direct_fact_ids",
            "QUALIFIED": "qualified_fact_ids",
            "PROHIBITED": "prohibited_fact_ids",
        }
        for field in buckets.values():
            permissions[field] = [
                value for value in permissions.get(field, [])
                if str(value) not in permission_changed_fact_ids
            ]
        by_id = {str(fact.get("fact_id")): fact for fact in facts if fact.get("fact_id")}
        for fact_id in sorted(permission_changed_fact_ids):
            policy = str(by_id[fact_id].get("assertion_policy") or "")
            field = buckets.get(policy)
            if field:
                permissions.setdefault(field, []).append(fact_id)

    report = {
        "schema_version": "1.0",
        "canonical_knowledge_statuses": list(CANONICAL_KNOWLEDGE_STATUSES),
        "normalized_count": len(changes),
        "changes": changes,
        "unresolved_count": len(unresolved),
        "unresolved": unresolved,
    }
    return normalized, report


def normalize_stage3_candidate(
    candidate: Mapping[str, Any],
    stage2: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Normalize Stage-3 project-definition knowledge status fields."""
    import copy

    normalized: dict[str, Any] = copy.deepcopy(dict(candidate))
    changes: list[dict[str, Any]] = []
    cp = normalized.get("central_proposition")
    if isinstance(cp, dict):
        raw = str(cp.get("knowledge_status") or "").strip().upper()
        if raw in LEGACY_KNOWLEDGE_ALIASES:
            # Stage 3 can only start from the Stage-2 artifact after the
            # PROJECT_OWNER confirmation gate.  Therefore the proposition is
            # confirmed *as the selected project design*, while PLANNED keeps
            # it from being misread as an achieved result.  The linked source
            # facts still retain their original USER_ASSERTED/DOCUMENT_EXTRACTED
            # provenance in the upstream ledger.
            target = "CONFIRMED"
            cp["knowledge_status"] = target
            changes.append({
                "path": "central_proposition/knowledge_status",
                "from": raw,
                "to": target,
                "reason": "Stage-2 project-owner gate confirmed this proposition as the selected design",
            })
        raw_role = str(cp.get("claim_role") or "").strip().upper()
        if not raw_role:
            cp["claim_role"] = "DESIGN_HYPOTHESIS"
            changes.append({
                "path": "central_proposition/claim_role",
                "from": None,
                "to": "DESIGN_HYPOTHESIS",
                "reason": "missing deterministic Stage-3 proposition role restored",
            })
        elif raw_role in {"PROJECT_DESIGN", "CONFIRMED_DESIGN", "PROJECT_PLAN", "PLAN", "DESIGN", "PLANNED"}:
            cp["claim_role"] = "DESIGN_HYPOTHESIS"
            changes.append({
                "path": "central_proposition/claim_role",
                "from": raw_role,
                "to": "DESIGN_HYPOTHESIS",
                "reason": "project-design alias mapped to the Stage-3 design hypothesis role",
            })

        raw_temporal = str(cp.get("temporal_status") or "").strip().upper()
        if not raw_temporal:
            cp["temporal_status"] = "PLANNED"
            changes.append({
                "path": "central_proposition/temporal_status",
                "from": None,
                "to": "PLANNED",
                "reason": "missing deterministic Stage-3 proposition time restored",
            })
        else:
            temporal_decision = normalize_temporal_status(raw_temporal)
            if temporal_decision.normalized and temporal_decision.canonical_value == "PLANNED":
                cp["temporal_status"] = "PLANNED"
                changes.append({
                    "path": "central_proposition/temporal_status",
                    "from": raw_temporal,
                    "to": "PLANNED",
                    "reason": temporal_decision.reason,
                })

    return normalized, {
        "schema_version": "1.0",
        "normalized_count": len(changes),
        "changes": changes,
    }
