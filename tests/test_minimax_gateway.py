from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from app.llm import LLMError, _extract_json
from app.runtime_gateway import BaseModelGateway
from app.security import Route


class _FakeStreamResponse:
    status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def aread(self):
        return b""

    async def aiter_lines(self):
        events = [
            {"choices": [{"delta": {"content": '{"status":'}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": '{"status":"PASS"}'}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        for event in events:
            yield "data: " + json.dumps(event)
        yield "data: [DONE]"


class _FakeAsyncClient:
    captured: dict = {}

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def stream(self, method, url, **kwargs):
        type(self).captured = {"method": method, "url": url, **kwargs}
        return _FakeStreamResponse()


class _DisconnectingStream:
    async def __aenter__(self):
        raise httpx.RemoteProtocolError("server disconnected")

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _DisconnectingAsyncClient(_FakeAsyncClient):
    def stream(self, method, url, **kwargs):
        return _DisconnectingStream()


def _route() -> Route:
    return Route(
        prompt_id="P-TEST",
        environment="OFFLINE_LOCAL",
        model_id="offline-general-primary",
        endpoint_id="offline-primary",
        provider_model_name="MiniMax-M3",
        endpoint={
            "base_url": "https://api.minimaxi.com/v1",
            "api_key_secret": "TEST_MINIMAX_API_KEY",
        },
        profile={"temperature": 0.0, "max_output_tokens": 4096},
    )


def _gateway() -> BaseModelGateway:
    return BaseModelGateway(
        SimpleNamespace(runtime_mode="LIVE", request_timeout_seconds=240),
        SimpleNamespace(),
    )


def test_minimax_uses_json_object_streaming(monkeypatch):
    monkeypatch.setenv("TEST_MINIMAX_API_KEY", "secret")
    monkeypatch.setattr("app.llm.httpx.AsyncClient", _FakeAsyncClient)

    result = asyncio.run(
        _gateway()._invoke_live(
            _route(),
            "P-TEST",
            "Return JSON.",
            {"payload": {"value": 1}},
            {"type": "object"},
        )
    )

    sent = _FakeAsyncClient.captured["json"]
    assert sent["stream"] is True
    assert sent["reasoning_split"] is True
    assert sent["response_format"] == {"type": "json_object"}
    assert result.output == {"status": "PASS"}
    assert result.model_id == "offline-general-primary"
    assert result.response_contract_mode == "JSON_OBJECT_MINIMAX"
    assert result.provider_attempts == 1


def test_minimax_transport_error_becomes_llm_error(monkeypatch):
    monkeypatch.setenv("TEST_MINIMAX_API_KEY", "secret")
    monkeypatch.setattr("app.llm.httpx.AsyncClient", _DisconnectingAsyncClient)
    async def no_sleep(_seconds):
        return None
    monkeypatch.setattr("app.llm.asyncio.sleep", no_sleep)

    with pytest.raises(LLMError, match="after 3 attempts.*RemoteProtocolError"):
        asyncio.run(
            _gateway()._invoke_live(
                _route(),
                "P-TEST",
                "Return JSON.",
                {"payload": {"value": 1}},
                {"type": "object"},
            )
        )


def test_minimax_stream_has_total_request_deadline(monkeypatch):
    monkeypatch.setenv("TEST_MINIMAX_API_KEY", "secret")
    gateway = BaseModelGateway(
        SimpleNamespace(runtime_mode="LIVE", request_timeout_seconds=0.01),
        SimpleNamespace(),
    )

    async def never_finishes(*_args, **_kwargs):
        await asyncio.sleep(60)
        return '{"status":"PASS"}'

    monkeypatch.setattr(gateway, "_stream_chat_completion", never_finishes)

    with pytest.raises(LLMError, match="total timeout of 0.01 seconds"):
        asyncio.run(
            gateway._invoke_live(
                _route(),
                "P-TEST",
                "Return JSON.",
                {"payload": {"value": 1}},
                {"type": "object"},
            )
        )


def test_extract_json_repairs_missing_member_comma():
    assert _extract_json('prefix {"status":"PASS" "findings":[]} suffix') == {
        "status": "PASS",
        "findings": [],
    }


def test_extract_json_repairs_unescaped_quotes_inside_string():
    assert _extract_json('{"description":"use "closed-loop" method","status":"PASS"}') == {
        "description": 'use "closed-loop" method',
        "status": "PASS",
    }


def test_extract_json_rejects_truncated_object():
    with pytest.raises(LLMError, match="does not contain a JSON object"):
        _extract_json('{"status":"PASS"')


def test_extract_json_repairs_duplicate_comma_and_unquoted_key():
    assert _extract_json('{"status":"PASS",, findings:[]}') == {
        "status": "PASS",
        "findings": [],
    }


def test_extract_json_repairs_missing_comma_between_container_values():
    assert _extract_json('{"findings":[{"code":"A"} {"code":"B"}]}') == {
        "findings": [{"code": "A"}, {"code": "B"}],
    }


def test_extract_json_repairs_missing_comma_before_scalar_value():
    assert _extract_json('{"values":["A" true]}') == {
        "values": ["A", True],
    }


def test_extract_json_repairs_more_than_32_local_punctuation_errors():
    members = " ".join(
        json.dumps(f"key_{index}") + ":" + str(index)
        for index in range(80)
    )

    repaired = _extract_json("{" + members + "}")

    assert repaired == {f"key_{index}": index for index in range(80)}


def test_extract_json_reports_every_local_syntax_repair():
    from app.llm import _extract_json_with_report

    output, report = _extract_json_with_report(
        'prefix {"status":"PASS",, findings:[{"code":"A"} {"code":"B"}]} suffix'
    )
    assert output["status"] == "PASS"
    assert [item["code"] for item in output["findings"]] == ["A", "B"]
    assert report["mode"] == "LOCALLY_REPAIRED_JSON"
    assert report["repair_count"] >= 3
    assert report["surrounding_text_removed"] is True
    assert all(item.get("kind") and isinstance(item.get("position"), int) for item in report["repairs"])


class _FakePostResponse:
    def __init__(self, status_code: int, *, body: str = "", content: str = '{"status":"PASS"}'):
        self.status_code = status_code
        self.text = body
        self._payload = {"choices": [{"message": {"content": content}}]}

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://example.test/v1/chat/completions")
            response = httpx.Response(self.status_code, request=request, text=self.text)
            raise httpx.HTTPStatusError("error", request=request, response=response)

    def json(self):
        return self._payload


class _SequencePostClient:
    responses: list[_FakePostResponse] = []
    requests: list[dict] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def post(self, url, **kwargs):
        type(self).requests.append(copy.deepcopy({"url": url, **kwargs}))
        return type(self).responses.pop(0)


def _generic_route() -> Route:
    return Route(
        prompt_id="P-TEST",
        environment="OFFLINE_LOCAL",
        model_id="offline-general-primary",
        endpoint_id="offline-primary",
        provider_model_name="generic-model",
        endpoint={
            "base_url": "https://example.test/v1",
            "api_key_secret": "TEST_GENERIC_API_KEY",
        },
        profile={"temperature": 0.0, "max_output_tokens": 4096},
    )


def test_generic_provider_downgrades_only_for_structured_output_rejection(monkeypatch):
    monkeypatch.setenv("TEST_GENERIC_API_KEY", "secret")
    _SequencePostClient.requests = []
    _SequencePostClient.responses = [
        _FakePostResponse(
            400,
            body="invalid request: response_format json_schema is unsupported",
        ),
        _FakePostResponse(200),
    ]
    monkeypatch.setattr("app.llm.httpx.AsyncClient", _SequencePostClient)

    result = asyncio.run(
        _gateway()._invoke_live(
            _generic_route(),
            "P-TEST",
            "Return JSON.",
            {"payload": {"value": 1}},
            {"type": "object"},
        )
    )

    assert len(_SequencePostClient.requests) == 2
    assert _SequencePostClient.requests[0]["json"]["response_format"]["type"] == "json_schema"
    assert _SequencePostClient.requests[1]["json"]["response_format"] == {"type": "json_object"}
    assert result.response_contract_mode == "JSON_OBJECT_FALLBACK"
    assert result.provider_attempts == 2
    assert "unsupported" in str(result.fallback_reason)


def test_generic_provider_does_not_hide_404_as_response_format_fallback(monkeypatch):
    monkeypatch.setenv("TEST_GENERIC_API_KEY", "secret")
    _SequencePostClient.requests = []
    _SequencePostClient.responses = [
        _FakePostResponse(404, body="model or endpoint not found"),
    ]
    monkeypatch.setattr("app.llm.httpx.AsyncClient", _SequencePostClient)

    with pytest.raises(LLMError, match="returned 404"):
        asyncio.run(
            _gateway()._invoke_live(
                _generic_route(),
                "P-TEST",
                "Return JSON.",
                {"payload": {"value": 1}},
                {"type": "object"},
            )
        )

    assert len(_SequencePostClient.requests) == 1
    assert _SequencePostClient.requests[0]["json"]["response_format"]["type"] == "json_schema"
