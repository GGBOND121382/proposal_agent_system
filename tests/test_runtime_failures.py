from __future__ import annotations

from app.executor import PromptExecutionError
from app.llm import ProviderError
from app.runtime_failures import (
    FailureCategory,
    ProviderFailureKind,
    classify_runtime_failure,
)


def _wrapped_provider_error(**kwargs):
    provider = ProviderError("provider failed", **kwargs)
    try:
        raise PromptExecutionError("prompt failed") from provider
    except PromptExecutionError as exc:
        return exc


def test_typed_empty_stream_survives_prompt_execution_wrapper():
    exc = _wrapped_provider_error(
        kind=ProviderFailureKind.EMPTY_STREAM,
        phase="stream_complete",
        retryable_hint=True,
    )
    result = classify_runtime_failure(exc)
    assert result.category is FailureCategory.PROVIDER_TRANSIENT
    assert result.retryable is True
    assert result.failure_kind == "EMPTY_STREAM"
    assert result.workflow_status == "WAITING_PROVIDER"
    assert result.consumes_semantic_repair_budget is False
    assert any("ProviderError" in item for item in result.cause_chain)


def test_typed_http_429_preserves_retry_after():
    exc = _wrapped_provider_error(
        kind=ProviderFailureKind.HTTP_STATUS,
        http_status=429,
        retry_after_seconds=3.5,
        phase="request",
    )
    result = classify_runtime_failure(exc)
    assert result.category is FailureCategory.PROVIDER_TRANSIENT
    assert result.http_status == 429
    assert result.retry_after_seconds == 3.5


def test_typed_http_authentication_error_is_configuration_not_retryable():
    exc = _wrapped_provider_error(
        kind=ProviderFailureKind.HTTP_STATUS,
        http_status=401,
        phase="request",
    )
    result = classify_runtime_failure(exc)
    assert result.category is FailureCategory.CONFIGURATION
    assert result.retryable is False
    assert result.workflow_status == "WAITING_CONFIGURATION"


def test_typed_malformed_response_allows_bounded_whole_object_regeneration():
    exc = _wrapped_provider_error(
        kind=ProviderFailureKind.RESPONSE_PARSE,
        phase="response_parse",
        retryable_hint=False,
    )
    result = classify_runtime_failure(exc)
    assert result.category is FailureCategory.OUTPUT_CONTRACT
    assert result.retryable is True
    assert result.workflow_status == "BLOCKED_CONTRACT"
    assert result.consumes_semantic_repair_budget is False


def test_typed_schema_shape_failure_allows_bounded_whole_object_regeneration():
    exc = _wrapped_provider_error(
        kind=ProviderFailureKind.RESPONSE_SHAPE,
        phase="output_schema_validation",
        retryable_hint=False,
        validation_errors=["/result: expected object"],
    )
    result = classify_runtime_failure(exc)
    assert result.category is FailureCategory.OUTPUT_CONTRACT
    assert result.retryable is True
    assert result.failure_kind == "RESPONSE_SHAPE"
    assert result.workflow_status == "BLOCKED_CONTRACT"
    assert result.consumes_semantic_repair_budget is False


def test_legacy_timeout_text_precedes_json_contract_marker():
    result = classify_runtime_failure(
        PromptExecutionError("provider timeout while parsing JSON response")
    )
    assert result.category is FailureCategory.PROVIDER_TRANSIENT
    assert result.retryable is True


def test_validation_error_remains_output_contract_failure():
    result = classify_runtime_failure(
        PromptExecutionError(
            "Output schema validation failed",
            validation_errors=["/result/id: required"],
        )
    )
    assert result.category is FailureCategory.OUTPUT_CONTRACT
    assert result.retryable is False
    assert result.details == {"validation_errors": ["/result/id: required"]}
