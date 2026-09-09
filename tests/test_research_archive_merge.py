from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.background_research import BackgroundResearchService
from app.skills.base import SkillContext, SkillResult
from app.skills.public_research import PublicResearchIntegrityError, PublicResearchPlanContractError
from app.skills.research_audit import upgrade_archive_result, verify_research_archive
from app.skills.research_merge import merge_research_archives
from app.skills.research_plan import normalize_and_validate_plan
from app.skills.research_quality import assess_candidate_relevance, build_query_relevance_profiles
from app.util import sha256_bytes, sha256_text, write_json
from app.wf3_contracts import compare_public_search_candidates


Q1 = "human machine decision support military operators"
Q2 = "agent workflow planning tool evaluation benchmarks"


def _plan(queries):
    raw = {"plan_id": "plan-merge", "task_type": "PUBLIC_BACKGROUND_RESEARCH", "binding_contract_version": "1.0",
           "research_questions": ["Decision support", "Agent workflows"], "time_scope": "2021-2026",
           "source_priorities": ["official government sources"], "evidence_requirements": [], "prohibited_inferences": [],
           "minimum_fulltext_sources_per_query": 1,
           "queries": [{"query_id": f"Q-{i}", "query": q, "linked_question_indexes": [i]} for i, q in enumerate(queries)]}
    return normalize_and_validate_plan(raw, strict=True)[0]


def _archive(tmp_path, session, plan, items):
    root = tmp_path / session
    root.mkdir()
    profiles = build_query_relevance_profiles(plan)
    records = []
    for index, item in enumerate(items):
        query = item.get("query", Q1)
        text = item.get("text", query) + f"\nPublic evidence {session} {index}."
        record = {"source_id": f"{session}-{index}", "url": item["url"], "title": query,
                  "doi": item.get("doi"), "excerpt": text, "text_length": len(text),
                  "fetch_mode": item.get("fetch_mode", "HTTP"), "extractor": "HTML_TEXT",
                  "extraction_quality": "USABLE", "content_type": "text/html", "published_at": "2025-01-01",
                  "matched_query": query, "snapshot_sha256": sha256_bytes(text.encode()), "text_sha256": sha256_text(text),
                  "verification": {"matched_queries": [query], "discovery_provider": "browser_search",
                                   "semantic_relevance_by_query": {query: assess_candidate_relevance(query, {"title": query, "excerpt": text}, profiles)}}}
        for key, suffix in (("raw_path", ".html"), ("text_path", ".txt")):
            path = root / f"{index}{suffix}"
            path.write_bytes(text.encode("utf-8"))
            record[key] = str(path)
        record["metadata_path"] = str(root / f"{index}.json")
        write_json(Path(record["metadata_path"]), record)
        records.append(record)
    path = root / "manifest.json"
    write_json(path, {"session_id": session, "project_id": "project-test", "workflow_id": "wf-test", "records": records})
    output = {"archive_session_id": session, "archive_manifest": str(path), "source_index": str(root / "index.csv")}
    result = upgrade_archive_result(SkillResult("PASS", output, [], []), plan, {"status": "PASS"}, [],
                                    quality_profile="application_background", min_fulltext_sources_per_query=1,
                                    retrieval_health={"status": "PASS", "providers": {"browser_search": {"result_count": 10}}})
    return result.output


def _context(tmp_path):
    return SkillContext("project-test", "wf-test", "PUBLIC", str(tmp_path / "merged"))


def _files(root):
    return {str(path): sha256_bytes(path.read_bytes()) for path in root.rglob("*") if path.is_file()}


def test_union_keeps_old_query_and_new_sources_and_rebuilds_all_outputs(tmp_path):
    first = _archive(tmp_path, "first", _plan([Q1]), [{"url": f"https://af.mil/old-{i}"} for i in range(3)])
    second = _archive(tmp_path, "second", _plan([Q1, Q2]), [{"url": f"https://af.mil/new-{i}", "query": Q2} for i in range(3)])
    before = _files(tmp_path)
    assert "QUERY_COVERAGE_REGRESSED" in compare_public_search_candidates(first, second)["regressions"]
    merged = merge_research_archives(first, second, context=_context(tmp_path))
    assert compare_public_search_candidates(first, merged)["accepted"]
    assert merged["coverage"]["by_query"][Q1]["source_count"] == 3
    assert merged["coverage"]["by_query"][Q2]["fulltext_source_count"] == 3
    assert merged["research_sufficiency"]["status"] == "SUFFICIENT"
    assert merged["research_gaps"] == []
    assert merged["evidence_funnel"]["usable_evidence"] == 6
    assert merged["evidence_funnel"]["search_hits"] == 20
    assert len(merged["sources"]) == len(merged["passages"]) == len(merged["source_catalog"]) == 6
    assert verify_research_archive(merged["archive_manifest"])["status"] == "PASS"
    assert all(sha256_bytes(Path(path).read_bytes()) == value for path, value in before.items())
    assert all(row["merge_provenance"] for row in merged["source_catalog"])
    bundle = Path(merged["validation_bundle_dir"])
    assert json.loads((bundle / "07_coverage.json").read_text(encoding="utf-8")) == merged["coverage"]
    assert json.loads((bundle / "06_source_catalog.json").read_text(encoding="utf-8"))[0]["merge_provenance"]
    assert (bundle / "11_merge_report.json").is_file()


def test_duplicate_upgrade_prefers_fulltext_and_preserves_id_bindings_and_provenance(tmp_path):
    first = _archive(tmp_path, "first", _plan([Q1]), [{"url": "https://af.mil/shared?utm_source=one", "fetch_mode": "SNIPPET_ONLY"}])
    second = _archive(tmp_path, "second", _plan([Q1, Q2]), [{"url": "https://af.mil/shared", "query": Q2, "text": Q1 + " " + Q2}])
    merged = merge_research_archives(first, second, context=_context(tmp_path))
    assert len(merged["source_catalog"]) == 1
    row = merged["source_catalog"][0]
    assert row["source_id"] == "first-0"
    assert row["full_text_available"]
    assert len(row["merge_provenance"]) == 2
    assert row["selected_snapshot_origin"]["archive_session_id"] == "second"
    assert merged["coverage"]["by_query"][Q1]["fulltext_source_count"] == 1
    assert merged["coverage"]["by_query"][Q2]["fulltext_source_count"] == 1
    assert merged["sources"][0]["source_hash"] == row["snapshot_sha256"]


def test_changed_page_cannot_transfer_unsupported_query_or_discard_old_evidence(tmp_path):
    first = _archive(tmp_path, "first", _plan([Q1]), [{"url": "https://af.mil/shared"}])
    second = _archive(tmp_path, "second", _plan([Q1, Q2]), [
        {"url": "https://af.mil/shared", "query": Q2, "text": Q2 * 10},
        {"url": "https://af.mil/new", "query": Q2},
    ])
    merged = merge_research_archives(first, second, context=_context(tmp_path))
    assert merged["coverage"]["by_query"][Q1]["source_ids"] == ["first-0"]
    assert merged["coverage"]["by_query"][Q2]["source_ids"] == ["second-1"]
    row = next(r for r in merged["source_catalog"] if r["source_id"] == "first-0")
    assert row["selected_snapshot_origin"]["archive_session_id"] == "first"
    assert len(row["merge_provenance"]) == 2


def test_doi_and_url_aliases_deduplicate_transitively(tmp_path):
    first = _archive(tmp_path, "first", _plan([Q1]), [{"url": "https://af.mil/paper"}])
    second = _archive(tmp_path, "second", _plan([Q1, Q2]), [
        {"url": "https://af.mil/paper", "doi": "10.1234/example"},
        {"url": "https://doi.org/10.1234/example", "doi": "https://doi.org/10.1234/example"},
    ])
    merged = merge_research_archives(first, second, context=_context(tmp_path))
    assert len(merged["source_catalog"]) == 1
    assert len(merged["source_catalog"][0]["merge_provenance"]) == 3


def test_missing_and_tampered_archives_fail_without_modifying_first_round(tmp_path):
    first = _archive(tmp_path, "first", _plan([Q1]), [{"url": "https://af.mil/old"}])
    with pytest.raises(PublicResearchIntegrityError):
        merge_research_archives(first, {}, context=_context(tmp_path))
    second = _archive(tmp_path, "second", _plan([Q1, Q2]), [{"url": "https://af.mil/new", "query": Q2}])
    (tmp_path / "second/0.txt").write_text("tampered", encoding="utf-8")
    with pytest.raises(PublicResearchIntegrityError):
        merge_research_archives(first, second, context=_context(tmp_path))
    assert verify_research_archive(first["archive_manifest"])["status"] == "PASS"


def test_merge_refuses_query_removal_and_foreign_workflow(tmp_path):
    first = _archive(tmp_path, "first", _plan([Q1]), [{"url": "https://af.mil/old"}])
    second = _archive(tmp_path, "second", _plan([Q2]), [{"url": "https://af.mil/new", "query": Q2}])
    with pytest.raises(PublicResearchPlanContractError):
        merge_research_archives(first, second, context=_context(tmp_path))
    with pytest.raises(PublicResearchIntegrityError):
        merge_research_archives(first, second, context=SkillContext("project-test", "foreign", "PUBLIC", str(tmp_path)))


def test_repeated_merge_does_not_double_count_and_remaining_gaps_stay_visible(tmp_path):
    first = _archive(tmp_path, "first", _plan([Q1]), [{"url": "https://af.mil/old"}])
    second = _archive(tmp_path, "second", _plan([Q1, Q2]), [{"url": "https://af.mil/new", "query": Q2, "fetch_mode": "SNIPPET_ONLY"}])
    merged = merge_research_archives(first, second, context=_context(tmp_path))
    assert merged["research_sufficiency"]["status"] == "DEGRADED"
    assert Q2 in merged["coverage"]["uncovered_queries"]
    assert merged["evidence_funnel"]["snippet_only_records"] == 1
    assert merged["evidence_funnel"]["usable_evidence"] == 1
    assert merged["retrieval_health"]["scope"] == "LATEST_SEARCH_ROUND"
    assert merge_research_archives(merged, second, context=_context(tmp_path)) == merged


def test_workflow_accepts_cumulative_result_when_raw_followup_regresses(tmp_path, monkeypatch):
    from app.workflow_repair import WorkflowRepairMixin

    first = _archive(tmp_path, "first", _plan([Q1]), [{"url": f"https://af.mil/old-{i}"} for i in range(3)])
    second = _archive(tmp_path, "second", _plan([Q1, Q2]), [{"url": "https://af.mil/new", "query": Q2}])
    service = BackgroundResearchService(SimpleNamespace(data_dir=tmp_path / "merged"))
    state = {"background_search_results": copy.deepcopy(first)}
    # Exercise the live result-selection branch with real on-disk archives;
    # replace only the network search itself.
    engine = WorkflowRepairMixin()
    plan = {"plan_id": "plan-merge", "queries": [
        {"query_id": f"Q-{i}", "query": q, "dimension": "APPLICATION_SCENARIO", "purpose": "Public evidence"}
        for i, q in enumerate([Q1, Q2])
    ]}
    monkeypatch.setattr(engine, "_context_result", lambda *a, **k: plan, raising=False)
    monkeypatch.setattr(engine, "_background_research", lambda: service, raising=False)
    async def search(*args, **kwargs):
        return second
    monkeypatch.setattr(service, "search", search)
    engine.executor = SimpleNamespace(gateway=SimpleNamespace(settings=SimpleNamespace(runtime_mode="LIVE", public_search_provider="hybrid")))
    asyncio.run(engine._run_background_search({"id": "wf-test", "project_id": "project-test"}, state))
    assert state["background_search_results"]["mode"] == "CUMULATIVE_ARCHIVE"
    assert state["background_search_candidate_history"][-1]["decision"] == "ACCEPT"
    assert "QUERY_COVERAGE_REGRESSED" in state["background_search_merge_history"][-1]["raw_round_comparison"]["regressions"]
    assert state["background_search_results"]["coverage"]["by_query"][Q1]["source_count"] == 3
    assert state["background_search_results"]["coverage"]["by_query"][Q2]["source_count"] == 1
    assert state["background_research_sufficiency"]["status"] == "DEGRADED"
    assert state["background_search_results"]["merge_report"]["baseline_coverage"]["status"] == "PASS"
