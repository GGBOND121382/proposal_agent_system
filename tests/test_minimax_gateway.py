from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from app.llm import LLMError, ProviderError, _extract_json
from app.runtime_failures import ProviderFailureKind
from app.runtime_gateway import AuditedModelGateway, BaseModelGateway
from app.security import Route


def _function_stream_events(output_json: str) -> list[dict]:
    wrapper = json.dumps({"output_json": output_json})
    split = len(wrapper) // 2
    return [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {
                                    "name": "submit_P-TEST",
                                    "arguments": wrapper[:split],
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": wrapper[split:]},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]



def _truncated_function_stream_events(output_json_prefix: str) -> list[dict]:
    wrapper_prefix = json.dumps({"output_json": output_json_prefix})[:-2]
    return [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {
                                    "name": "submit_P-TEST",
                                    "arguments": wrapper_prefix,
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "length"}]},
    ]



def _assistant_json_stream_events(content: str) -> list[dict]:
    split = len(content) // 2
    return [
        {
            "choices": [
                {
                    "delta": {"content": content[:split]},
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {"content": content[split:]},
                    "finish_reason": None,
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]


def _wrong_tool_stream_events(content: str = "") -> list[dict]:
    return [
        {
            "choices": [
                {
                    "delta": {
                        "content": content,
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {
                                    "name": "some_other_tool",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                    "finish_reason": None,
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]


class _FakeStreamResponse:
    status_code = 200

    def __init__(self, events: list[dict] | None = None):
        self.events = events or _function_stream_events('{"status":"PASS"}')

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def aread(self):
        return b""

    async def aiter_lines(self):
        for event in self.events:
            yield "data: " + json.dumps(event)
        yield "data: [DONE]"


class _FakeAsyncClient:
    captured: dict = {}
    stream_events: list[dict] | None = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def stream(self, method, url, **kwargs):
        type(self).captured = {"method": method, "url": url, **kwargs}
        return _FakeStreamResponse(type(self).stream_events)

    async def post(self, url, **kwargs):
        type(self).captured = {"method": "POST", "url": url, **kwargs}
        return _FakePostResponse(200, tool_arguments='{"status":"PASS"}')


class _DisconnectingStream:
    async def __aenter__(self):
        raise httpx.RemoteProtocolError("server disconnected")

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _DisconnectingAsyncClient(_FakeAsyncClient):
    def stream(self, method, url, **kwargs):
        return _DisconnectingStream()

    async def post(self, url, **kwargs):
        raise httpx.RemoteProtocolError("server disconnected")


class _TimingOutAsyncClient(_FakeAsyncClient):
    def stream(self, method, url, **kwargs):
        class _TimingOutStream:
            async def __aenter__(self):
                raise httpx.ReadTimeout("read timed out")

            async def __aexit__(self, exc_type, exc, traceback):
                return False

        return _TimingOutStream()


def _route(
    provider_model_name: str = "MiniMax-M3",
    desired_output_tokens: int = 4096,
) -> Route:
    return Route(
        prompt_id="P-TEST",
        environment="OFFLINE_LOCAL",
        model_id="offline-general-primary",
        endpoint_id="offline-primary",
        provider_model_name=provider_model_name,
        endpoint={
            "base_url": "https://api.minimaxi.com/v1",
            "api_key_secret": "TEST_MINIMAX_API_KEY",
        },
        profile={
            "temperature": 0.0,
            "desired_output_tokens": desired_output_tokens,
        },
    )


def _gateway() -> BaseModelGateway:
    capabilities = {
        "MiniMax-M3": {
            "context_window_tokens": 1_000_000,
            "recommended_output_tokens": 131_072,
            "hard_max_output_tokens": 524_288,
            "output_parameter": "max_completion_tokens",
        },
        "MiniMax-M2.7-highspeed": {
            "context_window_tokens": 204_800,
            "recommended_output_tokens": 65_536,
            "hard_max_output_tokens": 204_800,
            "output_parameter": "max_completion_tokens",
        },
    }

    def model_capability(name: str):
        if name not in capabilities:
            raise KeyError(name)
        return copy.deepcopy(capabilities[name])

    return BaseModelGateway(
        SimpleNamespace(runtime_mode="LIVE", request_timeout_seconds=240),
        SimpleNamespace(model_capability=model_capability),
    )



def test_minimax_m3_planning_budget_uses_model_capability_not_endpoint_ceiling():
    gateway = _gateway()
    report = gateway._resolve_output_token_budget(
        _route(desired_output_tokens=131_072),
        "Return one JSON object.",
        {"payload": {"value": 1}},
        {"type": "object"},
    )

    assert report["context_window_tokens"] == 1_000_000
    assert report["recommended_output_tokens"] == 131_072
    assert report["hard_max_output_tokens"] == 524_288
    assert report["desired_output_tokens"] == 131_072
    assert report["effective_output_tokens"] == 131_072
    assert report["output_parameter"] == "max_completion_tokens"


def test_minimax_m27_highspeed_uses_its_own_model_capability():
    gateway = _gateway()
    report = gateway._resolve_output_token_budget(
        _route("MiniMax-M2.7-highspeed", desired_output_tokens=65_536),
        "Return one JSON object.",
        {"payload": {"value": 1}},
        {"type": "object"},
    )

    assert report["context_window_tokens"] == 204_800
    assert report["recommended_output_tokens"] == 65_536
    assert report["effective_output_tokens"] == 65_536



def test_minimax_context_headroom_clamps_task_budget_before_provider_call():
    gateway = _gateway()
    report = gateway._resolve_output_token_budget(
        _route("MiniMax-M2.7-highspeed", desired_output_tokens=65_536),
        "研" * 150_000,
        {"payload": {"value": 1}},
        {"type": "object"},
    )

    assert 4096 <= report["effective_output_tokens"] < 65_536
    assert report["clamped_by_context"] is True
    assert report["estimated_input_tokens"] >= 150_000



def test_minimax_unknown_provider_model_fails_closed_before_live_call():
    gateway = _gateway()
    with pytest.raises(LLMError, match="capability is not registered"):
        gateway._resolve_output_token_budget(
            _route("MiniMax-M4", desired_output_tokens=65_536),
            "Return one JSON object.",
            {"payload": {"value": 1}},
            {"type": "object"},
        )



def test_minimax_uses_streamed_serialized_json_function(monkeypatch):
    monkeypatch.setenv("TEST_MINIMAX_API_KEY", "secret")
    _FakeAsyncClient.stream_events = None
    monkeypatch.setattr("app.llm.httpx.AsyncClient", _FakeAsyncClient)

    result = asyncio.run(
        _gateway()._invoke_live(
            _route(),
            "P-TEST",
            "Return JSON.",
            {"payload": {"value": 1}},
            {
                "type": "object",
                "properties": {
                    "value": {"type": ["string", "number", "boolean"]},
                },
            },
        )
    )

    sent = _FakeAsyncClient.captured["json"]
    assert sent["max_completion_tokens"] == 4096
    assert "max_tokens" not in sent
    assert sent["stream"] is True
    assert sent["reasoning_split"] is True
    assert "response_format" not in sent
    assert sent["tool_choice"] == "auto"
    assert len(sent["tools"]) == 1
    assert sent["tools"][0]["function"]["name"] == "submit_P-TEST"
    assert sent["tools"][0]["function"]["strict"] is True
    assert "sole `output_json` argument" in sent["messages"][0]["content"]
    assert "compact JSON" in sent["messages"][0]["content"]
    assert "no pretty-print indentation" in sent["messages"][0]["content"]
    parameters = sent["tools"][0]["function"]["parameters"]
    assert parameters["additionalProperties"] is False
    assert parameters["required"] == ["output_json"]
    assert parameters["properties"]["output_json"]["type"] == "string"
    assert result.output == {"status": "PASS"}
    assert result.model_id == "offline-general-primary"
    assert result.response_contract_mode == "FUNCTION_SERIALIZED_JSON_STREAM_MINIMAX"
    assert result.parse_report["wire_protocol"] == "STRICT_MINIMAX_TOOL_OR_JSON"
    assert result.parse_report["wire_wrapper_parse_report"]["transport"] == "FUNCTION_OUTPUT_JSON"
    assert result.parse_report["wire_wrapper_parse_report"]["repair_count"] == 0
    assert result.provider_attempts == 1


def test_minimax_accepts_strict_assistant_json_when_auto_tool_is_not_called(monkeypatch):
    monkeypatch.setenv("TEST_MINIMAX_API_KEY", "secret")
    _FakeAsyncClient.stream_events = _assistant_json_stream_events('{"status":"PASS"}')
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

    assert result.output == {"status": "PASS"}
    assert result.raw_text == '{"status":"PASS"}'
    assert result.response_contract_mode == "FUNCTION_SERIALIZED_JSON_STREAM_MINIMAX"
    assert result.parse_report["wire_protocol"] == "STRICT_MINIMAX_TOOL_OR_JSON"
    assert result.parse_report["wire_wrapper_parse_report"]["mode"] == "STRICT_ASSISTANT_JSON_FALLBACK"
    assert result.parse_report["wire_wrapper_parse_report"]["transport"] == "ASSISTANT_JSON"
    _FakeAsyncClient.stream_events = None


def test_minimax_rejects_assistant_prose_even_when_it_contains_json(monkeypatch):
    monkeypatch.setenv("TEST_MINIMAX_API_KEY", "secret")
    _FakeAsyncClient.stream_events = _assistant_json_stream_events(
        'Here is the result: {"status":"PASS"}'
    )
    monkeypatch.setattr("app.llm.httpx.AsyncClient", _FakeAsyncClient)

    with pytest.raises(ProviderError, match="strict JSON object") as captured:
        asyncio.run(
            _gateway()._invoke_live(
                _route(),
                "P-TEST",
                "Return JSON.",
                {"payload": {"value": 1}},
                {"type": "object"},
            )
        )

    assert captured.value.provider_failure_kind is ProviderFailureKind.RESPONSE_PARSE
    assert captured.value.provider_phase == "assistant_json_parse"
    _FakeAsyncClient.stream_events = None


def test_minimax_rejects_code_fenced_assistant_json(monkeypatch):
    monkeypatch.setenv("TEST_MINIMAX_API_KEY", "secret")
    _FakeAsyncClient.stream_events = _assistant_json_stream_events(
        '```json\n{"status":"PASS"}\n```'
    )
    monkeypatch.setattr("app.llm.httpx.AsyncClient", _FakeAsyncClient)

    with pytest.raises(ProviderError, match="strict JSON object") as captured:
        asyncio.run(
            _gateway()._invoke_live(
                _route(),
                "P-TEST",
                "Return JSON.",
                {"payload": {"value": 1}},
                {"type": "object"},
            )
        )

    assert captured.value.provider_failure_kind is ProviderFailureKind.RESPONSE_PARSE
    _FakeAsyncClient.stream_events = None


def test_minimax_rejects_wrong_tool_even_if_assistant_content_is_valid_json(monkeypatch):
    monkeypatch.setenv("TEST_MINIMAX_API_KEY", "secret")
    _FakeAsyncClient.stream_events = _wrong_tool_stream_events('{"status":"PASS"}')
    monkeypatch.setattr("app.llm.httpx.AsyncClient", _FakeAsyncClient)

    with pytest.raises(ProviderError, match="must either call submit_P-TEST") as captured:
        asyncio.run(
            _gateway()._invoke_live(
                _route(),
                "P-TEST",
                "Return JSON.",
                {"payload": {"value": 1}},
                {"type": "object"},
            )
        )

    assert captured.value.provider_failure_kind is ProviderFailureKind.RESPONSE_SHAPE
    assert captured.value.provider_phase == "function_call"
    _FakeAsyncClient.stream_events = None


def test_strict_schema_response_is_not_locally_repaired(monkeypatch):
    monkeypatch.setenv("TEST_MINIMAX_API_KEY", "secret")
    _FakeAsyncClient.stream_events = _function_stream_events('{"status":"PASS"')
    monkeypatch.setattr("app.llm.httpx.AsyncClient", _FakeAsyncClient)

    with pytest.raises(ProviderError, match="malformed JSON") as captured:
        asyncio.run(
            _gateway()._invoke_live(
                _route(),
                "P-TEST",
                "Return JSON.",
                {"payload": {"value": 1}},
                {"type": "object"},
            )
        )

    assert captured.value.provider_failure_kind is ProviderFailureKind.RESPONSE_PARSE
    _FakeAsyncClient.stream_events = None



def test_audited_gateway_persists_full_raw_response_before_parse_failure(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("TEST_MINIMAX_API_KEY", "secret")
    monkeypatch.setenv("MODEL_CALL_EVIDENCE_DIR", str(tmp_path / "model_calls"))
    _FakeAsyncClient.stream_events = _function_stream_events('{"status":"PASS"')
    monkeypatch.setattr("app.llm.httpx.AsyncClient", _FakeAsyncClient)

    base = _gateway()
    gateway = AuditedModelGateway(
        SimpleNamespace(
            runtime_mode="LIVE",
            request_timeout_seconds=240,
            data_dir=tmp_path,
        ),
        base.pack,
    )

    with pytest.raises(ProviderError) as captured:
        asyncio.run(
            gateway.invoke(
                _route(),
                "P-TEST",
                "Return JSON.",
                {"payload": {"value": 1}},
                {"type": "object"},
                call_key="call-malformed-response",
            )
        )

    assert captured.value.provider_failure_kind is ProviderFailureKind.RESPONSE_PARSE

    provider_raw_path, provider_meta_path = gateway.evidence_store.provider_response_paths(
        "call-malformed-response",
        1,
    )
    rejected_path, failed_meta_path = gateway.evidence_store.failed_response_paths(
        "call-malformed-response"
    )
    _, parsed_path, success_meta_path = gateway.evidence_store.response_paths(
        "call-malformed-response"
    )

    provider_raw = provider_raw_path.read_text(encoding="utf-8")
    assert "data: " in provider_raw
    assert "submit_P-TEST" in provider_raw
    assert provider_meta_path.exists()
    assert rejected_path.read_text(encoding="utf-8") == '{"status":"PASS"'
    assert failed_meta_path.exists()
    assert not parsed_path.exists()
    assert not success_meta_path.exists()

    failed = gateway.evidence_store.load_failed_response("call-malformed-response")
    assert failed["metadata"]["failure_kind"] == "RESPONSE_PARSE"
    assert failed["metadata"]["provider_phase"] == "response_parse"
    assert failed["metadata"]["provider_response_count"] == 1
    assert failed["rejected_text"] == '{"status":"PASS"'

    _FakeAsyncClient.stream_events = None




def test_audited_gateway_persists_partial_raw_response_on_output_truncation(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("TEST_MINIMAX_API_KEY", "secret")
    monkeypatch.setenv("MODEL_CALL_EVIDENCE_DIR", str(tmp_path / "model_calls"))
    _FakeAsyncClient.stream_events = _truncated_function_stream_events(
        '{"status":"PASS","result":'
    )
    monkeypatch.setattr("app.llm.httpx.AsyncClient", _FakeAsyncClient)

    base = _gateway()
    gateway = AuditedModelGateway(
        SimpleNamespace(
            runtime_mode="LIVE",
            request_timeout_seconds=240,
            data_dir=tmp_path,
        ),
        base.pack,
    )

    with pytest.raises(ProviderError) as captured:
        asyncio.run(
            gateway.invoke(
                _route(),
                "P-TEST",
                "Return JSON.",
                {"payload": {"value": 1}},
                {"type": "object"},
                call_key="call-truncated-response",
            )
        )

    assert captured.value.provider_failure_kind is ProviderFailureKind.OUTPUT_TRUNCATED
    provider_raw_path, _ = gateway.evidence_store.provider_response_paths(
        "call-truncated-response",
        1,
    )
    rejected_path, failed_meta_path = gateway.evidence_store.failed_response_paths(
        "call-truncated-response"
    )
    provider_raw = provider_raw_path.read_text(encoding="utf-8")
    assert '"finish_reason": "length"' in provider_raw
    assert rejected_path.exists()
    assert failed_meta_path.exists()

    failed = gateway.evidence_store.load_failed_response("call-truncated-response")
    assert failed["metadata"]["failure_kind"] == "OUTPUT_TRUNCATED"
    assert failed["metadata"]["provider_response_count"] == 1
    assert failed["rejected_text"]

    _FakeAsyncClient.stream_events = None



def test_minimax_transport_error_is_typed_for_workflow_owned_retry(monkeypatch):
    monkeypatch.setenv("TEST_MINIMAX_API_KEY", "secret")
    monkeypatch.setattr("app.llm.httpx.AsyncClient", _DisconnectingAsyncClient)
    with pytest.raises(ProviderError, match="RemoteProtocolError") as captured:
        asyncio.run(
            _gateway()._invoke_live(
                _route(),
                "P-TEST",
                "Return JSON.",
                {"payload": {"value": 1}},
                {"type": "object"},
            )
        )
    assert captured.value.provider_failure_kind is ProviderFailureKind.TRANSPORT
    assert captured.value.retryable_hint is True


def test_minimax_request_timeout_is_typed_for_workflow_owned_retry(monkeypatch):
    monkeypatch.setenv("TEST_MINIMAX_API_KEY", "secret")
    monkeypatch.setattr("app.llm.httpx.AsyncClient", _TimingOutAsyncClient)

    with pytest.raises(ProviderError, match="ReadTimeout") as captured:
        asyncio.run(
            _gateway()._invoke_live(
                _route(),
                "P-TEST",
                "Return JSON.",
                {"payload": {"value": 1}},
                {"type": "object"},
            )
        )
    assert captured.value.provider_failure_kind is ProviderFailureKind.TIMEOUT
    assert captured.value.retryable_hint is True


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
    def __init__(
        self,
        status_code: int,
        *,
        body: str = "",
        content: str = '{"status":"PASS"}',
        tool_arguments: str | None = None,
    ):
        self.status_code = status_code
        self.text = body
        self.headers = {}
        message = {"content": content}
        if tool_arguments is not None:
            message["tool_calls"] = [
                {
                    "id": "call-test",
                    "type": "function",
                    "function": {
                        "name": "submit_P-TEST",
                        "arguments": tool_arguments,
                    },
                }
            ]
        self._payload = {"choices": [{"message": message}]}

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
        profile={"temperature": 0.0, "desired_output_tokens": 4096},
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
