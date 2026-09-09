from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.skills.research_quality import (
    assess_candidate_relevance,
    build_query_relevance_profiles,
    concept_tokens,
)
from app.skills.research_screening import _query_alignment, screen_and_select_candidates
from app.skills.search_gateway import normalize_search_queries
from app.skills.verifiable_public_research import VerifiablePublicResearchArchiveSkill


@pytest.fixture
def archived():
    return json.loads((Path(__file__).parent / "fixtures/chinese_retrieval_regression.json").read_text(encoding="utf-8"))


def test_han_bigrams_preserve_english_tokens_without_single_character_inflation():
    assert concept_tokens("human-machine decision systems") == {"human", "machine", "decision"}
    assert concept_tokens("人机协同决策 系统 论文") == {"人机", "机协", "协同", "同决", "决策"}
    assert "多" in concept_tokens("多")
    assert "多" not in concept_tokens("多智能体")
    # Whitespace changes at actual word boundaries do not remove the words.
    assert {"人机", "协同", "决策"} <= concept_tokens("人机 协同 决策")


@pytest.mark.parametrize("query", ["人机协同决策 智能作战", "LLM 人机协同决策 智能作战"])
def test_english_question_anchors_do_not_veto_chinese_or_mixed_query(query):
    plan = {"query_items": [
        {"query": q, "linked_question_indexes": [0]} for q in [
            "AI decision workflow evaluation", "AI decision workflow collaboration", query,
        ]
    ]}
    profiles = build_query_relevance_profiles(plan)
    result = assess_candidate_relevance(query, {"title": "人机协同决策在智能作战中的应用"}, profiles)
    assert result["qualifies_for_coverage"]
    if "LLM" not in query:
        assert profiles[query]["domain_anchors"] == []


@pytest.mark.parametrize("title", [
    "人工智能生成艺术：人机协同的创造系统",
    "智能控制系统与企业协同管理研究",
    "多的意思、字形、读音与组词",
    "决策系统论文研究方法综述",
])
def test_shared_characters_and_generic_terms_do_not_establish_relevance(title):
    query = "智能作战 人机协同决策 系统"
    result = assess_candidate_relevance(query, {"title": title}, {})
    assert not result["qualifies_for_coverage"]


def test_matching_one_long_phrase_does_not_cover_a_long_chinese_query():
    query = "人机协同决策 空军 作战 情报融合 风险控制 任务分配"
    result = assess_candidate_relevance(query, {"title": "人机协同决策"}, {})
    assert result["overlap_count"] >= 5
    assert not result["qualifies_for_coverage"]


@pytest.mark.parametrize("title, expected", [
    ("美空军正在推进人机协同决策优势冲刺实验", True),
    ("美空军DASH实验进展", True),
    ("美空军Dashboard系统介绍", False),
    ("医院DASH临床决策实验", False),
])
def test_chinese_entity_names_match_inside_sentences_with_latin_boundaries(title, expected):
    query = "美空军 DASH 实验"
    profiles = build_query_relevance_profiles({"query_items": [{"query": query, "entity_groups": [
        ["DASH", "人机协同决策优势冲刺"], ["美空军", "美国空军"],
    ]}]})
    assert assess_candidate_relevance(query, {"title": title}, profiles)["qualifies_for_coverage"] is expected


def test_actual_chinese_web_results_survive_screening_and_selection(archived):
    selected, report = screen_and_select_candidates(
        archived["candidates"], archived["normalized_plan"], max_results=80,
        strict=True, min_per_query=3, enforce_semantic_relevance=True,
    )
    for query in archived["chinese_queries"]:
        assert report["selected_by_query"][query] >= 3
    assert any("DASH" in item["title"] for item in selected)
    assert any("人在回路" in item["title"] for item in selected)
    assert not any(any(term in item["title"] for term in ["汉语", "字典", "神经系统疾病", "生成艺术", "中小企业", "滑坡"])
                   for item in selected)


def test_chinese_alignment_contributes_to_ranking():
    query = "智能作战 人机协同决策 系统"
    relevant, _, _ = _query_alignment(query, {"title": "美空军人机协同决策优势冲刺实验"})
    unrelated, _, _ = _query_alignment(query, {"title": "核磁共振成像在神经系统疾病诊断中的优势与挑战"})
    assert relevant > unrelated == 0


def test_browser_fallback_counts_chinese_relevant_hits(archived):
    query = archived["chinese_queries"][0]
    profiles = build_query_relevance_profiles(archived["normalized_plan"])
    queries = normalize_search_queries([query])
    irrelevant = [{"title": "多的读音与字形", "matched_query": query}] * 8
    fallback = VerifiablePublicResearchArchiveSkill._browser_fallback_queries
    assert fallback(irrelevant, queries, min_hits=1, browser_required=False, relevance_profiles=profiles) == queries
    relevant = {"title": "美空军智能作战中的人机协同决策", "matched_query": query}
    assert fallback([*irrelevant, relevant], queries, min_hits=1, browser_required=False, relevance_profiles=profiles) == []
