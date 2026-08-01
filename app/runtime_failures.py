from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Iterable

from .workflow_status import WorkflowStatus


class FailureCategory(str, Enum):
    CONFIGURATION = "CONFIGURATION_ERROR"
    PROVIDER_TRANSIENT = "PROVIDER_TRANSIENT_ERROR"
    OUTPUT_CONTRACT = "OUTPUT_CONTRACT_ERROR"
    SEMANTIC_REVISE = "SEMANTIC_REVISE"
    HUMAN_INPUT = "HUMAN_INPUT_REQUIRED"
    TECHNICAL = "TECHNICAL_ERROR"


class ProviderFailureKind(str, Enum):
    HTTP_STATUS = "HTTP_STATUS"
    TRANSPORT = "TRANSPORT"
    TIMEOUT = "TIMEOUT"
    EMPTY_STREAM = "EMPTY_STREAM"
    STREAM_EVENT = "STREAM_EVENT"
    RESPONSE_PARSE = "RESPONSE_PARSE"
    RESPONSE_SHAPE = "RESPONSE_SHAPE"
    OUTPUT_TRUNCATED = "OUTPUT_TRUNCATED"


@dataclass(frozen=True)
class FailureClassification:
    category: FailureCategory
    workflow_status: str
    retryable: bool
    consumes_semantic_repair_budget: bool
    reason: str
    failure_kind: str | None = None
    http_status: int | None = None
    retry_after_seconds: float | None = None
    cause_chain: tuple[str, ...] = ()
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["category"] = self.category.value
        payload["cause_chain"] = list(self.cause_chain)
        payload["details"] = dict(self.details or {})
        return payload


_TRANSIENT_MARKERS = (
    "429",
    "too many requests",
    "rate limit",
    "timeout",
    "timed out",
    "connection reset",
    "connection refused",
    "temporarily unavailable",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "empty stream",
    "stream completed without message content",
    "stream returned an invalid event",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "endpoint returned 500",
    "endpoint returned 502",
    "endpoint returned 503",
    "endpoint returned 504",
)

_CONTRACT_MARKERS = (
    "schema",
    "json",
    "validation",
    "contract",
    "required field",
    "reference id",
    "output container",
)

_CONFIGURATION_MARKERS = (
    "configuration",
    "required environment",
    "missing endpoint",
    "missing model",
    "base_url",
    "api key",
)

_TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_CONFIGURATION_HTTP_STATUSES = frozenset({401, 403, 404})
_CONTRACT_HTTP_STATUSES = frozenset({400, 409, 415, 422})
_TRANSIENT_PROVIDER_KINDS = frozenset(
    {
        ProviderFailureKind.TRANSPORT.value,
        ProviderFailureKind.TIMEOUT.value,
        ProviderFailureKind.EMPTY_STREAM.value,
        ProviderFailureKind.STREAM_EVENT.value,
    }
)
_CONTRACT_PROVIDER_KINDS = frozenset(
    {
        ProviderFailureKind.RESPONSE_PARSE.value,
        ProviderFailureKind.RESPONSE_SHAPE.value,
        ProviderFailureKind.OUTPUT_TRUNCATED.value,
    }
)


def _exception_chain(exc: BaseException, *, limit: int = 12) -> tuple[BaseException, ...]:
    values: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(values) < limit:
        values.append(current)
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return tuple(values)


def _chain_labels(chain: Iterable[BaseException]) -> tuple[str, ...]:
    return tuple(f"{type(item).__name__}: {item}" for item in chain)


def _provider_metadata(chain: Iterable[BaseException]) -> dict[str, Any] | None:
    """Read the typed provider envelope without importing app.llm.

    app.llm owns the exception classes because they derive from LLMError.  The
    stable cross-module contract is the provider_* attribute set below; wrapped
    PromptExecutionError instances retain it through their cause chain.
    """

    for item in chain:
        kind = getattr(item, "provider_failure_kind", None)
        if not kind:
            continue
        return {
            "failure_kind": str(getattr(kind, "value", kind)),
            "http_status": getattr(item, "http_status", None),
            "retry_after_seconds": getattr(item, "retry_after_seconds", None),
            "phase": getattr(item, "provider_phase", None),
            "response_excerpt": getattr(item, "response_excerpt", None),
            "retryable_hint": getattr(item, "retryable_hint", None),
        }
    return None


def _classification_from_provider(
    metadata: dict[str, Any],
    *,
    cause_chain: tuple[str, ...],
) -> FailureClassification:
    kind = str(metadata.get("failure_kind") or "")
    status_raw = metadata.get("http_status")
    status = int(status_raw) if isinstance(status_raw, int) or str(status_raw or "").isdigit() else None
    retry_after = metadata.get("retry_after_seconds")
    retryable_hint = metadata.get("retryable_hint")
    details = {
        key: value
        for key, value in metadata.items()
        if key not in {"failure_kind", "http_status", "retry_after_seconds"}
        and value is not None
    }

    if kind == ProviderFailureKind.HTTP_STATUS.value:
        if status in _TRANSIENT_HTTP_STATUSES or retryable_hint is True:
            return FailureClassification(
                FailureCategory.PROVIDER_TRANSIENT,
                WorkflowStatus.WAITING_PROVIDER.value,
                True,
                False,
                f"provider HTTP {status} is transient",
                kind,
                status,
                retry_after,
                cause_chain,
                details,
            )
        if status in _CONFIGURATION_HTTP_STATUSES:
            return FailureClassification(
                FailureCategory.CONFIGURATION,
                WorkflowStatus.WAITING_CONFIGURATION.value,
                False,
                False,
                f"provider HTTP {status} indicates endpoint, model, or credential configuration",
                kind,
                status,
                retry_after,
                cause_chain,
                details,
            )
        if status in _CONTRACT_HTTP_STATUSES:
            return FailureClassification(
                FailureCategory.OUTPUT_CONTRACT,
                WorkflowStatus.BLOCKED_CONTRACT.value,
                False,
                False,
                f"provider HTTP {status} rejected the declared request or response contract",
                kind,
                status,
                retry_after,
                cause_chain,
                details,
            )
        return FailureClassification(
            FailureCategory.TECHNICAL,
            WorkflowStatus.BLOCKED_TECHNICAL.value,
            False,
            False,
            f"provider HTTP {status or 'unknown'} is not classified as retryable",
            kind,
            status,
            retry_after,
            cause_chain,
            details,
        )

    if kind in _TRANSIENT_PROVIDER_KINDS or retryable_hint is True:
        return FailureClassification(
            FailureCategory.PROVIDER_TRANSIENT,
            WorkflowStatus.WAITING_PROVIDER.value,
            True,
            False,
            "typed provider transport or streaming failure is transient",
            kind,
            status,
            retry_after,
            cause_chain,
            details,
        )

    if kind in _CONTRACT_PROVIDER_KINDS:
        return FailureClassification(
            FailureCategory.OUTPUT_CONTRACT,
            WorkflowStatus.BLOCKED_CONTRACT.value,
            False,
            False,
            "provider response could not satisfy the declared output contract",
            kind,
            status,
            retry_after,
            cause_chain,
            details,
        )

    return FailureClassification(
        FailureCategory.TECHNICAL,
        WorkflowStatus.BLOCKED_TECHNICAL.value,
        False,
        False,
        "typed provider failure has an unknown kind",
        kind or None,
        status,
        retry_after,
        cause_chain,
        details,
    )


def classify_runtime_failure(exc: BaseException) -> FailureClassification:
    chain = _exception_chain(exc)
    labels = _chain_labels(chain)

    if any(item.__class__.__name__ == "WorkflowInputRequired" for item in chain):
        return FailureClassification(
            FailureCategory.HUMAN_INPUT,
            WorkflowStatus.WAITING_GATE.value,
            False,
            False,
            "workflow requested explicit human input",
            cause_chain=labels,
        )

    provider = _provider_metadata(chain)
    if provider is not None:
        return _classification_from_provider(provider, cause_chain=labels)

    validation_errors = next(
        (
            getattr(item, "validation_errors", None)
            for item in chain
            if getattr(item, "validation_errors", None)
        ),
        None,
    )
    text = " | ".join(labels).lower()

    # Transient transport markers precede textual contract markers.  Typed
    # exceptions are preferred, but this preserves safe behavior for legacy
    # wrappers such as "timeout while parsing JSON response".
    if any(marker in text for marker in _TRANSIENT_MARKERS):
        return FailureClassification(
            FailureCategory.PROVIDER_TRANSIENT,
            WorkflowStatus.WAITING_PROVIDER.value,
            True,
            False,
            "legacy exception text indicates a transient provider or transport failure",
            cause_chain=labels,
        )

    if validation_errors or any(marker in text for marker in _CONTRACT_MARKERS):
        return FailureClassification(
            FailureCategory.OUTPUT_CONTRACT,
            WorkflowStatus.BLOCKED_CONTRACT.value,
            False,
            False,
            "provider output could not satisfy the declared output contract",
            cause_chain=labels,
            details={"validation_errors": list(validation_errors or [])},
        )

    if any(marker in text for marker in _CONFIGURATION_MARKERS):
        return FailureClassification(
            FailureCategory.CONFIGURATION,
            WorkflowStatus.WAITING_CONFIGURATION.value,
            False,
            False,
            "runtime configuration or dependency is incomplete",
            cause_chain=labels,
        )

    return FailureClassification(
        FailureCategory.TECHNICAL,
        WorkflowStatus.BLOCKED_TECHNICAL.value,
        False,
        False,
        "unclassified technical failure",
        cause_chain=labels,
    )


def semantic_revise_classification() -> FailureClassification:
    return FailureClassification(
        FailureCategory.SEMANTIC_REVISE,
        WorkflowStatus.BLOCKED_CONTENT.value,
        False,
        True,
        "critic requested semantic revision",
    )
