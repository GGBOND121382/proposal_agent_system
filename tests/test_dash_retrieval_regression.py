from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.background_research import background_search_feedback, merge_background_followup_plan
from app.skills.content_extraction import ContentExtractor
from app.skills.fetch_gateway import FetchedDocument, FetchGatewayRetrievalError
from app.skills.research_audit import coverage_report, source_category
from app.skills.research_evidence import is_fulltext
from app.skills.research_execution import build_plan_lock, validate_plan_transition, ResearchExecutionContractError
from app.skills.research_plan import normalize_and_validate_plan
from app.skills.research_quality import assess_candidate_relevance, build_query_relevance_profiles, build_research_sufficiency
from app.skills.research_screening import screen_and_select_candidates
from app.skills.search_gateway import normalize_search_queries
from app.skills.search_providers.searxng import SearxngSearchProvider
from app.skills.verifiable_public_research import VerifiablePublicResearchArchiveSkill, _LIVE_DISCOVERY


@pytest.fixture
def dash():
    return json.loads((Path(__file__).parent / "fixtures/dash_retrieval_regression.json").read_text(encoding="utf-8"))


def _plan(dash):
    return {
        "plan_id": "plan-dash", "task_type": "PUBLIC_BACKGROUND_RESEARCH", "binding_contract_version": "1.0",
        "research_questions": ["Application scenarios"], "source_priorities": ["official government sources"],
        "time_scope": "2021-09-04/2026-09-04", "evidence_requirements": [], "prohibited_inferences": [],
        "queries": [{"query_id": f"Q-{i}", "query": dash[key], "linked_question_indexes": [0]}
                    for i, key in enumerate(["entity_query", "broad_query"])],
    }


def test_original_dash_pages_survive_provider_normalization_and_selection(dash):
    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, *, params):
            assert params["q"] == dash["broad_query"]
            return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: dash["web_response"])

    settings = SimpleNamespace(public_search_base_url="http://localhost:8888", public_search_engines="bing,brave")
    result = SearxngSearchProvider(settings, client_factory=Client).search(
        normalize_search_queries([dash["broad_query"]]), per_query_limit=8)
    assert len(result.hits) == 29
    assert result.runs[0].details["truncated_result_count"] == 0
    expected_urls = {dash["web_response"]["results"][rank - 1]["url"] for rank in [13, 15, 29]}
    assert expected_urls <= {hit.url for hit in result.hits}
    plan, _ = normalize_and_validate_plan(_plan(dash), strict=True)
    selected, report = screen_and_select_candidates(
        [*dash["academic_candidates"], *(hit.to_candidate() for hit in result.hits)], plan,
        max_results=8, strict=True, min_per_query=3, enforce_semantic_relevance=True)
    assert expected_urls <= {item["url"] for item in selected}
    assert report["selected_by_query"][dash["entity_query"]] >= 3
    assert not any("Residential Buildings" in item["title"] or "Stroke Decision" in item["title"]
                   or "power system control" in item["title"] for item in selected)
    primary = next(item for item in selected if "/4371071/" in item["url"])
    assert primary["verification"]["discovery_matched_queries"] == [dash["broad_query"]]
    assert dash["entity_query"] in primary["matched_queries"]


def test_named_entity_aliases_and_context_prevent_keyword_collisions():
    query = '"DASH" "Air Force"'
    plan = {"query_items": [{"query": query, "entity_groups": [["DASH", "Decision Advantage Sprint"], ["Air Force", "USAF"]]}]}
    profiles = build_query_relevance_profiles(plan)
    for title, expected in [
        ("Air Force Decision Advantage Sprint progress", True),
        ("DASH Air Force experiments", True),
        ("Air Force dashboard decision support", False),
        ("DASH software for medical decision support", False),
    ]:
        result = assess_candidate_relevance(query, {"title": title}, profiles)
        assert result["qualifies_for_coverage"] is expected


def test_entity_constraints_survive_normalization_and_cannot_be_weakened(dash):
    plan = _plan(dash)
    plan["queries"][0]["entity_groups"] = [["DASH", "Decision Advantage Sprint"]]
    normalized, validation = normalize_and_validate_plan(plan, strict=True)
    assert validation["status"] == "PASS"
    assert normalized["query_items"][0]["entity_groups"] == plan["queries"][0]["entity_groups"]
    lock = build_plan_lock(plan)
    plan["queries"][0]["entity_groups"] = []
    with pytest.raises(ResearchExecutionContractError):
        validate_plan_transition(lock, plan, allow_binding_enrichment=True)


def test_long_abstract_does_not_satisfy_fulltext_depth(dash):
    plan, _ = normalize_and_validate_plan(_plan(dash), strict=True)
    query = plan["queries"][0]
    record = {"source_id": "source-one", "matched_query": query, "authority_rank": 94,
              "fetch_mode": "PROVIDER_PAYLOAD", "extractor": "PROVIDER_TEXT", "extraction_quality": "USABLE",
              "text_length": 9000, "verification": {"discovery_provider": "searxng"}}
    coverage = coverage_report([record], plan, quality_profile="application_background", min_sources_per_query=1)
    assert coverage["by_query"][query]["fulltext_source_count"] == 0
    assert coverage["dimensions"]["query_fulltext_depth"]["status"] == "INSUFFICIENT"
    sufficiency = build_research_sufficiency(coverage, plan, {"status": "PASS"})
    assert "FULLTEXT" in next(gap for gap in sufficiency["research_gaps"] if gap.get("query") == query)["gap_types"]
    assert not is_fulltext(record)
    record.update(fetch_mode="HTTP", extractor="HTML_TEXT")
    assert is_fulltext(record)


def test_official_military_domains_are_authoritative_without_substring_spoofing():
    assert source_category({"url": "https://www.af.mil/News/Article/123"}) == "GOVERNMENT"
    assert source_category({"url": "https://csiac.dtic.mil/articles/123"}) == "GOVERNMENT"
    assert source_category({"url": "https://af.mil.example.org/article"}) == "OTHER"


@pytest.mark.parametrize("blocked", [False, True])
def test_live_hybrid_web_candidate_fetches_document_and_preserves_blocked_snippet(tmp_path, monkeypatch, blocked):
    skill = VerifiablePublicResearchArchiveSkill.__new__(VerifiablePublicResearchArchiveSkill)
    skill.content_extractor = ContentExtractor()
    monkeypatch.setattr(skill, "_validate_public_url", lambda *args, **kwargs: None)
    calls = []

    def fetch(url):
        calls.append(url)
        if blocked:
            raise FetchGatewayRetrievalError("HTTP 503")
        return FetchedDocument(url, url, "text/html", 200, ("<article>Air Force DASH findings. " * 20 + "</article>").encode())

    skill.fetch_gateway = SimpleNamespace(fetch=fetch)
    candidate = {"url": "https://www.af.mil/news/dash", "title": "Air Force DASH", "excerpt": "Only a search snippet",
                 "discovery_provider": "searxng", "matched_query": "Air Force DASH"}
    token = _LIVE_DISCOVERY.set(True)
    try:
        record = skill._archive_candidate(candidate, tmp_path, tmp_path, tmp_path, "connector")
    finally:
        _LIVE_DISCOVERY.reset(token)
    assert calls == [candidate["url"]]
    assert is_fulltext(record) is not blocked
    assert record["fetch_mode"] == ("SNIPPET_ONLY" if blocked else "HTTP")
    assert json.loads(Path(record["metadata_path"]).read_text(encoding="utf-8"))["fetch_mode"] == record["fetch_mode"]


def test_followup_uses_sources_and_gaps_and_retains_locked_queries(dash):
    plan = _plan(dash)
    state = {"background_last_executed_plan": plan,
             "background_search_results": {"source_catalog": [{"title": "DASH full name", "url": "https://www.af.mil", "excerpt": "Decision Advantage Sprint"}],
                                           "research_gaps": [{"query": dash["entity_query"], "description": "Missing entity", "gap_types": ["TARGET_ENTITY"]}]}}
    feedback = background_search_feedback(state)
    assert "Decision Advantage Sprint" in feedback["source_summaries"][0]
    revised = copy.deepcopy(plan)
    revised["queries"] = [{"query_id": "Q-0", "query": '"Decision Advantage Sprint" Air Force', "linked_question_indexes": [0]}]
    combined = merge_background_followup_plan(revised, feedback)
    assert combined["queries"][:2] == plan["queries"]
    assert len(combined["queries"]) == 3
    validate_plan_transition(build_plan_lock(plan), combined)
    assert plan == state["background_last_executed_plan"]
    state["background_search_refinement_rounds"] = 1
    assert background_search_feedback(state) is None


def test_browser_fallback_uses_relevant_hits_not_raw_hit_count(dash):
    plan, _ = normalize_and_validate_plan(_plan(dash), strict=True)
    queries = normalize_search_queries(plan["query_items"])
    candidates = [{"title": "Residential decision support machine operational scenarios", "matched_query": dash["entity_query"]}] * 8
    fallback = VerifiablePublicResearchArchiveSkill._browser_fallback_queries(
        candidates, queries, min_hits=1, browser_required=False,
        relevance_profiles=build_query_relevance_profiles(plan))
    assert queries[0] in fallback
