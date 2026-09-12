from __future__ import annotations

import copy
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .gate_answer_contract import widen_gate_questions
from .output_integrity import build_trusted_source_catalog
from .util import sha256_json


WF3_PRODUCER_PROMPTS = frozenset(
    {
        "P-SAFE-ONLINE-PACKAGE",
        "P-PUBLIC-RESEARCH-PLAN",
        "P-PUBLIC-RESEARCH-SYNTHESIS",
    }
)
WF3_RESEARCH_CRITIC = "P-PUBLIC-RESEARCH-CRITIC"
WF3_MODEL_PROMPTS = frozenset(
    {
        "P-SAFE-ONLINE-PACKAGE",
        "P-SAFE-ONLINE-PACKAGE-CRITIC",
        "P-PUBLIC-RESEARCH-PLAN",
        "P-PUBLIC-RESEARCH-PLAN-SCOPE-CRITIC",
        "P-PUBLIC-RESEARCH-SYNTHESIS",
        "P-PUBLIC-RESEARCH-CRITIC",
        "P-ONLINE-RESULT-IMPORT-CRITIC",
    }
)

# This table is executable documentation for the WF-3 ownership boundary.  It
# intentionally describes the current canonical schema without changing it:
# the provider may still have to emit compatibility placeholders, but the
# runtime never trusts provider-authored values for the non-semantic classes.
WF3_FIELD_OWNERSHIP: dict[str, dict[str, tuple[str, ...]]] = {
    "P-SAFE-ONLINE-PACKAGE": {
        "MODEL_SEMANTIC": (
            "result.task_description",
            "result.queries",
            "result.allowed_context",
            "result.prohibited_inferences",
            "result.prohibited_outputs",
        ),
        "RUNTIME_DERIVED": (
            "result.package_id",
            "result.entity_placeholders",
            "result.valid_until",
            "protocol identity",
            "finding/question IDs",
            "source_refs",
        ),
        "INPUT_COPIED": ("result.task_type", "result.removed_fields", "allowed topic boundary"),
        "GUARD_CONTROLLED": ("result.security_level", "source metadata", "Gate controls"),
    },
    "P-SAFE-ONLINE-PACKAGE-CRITIC": {
        "MODEL_SEMANTIC": ("result.reidentification_risk", "result.required_redactions"),
        "RUNTIME_DERIVED": (
            "protocol identity",
            "finding/question IDs",
            "status/verdict",
            "source_refs",
        ),
        "INPUT_COPIED": ("review subject references",),
        "GUARD_CONTROLLED": ("prohibited-field receipt", "Gate controls"),
    },
    "P-PUBLIC-RESEARCH-PLAN": {
        "MODEL_SEMANTIC": (
            "result.research_questions",
            "result.queries",
            "result.source_priorities",
        ),
        "RUNTIME_DERIVED": (
            "result.plan_id",
            "result.queries[].query_id",
            "result.binding_contract_version",
            "protocol identity",
            "finding/question IDs",
            "source_refs",
        ),
        "INPUT_COPIED": (
            "result.task_type",
            "result.time_scope",
            "result.evidence_requirements",
            "result.prohibited_inferences",
            "topic boundary",
        ),
        "GUARD_CONTROLLED": ("Gate controls",),
    },
    "P-PUBLIC-RESEARCH-PLAN-SCOPE-CRITIC": {
        "MODEL_SEMANTIC": ("query scope issues",),
        "RUNTIME_DERIVED": (
            "result.verdict",
            "result.approved_query_indexes",
            "result.rejected_query_indexes",
            "finding IDs",
            "severity/blocking/route/status",
        ),
        "INPUT_COPIED": ("approved boundary", "research questions", "executable queries"),
        "GUARD_CONTROLLED": ("workflow routing", "Gate controls"),
    },
    "PUBLIC-RESEARCH-SEARCH": {
        "MODEL_SEMANTIC": (),
        "RUNTIME_DERIVED": ("archive identity", "coverage report", "query/source bindings"),
        "INPUT_COPIED": ("approved plan and queries",),
        "GUARD_CONTROLLED": ("source hash", "source metadata", "security labels"),
    },
    "P-PUBLIC-RESEARCH-SYNTHESIS": {
        "MODEL_SEMANTIC": (
            "result.claims",
            "result.source_comparisons",
            "result.conflicts",
            "result.limitations",
            "result.coverage_summary",
        ),
        "RUNTIME_DERIVED": (
            "result.claims[].claim_id",
            "protocol identity",
            "status",
            "source_refs",
        ),
        "INPUT_COPIED": ("result.claims[].source_refs[].source_id",),
        "GUARD_CONTROLLED": ("source metadata", "security_level", "Gate controls"),
    },
    "P-PUBLIC-RESEARCH-CRITIC": {
        "MODEL_SEMANTIC": (
            "semantic evidence-support issues",
            "result.missing_counterevidence_topics",
        ),
        "RUNTIME_DERIVED": (
            "result.source_quality_summary",
            "result.unsupported_claim_ids",
            "finding/question IDs",
            "canonical finding/question paths",
            "capability route",
            "status/verdict",
            "source_refs",
        ),
        "INPUT_COPIED": ("canonical source/claim IDs",),
        "GUARD_CONTROLLED": ("hash/existence/coverage report", "Gate controls"),
    },
    "P-ONLINE-RESULT-IMPORT-CRITIC": {
        "MODEL_SEMANTIC": (
            "claim import decisions",
            "semantic import security issues",
        ),
        "RUNTIME_DERIVED": (
            "result.import_recommendation",
            "result.accepted_claim_ids",
            "result.reference_only_claim_ids",
            "result.rejected_claim_ids",
            "finding/question IDs",
            "result.required_user_confirmations",
            "status",
            "source_refs",
        ),
        "INPUT_COPIED": ("result accepted/reference-only/rejected canonical claim IDs",),
        "GUARD_CONTROLLED": ("injection/scope report", "source manifest", "Gate controls"),
    },
}


# These are the canonical business inputs actually consumed by each WF-3 model
# node.  Their trusted identities stay in the local validation envelope; the
# provider does not need to echo opaque catalog IDs.  The runtime projects the
# corresponding top-level provenance set after semantic generation.
WF3_PROVENANCE_PAYLOAD_FIELDS: dict[str, tuple[str, ...]] = {
    "P-SAFE-ONLINE-PACKAGE": (
        "research_need",
        "source_items",
        "security_policy",
        "allowed_topics",
        "prohibited_fields",
        "human_resolutions",
    ),
    "P-SAFE-ONLINE-PACKAGE-CRITIC": (
        "package_candidate",
        "source_summary",
        "security_policy",
        "deterministic_scan",
    ),
    "P-PUBLIC-RESEARCH-PLAN": (
        "safe_online_package",
        "time_constraints",
        "known_public_sources",
        "evidence_requirements",
    ),
    "P-PUBLIC-RESEARCH-PLAN-SCOPE-CRITIC": (
        "approved_boundary",
        "research_questions",
        "executable_queries",
    ),
    "P-PUBLIC-RESEARCH-SYNTHESIS": (
        "research_plan",
        "retrieved_sources",
        "extracted_passages",
        "safe_online_package",
    ),
    "P-PUBLIC-RESEARCH-CRITIC": (
        "research_plan",
        "synthesis_candidate",
        "retrieved_sources",
        "safe_online_package",
    ),
    "P-ONLINE-RESULT-IMPORT-CRITIC": (
        "approved_safe_package",
        "result_package",
        "public_sources",
        "transfer_manifest",
        "security_policy",
    ),
}


# Character budgets apply to the complete provider-visible request (system
# instructions plus compact JSON envelope).  They are deliberately per-node:
# a short Plan must not inherit the much larger Synthesis/Critic allowance.
WF3_PROVIDER_REQUEST_CHAR_BUDGETS: dict[str, int] = {
    "P-SAFE-ONLINE-PACKAGE": 65_000,
    "P-SAFE-ONLINE-PACKAGE-CRITIC": 75_000,
    "P-PUBLIC-RESEARCH-PLAN": 60_000,
    "P-PUBLIC-RESEARCH-PLAN-SCOPE-CRITIC": 35_000,
    "P-PUBLIC-RESEARCH-SYNTHESIS": 95_000,
    "P-PUBLIC-RESEARCH-CRITIC": 105_000,
    "P-ONLINE-RESULT-IMPORT-CRITIC": 105_000,
    # WF-1 semantic contracts.  The semantic projection removes the protocol
    # bloat (a 2.3K-char DASH brief now yields a ~6.4K request instead of
    # ~240K), so what remains scales with real source-document content.
    # Simulated fixtures with large briefs measure ~100K provider-visible
    # chars; budgets keep ~2x headroom above that while still catching
    # runaway protocol overhead.
    "P-SCHEME-EXTRACT": 120_000,
    "P-SCHEME-CRITIC": 120_000,
    "P-PROJECT-DEFINITION-EXTRACT": 200_000,
    "P-PROJECT-DEFINITION-CRITIC": 200_000,
}



class WF3PreModelGuardError(ValueError):
    """Deterministic WF-3 precondition failed before any provider call.

    ``guard_kind`` distinguishes an internal contract/pipeline defect from a
    content-level deterministic safety finding.  The runtime failure classifier
    uses these stable attributes without importing this module, which keeps the
    dependency direction acyclic.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        guard_kind: str = "CONTRACT",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.wf3_guard_code = str(code)
        self.wf3_guard_kind = str(guard_kind).upper()
        self.wf3_guard_details = dict(details or {})


def enforce_wf3_pre_model_guards(
    prompt_id: str,
    canonical_envelope: Mapping[str, Any],
) -> None:
    """Enforce deterministic WF-3 invariants before an LLM/provider call.

    A model may assess semantic risk, but it must never decide whether an
    authoritative boundary exists or whether a deterministic sensitive-value
    scan passed.  Those are runtime facts.
    """

    if prompt_id not in WF3_MODEL_PROMPTS:
        return
    payload = (
        canonical_envelope.get("payload")
        if isinstance(canonical_envelope.get("payload"), Mapping)
        else {}
    )

    if prompt_id == "P-SAFE-ONLINE-PACKAGE":
        allowed_topics = [
            str(item).strip()
            for item in payload.get("allowed_topics") or []
            if str(item).strip()
        ]
        if not allowed_topics:
            raise WF3PreModelGuardError(
                "WF3_APPROVED_BOUNDARY_MISSING",
                "Safe Package Producer has no authoritative allowed_topics boundary",
                guard_kind="CONTRACT",
            )
        return

    if prompt_id == "P-PUBLIC-RESEARCH-PLAN-SCOPE-CRITIC":
        boundary = payload.get("approved_boundary") if isinstance(payload.get("approved_boundary"), Mapping) else {}
        allowed_topics = [str(item).strip() for item in boundary.get("allowed_topics") or [] if str(item).strip()]
        queries = [item for item in payload.get("executable_queries") or [] if isinstance(item, Mapping) and str(item.get("query") or "").strip()]
        if not allowed_topics:
            raise WF3PreModelGuardError(
                "WF3_APPROVED_BOUNDARY_MISSING",
                "Research Plan scope guard has no authoritative allowed_topics boundary",
                guard_kind="CONTRACT",
            )
        if not queries:
            raise WF3PreModelGuardError(
                "WF3_EXECUTABLE_QUERY_SET_MISSING",
                "Research Plan scope guard received no executable queries",
                guard_kind="CONTRACT",
            )
        return

    if prompt_id == "P-SAFE-ONLINE-PACKAGE-CRITIC":
        allowed_topics = [
            str(item).strip()
            for item in payload.get("allowed_topics") or []
            if str(item).strip()
        ]
        if not allowed_topics:
            raise WF3PreModelGuardError(
                "WF3_APPROVED_BOUNDARY_MISSING",
                "Safe Package Critic lost the workflow-approved topic boundary",
                guard_kind="CONTRACT",
            )
        scan = (
            payload.get("deterministic_scan")
            if isinstance(payload.get("deterministic_scan"), Mapping)
            else {}
        )
        if scan.get("passed") is not True:
            matched = [str(item) for item in scan.get("matched_rules") or [] if str(item)]
            raise WF3PreModelGuardError(
                "WF3_DETERMINISTIC_SCAN_FAILED",
                "deterministic outbound scan failed; semantic Critic must not override it",
                guard_kind="CONTENT",
                details={"matched_rules": matched[:32]},
            )


WF3_RUNTIME_ONLY_TARGET_PREFIXES = (
    "/security_context",
    "/freshness",
    "/task",
    "/scope",
    "/trusted_source_catalog",
    "/payload/source_summary",
    "/payload/security_policy",
    "/payload/deterministic_scan",
    "/payload/package_candidate/valid_until",
    "/payload/transfer_manifest",
    "/payload/result_package/request_hash",
    "/payload/result_package/manifest_hash",
    "/result/valid_until",
)


def wf3_safe_package_ttl_days() -> int:
    raw = str(os.getenv("WF3_SAFE_PACKAGE_TTL_DAYS", "7") or "7").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("WF3_SAFE_PACKAGE_TTL_DAYS must be an integer between 1 and 30") from exc
    if value < 1 or value > 30:
        raise ValueError("WF3_SAFE_PACKAGE_TTL_DAYS must be between 1 and 30")
    return value


def wf3_safe_package_valid_until() -> str:
    return (datetime.now(timezone.utc).date() + timedelta(days=wf3_safe_package_ttl_days())).isoformat()


def _wf3_runtime_only_target(value: Any) -> bool:
    target = _canonical_target_pointer(value)
    return any(
        target == prefix or target.startswith(prefix + "/")
        for prefix in WF3_RUNTIME_ONLY_TARGET_PREFIXES
    )

def _stable_machine_id(prefix: str, value: Any, index: int) -> str:
    # The identity must survive harmless array reordering.  ``index`` remains
    # in the signature for call-site compatibility, but is not identity input.
    return f"{prefix}-{sha256_json({'value': value})[:20]}"


def wf3_provider_request_budget_report(
    prompt_id: str,
    system_prompt: str,
    provider_envelope: Mapping[str, Any],
) -> dict[str, Any] | None:
    limit = WF3_PROVIDER_REQUEST_CHAR_BUDGETS.get(prompt_id)
    if limit is None:
        return None
    system_chars = len(system_prompt)
    envelope_chars = len(
        json.dumps(provider_envelope, ensure_ascii=False, separators=(",", ":"))
    )
    total = system_chars + envelope_chars
    return {
        "prompt_id": prompt_id,
        "system_prompt_chars": system_chars,
        "provider_envelope_chars": envelope_chars,
        "provider_visible_chars": total,
        "estimated_input_tokens": (total + 3) // 4,
        "limit_chars": limit,
        "within_budget": total <= limit,
    }


def _canonical_evidence_ref(
    value: Any,
    envelope: Mapping[str, Any] | None = None,
) -> str:
    raw = str(value or "").strip()
    if raw.startswith("/"):
        raw = raw[1:].replace("/", ".")
    raw = re.sub(r"\[(\d+)\]", r".\1", raw)
    if raw.startswith("payload.") and isinstance(envelope, Mapping):
        parts = raw.split(".")
        if len(parts) >= 2:
            owner_path = ".".join(parts[:2])
            owner = next(
                (
                    entry
                    for entry in build_trusted_source_catalog(envelope)
                    if str(entry.get("object_path") or "") == owner_path
                ),
                None,
            )
            owner_id = str((owner or {}).get("source_id") or "").strip()
            if owner_id:
                suffix = ".".join(parts[2:])
                return owner_id + (f".{suffix}" if suffix else "")
    return raw


def _canonical_target_pointer(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return raw
    if raw.startswith("/"):
        return raw
    raw = re.sub(r"\[(\d+)\]", r".\1", raw)
    return "/" + "/".join(part for part in raw.split(".") if part)


def _wf3_runtime_source_refs(
    prompt_id: str,
    envelope: Mapping[str, Any] | None,
) -> list[dict[str, str]]:
    """Return the trusted top-level inputs actually consumed by a WF-3 node.

    ``trusted_source_catalog`` is validation-only context and is intentionally
    not sent to the provider. Consequently the provider cannot author these
    opaque identifiers correctly. Bind the node's declared business inputs
    here and let the shared provenance binder add their trusted metadata.
    """

    fields = WF3_PROVENANCE_PAYLOAD_FIELDS.get(prompt_id, ())
    if not fields or not isinstance(envelope, Mapping):
        return []
    catalog_by_path = {
        str(entry.get("object_path") or ""): entry
        for entry in build_trusted_source_catalog(envelope)
        if isinstance(entry, Mapping)
    }
    refs: list[dict[str, str]] = []
    seen: set[str] = set()
    for field in fields:
        entry = catalog_by_path.get(f"payload.{field}")
        source_id = str((entry or {}).get("source_id") or "").strip()
        if source_id and source_id not in seen:
            refs.append({"source_id": source_id})
            seen.add(source_id)
    return refs


def canonicalize_wf3_machine_fields(
    prompt_id: str,
    output: dict[str, Any],
    envelope: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Project model semantics into stable WF-3 machine/control fields.

    This function does not invent research content. It owns identifiers,
    input-copied constants, source-path spelling, Gate-control representation
    and internal finding namespace fields that the runtime can derive exactly.
    """

    if prompt_id not in WF3_MODEL_PROMPTS:
        return output, None
    normalized = copy.deepcopy(output)
    changes: list[str] = []
    payload = (
        dict((envelope or {}).get("payload") or {})
        if isinstance((envelope or {}).get("payload"), Mapping)
        else {}
    )
    result = normalized.get("result")
    if isinstance(result, dict):
        if prompt_id == "P-SAFE-ONLINE-PACKAGE":
            need = payload.get("research_need") if isinstance(payload.get("research_need"), Mapping) else {}
            package_id = _stable_machine_id(
                "safe-package",
                {
                    "need_id": need.get("need_id"),
                    "question": need.get("question"),
                    "task_type": payload.get("target_task_type"),
                },
                0,
            )
            for field, value in (
                ("package_id", package_id),
                ("task_type", str(payload.get("target_task_type") or "PUBLIC_RESEARCH")),
                ("valid_until", wf3_safe_package_valid_until()),
                ("security_level", "PUBLIC"),
            ):
                if result.get(field) != value:
                    result[field] = value
                    changes.append(f"/result/{field}")
        elif prompt_id == "P-SAFE-ONLINE-PACKAGE-CRITIC":
            policy = payload.get("security_policy") if isinstance(payload.get("security_policy"), Mapping) else {}
            checked = [
                str(item)
                for item in policy.get("prohibited_external_fields") or []
                if str(item).strip()
            ]
            if result.get("checked_prohibited_fields") != checked:
                result["checked_prohibited_fields"] = checked
                changes.append("/result/checked_prohibited_fields")
        elif prompt_id == "P-PUBLIC-RESEARCH-PLAN":
            plan_id = _stable_machine_id(
                "research-plan",
                {
                    "safe_package": payload.get("safe_online_package"),
                    "research_questions": result.get("research_questions"),
                },
                0,
            )
            if result.get("plan_id") != plan_id:
                result["plan_id"] = plan_id
                changes.append("/result/plan_id")
            task_type = str(payload.get("task_type") or result.get("task_type") or "PUBLIC_RESEARCH")
            if result.get("task_type") != task_type:
                result["task_type"] = task_type
                changes.append("/result/task_type")
            if result.get("binding_contract_version") != "1.0":
                result["binding_contract_version"] = "1.0"
                changes.append("/result/binding_contract_version")
            constraints = (
                payload.get("time_constraints")
                if isinstance(payload.get("time_constraints"), Mapping)
                else {}
            )
            start_date = str(constraints.get("start_date") or "").strip()
            end_date = str(constraints.get("end_date") or "").strip()
            runtime_time_scope = (
                f"{start_date}/{end_date}" if start_date and end_date else None
            )
            if result.get("time_scope") != runtime_time_scope:
                result["time_scope"] = runtime_time_scope
                changes.append("/result/time_scope")
            for index, query in enumerate(result.get("queries") or []):
                if not isinstance(query, dict):
                    continue
                query_id = _stable_machine_id(
                    "query",
                    {
                        "query": query.get("query"),
                        "linked_question_indexes": query.get("linked_question_indexes"),
                    },
                    index,
                )
                if query.get("query_id") != query_id:
                    query["query_id"] = query_id
                    changes.append(f"/result/queries/{index}/query_id")
        elif prompt_id == "P-PUBLIC-RESEARCH-SYNTHESIS":
            for index, claim in enumerate(result.get("claims") or []):
                if not isinstance(claim, dict):
                    continue
                claim_id = _stable_machine_id(
                    "public-claim",
                    {
                        key: claim.get(key)
                        for key in (
                            "claim_text",
                            "claim_type",
                            "subject_id",
                            "temporal_status",
                            "qualifiers",
                        )
                    },
                    index,
                )
                if claim.get("claim_id") != claim_id:
                    claim["claim_id"] = claim_id
                    changes.append(f"/result/claims/{index}/claim_id")
                if claim.get("security_level") != "PUBLIC":
                    claim["security_level"] = "PUBLIC"
                    changes.append(f"/result/claims/{index}/security_level")

    # Top-level provenance describes which trusted input objects this node
    # consumed. It is runtime-owned: provider-authored path aliases, invented
    # identifiers, or an empty compatibility placeholder must never decide it.
    if isinstance(envelope, Mapping):
        runtime_source_refs = _wf3_runtime_source_refs(prompt_id, envelope)
        if normalized.get("source_refs") != runtime_source_refs:
            normalized["source_refs"] = runtime_source_refs
            changes.append("/source_refs")

    for index, finding in enumerate(normalized.get("findings") or []):
        if not isinstance(finding, dict):
            continue
        finding_code = str(finding.get("code") or "").upper()
        runtime_only_target = _wf3_runtime_only_target(finding.get("target_path_or_span"))
        if runtime_only_target:
            for field, value in (
                ("blocking", False),
                ("severity", "P2"),
                ("category", "SYSTEM"),
                ("repairable", False),
                ("suggested_route", "BLOCK"),
                ("repair_instruction", "由运行时确定性检查处理，不向用户提问。"),
            ):
                if finding.get(field) != value:
                    finding[field] = value
                    changes.append(f"/findings/{index}/{field}")
        if (
            not runtime_only_target
            and prompt_id == "P-ONLINE-RESULT-IMPORT-CRITIC"
            and finding_code == "IMPORT_PROMPT_INJECTION"
        ):
            for field, value in (
                ("blocking", True),
                ("severity", "P0"),
                ("repairable", False),
                ("suggested_route", "BLOCK"),
            ):
                if finding.get(field) != value:
                    finding[field] = value
                    changes.append(f"/findings/{index}/{field}")
        finding_id = _stable_machine_id(
            "wf3-finding",
            {
                key: finding.get(key)
                for key in ("code", "category", "target_type", "target_path_or_span", "description")
            },
            index,
        )
        if finding.get("finding_instance_id") != finding_id:
            finding["finding_instance_id"] = finding_id
            changes.append(f"/findings/{index}/finding_instance_id")
        if finding.get("defect_namespace") != "SEMANTIC_OBSERVATION":
            finding["defect_namespace"] = "SEMANTIC_OBSERVATION"
            changes.append(f"/findings/{index}/defect_namespace")
        target = finding.get("target_path_or_span")
        if isinstance(target, str) and (
            target.startswith("/") or "." in target or "[" in target
        ):
            canonical_target = _canonical_target_pointer(target)
            if canonical_target != target:
                finding["target_path_or_span"] = canonical_target
                changes.append(f"/findings/{index}/target_path_or_span")
        refs = finding.get("evidence_refs")
        if isinstance(refs, list):
            canonical_refs = [
                _canonical_evidence_ref(item, envelope) for item in refs
            ]
            if canonical_refs != refs:
                finding["evidence_refs"] = canonical_refs
                changes.append(f"/findings/{index}/evidence_refs")
        if (
            bool(finding.get("blocking"))
            and str(finding.get("suggested_route") or "").upper() == "USER"
            and finding.get("repairable") is not False
        ):
            finding["repairable"] = False
            changes.append(f"/findings/{index}/repairable")
        severity = str(finding.get("severity") or "").upper()
        canonical_severity = severity
        if bool(finding.get("blocking")) and severity in {"P2", "P3"}:
            canonical_severity = "P1"
        elif not bool(finding.get("blocking")) and severity in {"P0", "P1"}:
            canonical_severity = "P2"
        if canonical_severity != severity:
            finding["severity"] = canonical_severity
            changes.append(f"/findings/{index}/severity")

    if prompt_id == "P-ONLINE-RESULT-IMPORT-CRITIC" and isinstance(result, dict):
        all_finding_codes = {
            str(item.get("code") or "").upper()
            for item in normalized.get("findings") or []
            if isinstance(item, Mapping)
            and not _wf3_runtime_only_target(item.get("target_path_or_span"))
        }
        blocking_finding_codes = {
            str(item.get("code") or "").upper()
            for item in normalized.get("findings") or []
            if isinstance(item, Mapping) and item.get("blocking") is True
        }
        injection_detected = "IMPORT_PROMPT_INJECTION" in blocking_finding_codes
        scope_violation_detected = bool(
            all_finding_codes
            & {"IMPORT_SCOPE_VIOLATION", "IMPORT_SENSITIVE_INFERENCE"}
        )
        for field, value in (
            ("prompt_injection_detected", injection_detected),
            ("scope_violation_detected", scope_violation_detected),
        ):
            if result.get(field) != value:
                result[field] = value
                changes.append(f"/result/{field}")
        if injection_detected:
            package = payload.get("result_package")
            package = package if isinstance(package, Mapping) else {}
            claim_ids = [
                str(item.get("claim_id"))
                for item in package.get("claims") or []
                if isinstance(item, Mapping) and item.get("claim_id")
            ]
            for field, value in (
                ("import_recommendation", "REJECT"),
                ("accepted_claim_ids", []),
                ("reference_only_claim_ids", []),
                ("rejected_claim_ids", claim_ids),
            ):
                if result.get(field) != value:
                    result[field] = value
                    changes.append(f"/result/{field}")

    for index, item in enumerate(normalized.get("unresolved_items") or []):
        if not isinstance(item, dict):
            continue
        item_id = _stable_machine_id(
            "wf3-unresolved",
            {
                key: item.get(key)
                for key in ("type", "description", "target_paths", "required_action")
            },
            index,
        )
        if item.get("item_id") != item_id:
            item["item_id"] = item_id
            changes.append(f"/unresolved_items/{index}/item_id")

    questions = normalized.get("user_questions")
    if isinstance(questions, list):
        filtered_questions = []
        for index, question in enumerate(questions):
            if not isinstance(question, Mapping):
                filtered_questions.append(question)
                continue
            targets = question.get("target_paths") or []
            if any(_wf3_runtime_only_target(target) for target in targets):
                changes.append(f"/user_questions/{index}:runtime-owned-question-dropped")
                continue
            filtered_questions.append(question)
        widened = widen_gate_questions(filtered_questions)
        for index, question in enumerate(widened):
            if not isinstance(question, dict):
                continue
            targets = question.get("target_paths")
            if isinstance(targets, list):
                canonical_targets = [_canonical_target_pointer(item) for item in targets]
                if canonical_targets != targets:
                    question["target_paths"] = canonical_targets
                    changes.append(f"/user_questions/{index}/target_paths")
            question_id = _stable_machine_id(
                "wf3-question",
                {
                    "question": question.get("question"),
                    "target_paths": question.get("target_paths"),
                },
                index,
            )
            if question.get("question_id") != question_id:
                question["question_id"] = question_id
                changes.append(f"/user_questions/{index}/question_id")
            priority = "P0" if bool(question.get("blocking")) else "P2"
            if question.get("priority") != priority:
                question["priority"] = priority
                changes.append(f"/user_questions/{index}/priority")
            answer_type = str((question.get("answer_schema") or {}).get("type") or "").upper()
            derived_type = None
            if answer_type == "BOOLEAN":
                derived_type = "CONFIRMATION"
            elif answer_type == "ENUM" and question.get("question_type") != "SECURITY_APPROVAL_INPUT":
                derived_type = "CHOICE"
            if derived_type and question.get("question_type") != derived_type:
                question["question_type"] = derived_type
                changes.append(f"/user_questions/{index}/question_type")
        normalized["user_questions"] = widened

    if not changes:
        return normalized, None
    return normalized, {
        "prompt_id": prompt_id,
        "changes": changes,
        "change_count": len(changes),
    }


def _blocking_items(output: Mapping[str, Any], field: str) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in output.get(field) or []
        if isinstance(item, Mapping) and item.get("blocking") is True
    ]


def canonicalize_wf3_producer_status(
    prompt_id: str,
    output: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Derive a WF-3 Producer status from authored blocking objects.

    This is a control-plane projection only.  It never changes a Finding,
    unresolved item, question, or business result.  In particular, advisory
    Findings cannot consume a semantic retry or turn a usable research package
    into a content block merely because the model wrote ``REVISE``.
    """

    if prompt_id not in WF3_PRODUCER_PROMPTS:
        return output, None
    normalized = copy.deepcopy(output)
    before = str(normalized.get("status") or "").upper()
    blockers = [
        *_blocking_items(normalized, "findings"),
        *_blocking_items(normalized, "unresolved_items"),
    ]
    questions = _blocking_items(normalized, "user_questions")
    after = before
    reason = "UNCHANGED"
    if questions:
        after = "NEED_USER_INPUT"
        reason = "BLOCKING_USER_QUESTION"
    elif blockers:
        executable_findings = [
            item
            for item in _blocking_items(normalized, "findings")
            if item.get("repairable") is True
            and str(item.get("suggested_route") or "").upper()
            not in {"", "USER", "BLOCK"}
        ]
        after = "REVISE" if executable_findings else "BLOCK"
        reason = (
            "EXECUTABLE_BLOCKING_CONTENT_ITEM"
            if executable_findings
            else "UNROUTABLE_BLOCKING_CONTENT_ITEM"
        )
    elif before in {"REVISE", "BLOCK", "NEED_USER_INPUT"}:
        after = "PASS"
        reason = "ADVISORY_ONLY"

    if after == before:
        return normalized, None
    normalized["status"] = after
    normalized.setdefault("warnings", []).append(
        f"SYSTEM_WF3_STATUS_CANONICALIZATION: {before}->{after}; reason={reason}"
    )
    return normalized, {
        "prompt_id": prompt_id,
        "before": before,
        "after": after,
        "reason": reason,
        "blocking_item_count": len(blockers),
        "blocking_question_count": len(questions),
    }


def canonicalize_wf3_critic_control(
    prompt_id: str,
    output: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Derive Critic control fields from canonical blocking objects.

    The model remains responsible for the semantic review.  It is not allowed
    to create an inconsistent workflow control state such as ``REVISE`` with
    advisory-only findings or ``PASS`` with a blocking question.
    """

    if prompt_id not in {
        "P-SAFE-ONLINE-PACKAGE-CRITIC",
        "P-PUBLIC-RESEARCH-PLAN-SCOPE-CRITIC",
        "P-PUBLIC-RESEARCH-CRITIC",
        "P-ONLINE-RESULT-IMPORT-CRITIC",
    }:
        return output, None
    normalized = copy.deepcopy(output)
    findings = _blocking_items(normalized, "findings")
    unresolved = _blocking_items(normalized, "unresolved_items")
    questions = _blocking_items(normalized, "user_questions")
    blockers = [*findings, *unresolved]
    routes = [wf3_finding_route(item) for item in findings]
    before_status = str(normalized.get("status") or "").upper()
    if questions:
        status = "NEED_USER_INPUT"
    elif blockers:
        status = (
            "REVISE"
            if routes and "BLOCK" not in routes and len(routes) == len(blockers)
            else "BLOCK"
        )
    else:
        status = "PASS"
    result = normalized.get("result")
    verdict_before = None
    verdict_after = None
    if isinstance(result, dict) and prompt_id != "P-ONLINE-RESULT-IMPORT-CRITIC":
        verdict_before = result.get("verdict")
        if prompt_id == "P-SAFE-ONLINE-PACKAGE-CRITIC":
            accept = "ACCEPT_FOR_HUMAN_APPROVAL"
        elif prompt_id == "P-PUBLIC-RESEARCH-PLAN-SCOPE-CRITIC":
            accept = "ACCEPT"
        else:
            accept = "ACCEPT_FOR_IMPORT_REVIEW"
        verdict_after = "BLOCK" if status == "BLOCK" else ("REVISE" if status != "PASS" else accept)
        result["verdict"] = verdict_after
    if isinstance(result, dict) and prompt_id == "P-ONLINE-RESULT-IMPORT-CRITIC":
        # Confirmation IDs are control-plane links, never free-form model IDs.
        confirmation_ids = [
            str(item.get("question_id") or "")
            for item in questions
            if str(item.get("question_id") or "")
        ]
        result["required_user_confirmations"] = confirmation_ids
    normalized["status"] = status
    changed = before_status != status or (
        verdict_after is not None and verdict_before != verdict_after
    )
    if not changed:
        return normalized, None
    normalized.setdefault("warnings", []).append(
        "SYSTEM_WF3_CRITIC_CONTROL_PROJECTION: "
        f"{before_status}->{status}; blocking={len(blockers)}; questions={len(questions)}"
    )
    return normalized, {
        "prompt_id": prompt_id,
        "before": before_status,
        "after": status,
        "blocking_item_count": len(blockers),
        "blocking_question_count": len(questions),
        "routes": routes,
    }


def wf3_finding_route(finding: Mapping[str, Any]) -> str:
    """Return the only WF-3 capability allowed to resolve one Finding."""

    suggested = str(finding.get("suggested_route") or "").upper()
    target = " ".join(
        str(finding.get(key) or "")
        for key in ("target_type", "target_path_or_span", "code")
    ).lower()
    narrative = " ".join(
        str(finding.get(key) or "")
        for key in ("description", "repair_instruction")
    ).lower()

    if suggested == "USER":
        return "USER"
    if suggested == "PLANNING_AGENT":
        return "PLAN"
    if any(token in target for token in ("safe_online_package", "package_candidate")):
        return "PRODUCER"
    if any(
        token in target
        for token in (
            "retrieved_source",
            "public_search",
            "source_catalog",
            "search_result",
            "archive",
        )
    ):
        return "RETRIEVAL"
    if any(
        token in narrative
        for token in (
            "重新检索",
            "补充检索",
            "获取全文",
            "re-search",
            "rerun search",
            "retrieve full text",
            "fetch full text",
        )
    ):
        return "RETRIEVAL"
    if any(token in target for token in ("research_plan", "research_question", "query")):
        return "PLAN"
    if any(token in target for token in ("synthesis_candidate", "claim", "limitation", "conflict")):
        return "SYNTHESIS"
    return "BLOCK"


def wf3_critic_routing_report(
    output: Mapping[str, Any],
    *,
    prompt_id: str = WF3_RESEARCH_CRITIC,
) -> dict[str, Any]:
    routed: list[dict[str, Any]] = []
    for finding in output.get("findings") or []:
        if not isinstance(finding, Mapping) or finding.get("blocking") is not True:
            continue
        routed.append(
            {
                "finding_instance_id": str(finding.get("finding_instance_id") or ""),
                "code": str(finding.get("code") or ""),
                "target_path_or_span": str(finding.get("target_path_or_span") or ""),
                "route": wf3_finding_route(finding),
            }
        )
    counts = {
        route: sum(1 for item in routed if item["route"] == route)
        for route in ("RETRIEVAL", "PLAN", "SYNTHESIS", "USER", "BLOCK")
    }
    return {
        "schema_version": "1.0",
        "prompt_id": str(prompt_id or WF3_RESEARCH_CRITIC),
        "blocking_finding_count": len(routed),
        "route_counts": counts,
        "routes": routed,
        "synthesis_only": bool(routed) and counts["SYNTHESIS"] == len(routed),
        "has_non_synthesis_route": any(item["route"] != "SYNTHESIS" for item in routed),
    }


def summarize_public_search(candidate: Mapping[str, Any]) -> dict[str, Any]:
    sources = [item for item in candidate.get("sources") or [] if isinstance(item, Mapping)]
    catalog = [item for item in candidate.get("source_catalog") or [] if isinstance(item, Mapping)]
    issues = [item for item in candidate.get("issues") or [] if isinstance(item, Mapping)]
    coverage = candidate.get("coverage") if isinstance(candidate.get("coverage"), Mapping) else {}
    by_query = coverage.get("by_query") if isinstance(coverage.get("by_query"), Mapping) else {}
    dimensions = coverage.get("dimensions") if isinstance(coverage.get("dimensions"), Mapping) else {}
    queries = {str(item).strip() for item in candidate.get("queries") or [] if str(item).strip()}
    covered_queries = {
        str(query)
        for query, item in by_query.items()
        if isinstance(item, Mapping) and int(item.get("source_count") or 0) > 0
    }
    passed_dimensions = {
        str(name)
        for name, item in dimensions.items()
        if isinstance(item, Mapping) and str(item.get("status") or "").upper() == "PASS"
    }
    critical_issues = [
        item
        for item in issues
        if str(item.get("type") or "").upper()
        in {"EVIDENCE_GAP", "SOURCE_CONFLICT", "SOURCE_FETCH_FAILURE", "SECURITY"}
    ]
    source_ids = {str(item.get("source_id")) for item in sources if item.get("source_id")}
    authoritative = sum(1 for item in catalog if int(item.get("authority_rank") or 0) >= 80)
    full_text = sum(
        1
        for item in catalog
        if int(item.get("text_length") or 0) >= 2000 or bool(item.get("full_text_available"))
    )
    return {
        "candidate_hash": sha256_json(candidate),
        "queries": sorted(queries),
        "covered_queries": sorted(covered_queries or (queries - set(coverage.get("uncovered_queries") or []))),
        "passed_dimensions": sorted(passed_dimensions),
        "source_ids": sorted(source_ids),
        "source_count": len(source_ids),
        "authoritative_source_count": authoritative,
        "full_text_source_count": full_text,
        "critical_issue_count": len(critical_issues),
        "archive_verified": str(
            ((candidate.get("archive_verification") or {}).get("status") or "PASS")
        ).upper()
        == "PASS",
    }


def compare_public_search_candidates(
    accepted: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply conservative whole-candidate non-regression acceptance."""

    baseline = summarize_public_search(accepted)
    proposed = summarize_public_search(candidate)
    regressions: list[str] = []
    baseline_queries = set(baseline["queries"])
    proposed_queries = set(proposed["queries"])
    if not baseline_queries.issubset(proposed_queries):
        regressions.append("QUERY_SET_SHRANK")
    if not set(baseline["covered_queries"]).issubset(set(proposed["covered_queries"])):
        regressions.append("QUERY_COVERAGE_REGRESSED")
    if not set(baseline["passed_dimensions"]).issubset(set(proposed["passed_dimensions"])):
        regressions.append("COVERAGE_DIMENSION_REGRESSED")
    if baseline["archive_verified"] and not proposed["archive_verified"]:
        regressions.append("ARCHIVE_VERIFICATION_REGRESSED")
    if (
        proposed["source_count"] < baseline["source_count"]
        and proposed["critical_issue_count"] >= baseline["critical_issue_count"]
    ):
        regressions.append("SOURCE_SET_SHRANK_WITHOUT_ISSUE_REDUCTION")
    improvements = [
        key
        for key in (
            "source_count",
            "authoritative_source_count",
            "full_text_source_count",
        )
        if proposed[key] > baseline[key]
    ]
    if proposed["critical_issue_count"] < baseline["critical_issue_count"]:
        improvements.append("critical_issue_count")
    return {
        "accepted": not regressions,
        "regressions": regressions,
        "improvements": improvements,
        "baseline": baseline,
        "candidate": proposed,
    }


def summarize_wf3_plan(candidate: Mapping[str, Any]) -> dict[str, Any]:
    result = candidate.get("result") if isinstance(candidate.get("result"), Mapping) else candidate
    questions = [str(item).strip() for item in result.get("research_questions") or [] if str(item).strip()]
    queries = [item for item in result.get("queries") or [] if isinstance(item, Mapping)]
    bindings = {
        str(item.get("query") or "").strip(): tuple(
            sorted({int(value) for value in item.get("linked_question_indexes") or [] if isinstance(value, int)})
        )
        for item in queries
        if str(item.get("query") or "").strip()
    }
    covered = sorted({index for values in bindings.values() for index in values})
    return {
        "candidate_hash": sha256_json(candidate),
        "research_questions": questions,
        "queries": sorted(bindings),
        "query_bindings": {key: list(value) for key, value in sorted(bindings.items())},
        "covered_question_indexes": covered,
        "source_priorities": sorted({str(item) for item in result.get("source_priorities") or []}),
        "evidence_requirements": sorted({str(item) for item in result.get("evidence_requirements") or []}),
        "prohibited_inferences": sorted({str(item) for item in result.get("prohibited_inferences") or []}),
        "preflight_errors": wf3_plan_preflight_errors(candidate),
    }


def wf3_plan_preflight_errors(candidate: Mapping[str, Any]) -> list[str]:
    result = candidate.get("result") if isinstance(candidate.get("result"), Mapping) else candidate
    questions = list(result.get("research_questions") or [])
    queries = list(result.get("queries") or [])
    errors: list[str] = []
    covered: set[int] = set()
    for row_index, query in enumerate(queries):
        if not isinstance(query, Mapping):
            errors.append(f"/result/queries/{row_index}: expected object")
            continue
        for binding_index, value in enumerate(query.get("linked_question_indexes") or []):
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < len(questions):
                errors.append(
                    f"/result/queries/{row_index}/linked_question_indexes/{binding_index}: "
                    f"index {value!r} is outside research_questions[0:{len(questions)}]"
                )
            else:
                covered.add(value)
    for question_index in range(len(questions)):
        if question_index not in covered:
            errors.append(
                f"/result/research_questions/{question_index}: no query is bound to this question"
            )
    # The execution skill is the authoritative consumer of this plan. Reusing
    # its strict validator here prevents a candidate from becoming the accepted
    # Plan baseline only to fail deterministically at the following step.
    from .skills.research_plan import normalize_and_validate_plan

    _, validation = normalize_and_validate_plan(dict(result), strict=True)
    existing_codes = {error.split(":", 1)[0] for error in errors}
    for finding in validation.get("findings") or []:
        code = str(finding.get("code") or "RESEARCH_PLAN_INVALID")
        if code in existing_codes:
            continue
        errors.append(f"{code}: {finding.get('message') or 'strict plan validation failed'}")
        existing_codes.add(code)
    return errors


def wf3_output_semantic_errors(
    prompt_id: str,
    envelope: Mapping[str, Any],
    output: Mapping[str, Any],
) -> list[str]:
    """Validate WF-3 cross-list identities from authoritative input only."""

    payload = envelope.get("payload") if isinstance(envelope.get("payload"), Mapping) else {}
    result = output.get("result") if isinstance(output.get("result"), Mapping) else {}
    if prompt_id == "P-PUBLIC-RESEARCH-PLAN":
        return wf3_plan_preflight_errors(output)
    errors: list[str] = []
    if prompt_id == "P-PUBLIC-RESEARCH-SYNTHESIS":
        source_ids = {
            str(item.get("source_id"))
            for item in payload.get("retrieved_sources") or []
            if isinstance(item, Mapping) and item.get("source_id")
        }
        source_ids.update(
            str(source_ref.get("source_id"))
            for item in payload.get("extracted_passages") or []
            if isinstance(item, Mapping)
            for source_ref in [item.get("source_ref")]
            if isinstance(source_ref, Mapping) and source_ref.get("source_id")
        )
        for claim_index, claim in enumerate(result.get("claims") or []):
            if not isinstance(claim, Mapping):
                continue
            claim_source_ids = [
                str(ref.get("source_id"))
                for ref in claim.get("source_refs") or []
                if isinstance(ref, Mapping) and ref.get("source_id")
            ]
            if not claim_source_ids:
                errors.append(
                    f"/result/claims/{claim_index}/source_refs: substantive claim requires at least one retrieved input source"
                )
                continue
            for ref_index, source_id in enumerate(claim_source_ids):
                if source_id not in source_ids:
                    errors.append(
                        f"/result/claims/{claim_index}/source_refs/{ref_index}/source_id: "
                        f"{source_id!r} is not a retrieved input source ID"
                    )
        for comparison_index, comparison in enumerate(result.get("source_comparisons") or []):
            if not isinstance(comparison, Mapping):
                continue
            for source_index, source_id in enumerate(comparison.get("source_ids") or []):
                if str(source_id) not in source_ids:
                    errors.append(
                        f"/result/source_comparisons/{comparison_index}/source_ids/{source_index}: "
                        f"{source_id!r} is not a retrieved input source ID"
                    )
    if prompt_id == "P-PUBLIC-RESEARCH-CRITIC":
        source_ids = {
            str(item.get("source_id"))
            for item in payload.get("retrieved_sources") or []
            if isinstance(item, Mapping) and item.get("source_id")
        }
        synthesis = payload.get("synthesis_candidate") if isinstance(payload.get("synthesis_candidate"), Mapping) else {}
        claim_ids = {
            str(item.get("claim_id"))
            for item in synthesis.get("claims") or []
            if isinstance(item, Mapping) and item.get("claim_id")
        }
        for index, item in enumerate(result.get("source_quality_summary") or []):
            source_id = str(item.get("source_id") or "") if isinstance(item, Mapping) else ""
            if source_id not in source_ids:
                errors.append(
                    f"/result/source_quality_summary/{index}/source_id: {source_id!r} is not an input source ID"
                )
        for index, claim_id in enumerate(result.get("unsupported_claim_ids") or []):
            if str(claim_id) not in claim_ids:
                errors.append(
                    f"/result/unsupported_claim_ids/{index}: {claim_id!r} is not an input claim ID"
                )
    if prompt_id == "P-ONLINE-RESULT-IMPORT-CRITIC":
        package = payload.get("result_package") if isinstance(payload.get("result_package"), Mapping) else {}
        claim_ids = [
            str(item.get("claim_id"))
            for item in package.get("claims") or []
            if isinstance(item, Mapping) and item.get("claim_id")
        ]
        accepted = [str(item) for item in result.get("accepted_claim_ids") or []]
        reference_only = [str(item) for item in result.get("reference_only_claim_ids") or []]
        rejected = [str(item) for item in result.get("rejected_claim_ids") or []]
        classified_sets = {
            "accepted_claim_ids": set(accepted),
            "reference_only_claim_ids": set(reference_only),
            "rejected_claim_ids": set(rejected),
        }
        overlaps: list[str] = []
        names = list(classified_sets)
        for left_index, left_name in enumerate(names):
            for right_name in names[left_index + 1:]:
                shared = sorted(classified_sets[left_name] & classified_sets[right_name])
                if shared:
                    overlaps.append(f"{left_name}<->{right_name}: " + ", ".join(shared))
        if overlaps:
            errors.append("/result: claim classification lists overlap: " + "; ".join(overlaps))
        classified = set().union(*classified_sets.values())
        unknown = sorted(classified - set(claim_ids))
        missing = sorted(set(claim_ids) - classified)
        if unknown:
            errors.append(
                "/result: claim IDs not present in payload.result_package.claims: "
                + ", ".join(unknown)
            )
        if missing:
            errors.append(
                "/result: input claims not classified as accepted, reference-only, or rejected: "
                + ", ".join(missing)
            )
        for field_name, values in (
            ("accepted_claim_ids", accepted),
            ("reference_only_claim_ids", reference_only),
            ("rejected_claim_ids", rejected),
        ):
            if len(values) != len(set(values)):
                errors.append(f"/result/{field_name}: duplicate claim IDs")
        expected_confirmations = {
            str(item.get("question_id"))
            for item in output.get("user_questions") or []
            if isinstance(item, Mapping) and item.get("blocking") is True and item.get("question_id")
        }
        actual_confirmations = {
            str(item) for item in result.get("required_user_confirmations") or []
        }
        if actual_confirmations != expected_confirmations:
            errors.append(
                "/result/required_user_confirmations: must equal blocking user_questions question IDs"
            )
    return errors


def compare_wf3_plan_candidates(
    accepted: Mapping[str, Any], candidate: Mapping[str, Any]
) -> dict[str, Any]:
    baseline = summarize_wf3_plan(accepted)
    proposed = summarize_wf3_plan(candidate)
    regressions: list[str] = []
    if proposed["preflight_errors"]:
        regressions.append("PLAN_SEARCH_PREFLIGHT_FAILED")
    if baseline["research_questions"] != proposed["research_questions"]:
        regressions.append("RESEARCH_QUESTION_ORDER_OR_SET_CHANGED")
    if not set(baseline["queries"]).issubset(set(proposed["queries"])):
        regressions.append("QUERY_SET_SHRANK")
    for query, bindings in baseline["query_bindings"].items():
        if query in proposed["query_bindings"] and not set(bindings).issubset(
            set(proposed["query_bindings"][query])
        ):
            regressions.append(f"QUERY_BINDING_REGRESSED:{query}")
    for key, code in (
        ("source_priorities", "SOURCE_PRIORITIES_SHRANK"),
        ("evidence_requirements", "EVIDENCE_REQUIREMENTS_SHRANK"),
        ("prohibited_inferences", "PROHIBITED_INFERENCES_SHRANK"),
    ):
        if not set(baseline[key]).issubset(set(proposed[key])):
            regressions.append(code)
    return {
        "accepted": not regressions,
        "regressions": regressions,
        "improvements": [
            key
            for key in ("queries", "source_priorities", "evidence_requirements")
            if len(proposed[key]) > len(baseline[key])
        ],
        "baseline": baseline,
        "candidate": proposed,
    }


def summarize_wf3_synthesis(candidate: Mapping[str, Any]) -> dict[str, Any]:
    result = candidate.get("result") if isinstance(candidate.get("result"), Mapping) else candidate
    claims = [item for item in result.get("claims") or [] if isinstance(item, Mapping)]
    claim_signatures = {
        sha256_json(
            {
                "claim_text": item.get("claim_text"),
                "claim_type": item.get("claim_type"),
                "source_ids": sorted(
                    str(ref.get("source_id") or "")
                    for ref in item.get("source_refs") or []
                    if isinstance(ref, Mapping) and ref.get("source_id")
                ),
            }
        )
        for item in claims
    }
    source_ids = {
        str(ref.get("source_id"))
        for item in claims
        for ref in item.get("source_refs") or []
        if isinstance(ref, Mapping) and ref.get("source_id")
    }
    comparisons = [item for item in result.get("source_comparisons") or [] if isinstance(item, Mapping)]
    return {
        "candidate_hash": sha256_json(candidate),
        "claim_signatures": sorted(claim_signatures),
        "claim_count": len(claims),
        "source_ids": sorted(source_ids),
        "comparison_topics": sorted({str(item.get("topic")) for item in comparisons if item.get("topic")}),
        "conflicts": sorted({str(item) for item in result.get("conflicts") or []}),
        "limitations": sorted({str(item) for item in result.get("limitations") or []}),
        "coverage_summary_present": bool(str(result.get("coverage_summary") or "").strip()),
    }


def compare_wf3_synthesis_candidates(
    accepted: Mapping[str, Any], candidate: Mapping[str, Any]
) -> dict[str, Any]:
    baseline = summarize_wf3_synthesis(accepted)
    proposed = summarize_wf3_synthesis(candidate)
    regressions: list[str] = []
    for key, code in (
        ("claim_signatures", "VALIDATED_CLAIMS_SHRANK_OR_CHANGED"),
        ("source_ids", "CLAIM_SOURCE_BINDINGS_SHRANK"),
        ("comparison_topics", "SOURCE_COMPARISON_TOPICS_SHRANK"),
        ("conflicts", "CONFLICTS_DISAPPEARED"),
        ("limitations", "LIMITATIONS_DISAPPEARED"),
    ):
        if not set(baseline[key]).issubset(set(proposed[key])):
            regressions.append(code)
    if baseline["coverage_summary_present"] and not proposed["coverage_summary_present"]:
        regressions.append("COVERAGE_SUMMARY_DISAPPEARED")
    return {
        "accepted": not regressions,
        "regressions": regressions,
        "improvements": [
            key
            for key in ("claim_count", "source_ids", "comparison_topics", "limitations")
            if (
                len(proposed[key]) > len(baseline[key])
                if isinstance(proposed[key], list)
                else proposed[key] > baseline[key]
            )
        ],
        "baseline": baseline,
        "candidate": proposed,
    }


def compact_wf3_research_envelope(
    prompt_id: str,
    envelope: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Remove duplicated passage text from the model projection only."""

    if prompt_id not in {
        "P-PUBLIC-RESEARCH-SYNTHESIS",
        "P-PUBLIC-RESEARCH-CRITIC",
        "P-ONLINE-RESULT-IMPORT-CRITIC",
    }:
        return envelope, None
    compact = copy.deepcopy(envelope)
    payload = compact.get("payload")
    if not isinstance(payload, dict):
        return envelope, None
    passages = [item for item in payload.get("extracted_passages") or [] if isinstance(item, dict)]
    passage_source_ids = {
        str((item.get("source_ref") or {}).get("source_id") or "")
        for item in passages
        if isinstance(item.get("source_ref"), dict)
    }
    removed_fields = 0
    original_passage_chars = sum(len(str(item.get("text") or "")) for item in passages)
    compact_passage_chars = original_passage_chars
    passage_char_cap: int | None = None
    if prompt_id == "P-PUBLIC-RESEARCH-SYNTHESIS" and passages:
        # The deterministic coverage guard needs the complete archive, while the
        # provider only needs a representative evidence window per source. Keep
        # every source/passsage identity and split a fixed total text budget
        # across them so 30-source strict coverage cannot exceed the request cap.
        passage_char_cap = max(600, min(1600, 42_000 // len(passages)))
        for passage in passages:
            text = str(passage.get("text") or "")
            if len(text) <= passage_char_cap:
                continue
            head = max(1, passage_char_cap * 2 // 3)
            tail = max(1, passage_char_cap - head - 1)
            passage["text"] = text[:head] + "…" + text[-tail:]
        compact_passage_chars = sum(
            len(str(item.get("text") or "")) for item in passages
        )
    for passage in passages:
        source_ref = passage.get("source_ref")
        if not isinstance(source_ref, dict):
            continue
        passage["source_ref"] = {
            key: source_ref[key]
            for key in (
                "source_id",
                "source_type",
                "source_hash",
                "authority_rank",
                "security_level",
            )
            if key in source_ref
        }
        removed_fields += max(0, len(source_ref) - len(passage["source_ref"]))
    for source_ref in payload.get("retrieved_sources") or []:
        if not isinstance(source_ref, dict):
            continue
        if str(source_ref.get("source_id") or "") in passage_source_ids and source_ref.get("quoted_text"):
            source_ref.pop("quoted_text", None)
            removed_fields += 1
    if not removed_fields and compact_passage_chars == original_passage_chars:
        return envelope, None
    return compact, {
        "strategy": "WF3_BOUNDED_RESEARCH_SOURCE_TEXT",
        "original_chars": len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))),
        "model_chars": len(json.dumps(compact, ensure_ascii=False, separators=(",", ":"))),
        "removed_duplicate_source_fields": removed_fields,
        "original_passage_chars": original_passage_chars,
        "model_passage_chars": compact_passage_chars,
        "passage_char_cap": passage_char_cap,
        "quality_guard_uses_full_context": True,
    }
