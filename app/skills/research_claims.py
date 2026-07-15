from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ..util import utc_now

_INNOVATION_TERMS = {"innovation", "innovative", "novelty", "创新", "首创", "首次", "突破", "填补空白"}


def _compact(text: str) -> str:
    return "\n".join(line.strip() for line in str(text or "").splitlines() if line.strip())


def _searchable(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", str(text or "").lower()))


def _innovation_claim(claim: dict[str, Any]) -> bool:
    subject = str(claim.get("subject_id") or "").lower()
    qualifiers = " ".join(str(item) for item in claim.get("qualifiers") or []).lower()
    return subject.startswith("innovation") or any(term in qualifiers for term in _INNOVATION_TERMS)


def validate_public_claims(synthesis: dict[str, Any], research_output: dict[str, Any]) -> dict[str, Any]:
    mode = str(research_output.get("mode") or "")
    if mode in {"REPLAY", "MOCK", "SIMULATED_EMPTY"}:
        return {"status": "PASS", "validation_mode": "ORCHESTRATION_ONLY", "findings": [], "bindings": [], "validated_at": utc_now()}
    catalog = {str(item.get("source_id")): item for item in research_output.get("source_catalog", []) if item.get("source_id")}
    coverage = research_output.get("coverage") or {}
    findings: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    claims = synthesis.get("claims") or []
    if catalog and not claims:
        findings.append({"code": "PUBLIC_SYNTHESIS_NO_CLAIMS", "severity": "P0"})
    for claim in claims:
        if not isinstance(claim, dict):
            findings.append({"code": "PUBLIC_CLAIM_INVALID_OBJECT", "severity": "P0"})
            continue
        claim_id = str(claim.get("claim_id") or "")
        if not claim_id or claim_id in seen_ids:
            findings.append({"code": "PUBLIC_CLAIM_DUPLICATE_ID", "severity": "P0", "claim_id": claim_id})
            continue
        seen_ids.add(claim_id)
        if claim.get("claim_type") != "PUBLIC_CLAIM":
            findings.append({"code": "PUBLIC_CLAIM_WRONG_TYPE", "severity": "P0", "claim_id": claim_id})
        refs = claim.get("source_refs") or []
        if not refs:
            findings.append({"code": "PUBLIC_CLAIM_NO_EVIDENCE", "severity": "P0", "claim_id": claim_id})
            continue
        bound: list[str] = []
        direct: list[str] = []
        for ref in refs:
            source_id = str((ref or {}).get("source_id") or "")
            record = catalog.get(source_id)
            if record is None:
                findings.append({"code": "PUBLIC_CLAIM_UNKNOWN_SOURCE", "severity": "P0", "claim_id": claim_id, "source_id": source_id})
                continue
            bound.append(source_id)
            if (ref or {}).get("source_type") != "PUBLIC_SOURCE":
                findings.append({"code": "PUBLIC_CLAIM_NONPUBLIC_REF", "severity": "P0", "claim_id": claim_id, "source_id": source_id})
            source_hash = (ref or {}).get("source_hash")
            if not source_hash:
                findings.append({"code": "PUBLIC_CLAIM_HASH_MISSING", "severity": "P0", "claim_id": claim_id, "source_id": source_id})
            elif source_hash != record.get("snapshot_sha256"):
                findings.append({"code": "PUBLIC_CLAIM_HASH_MISMATCH", "severity": "P0", "claim_id": claim_id, "source_id": source_id})
            quoted = _compact(str((ref or {}).get("quoted_text") or ""))
            if quoted:
                if _searchable(quoted) in _searchable(f"{record.get('title', '')}\n{record.get('excerpt', '')}"):
                    direct.append(source_id)
                else:
                    findings.append({"code": "PUBLIC_CLAIM_QUOTE_NOT_FOUND", "severity": "P1", "claim_id": claim_id, "source_id": source_id})
        if _innovation_claim(claim):
            dimensions = coverage.get("dimensions") or {}
            missing = [name for name in ("recent_work", "comparable_baselines", "limitation_mechanisms") if (dimensions.get(name) or {}).get("status") != "PASS"]
            if missing:
                findings.append({"code": "PUBLIC_INNOVATION_EVIDENCE_GAP", "severity": "P0", "claim_id": claim_id, "missing_dimensions": missing})
        bindings.append({
            "claim_id": claim_id, "source_ids": sorted(set(bound)),
            "evidence_mode": "DIRECT_SOURCE_SUPPORTED" if direct else "MODEL_SYNTHESIS",
            "direct_quote_source_ids": sorted(set(direct)),
            "evidence_layers": ["ORIGINAL_SNAPSHOT", "SOURCE_EXTRACT", "MODEL_SYNTHESIS"],
        })
    for comparison in synthesis.get("source_comparisons") or []:
        for source_id in comparison.get("source_ids") or []:
            if str(source_id) not in catalog:
                findings.append({"code": "PUBLIC_COMPARISON_UNKNOWN_SOURCE", "severity": "P0", "source_id": str(source_id)})
    if any(item.get("type") == "SOURCE_CONFLICT" for item in research_output.get("issues") or []) and not synthesis.get("conflicts"):
        findings.append({"code": "PUBLIC_SOURCE_CONFLICT_SUPPRESSED", "severity": "P0"})
    return {
        "status": "BLOCK" if findings else "PASS",
        "validation_mode": "DETERMINISTIC_PUBLIC_CLAIM_BINDING",
        "findings": findings, "bindings": bindings, "claim_count": len(claims), "catalog_source_count": len(catalog),
        "synthesis_sha256": hashlib.sha256(json.dumps(synthesis, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
        "validated_at": utc_now(),
    }
