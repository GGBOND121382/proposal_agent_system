from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.skills.browser_worker import BrowserPageResult, BrowserWorker
from app.skills.fetch_gateway import (
    FetchGatewaySecurityError,
    HttpFetchGateway,
    validate_public_url,
)
from app.skills.search_gateway import SearchGateway, normalize_search_queries
from app.skills.search_providers import BrowserSearchProvider
from app.skills.search_providers.base import SearchProvider, SearchProviderRetrievalError
from app.dependency_preflight import RuntimeDependencyPreflight
from app.util import utc_now


def _settings(tmp_path, **overrides):
    values = {
        "data_dir": tmp_path,
        "browser_search_url_template": "https://search.example/search?q={query}",
        "browser_navigation_timeout_seconds": 10,
        "browser_rate_limit_seconds": 0,
        "browser_cache_ttl_seconds": 3600,
        "browser_headless": True,
        "browser_executable": "",
        "browser_fetch_fallback_enabled": True,
        "research_fetch_timeout_seconds": 10,
        "research_max_source_bytes": 1024 * 1024,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _page(url: str, *, html: str, text: str, status: str = "PASS", http_status: int = 200):
    return BrowserPageResult(
        requested_url=url,
        final_url=url,
        status=status,
        http_status=http_status,
        content_type="text/html",
        html=html,
        text=text,
        title="Search",
        rendered_at=utc_now(),
    )


def test_browser_provider_saves_raw_page_and_parses_real_result_shape(tmp_path) -> None:
    html = """
    <html><body><ol>
      <li class="b_algo"><h2><a href="https://public.example/a">Result A</a></h2>
      <div class="b_caption"><p>Useful web snippet.</p></div></li>
    </ol></body></html>
    """
    worker = BrowserWorker(
        _settings(tmp_path),
        renderer=lambda url, _timeout: _page(url, html=html, text="Result A Useful web snippet."),
        url_validator=lambda _url, *, resolve_dns: None,
    )
    provider = BrowserSearchProvider(
        _settings(tmp_path),
        worker=worker,
        evidence_dir=tmp_path / "evidence",
    )

    result = provider.search(normalize_search_queries(["test query"]), per_query_limit=5)

    assert [hit.title for hit in result.hits] == ["Result A"]
    assert result.hits[0].provider == "browser_search"
    assert result.runs[0].status == "PASS"
    assert result.runs[0].raw_response["html_path"]
    assert len(list((tmp_path / "evidence").glob("*.html"))) == 1
    assert len(list((tmp_path / "evidence").glob("*.json"))) == 1


def test_captcha_is_provider_blocked_and_never_reported_as_success(tmp_path) -> None:
    worker = BrowserWorker(
        _settings(tmp_path),
        renderer=lambda url, _timeout: _page(
            url,
            html="<html><body>Verify you are human</body></html>",
            text="Verify you are human CAPTCHA",
        ),
        url_validator=lambda _url, *, resolve_dns: None,
    )
    result = BrowserSearchProvider(
        _settings(tmp_path), worker=worker, evidence_dir=tmp_path / "evidence"
    ).search(normalize_search_queries(["blocked query"]), per_query_limit=5)

    assert result.hits == []
    assert result.runs[0].status == "PROVIDER_BLOCKED"
    assert result.runs[0].blockage_type == "CAPTCHA"
    assert result.failures[0]["error_code"] == "BROWSER_SEARCH_PROVIDER_BLOCKED"


def test_searxng_failure_can_fall_through_to_browser_provider(tmp_path) -> None:
    class FailingProvider(SearchProvider):
        provider_id = "searxng"

        def search(self, queries, *, per_query_limit):
            raise SearchProviderRetrievalError("searxng unavailable", provider=self.provider_id)

    html = '<li class="b_algo"><h2><a href="https://public.example/a">A</a></h2></li>'
    worker = BrowserWorker(
        _settings(tmp_path),
        renderer=lambda url, _timeout: _page(url, html=html, text="A"),
        url_validator=lambda _url, *, resolve_dns: None,
    )
    browser = BrowserSearchProvider(
        _settings(tmp_path), worker=worker, evidence_dir=tmp_path / "evidence"
    )

    batch = SearchGateway([FailingProvider(), browser]).search(
        normalize_search_queries(["fallback query"]),
        per_query_limit=3,
        continue_on_error=True,
    )

    assert batch.hits[0].provider == "browser_search"
    assert batch.failures[0]["provider"] == "searxng"
    assert batch.runs[0].provider == "browser_search"


def test_repeat_query_hits_cache_but_emits_new_provider_receipt(tmp_path) -> None:
    calls = []
    html = '<li class="b_algo"><h2><a href="https://public.example/a">A</a></h2></li>'

    def renderer(url, _timeout):
        calls.append(url)
        return _page(url, html=html, text="A")

    settings = _settings(tmp_path)
    worker = BrowserWorker(
        settings,
        renderer=renderer,
        url_validator=lambda _url, *, resolve_dns: None,
    )
    provider = BrowserSearchProvider(settings, worker=worker, evidence_dir=tmp_path / "evidence")
    queries = normalize_search_queries(["cached query"])

    first = provider.search(queries, per_query_limit=3)
    second = provider.search(queries, per_query_limit=3)

    assert len(calls) == 1
    assert first.runs[0].details["cache_hit"] is False
    assert second.runs[0].details["cache_hit"] is True
    assert first.runs[0].raw_response["receipt_path"] != second.runs[0].raw_response["receipt_path"]


def test_redirect_to_private_address_is_rejected(tmp_path) -> None:
    def renderer(url, _timeout):
        return BrowserPageResult(
            requested_url=url,
            final_url="http://127.0.0.1/admin",
            status="PASS",
            http_status=200,
            content_type="text/html",
            html="private",
            text="private",
            title="private",
            rendered_at=utc_now(),
        )

    worker = BrowserWorker(_settings(tmp_path), renderer=renderer)

    with pytest.raises(FetchGatewaySecurityError):
        worker.fetch("https://example.org/start")


def test_public_url_policy_rejects_credentials_and_private_targets() -> None:
    with pytest.raises(FetchGatewaySecurityError):
        validate_public_url("https://user:secret@example.org/page", resolve_dns=False)
    with pytest.raises(FetchGatewaySecurityError):
        validate_public_url("http://127.0.0.1/admin", resolve_dns=False)


class _Response:
    def __init__(self, content: bytes):
        self.url = "https://example.org/page"
        self.status_code = 200
        self.headers = {"content-type": "text/html"}
        self.content = content

    def raise_for_status(self):
        return None

    def iter_bytes(self):
        yield self.content

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Client:
    content = b""

    def __init__(self, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def stream(self, _method, _url):
        return _Response(self.content)


def test_http_redirect_is_validated_before_private_target_is_requested(tmp_path) -> None:
    calls = []

    class RedirectResponse(_Response):
        def __init__(self, url):
            super().__init__(b"")
            self.url = url
            self.status_code = 302
            self.headers = {"location": "http://127.0.0.1/admin"}

    class RedirectClient(_Client):
        def stream(self, _method, url):
            calls.append(url)
            return RedirectResponse(url)

    def validator(url, *, resolve_dns):
        if "127.0.0.1" in url:
            raise FetchGatewaySecurityError("private redirect")

    fetcher = HttpFetchGateway(
        _settings(tmp_path, browser_fetch_fallback_enabled=False),
        client_factory=RedirectClient,
        url_validator=validator,
    )

    with pytest.raises(FetchGatewaySecurityError):
        fetcher.fetch("https://example.org/start")
    assert calls == ["https://example.org/start"]


def test_static_js_shell_falls_back_to_rendered_dom(tmp_path) -> None:
    _Client.content = b'<html><body><div id="root"></div><script>boot()</script></body></html>'
    rendered = "<html><body><main>" + ("Rendered research content. " * 20) + "</main></body></html>"
    worker = BrowserWorker(
        _settings(tmp_path),
        renderer=lambda url, _timeout: _page(url, html=rendered, text="Rendered research content"),
        url_validator=lambda _url, *, resolve_dns: None,
    )
    fetcher = HttpFetchGateway(
        _settings(tmp_path),
        client_factory=_Client,
        url_validator=lambda _url, *, resolve_dns: None,
        browser_worker=worker,
    )

    fetched = fetcher.fetch("https://example.org/page")

    assert fetched.fetch_mode == "PLAYWRIGHT_RENDERED"
    assert fetched.fallback_reason in {"STATIC_TEXT_EMPTY", "STATIC_TEXT_SHORT", "STATIC_JS_SHELL"}
    assert b"Rendered research content" in fetched.raw_bytes


def test_blocked_dynamic_fallback_is_explicitly_snippet_only(tmp_path) -> None:
    _Client.content = b'<html><body><div id="root"></div></body></html>'
    worker = BrowserWorker(
        _settings(tmp_path),
        renderer=lambda url, _timeout: _page(
            url,
            html="captcha",
            text="CAPTCHA verify you are human",
        ),
        url_validator=lambda _url, *, resolve_dns: None,
    )
    fetcher = HttpFetchGateway(
        _settings(tmp_path),
        client_factory=_Client,
        url_validator=lambda _url, *, resolve_dns: None,
        browser_worker=worker,
    )

    fetched = fetcher.fetch("https://example.org/page")

    assert fetched.fetch_mode == "SNIPPET_ONLY"
    assert fetched.browser_status == "PROVIDER_BLOCKED"
    assert fetched.blockage_type == "CAPTCHA"


def test_browser_provider_preflight_requires_python_package(tmp_path, monkeypatch) -> None:
    settings = _settings(
        tmp_path,
        public_search_provider="browser_search",
        runtime_mode="LIVE",
        public_search_base_url="",
        public_research_connector_file="",
        public_research_record_file="",
        public_search_max_results=10,
    )
    monkeypatch.setattr("app.dependency_preflight.importlib.util.find_spec", lambda _name: None)
    monkeypatch.setattr(RuntimeDependencyPreflight, "_find_browser", staticmethod(lambda _value: "edge"))

    report = RuntimeDependencyPreflight(settings, pack=None)._search_report()

    assert any(issue.code == "PLAYWRIGHT_PYTHON_PACKAGE_MISSING" for issue in report.blocking_issues)
    assert any(check["name"] == "PUBLIC_SEARCH_BROWSER" for check in report.checks)


def test_browser_fallback_queries_cover_empty_required_and_low_hit_cases() -> None:
    from app.skills.search_gateway import normalize_search_queries
    from app.skills.verifiable_public_research import VerifiablePublicResearchArchiveSkill

    queries = normalize_search_queries(["q1", "q2", "q3"])
    candidates = [
        {"matched_query": "q1", "url": "https://public.example/a"},
        {"matched_query": "q1", "url": "https://public.example/b"},
    ]

    # No candidates at all: full fallback regardless of the threshold.
    assert VerifiablePublicResearchArchiveSkill._browser_fallback_queries(
        [], queries, min_hits=1, browser_required=False
    ) == queries

    # Required provider: full coverage even when SearXNG returned hits.
    assert VerifiablePublicResearchArchiveSkill._browser_fallback_queries(
        candidates, queries, min_hits=0, browser_required=True
    ) == queries

    # Threshold zero disables the low-hit fallback.
    assert VerifiablePublicResearchArchiveSkill._browser_fallback_queries(
        candidates, queries, min_hits=0, browser_required=False
    ) == []

    # Only queries below the threshold are retried through the browser.
    fallback = VerifiablePublicResearchArchiveSkill._browser_fallback_queries(
        candidates, queries, min_hits=1, browser_required=False
    )
    assert [query.query for query in fallback] == ["q2", "q3"]

    fallback = VerifiablePublicResearchArchiveSkill._browser_fallback_queries(
        candidates, queries, min_hits=3, browser_required=False
    )
    assert [query.query for query in fallback] == ["q1", "q2", "q3"]


def test_zero_candidate_error_diagnostics_summarize_selection_and_channels() -> None:
    from app.skills.verifiable_public_research import VerifiablePublicResearchArchiveSkill

    selection = VerifiablePublicResearchArchiveSkill._selection_summary(
        {
            "status": "INSUFFICIENT",
            "input_candidate_count": 12,
            "screened_candidate_count": 0,
            "selected_candidate_count": 0,
            "semantic_relevance_enforced": True,
            "semantic_relevance_counts": {"OFF_SCOPE": 12},
            "selected_by_query": {},
            "query_relevance_profiles": {"large": "object"},
            "issues": [{"code": "X"} for _ in range(15)],
        }
    )
    assert selection["input_candidate_count"] == 12
    assert selection["semantic_relevance_counts"] == {"OFF_SCOPE": 12}
    assert "query_relevance_profiles" not in selection
    assert selection["issue_count"] == 15
    assert len(selection["issues"]) == 10
    assert VerifiablePublicResearchArchiveSkill._selection_summary(None) is None

    summary = VerifiablePublicResearchArchiveSkill._discovery_provider_summary(
        {
            "provider_runs": [
                {"provider": "searxng", "result_count": 7},
                {"provider": "searxng", "result_count": 0},
                {"provider": "openalex", "result_count": 8},
            ],
            "failures": [
                {"provider": "searxng", "category": "RETRIEVAL"},
                {"provider": "crossref", "category": "RETRIEVAL"},
            ],
        }
    )
    assert summary == {
        "searxng": {"runs": 2, "hits": 7, "failures": 1},
        "openalex": {"runs": 1, "hits": 8, "failures": 0},
        "crossref": {"runs": 0, "hits": 0, "failures": 1},
    }
    assert VerifiablePublicResearchArchiveSkill._discovery_provider_summary(None) == {}
