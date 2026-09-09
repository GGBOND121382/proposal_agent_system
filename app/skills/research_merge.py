from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

from ..util import new_id, safe_filename, sha256_bytes, utc_now, write_json
from .base import SkillContext, SkillResult
from .public_research import PublicResearchIntegrityError, PublicResearchPlanContractError
from .research_audit import _record_queries, coverage_report, upgrade_archive_result, verify_research_archive
from .research_evidence import is_fulltext
from .research_execution import ResearchExecutionContractError, build_plan_lock, validate_plan_transition
from .research_plan import canonical_url, normalize_doi
from .research_quality import assess_candidate_relevance, build_query_relevance_profiles
from .research_validation import write_validation_bundle


def _identity_keys(record: dict[str, Any]) -> set[tuple[str, str]]:
    keys = {("url", canonical_url(str(record[key]))) for key in ("url", "final_url", "canonical_url") if record.get(key)}
    doi = normalize_doi(record.get("doi"), str(record.get("url") or ""))
    if doi:
        keys.add(("doi", doi))
    # Consolidate identical text before the archive upgrader deduplicates it,
    # so query bindings and provenance survive content deduplication as well.
    if record.get("text_sha256"):
        keys.add(("text", str(record["text_sha256"])))
    return keys


def _groups(records: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: list[tuple[set[tuple[str, str]], list[dict[str, Any]]]] = []
    for record in records:
        keys, members = _identity_keys(record), [record]
        remaining = []
        for existing_keys, existing in groups:
            if keys & existing_keys:
                keys |= existing_keys
                members = existing + members
            else:
                remaining.append((existing_keys, existing))
        remaining.append((keys, members))
        groups = remaining
    return [members for _, members in groups]


def _qualifying_bindings(record: dict[str, Any]) -> set[str]:
    assessments = (record.get("verification") or {}).get("semantic_relevance_by_query") or {}
    return {query for query in _record_queries(record)
            if query not in assessments or assessments[query].get("qualifies_for_coverage")
            or assessments[query].get("label") in {"DIRECT", "SUPPORTING"}}


def _merge_group(group: list[dict[str, Any]], profiles: dict[str, Any]) -> dict[str, Any]:
    baseline = [record for record in group if record["_merge_round"] == 0]
    required = {query for record in baseline if record.get("fetch_mode") != "SNIPPET_ONLY"
                for query in _qualifying_bindings(record)}
    queries = list(dict.fromkeys(query for record in group for query in _record_queries(record)))
    choices = []
    for original in group:
        record = copy.deepcopy(original)
        verification = record.setdefault("verification", {})
        assessments = verification.setdefault("semantic_relevance_by_query", {})
        # Keep existing assessments for unchanged, reviewed queries. Transfer a
        # binding from another snapshot only if this snapshot supports it too.
        text = Path(record["text_path"]).read_text(encoding="utf-8")
        own_bindings = set(_record_queries(record))
        for query in queries:
            if query not in own_bindings:
                assessments[query] = assess_candidate_relevance(query, {**record, "content_text": text}, profiles)
        verification["matched_queries"] = queries
        supports = _qualifying_bindings(record)
        quality = (required.issubset(supports) and record.get("fetch_mode") != "SNIPPET_ONLY",
                   is_fulltext(record), record.get("fetch_mode") != "SNIPPET_ONLY",
                   record.get("extraction_quality") == "USABLE", len(supports), len(text))
        choices.append((quality, record))
    selected = max(choices, key=lambda item: item[0])[1]
    # Stable baseline IDs keep downstream references stable when a snippet or
    # abstract is upgraded to a verified document from the follow-up round.
    if baseline:
        selected["source_id"] = baseline[0]["source_id"]
    verification = selected["verification"]
    verification["discovery_providers"] = list(dict.fromkeys(
        provider for record in group for provider in [
            *((record.get("verification") or {}).get("discovery_providers") or []),
            (record.get("verification") or {}).get("discovery_provider"),
        ] if provider))
    verification["discovery_matched_queries"] = list(dict.fromkeys(
        query for record in group for query in
        ((record.get("verification") or {}).get("discovery_matched_queries") or _record_queries(record))))
    selected["merge_provenance"] = [origin for record in group for origin in record["merge_provenance"]]
    selected["selected_snapshot_origin"] = selected.pop("_merge_origin")
    selected.pop("_merge_round")
    selected["matched_queries"] = queries
    return selected


def merge_research_archives(
    baseline: dict[str, Any], candidate: dict[str, Any], *, context: SkillContext,
) -> dict[str, Any]:
    """Create an independent cumulative archive; never mutate either input archive."""
    manifests, parents, records = [], [], []
    for round_index, output in enumerate((baseline, candidate)):
        path = Path(str(output.get("archive_manifest") or ""))
        if not path.is_file():
            raise PublicResearchIntegrityError("Cannot merge research without an archive manifest")
        verification = verify_research_archive(path)
        if verification["status"] != "PASS":
            raise PublicResearchIntegrityError("Cannot merge an invalid research archive", details={"verification": verification})
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("project_id") != context.project_id or manifest.get("workflow_id") != context.workflow_id:
            raise PublicResearchIntegrityError("Cannot merge archives from another project or workflow")
        manifests.append(manifest)
        parents.append({"archive_session_id": manifest["session_id"], "archive_manifest": str(path),
                        "manifest_sha256": sha256_bytes(path.read_bytes()),
                        "source_count": len(manifest["records"]), "retrieval_health": manifest.get("retrieval_health")})
        for original in manifest["records"]:
            record = copy.deepcopy(original)
            origin = {"archive_session_id": manifest["session_id"], "source_id": record["source_id"],
                      "archive_manifest": str(path), "url": record.get("url"),
                      "raw_path": record["raw_path"], "text_path": record["text_path"], "metadata_path": record["metadata_path"],
                      "snapshot_sha256": record["snapshot_sha256"], "text_sha256": record["text_sha256"],
                      "matched_queries": _record_queries(record)}
            record.setdefault("merge_provenance", [origin])
            record["_merge_origin"] = origin
            record["_merge_round"] = round_index
            records.append(record)
    already_merged = {str(Path(p["archive_manifest"]).resolve())
                      for p in (manifests[0].get("merge_report") or {}).get("parents", [])}
    already_merged.add(str(Path(baseline["archive_manifest"]).resolve()))
    if str(Path(candidate["archive_manifest"]).resolve()) in already_merged:
        return copy.deepcopy(baseline)
    plan = copy.deepcopy(manifests[1]["normalized_plan"])
    try:
        validate_plan_transition(build_plan_lock(manifests[0]["normalized_plan"]), plan,
                                 allow_additive=True, allow_binding_enrichment=True)
    except ResearchExecutionContractError as exc:
        raise PublicResearchPlanContractError(str(exc), details={"code": exc.code}) from exc
    profiles = build_query_relevance_profiles(plan)
    merged = [_merge_group(group, profiles) for group in _groups(records)]
    if len({record["source_id"] for record in merged}) != len(merged):
        raise PublicResearchIntegrityError("Conflicting source IDs in cumulative research archive")
    root = Path(context.data_dir) / "research_archive" / safe_filename(context.project_id) / new_id("research-merged")
    root.mkdir(parents=True, exist_ok=False)
    for folder in ("raw", "text", "meta"):
        (root / folder).mkdir()
    for record in merged:
        stem = safe_filename(record["source_id"])
        for key, folder in (("raw_path", "raw"), ("text_path", "text")):
            original = Path(record[key])
            destination = root / folder / f"{stem}{original.suffix}"
            shutil.copyfile(original, destination)
            record[key] = str(destination)
        record["metadata_path"] = str(root / "meta" / f"{stem}.json")
        write_json(Path(record["metadata_path"]), record)

    report = {"schema_version": "1.0", "strategy": "CUMULATIVE_EVIDENCE", "parents": parents,
              "input_record_count": len(records), "merged_record_count": len(merged),
              "duplicate_record_count": len(records) - len(merged)}
    # Execution health describes the latest actual search, not a synthetic search
    # of the union. Parent health remains observable in the cumulative report.
    health = copy.deepcopy(candidate.get("retrieval_health") or manifests[1].get("retrieval_health") or {"status": "UNOBSERVED"})
    health["scope"] = "LATEST_SEARCH_ROUND"
    selection = {"schema_version": "1.2", "status": "PASS", "strategy": "CUMULATIVE_EVIDENCE",
                 "input_candidate_count": len(records), "screened_candidate_count": len(records),
                 "deduplicated_candidate_count": len(merged), "selected_candidate_count": len(merged),
                 "selected_by_query": {q: sum(q in _qualifying_bindings(r) for r in merged) for q in plan["queries"]},
                 "issues": []}
    manifest_path = root / "manifest.json"
    manifest = {"session_id": root.name, "project_id": context.project_id, "workflow_id": context.workflow_id,
                "provider": "cumulative", "retrieval_mode": "CUMULATIVE_ARCHIVE", "created_at": utc_now(),
                "records": merged, "merge_report": report}
    write_json(manifest_path, manifest)
    result = SkillResult("PASS", {"archive_session_id": root.name, "archive_root": str(root),
                                  "archive_manifest": str(manifest_path), "source_index": str(root / "source_index.csv"),
                                  "mode": "CUMULATIVE_ARCHIVE", "warnings": [], "merge_report": report}, [], [])
    minimum = int(((manifests[1].get("coverage") or {}).get("dimensions", {}).get("query_depth") or {}).get("minimum_sources_per_query") or 3)
    result = upgrade_archive_result(result, plan, manifests[1].get("plan_validation") or {}, [],
                                    quality_profile="application_background", selection_report=selection,
                                    execution_report=candidate.get("execution_report"), min_sources_per_query=minimum,
                                    min_fulltext_sources_per_query=int(plan.get("minimum_fulltext_sources_per_query") or 0),
                                    retrieval_health=health)
    # Counts of executed search hits are cumulative and may include repeated hits;
    # evidence/source/fulltext counts above are recalculated from the deduped pool.
    hits = [(m.get("evidence_funnel") or {}).get("search_hits") for m in manifests]
    result.output["evidence_funnel"]["search_hits"] = sum(hits) if all(isinstance(n, int) for n in hits) else None
    result.output["evidence_funnel"]["search_hits_scope"] = "ALL_ROUNDS_WITH_REPEATS"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["evidence_funnel"] = result.output["evidence_funnel"]
    # New queries can introduce legitimate new gaps. Compare retained evidence
    # against the original scope/thresholds, while reporting final sufficiency
    # against the complete new plan and the latest execution health.
    old_dimensions = (manifests[0].get("coverage") or {}).get("dimensions") or {}
    report["baseline_coverage"] = coverage_report(
        [r for r in manifest["records"] if r.get("fetch_mode") != "SNIPPET_ONLY"],
        manifests[0]["normalized_plan"], quality_profile="application_background",
        min_sources_per_query=int((old_dimensions.get("query_depth") or {}).get("minimum_sources_per_query") or 3),
        min_fulltext_sources_per_query=int(manifests[0]["normalized_plan"].get("minimum_fulltext_sources_per_query") or 0),
        retrieval_health=manifests[0].get("retrieval_health"),
    )
    manifest["merge_report"] = report
    write_json(manifest_path, manifest)
    for row in result.output["source_catalog"]:
        record = next(r for r in manifest["records"] if r["source_id"] == row["source_id"])
        row["merge_provenance"] = record["merge_provenance"]
        row["selected_snapshot_origin"] = record["selected_snapshot_origin"]
    validation_root = write_validation_bundle(
        context=context, original_plan=plan, normalized_plan=plan,
        plan_validation=manifests[1].get("plan_validation") or {}, provider="cumulative",
        quality_profile="application_background", configured_max_results=len(merged), effective_max_results=len(merged),
        discovery_manifest=None, discovery_input=None, result_output=result.output)
    write_json(validation_root / "11_merge_report.json", report)
    result.output["validation_bundle_dir"] = str(validation_root)
    return result.output
