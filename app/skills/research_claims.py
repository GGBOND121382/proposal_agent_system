from __future__ import annotations

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ..privacy import redact_public_retrieval_content
from ..util import utc_now

_INNOVATION_TERMS = {"innovation", "innovative", "novelty", "novel", "first", "no existing", "no prior", "has not been", "创新", "首创", "首次", "突破", "填补空白", "尚无", "未有", "空白"}
PUBLIC_CLAIM_VALIDATOR_VERSION = "2026-09-08.v3-quote-against-archived-fulltext"


def _compact(text: str) -> str:
    return "\n".join(line.strip() for line in str(text or "").splitlines() if line.strip())


def _searchable(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", str(text or "").lower()))


def _innovation_claim(claim: dict[str, Any]) -> bool:
    subject = str(claim.get("subject_id") or "").lower()
    qualifiers = [str(item).strip() for item in claim.get("qualifiers") or []]
    qualifier_markers = {item.upper() for item in qualifiers}
    claim_text = str(claim.get("claim_text") or "").lower()
    profiles = {
        str(item).strip().upper()
        for item in claim.get("target_section_profiles") or []
    }
    explicitly_scoped = (
        subject.startswith("innovation")
        or bool(
            qualifier_markers
            & {"INNOVATION_CLAIM", "PROJECT_INNOVATION", "NOVELTY_CLAIM"}
        )
    )
    if explicitly_scoped:
        return True
    # Mentioning an external innovation or a published "novel method" in a
    # background/case claim is not itself a novelty assertion by this project.
    # Text heuristics are used only when the model explicitly routes the claim
    # to the proposal's INNOVATION section.
    searchable = f"{' '.join(qualifiers).lower()} {claim_text}"
    return "INNOVATION" in profiles and any(
        term in searchable for term in _INNOVATION_TERMS
    )


def _archive_text_index(research_output: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """Map source_id to its hash-pinned archive text file, when available."""
    manifest_path = str(research_output.get("archive_manifest") or "").strip()
    if not manifest_path:
        return {}
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    index: dict[str, tuple[str, str]] = {}
    for record in manifest.get("records") or []:
        if not isinstance(record, dict):
            continue
        source_id = str(record.get("source_id") or "")
        text_path = str(record.get("text_path") or "")
        text_sha256 = str(record.get("text_sha256") or "")
        if source_id and text_path and text_sha256:
            index[source_id] = (text_path, text_sha256)
    return index


def _load_archived_text(
    index: dict[str, tuple[str, str]],
    cache: dict[str, str],
    source_id: str,
) -> str:
    """Load a source's archived text only when its sha256 matches the manifest.

    The archive keeps the original page text while the model saw the privacy
    projection ([EMAIL]/[PHONE]/credentials redacted).  Apply the same
    redaction here so genuine quotes from the projection remain verifiable.
    """
    if source_id in cache:
        return cache[source_id]
    text = ""
    entry = index.get(source_id)
    if entry:
        text_path, expected_sha256 = entry
        try:
            candidate = Path(text_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            candidate = ""
        if candidate and hashlib.sha256(candidate.encode("utf-8")).hexdigest() == expected_sha256:
            redacted, _ = redact_public_retrieval_content(candidate)
            text = redacted if isinstance(redacted, str) else ""
    cache[source_id] = text
    return text


def validate_public_claims(synthesis: dict[str, Any], research_output: dict[str, Any]) -> dict[str, Any]:
    mode = str(research_output.get("mode") or "")
    if mode in {"REPLAY", "MOCK", "SIMULATED_EMPTY"}:
        return {"status": "PASS", "validation_mode": "ORCHESTRATION_ONLY", "findings": [], "bindings": [], "validated_at": utc_now()}
    catalog = {str(item.get("source_id")): item for item in research_output.get("source_catalog", []) if item.get("source_id")}
    archive_text_index = _archive_text_index(research_output)
    archive_text_cache: dict[str, str] = {}
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
                searchable_quote = _searchable(quoted)
                if searchable_quote in _searchable(f"{record.get('title', '')}\n{record.get('excerpt', '')}"):
                    direct.append(source_id)
                else:
                    # Legitimate quotes often come from the fetched full text,
                    # which the catalog deliberately does not inline.  Verify
                    # against the hash-pinned archive snapshot before flagging.
                    archived_text = _load_archived_text(archive_text_index, archive_text_cache, source_id)
                    if archived_text and searchable_quote in _searchable(archived_text):
                        direct.append(source_id)
                    else:
                        findings.append({"code": "PUBLIC_CLAIM_QUOTE_NOT_FOUND", "severity": "P1", "claim_id": claim_id, "source_id": source_id})
        if _innovation_claim(claim):
            sufficiency = research_output.get("research_sufficiency") or {}
            if sufficiency.get("status") == "DEGRADED":
                findings.append({
                    "code": "PUBLIC_INNOVATION_RESEARCH_SUFFICIENCY_GAP",
                    "severity": "P0",
                    "claim_id": claim_id,
                    "research_gap_ids": [str(item.get("gap_id")) for item in sufficiency.get("research_gaps") or [] if isinstance(item, dict) and item.get("gap_id")],
                })
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
        "validator_version": PUBLIC_CLAIM_VALIDATOR_VERSION,
        "validation_mode": "DETERMINISTIC_PUBLIC_CLAIM_BINDING",
        "findings": findings, "bindings": bindings, "claim_count": len(claims), "catalog_source_count": len(catalog),
        "synthesis_sha256": hashlib.sha256(json.dumps(synthesis, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
        "validated_at": utc_now(),
    }
