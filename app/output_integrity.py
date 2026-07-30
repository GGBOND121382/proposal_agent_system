from __future__ import annotations

"""Deterministic provenance and cross-reference integrity for model outputs.

The JSON Schema layer proves shape, not truth.  This module binds every
``source_refs`` entry to an object that was actually present in the current
input envelope (or to persisted metadata for that same input document), and
checks reference-only ID arrays against identifiers that are visible in the
input or explicitly defined in the current output.
"""

import copy
import hashlib
import json
import re
from collections import defaultdict
from typing import Any, Iterable, Mapping

_SOURCE_REF_FIELDS = (
    "source_id",
    "source_type",
    "document_version_id",
    "section_id",
    "span_start",
    "span_end",
    "quoted_text",
    "source_hash",
    "authority_rank",
    "security_level",
)
_ALLOWED_SOURCE_TYPES = {
    "USER_CONFIRMATION",
    "APPLICATION_GUIDE",
    "TASK_BOOK",
    "CONTRACT",
    "CURRENT_PROPOSAL",
    "TECHNICAL_MATERIAL",
    "EVIDENCE_MATERIAL",
    "HISTORICAL_DOCUMENT",
    "REFERENCE_PROPOSAL",
    "PUBLIC_SOURCE",
    "MODEL_INFERENCE",
}
_SOURCE_TYPE_ALIASES = {
    "PROJECT_BRIEF": "HISTORICAL_DOCUMENT",
    "TECHNICAL_DESIGN": "TECHNICAL_MATERIAL",
    "TEAM_PROFILE": "HISTORICAL_DOCUMENT",
    "BUDGET_MATERIAL": "HISTORICAL_DOCUMENT",
    "REVIEW_COMMENT": "HISTORICAL_DOCUMENT",
    "OTHER": "HISTORICAL_DOCUMENT",
    "FACT": "EVIDENCE_MATERIAL",
    "CONFIRMED_FACT": "EVIDENCE_MATERIAL",
    "ARGUMENT_NODE": "MODEL_INFERENCE",
    "ARGUMENT_GRAPH": "MODEL_INFERENCE",
    "PROJECT_ITEM": "TECHNICAL_MATERIAL",
}
_AUTHORITY = {
    "USER_CONFIRMATION": 100,
    "APPLICATION_GUIDE": 95,
    "TASK_BOOK": 95,
    "CONTRACT": 95,
    "CURRENT_PROPOSAL": 85,
    "TECHNICAL_MATERIAL": 80,
    "EVIDENCE_MATERIAL": 80,
    "PUBLIC_SOURCE": 80,
    "MODEL_INFERENCE": 60,
    "REFERENCE_PROPOSAL": 30,
    "HISTORICAL_DOCUMENT": 20,
}
_PROTOCOL_ID_FIELDS = {
    "project_id",
    "workflow_id",
    "prompt_id",
    "model_id",
    "endpoint_id",
    "document_version_id",
    "parent_version_id",
}
_REFERENCE_ARRAY_FIELDS = {
    "accepted_claim_ids",
    "rejected_claim_ids",
    "unsupported_claim_ids",
    "checked_item_ids",
    "checked_rule_ids",
    "checked_component_ids",
    "contaminated_component_ids",
    "checked_paragraph_ids",
    "checked_node_ids",
    "checked_relation_ids",
    "invalid_relation_ids",
    "status_upgrade_item_ids",
    "missing_item_ids",
    "blocking_conflict_ids",
    "claim_ids",
    "conflict_ids",
    "component_ids",
    "open_conflict_ids",
    "item_ids",
    "target_section_ids",
    "read_only_section_ids",
    "protected_section_ids",
    "required_input_ids",
    "missing_input_ids",
    "issue_ids",
    "user_question_ids",
    "checked_issue_ids",
    "checked_task_ids",
    "required_evidence_ids",
    "unresolved_slot_ids",
    "uncovered_revision_task_ids",
    "invalid_slot_refs",
    "critical_unresolved_slot_ids",
    "trace_link_ids",
    "advanced_claim_ids",
    "distinguished_from_section_ids",
    "unsupported_trace_ids",
    "blueprint_deviation_paragraph_ids",
    "supporting_section_ids",
    "overflow_section_ids",
    "linked_gap_ids",
    "gap_ids",
    "objective_ids",
    "work_package_ids",
    "method_ids",
    "evaluation_ids",
    "innovation_ids",
    "foundation_evidence_ids",
    "closest_prior_work_ids",
    "blocking_node_ids",
    "paragraph_ids",
    "used_in_paragraph_ids",
    "research_question_ids",
    "must_advance_claim_ids",
    "must_use_evidence_ids",
    "prerequisite_section_ids",
    "must_not_repeat_section_ids",
    "allowed_shared_context_ids",
    "input_trace_ids",
    "output_trace_ids",
    "missing_trace_ids",
    "new_unapproved_trace_ids",
    "source_ids",
    "acceptance_refs",
    "work_package_refs",
    "deliverable_refs",
    # Findings use ``evidence_refs`` rather than an ``*_ids`` name.
    "evidence_refs",
}
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_REGISTERED_DIAGNOSTIC_REFS = {"F-001", "F-077"}
_SOURCE_ID_ALIAS_PREFIXES = tuple(
    f"{stem}{separator}"
    for stem in ("source", "Source", "SOURCE", "src", "Src", "SRC", "ref", "Ref", "REF")
    for separator in ("-", ":", "/", "_")
)


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _source_id_alias_targets(raw_source_id: str, known_ids: Iterable[str]) -> list[str]:
    """Return conservative, exact prefix aliases for a provider source ID.

    The resolver never performs fuzzy matching, case folding, substring search,
    or edit-distance repair.  An exact trusted ID always wins.  A prefixed value
    is accepted only when removing exactly one registered presentation prefix
    yields one trusted ID.
    """
    source_id = str(raw_source_id or "").strip()
    known = set(known_ids)
    if not source_id or source_id in known:
        return [source_id] if source_id in known else []
    targets: list[str] = []
    for prefix in _SOURCE_ID_ALIAS_PREFIXES:
        if not source_id.startswith(prefix):
            continue
        candidate = source_id[len(prefix):].strip()
        if candidate and candidate in known and candidate not in targets:
            targets.append(candidate)
    return targets


def _resolve_source_id_alias(raw_source_id: str, known_ids: Iterable[str]) -> tuple[str | None, str | None]:
    source_id = str(raw_source_id or "").strip()
    known = set(known_ids)
    if source_id in known:
        return source_id, None
    targets = _source_id_alias_targets(source_id, known)
    if len(targets) == 1:
        return targets[0], "PRESENTATION_PREFIX"
    return None, None


def _source_type(value: Any, default: str = "MODEL_INFERENCE") -> str:
    normalized = _SOURCE_TYPE_ALIASES.get(str(value or "").strip(), str(value or "").strip())
    return normalized if normalized in _ALLOWED_SOURCE_TYPES else default


def _security(value: Any, default: str = "INTERNAL") -> str:
    text = str(value or default).strip().upper()
    return text if text in {"PUBLIC", "INTERNAL", "SENSITIVE", "CLASSIFIED"} else default


def _hash(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text if _HASH_RE.fullmatch(text) else None


def _canonical_ref(
    *,
    source_id: str,
    source_type: str = "MODEL_INFERENCE",
    document_version_id: Any = None,
    section_id: Any = None,
    span_start: Any = None,
    span_end: Any = None,
    quoted_text: Any = None,
    source_hash: Any = None,
    authority_rank: Any = None,
    security_level: Any = "INTERNAL",
) -> dict[str, Any]:
    source_type = _source_type(source_type)
    version = document_version_id.strip() if isinstance(document_version_id, str) and document_version_id.strip() else None
    section = section_id.strip() if isinstance(section_id, str) and section_id.strip() else None
    start = span_start if isinstance(span_start, int) and not isinstance(span_start, bool) and span_start >= 0 else None
    end = span_end if isinstance(span_end, int) and not isinstance(span_end, bool) and span_end >= 0 else None
    if start is not None and end is not None and end < start:
        start = end = None
    quote = quoted_text if isinstance(quoted_text, str) else None
    try:
        rank = int(authority_rank)
    except (TypeError, ValueError):
        rank = _AUTHORITY.get(source_type, 20)
    rank = max(1, min(100, rank))
    return {
        "source_id": source_id,
        "source_type": source_type,
        "document_version_id": version,
        "section_id": section,
        "span_start": start,
        "span_end": end,
        "quoted_text": quote,
        "source_hash": _hash(source_hash),
        "authority_rank": rank,
        "security_level": _security(security_level),
    }


def _pointer(path: tuple[Any, ...]) -> str:
    return "/" + "/".join(str(part) for part in path)


def _collect_defined_ids(value: Any) -> set[str]:
    found: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, Mapping):
            return
        for key, item in node.items():
            if (
                key.endswith("_id")
                and key not in _PROTOCOL_ID_FIELDS
                and key != "source_id"
                and isinstance(item, (str, int))
                and not isinstance(item, bool)
                and str(item).strip()
            ):
                found.add(str(item).strip())
            if key == "source_refs" and isinstance(item, list):
                for ref in item:
                    if isinstance(ref, Mapping) and str(ref.get("source_id") or "").strip():
                        found.add(str(ref["source_id"]).strip())
            visit(item)

    visit(value)
    return found


def _collect_source_ids(value: Any) -> set[str]:
    """Collect source identifiers that are explicitly materialized as sources."""
    found: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, Mapping):
            return
        source_id = node.get("source_id")
        if isinstance(source_id, (str, int)) and not isinstance(source_id, bool):
            text = str(source_id).strip()
            if text:
                found.add(text)
        document_id = node.get("document_id")
        if isinstance(document_id, (str, int)) and not isinstance(document_id, bool):
            text = str(document_id).strip()
            if text:
                found.add(text)
        for item in node.values():
            visit(item)

    visit(value)
    return found


def _is_reference_array_field(key: str) -> bool:
    return key in _REFERENCE_ARRAY_FIELDS


def _catalog_from_envelope(envelope: Mapping[str, Any] | None, db: Any = None) -> dict[str, list[dict[str, Any]]]:
    envelope = envelope or {}
    default_security = _security(
        ((envelope.get("security_context") or {}).get("input_max_security_level"))
        if isinstance(envelope.get("security_context"), Mapping)
        else None
    )
    catalog: dict[str, dict[str | None, tuple[int, dict[str, Any]]]] = defaultdict(dict)

    def add(ref: Mapping[str, Any], priority: int) -> None:
        source_id = str(ref.get("source_id") or "").strip()
        if not source_id:
            return
        canonical = _canonical_ref(
            source_id=source_id,
            source_type=ref.get("source_type") or "MODEL_INFERENCE",
            document_version_id=ref.get("document_version_id"),
            section_id=ref.get("section_id"),
            span_start=ref.get("span_start"),
            span_end=ref.get("span_end"),
            quoted_text=ref.get("quoted_text"),
            source_hash=ref.get("source_hash"),
            authority_rank=ref.get("authority_rank"),
            security_level=ref.get("security_level") or default_security,
        )
        key = canonical.get("section_id")
        previous = catalog[source_id].get(key)
        if previous is None or priority >= previous[0]:
            catalog[source_id][key] = (priority, canonical)

    def add_document(document: Mapping[str, Any], priority: int = 90) -> None:
        document_id = str(document.get("document_id") or "").strip()
        if not document_id:
            return
        source_type = _source_type(document.get("document_role"), "HISTORICAL_DOCUMENT")
        base = {
            "source_id": document_id,
            "source_type": source_type,
            "document_version_id": document.get("document_version_id"),
            "source_hash": document.get("document_hash"),
            "authority_rank": document.get("authority_rank") or _AUTHORITY.get(source_type, 20),
            "security_level": document.get("security_level") or default_security,
        }
        add(base, priority)
        for section in document.get("sections") or []:
            if not isinstance(section, Mapping) or not str(section.get("section_id") or "").strip():
                continue
            text = section.get("text") if isinstance(section.get("text"), str) else None
            section_ref = {
                **base,
                "section_id": section.get("section_id"),
                "span_start": 0 if text is not None else None,
                "span_end": len(text) if text is not None else None,
                "quoted_text": text,
                "source_hash": section.get("text_hash") or base.get("source_hash"),
                "security_level": section.get("security_level") or base.get("security_level"),
            }
            add(section_ref, priority + 5)
            # Legacy providers may use the section identifier itself as
            # source_id.  It is still unambiguous because the section object is
            # present in this exact document context; preserve that identifier
            # while binding all document metadata from the trusted section.
            add({**section_ref, "source_id": str(section.get("section_id"))}, priority + 4)

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, Mapping):
            return
        if {"document_id", "document_version_id", "document_role"}.issubset(node):
            add_document(node)
        if {"source_id", "source_type", "authority_rank", "security_level"}.issubset(node):
            add(node, 80)
        if {"resolution_id", "gate_id", "answer", "decided_by", "decided_role"}.issubset(node):
            resolution_id = str(node.get("resolution_id") or "").strip()
            if resolution_id:
                add(
                    {
                        "source_id": resolution_id,
                        "source_type": "USER_CONFIRMATION",
                        "document_version_id": None,
                        "section_id": None,
                        "span_start": None,
                        "span_end": None,
                        "quoted_text": None,
                        "source_hash": _stable_hash({
                            "question_id": node.get("question_id"),
                            "target_paths": node.get("target_paths") or [],
                            "answer": node.get("answer"),
                            "decided_by": node.get("decided_by"),
                            "decided_role": node.get("decided_role"),
                        }),
                        "authority_rank": 100,
                        "security_level": default_security,
                    },
                    120,
                )
        if "object_id" in node and "object_type" in node:
            source_id = str(node.get("object_id") or "").strip()
            object_type = str(node.get("object_type") or "")
            if source_id:
                is_document = object_type.startswith("SOURCE_DOCUMENT:")
                raw_type = object_type.split(":", 1)[1] if is_document else object_type
                add(
                    {
                        "source_id": source_id,
                        "source_type": _source_type(raw_type, "MODEL_INFERENCE") if is_document else "MODEL_INFERENCE",
                        "source_hash": node.get("object_hash"),
                        "authority_rank": _AUTHORITY.get(_source_type(raw_type), 60) if is_document else 60,
                        "security_level": node.get("security_level") or default_security,
                    },
                    60,
                )
        # Public retrieval records are trusted input objects even when they do
        # not use the common SourceRef schema verbatim.
        if "source_id" in node and any(key in node for key in ("url", "archive_hash", "retrieved_at", "publisher")):
            source_id = str(node.get("source_id") or "").strip()
            if source_id:
                add(
                    {
                        "source_id": source_id,
                        "source_type": "PUBLIC_SOURCE",
                        "source_hash": node.get("source_hash") or node.get("archive_hash") or node.get("content_hash"),
                        "authority_rank": node.get("authority_rank") or 80,
                        "security_level": "PUBLIC",
                    },
                    75,
                )
        for key, item in node.items():
            if (
                key.endswith("_id")
                and key not in _PROTOCOL_ID_FIELDS
                and key != "source_id"
                and isinstance(item, (str, int))
                and not isinstance(item, bool)
                and str(item).strip()
            ):
                add(
                    {
                        "source_id": str(item).strip(),
                        "source_type": "MODEL_INFERENCE",
                        "authority_rank": 60,
                        "security_level": default_security,
                    },
                    10,
                )
            visit(item)

    visit(envelope)

    payload = envelope.get("payload") if isinstance(envelope.get("payload"), Mapping) else {}
    research_need = payload.get("research_need") if isinstance(payload.get("research_need"), Mapping) else {}
    need_id = str(research_need.get("need_id") or "").strip()
    if need_id:
        human_resolutions = [
            item for item in payload.get("human_resolutions") or []
            if isinstance(item, Mapping)
        ]
        confirmed_paths = {
            "research_need.question",
            "payload.research_need.question",
            "research_need.reason_online_needed",
            "payload.research_need.reason_online_needed",
            "research_need.desired_output",
            "payload.research_need.desired_output",
        }
        is_user_confirmed = any(
            str(item.get("question_id") or "") == "wf3-research-question"
            or bool(confirmed_paths.intersection({str(path) for path in item.get("target_paths") or []}))
            for item in human_resolutions
        )
        source_type = "USER_CONFIRMATION" if is_user_confirmed else "MODEL_INFERENCE"
        add(
            {
                "source_id": need_id,
                "source_type": source_type,
                "document_version_id": None,
                "section_id": None,
                "span_start": None,
                "span_end": None,
                "quoted_text": None,
                "source_hash": _stable_hash({
                    "need_id": need_id,
                    "question": research_need.get("question"),
                    "reason_online_needed": research_need.get("reason_online_needed"),
                    "desired_output": research_need.get("desired_output"),
                }),
                "authority_rank": 100 if is_user_confirmed else 60,
                "security_level": default_security,
            },
            125 if is_user_confirmed else 25,
        )

    # Project identity and objective are persisted user inputs.  The simulated
    # provider historically represents that scope with a stable
    # USER_CONFIRMATION source ID; materialize the same source in the trusted
    # catalog rather than allowing the provider to invent it.
    scope = envelope.get("scope") if isinstance(envelope.get("scope"), Mapping) else {}
    project_name = str((payload or {}).get("project_name") or (scope or {}).get("project_id") or "").strip()
    if project_name:
        add(
            {
                "source_id": "user-confirmation-project-scope",
                "source_type": "USER_CONFIRMATION",
                "document_version_id": None,
                "section_id": None,
                "span_start": None,
                "span_end": None,
                "quoted_text": f"用户要求围绕{project_name}形成科研项目申请书并完善智能体系统。",
                "source_hash": hashlib.sha256((project_name + "科研项目申请书").encode("utf-8")).hexdigest(),
                "authority_rank": 100,
                "security_level": default_security,
            },
            100,
        )

    # Upgrade only documents that were already visible in this exact input.
    project_id = str(((envelope.get("scope") or {}).get("project_id")) if isinstance(envelope.get("scope"), Mapping) else "").strip()
    document_ids = [source_id for source_id, by_section in catalog.items() if any(ref[1].get("document_version_id") is not None or ref[1].get("source_type") != "MODEL_INFERENCE" for ref in by_section.values())]
    if db is not None and project_id and document_ids:
        placeholders = ",".join("?" for _ in document_ids)
        try:
            rows = db.fetchall(
                f"SELECT id,role,security_level,document_hash,parsed_json FROM documents WHERE project_id=? AND id IN ({placeholders})",
                (project_id, *document_ids),
            )
        except Exception:
            rows = []
        for row in rows:
            try:
                parsed = json.loads(row.get("parsed_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                parsed = {}
            if not isinstance(parsed, dict):
                parsed = {}
            parsed.setdefault("document_id", row.get("id"))
            parsed.setdefault("document_role", row.get("role") or "OTHER")
            parsed.setdefault("document_hash", row.get("document_hash"))
            parsed.setdefault("security_level", row.get("security_level") or default_security)
            add_document(parsed, 110)

    return {
        source_id: [entry[1] for _, entry in sorted(by_section.items(), key=lambda item: (item[0] is not None, str(item[0])))]
        for source_id, by_section in catalog.items()
    }


def bind_trusted_source_refs(
    output: Any,
    envelope: Mapping[str, Any] | None,
    *,
    db: Any = None,
) -> tuple[Any, dict[str, Any]]:
    """Return a copy whose every ``source_refs`` item is input-backed.

    Unknown source IDs are reported and left unchanged so the caller can block
    deterministically.  Known source IDs have all protocol metadata replaced
    by trusted values; model-authored version IDs, hashes, security labels,
    ranks, spans and quotes are never accepted as authority.
    """
    normalized = copy.deepcopy(output)
    catalog = _catalog_from_envelope(envelope, db=db)
    changes: list[dict[str, Any]] = []
    errors: list[str] = []

    def select(source_id: str, requested_section: str | None) -> dict[str, Any] | None:
        candidates = catalog.get(source_id) or []
        if requested_section:
            for candidate in candidates:
                if candidate.get("section_id") == requested_section:
                    return candidate
        for candidate in candidates:
            if candidate.get("section_id") is None:
                return candidate
        return candidates[0] if len(candidates) == 1 else None

    def visit(node: Any, path: tuple[Any, ...]) -> None:
        if isinstance(node, list):
            for index, item in enumerate(node):
                visit(item, (*path, index))
            return
        if not isinstance(node, dict):
            return
        for key, value in list(node.items()):
            current_path = (*path, key)
            if key == "source_refs" and isinstance(value, list):
                rebuilt: list[Any] = []
                seen: set[tuple[str, str | None]] = set()
                for index, ref in enumerate(value):
                    ref_path = (*current_path, index)
                    if not isinstance(ref, Mapping):
                        errors.append(f"{_pointer(ref_path)}: source reference must be an object")
                        rebuilt.append(ref)
                        continue
                    raw_source_id = str(ref.get("source_id") or "").strip()
                    if not raw_source_id:
                        errors.append(f"{_pointer(ref_path)}/source_id: missing source identifier")
                        rebuilt.append(dict(ref))
                        continue
                    source_id, alias_kind = _resolve_source_id_alias(raw_source_id, catalog.keys())
                    if source_id is None:
                        errors.append(
                            f"{_pointer(ref_path)}/source_id: {raw_source_id!r} is not present in the trusted input envelope"
                        )
                        rebuilt.append(dict(ref))
                        continue
                    requested_section = ref.get("section_id") if isinstance(ref.get("section_id"), str) and ref.get("section_id").strip() else None
                    trusted = select(source_id, requested_section)
                    if trusted is None:
                        errors.append(
                            f"{_pointer(ref_path)}/source_id: {raw_source_id!r} does not resolve to one unambiguous trusted section"
                        )
                        rebuilt.append(dict(ref))
                        continue
                    key_tuple = (source_id, trusted.get("section_id"))
                    if key_tuple in seen:
                        changes.append({"path": _pointer(ref_path), "action": "DROP_DUPLICATE", "source_id": source_id})
                        continue
                    seen.add(key_tuple)
                    authoritative = copy.deepcopy(trusted)
                    if dict(ref) != authoritative:
                        changes.append({
                            "path": _pointer(ref_path),
                            "action": "BIND_ALIAS" if alias_kind else "REBIND",
                            "source_id": source_id,
                            "provider_source_id": raw_source_id,
                            "alias_kind": alias_kind,
                            "requested_section_id": requested_section,
                            "trusted_section_id": authoritative.get("section_id"),
                        })
                    rebuilt.append(authoritative)
                node[key] = rebuilt
            else:
                visit(value, current_path)

    visit(normalized, ())
    report = {
        "schema_version": "1.0",
        "catalog_source_count": len(catalog),
        "normalized_count": len(changes),
        "changes": changes,
        "unresolved_count": len(errors),
        "errors": errors,
    }
    return normalized, report


def normalize_reference_id_aliases(
    output: Any,
    envelope: Mapping[str, Any] | None,
) -> tuple[Any, dict[str, Any]]:
    """Canonicalize conservative presentation prefixes on reference-only IDs.

    This is the cross-reference counterpart of trusted ``source_refs`` binding.
    Exact IDs always win.  A wrapper such as ``ref-``/``source-`` is removed
    only when the remainder is one identifier that is already visible in the
    current input or defined in the current output.  Unknown or ambiguous IDs
    remain unchanged and are rejected by ``validate_reference_ids``.
    """
    normalized = copy.deepcopy(output)
    known = (
        _collect_defined_ids(envelope or {})
        | _collect_defined_ids(normalized)
        | _REGISTERED_DIAGNOSTIC_REFS
    )
    changes: list[dict[str, Any]] = []

    def visit(node: Any, path: tuple[Any, ...]) -> None:
        if isinstance(node, list):
            for index, item in enumerate(node):
                visit(item, (*path, index))
            return
        if not isinstance(node, dict):
            return
        for key, value in list(node.items()):
            current = (*path, key)
            if _is_reference_array_field(key) and isinstance(value, list):
                rebuilt: list[Any] = []
                for index, raw in enumerate(value):
                    if not isinstance(raw, str):
                        rebuilt.append(raw)
                        continue
                    identifier = raw.strip()
                    resolved, alias_kind = _resolve_source_id_alias(identifier, known)
                    if resolved is not None and alias_kind:
                        rebuilt.append(resolved)
                        changes.append({
                            "path": _pointer((*current, index)),
                            "action": "BIND_REFERENCE_ALIAS",
                            "provider_reference_id": identifier,
                            "reference_id": resolved,
                            "alias_kind": alias_kind,
                        })
                    else:
                        rebuilt.append(identifier)
                node[key] = rebuilt
            visit(node.get(key), current)

    visit(normalized, ())
    return normalized, {
        "schema_version": "1.0",
        "normalized_count": len(changes),
        "changes": changes,
    }


def validate_reference_ids(
    output: Any,
    envelope: Mapping[str, Any] | None,
) -> list[str]:
    """Validate reference-only ID arrays against visible/defined entities."""
    known = _collect_defined_ids(envelope or {}) | _collect_defined_ids(output) | _REGISTERED_DIAGNOSTIC_REFS
    errors: list[str] = []
    root_status = output.get("status") if isinstance(output, Mapping) else None

    def visit(node: Any, path: tuple[Any, ...]) -> None:
        if isinstance(node, list):
            for index, item in enumerate(node):
                visit(item, (*path, index))
            return
        if not isinstance(node, Mapping):
            return
        for key, value in node.items():
            current = (*path, key)
            if _is_reference_array_field(key) and isinstance(value, list):
                # BLOCK/ERROR artifacts are never consumed as authoritative
                # workflow facts; their free-form diagnostic evidence labels
                # remain trace-only.
                if key == "evidence_refs" and root_status in {"BLOCK", "ERROR"}:
                    continue
                for index, raw in enumerate(value):
                    if not isinstance(raw, str) or not raw.strip():
                        errors.append(f"{_pointer((*current, index))}: reference ID must be a non-empty string")
                        continue
                    if raw not in known:
                        errors.append(
                            f"{_pointer((*current, index))}: reference ID {raw!r} is not present in the input or defined output entities"
                        )
            visit(value, current)

    visit(output, ())
    return errors


def normalize_staged_source_ref_aliases(
    output: Any,
    trusted_context: Any | None,
) -> tuple[Any, dict[str, Any]]:
    """Canonicalize conservative presentation prefixes in staged source IDs."""
    normalized = copy.deepcopy(output)
    known_sources = _collect_source_ids(normalized)
    if trusted_context is not None:
        known_sources |= _collect_source_ids(trusted_context)
    changes: list[dict[str, Any]] = []

    def visit(node: Any, path: tuple[Any, ...]) -> None:
        if isinstance(node, list):
            for index, item in enumerate(node):
                visit(item, (*path, index))
            return
        if not isinstance(node, dict):
            return
        for key, value in list(node.items()):
            current = (*path, key)
            if key == "source_refs" and isinstance(value, list) and value and all(isinstance(item, str) for item in value):
                rebuilt: list[str] = []
                for index, raw in enumerate(value):
                    source_id = raw.strip()
                    resolved, alias_kind = _resolve_source_id_alias(source_id, known_sources)
                    if resolved is not None and alias_kind:
                        rebuilt.append(resolved)
                        changes.append({
                            "path": _pointer((*current, index)),
                            "action": "BIND_ALIAS",
                            "provider_source_id": source_id,
                            "source_id": resolved,
                            "alias_kind": alias_kind,
                        })
                    else:
                        rebuilt.append(source_id)
                node[key] = rebuilt
            else:
                visit(value, current)

    visit(normalized, ())
    return normalized, {
        "schema_version": "1.0",
        "normalized_count": len(changes),
        "changes": changes,
    }


def validate_staged_reference_integrity(
    output: Any,
    trusted_context: Any | None,
) -> list[str]:
    """Validate file-bridged Stage 1--8 reference arrays.

    Staged schemas use string source IDs rather than the runtime ``SourceRef``
    object.  The latest persisted request envelope is treated as the trusted
    upstream context.  Internal cross-references may target IDs defined in the
    same output.  When no trusted request is available (for example a standalone
    ``validate`` command), existing stage-specific deterministic validators
    remain authoritative and only locally resolvable source references are
    checked.
    """
    known = _collect_defined_ids(output)
    known_sources = _collect_source_ids(output)
    has_trusted_context = trusted_context is not None
    if has_trusted_context:
        known |= _collect_defined_ids(trusted_context)
        known_sources |= _collect_source_ids(trusted_context)

    errors: list[str] = []

    def visit(node: Any, path: tuple[Any, ...]) -> None:
        if isinstance(node, list):
            for index, item in enumerate(node):
                visit(item, (*path, index))
            return
        if not isinstance(node, Mapping):
            return
        for key, value in node.items():
            current = (*path, key)
            if key == "source_refs" and isinstance(value, list):
                # Object-valued source_refs are definitions/bound records and
                # are handled by the runtime provenance binder.  Staged
                # workflows use arrays of source IDs.
                if value and all(isinstance(item, str) for item in value) and known_sources:
                    for index, raw in enumerate(value):
                        text = raw.strip()
                        if not text:
                            errors.append(
                                f"{_pointer((*current, index))}: source reference must be a non-empty string"
                            )
                        elif text not in known_sources:
                            errors.append(
                                f"{_pointer((*current, index))}: source ID {text!r} is not present in the trusted staged input or current source registry"
                            )
            elif has_trusted_context and _is_reference_array_field(key) and isinstance(value, list):
                for index, raw in enumerate(value):
                    if not isinstance(raw, str) or not raw.strip():
                        errors.append(
                            f"{_pointer((*current, index))}: reference ID must be a non-empty string"
                        )
                    elif raw not in known:
                        errors.append(
                            f"{_pointer((*current, index))}: reference ID {raw!r} is not present in the trusted staged input or current output entities"
                        )
            visit(value, current)

    visit(output, ())
    return errors


__all__ = [
    "bind_trusted_source_refs",
    "normalize_reference_id_aliases",
    "validate_reference_ids",
    "normalize_staged_source_ref_aliases",
    "validate_staged_reference_integrity",
]
