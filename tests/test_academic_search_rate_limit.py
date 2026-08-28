from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest

from app.skills import academic_search
from app.skills.academic_search import AcademicSearchClient


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _client(monkeypatch, *, interval: str, retries: str = "0") -> AcademicSearchClient:
    monkeypatch.setenv("SEMANTIC_SCHOLAR_MIN_INTERVAL_SECONDS", interval)
    monkeypatch.setenv("SEMANTIC_SCHOLAR_MAX_RETRIES", retries)
    monkeypatch.setenv("SEMANTIC_SCHOLAR_MAX_RETRY_AFTER_SECONDS", "60")
    return AcademicSearchClient(SimpleNamespace(research_fetch_timeout_seconds=5))


def test_semantic_scholar_requests_are_serialized_and_spaced(monkeypatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(academic_search.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(academic_search.time, "sleep", clock.sleep)
    client = _client(monkeypatch, interval="1")
    request_times: list[float] = []

    def fake_get_json(*_args, **_kwargs):
        request_times.append(clock.now)
        return {"data": []}

    monkeypatch.setattr(client, "_get_json", fake_get_json)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(client._get_semantic_scholar_json, params={"query": str(index)})
            for index in range(3)
        ]
        assert [future.result() for future in futures] == [{"data": []}] * 3

    assert request_times == pytest.approx([0.0, 1.0, 2.0])
    assert clock.sleeps == pytest.approx([1.0, 1.0])


def test_semantic_scholar_429_obeys_retry_after(monkeypatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(academic_search.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(academic_search.time, "sleep", clock.sleep)
    client = _client(monkeypatch, interval="0", retries="2")
    calls = 0

    def fake_get_json(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            request = httpx.Request("GET", client.SEMANTIC_SCHOLAR_URL)
            response = httpx.Response(429, request=request, headers={"Retry-After": "2"})
            raise httpx.HTTPStatusError("rate limited", request=request, response=response)
        return {"data": []}

    monkeypatch.setattr(client, "_get_json", fake_get_json)
    assert client._get_semantic_scholar_json(params={"query": "test"}) == {"data": []}
    assert calls == 2
    assert clock.sleeps == pytest.approx([2.0])


def test_semantic_scholar_does_not_retry_non_429_http_errors(monkeypatch) -> None:
    client = _client(monkeypatch, interval="0", retries="3")
    calls = 0

    def fake_get_json(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        request = httpx.Request("GET", client.SEMANTIC_SCHOLAR_URL)
        response = httpx.Response(503, request=request)
        raise httpx.HTTPStatusError("unavailable", request=request, response=response)

    monkeypatch.setattr(client, "_get_json", fake_get_json)
    with pytest.raises(httpx.HTTPStatusError):
        client._get_semantic_scholar_json(params={"query": "test"})
    assert calls == 1
