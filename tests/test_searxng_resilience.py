from __future__ import annotations

from types import SimpleNamespace
import json

import httpx
import pytest

from app.skills.base import SkillContext
from app.skills import public_research
from app.skills.public_research import (
    PublicResearchArchiveSkill,
    PublicResearchRetrievalError,
)
from app.skills.research_audit import verify_research_archive


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        public_search_provider="searxng",
        public_search_base_url="http://127.0.0.1:8888",
        public_search_engines="arxiv,openairepublications,semantic scholar",
        public_search_max_results=10,
        research_fetch_timeout_seconds=5,
        research_max_source_bytes=1024 * 1024,
    )


class _Response:
    def __init__(self, results):
        self._results = results

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {"results": self._results}


class _Client:
    calls: list[dict] = []
    fail_queries: set[str] = set()
    init_kwargs: list[dict] = []

    def __init__(self, **kwargs):
        self.init_kwargs.append(dict(kwargs))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def get(self, _endpoint, *, params):
        self.calls.append(dict(params))
        query = params["q"]
        if query in self.fail_queries:
            raise httpx.ReadTimeout("timed out")
        return _Response(
            [
                {
                    "title": f"{query}-result-{index}",
                    "url": f"https://example.org/{query}/{index}",
                    "content": "public evidence",
                    "engine": "arxiv",
                }
                for index in range(2)
            ]
        )


def test_searxng_query_timeout_does_not_discard_other_query_results(monkeypatch):
    _Client.calls = []
    _Client.init_kwargs = []
    _Client.fail_queries = {"query-one"}
    monkeypatch.setattr(public_research.httpx, "Client", _Client)

    candidates, failures = PublicResearchArchiveSkill(_settings())._search_searxng(
        ["query-one", "query-two"],
        10,
    )

    assert [item["matched_query"] for item in candidates] == ["query-two", "query-two"]
    assert [item["query"] for item in failures] == ["query-one"]
    assert failures[0]["category"] == "RETRIEVAL"
    assert _Client.calls[0]["engines"] == _settings().public_search_engines
    assert _Client.calls[0]["language"] == "all"
    assert all(item["trust_env"] is False for item in _Client.init_kwargs)


def test_searxng_results_are_interleaved_to_preserve_query_coverage(monkeypatch):
    _Client.calls = []
    _Client.fail_queries = set()
    monkeypatch.setattr(public_research.httpx, "Client", _Client)

    candidates, failures = PublicResearchArchiveSkill(_settings())._search_searxng(
        ["query-one", "query-two"],
        10,
    )

    assert failures == []
    assert [item["matched_query"] for item in candidates] == [
        "query-one",
        "query-two",
        "query-one",
        "query-two",
    ]


def test_searxng_bibliographic_metadata_is_not_discarded(monkeypatch):
    class MetadataClient(_Client):
        def get(self, _endpoint, *, params):
            return _Response(
                [
                    {
                        "title": "Metadata-rich result",
                        "url": "https://doi.org/10.1000/example",
                        "content": "public abstract",
                        "engine": "openairepublications",
                        "publishedDate": "2025-05-06",
                        "authors": [{"name": "A. Author"}, {"name": "B. Author"}],
                        "journal": "Journal of Reliable Research",
                        "doi": "10.1000/example",
                    }
                ]
            )

    monkeypatch.setattr(public_research.httpx, "Client", MetadataClient)
    candidates, failures = PublicResearchArchiveSkill(_settings())._search_searxng(
        ["metadata query"], 10
    )
    assert failures == []
    assert candidates[0]["published_at"] == "2025-05-06"
    assert candidates[0]["authors"] == ["A. Author", "B. Author"]
    assert candidates[0]["publisher"] == "Journal of Reliable Research"
    assert candidates[0]["doi"] == "10.1000/example"


def test_all_searxng_query_timeouts_are_retrieval_not_configuration(monkeypatch):
    _Client.calls = []
    _Client.fail_queries = {"query-one", "query-two"}
    monkeypatch.setattr(public_research.httpx, "Client", _Client)

    with pytest.raises(PublicResearchRetrievalError) as caught:
        PublicResearchArchiveSkill(_settings())._search_searxng(
            ["query-one", "query-two"],
            10,
        )

    assert caught.value.category == "RETRIEVAL"
    assert len(caught.value.details["query_failures"]) == 2


def test_archived_text_hash_is_stable_across_windows_newline_handling(tmp_path):
    record_file = tmp_path / "recorded.json"
    record_file.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "title": "Multiline source",
                        "url": "https://example.org/multiline",
                        "content_text": "line one\nline two\nline three",
                        "matched_query": "multiline evidence",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = PublicResearchArchiveSkill(_settings()).run(
        {
            "provider": "recorded",
            "record_file": str(record_file),
            "plan": {"queries": ["multiline evidence"]},
            "max_results": 1,
        },
        SkillContext(
            project_id="project-newline",
            workflow_id="workflow-newline",
            security_level="PUBLIC",
            data_dir=str(tmp_path),
        ),
    )

    assert verify_research_archive(result.output["archive_manifest"])["status"] == "PASS"
