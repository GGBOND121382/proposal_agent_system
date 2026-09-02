from __future__ import annotations

import json
from types import SimpleNamespace

from app.skills.content_extraction import ContentExtractor
from app.skills.fetch_gateway import FetchedDocument, HttpFetchGateway
from app.skills.search_gateway import SearchGateway, normalize_search_queries
from app.skills.search_providers import (
    AcademicSearchProvider,
    ConnectorSearchProvider,
    RecordedSearchProvider,
    SearxngSearchProvider,
)


def _settings(**overrides):
    values = {
        "public_search_base_url": "http://127.0.0.1:8888",
        "public_search_engines": "bing,google",
        "research_fetch_timeout_seconds": 15,
        "research_max_source_bytes": 1024 * 1024,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _Response:
    def __init__(self, *, payload=None, content=b"", url="https://example.org/final", status=200, content_type="text/html"):
        self._payload = payload
        self._content = content
        self.url = url
        self.status_code = status
        self.headers = {"content-type": content_type}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload

    def iter_bytes(self):
        yield self._content

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Client:
    payload = None
    response = None

    def __init__(self, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, _url, *, params):
        query = params["q"]
        return _Response(
            payload={
                "results": [
                    {
                        "title": f"Result for {query}",
                        "url": f"https://example.org/{query}",
                        "content": "search snippet",
                        "engine": "unit-engine",
                    }
                ]
            }
        )

    def stream(self, _method, _url):
        return self.response


def test_search_query_normalization_preserves_plan_ids_and_constraints() -> None:
    queries = normalize_search_queries(
        [
            {
                "query_id": "Q-7",
                "query": "human AI decision making",
                "purpose": "related work",
                "linked_question_indexes": [0, 2],
                "time_scope": {"from": 2020},
            },
            "human AI decision making",
            "decision support field study",
        ]
    )

    assert [item.query_id for item in queries] == ["Q-7", "query-003"]
    assert queries[0].linked_question_indexes == (0, 2)
    assert queries[0].constraints["time_scope"] == {"from": 2020}


def test_recorded_and_connector_providers_keep_legacy_payloads_readable(tmp_path) -> None:
    recorded = tmp_path / "recorded.json"
    recorded.write_text(
        json.dumps({"sources": [{"title": "Recorded", "url": "https://example.org/r", "matched_query": "q"}]}),
        encoding="utf-8",
    )
    connector = tmp_path / "connector.json"
    connector.write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "connector": "test-connector",
                "responses": [
                    {
                        "query": "q",
                        "results": [{"title": "Connected", "url": "https://example.org/c"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    queries = normalize_search_queries([{"query_id": "Q-1", "query": "q"}])

    recorded_result = RecordedSearchProvider(recorded).search(queries, per_query_limit=5)
    connector_result = ConnectorSearchProvider(connector).search(queries, per_query_limit=5)

    assert recorded_result.hits[0].query_id == "Q-1"
    assert connector_result.hits[0].metadata["verification"]["connector_run_id"] == "run-1"
    assert connector_result.raw_manifest["result_count"] == 1


def test_searxng_provider_emits_query_level_raw_execution_receipt() -> None:
    queries = normalize_search_queries([{"query_id": "Q-WEB", "query": "web-query"}])
    result = SearxngSearchProvider(
        _settings(),
        client_factory=_Client,
        max_workers=1,
    ).search(queries, per_query_limit=3)

    assert result.hits[0].provider == "searxng"
    assert result.hits[0].query_id == "Q-WEB"
    assert result.runs[0].status == "PASS"
    receipt = result.runs[0].to_dict()
    assert receipt["raw_response"]["results"][0]["engine"] == "unit-engine"
    assert len(receipt["raw_response_sha256"]) == 64


def test_academic_adapter_and_gateway_preserve_provider_manifest() -> None:
    class _AcademicClient:
        def __init__(self, _settings):
            pass

        def discover(self, queries, *, time_scope, per_query_limit):
            return {
                "created_at": "2026-09-02T00:00:00Z",
                "providers": ["openalex"],
                "responses": [
                    {
                        "query": queries[0],
                        "results": [
                            {
                                "title": "Paper",
                                "url": "https://example.org/paper",
                                "abstract": "abstract",
                                "academic_provider": "openalex",
                                "matched_query": queries[0],
                            }
                        ],
                    }
                ],
                "provider_runs": [
                    {
                        "query": queries[0],
                        "provider": "openalex",
                        "result_count": 1,
                        "raw_response": {"id": "W1"},
                    }
                ],
                "failures": [],
            }

    queries = normalize_search_queries([{"query_id": "Q-A", "query": "academic-query"}])
    provider = AcademicSearchProvider(
        _settings(),
        client_factory=_AcademicClient,
        time_scope={"from": 2020},
    )
    batch = SearchGateway([provider]).search(queries, per_query_limit=5)

    assert batch.providers == ["openalex"]
    assert batch.hits[0].provider == "openalex"
    assert batch.runs[0].raw_response == {"id": "W1"}


def test_http_fetch_and_static_extraction_are_separate_auditable_stages() -> None:
    html = b"<html><body><nav>menu</nav><main><h1>Title</h1><p>" + (b"usable content " * 20) + b"</p></main></body></html>"
    _Client.response = _Response(content=html)
    validated = []
    fetcher = HttpFetchGateway(
        _settings(),
        client_factory=_Client,
        url_validator=lambda url, *, resolve_dns: validated.append((url, resolve_dns)),
    )

    fetched = fetcher.fetch("https://example.org/start")
    extracted = ContentExtractor().extract(fetched)

    assert isinstance(fetched, FetchedDocument)
    assert validated == [
        ("https://example.org/start", True),
        ("https://example.org/final", True),
    ]
    assert fetched.fetch_mode == "HTTP"
    assert extracted.extractor == "BEAUTIFULSOUP_MAIN_TEXT"
    assert extracted.quality == "USABLE"
    assert "menu" not in extracted.text
    assert "usable content" in extracted.text
