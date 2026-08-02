from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .runtime_failures import FailureCategory, FailureClassification, WorkflowStatus


@dataclass(frozen=True)
class RetryDecision:
    should_retry: bool
    completed_attempts: int
    retry_number: int
    max_retries: int
    max_attempts: int
    delay_seconds: float
    waiting_status: str
    exhausted_status: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "should_retry": self.should_retry,
            "completed_attempts": self.completed_attempts,
            "retry_number": self.retry_number,
            "max_retries": self.max_retries,
            "max_attempts": self.max_attempts,
            "delay_seconds": self.delay_seconds,
            "waiting_status": self.waiting_status,
            "exhausted_status": self.exhausted_status,
            "reason": self.reason,
        }


class ProviderRetriesExhausted(RuntimeError):
    def __init__(
        self,
        original_exception: BaseException,
        *,
        classification: FailureClassification,
        decision: RetryDecision,
        failure_payload: dict[str, Any],
    ) -> None:
        super().__init__(
            f"Provider retries exhausted after {decision.completed_attempts} attempt(s): "
            f"{original_exception}"
        )
        self.original_exception = original_exception
        self.classification = classification
        self.decision = decision
        self.failure_payload = dict(failure_payload)


@dataclass(frozen=True)
class RetryPolicy:
    """Finite, deterministic retry policy for one workflow business node.

    ``max_retries`` is the number of retries after the initial call.  Therefore
    ``max_retries=2`` permits at most three provider attempts in total.
    """

    max_retries: int = 2
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 8.0

    @classmethod
    def from_options(cls, options: dict[str, Any] | None) -> "RetryPolicy":
        values = options or {}
        max_retries = cls._bounded_int(
            values.get("provider_retry_limit", 2), minimum=0, maximum=5, default=2
        )
        base_delay = cls._bounded_float(
            values.get("provider_retry_base_delay_seconds", 1.0),
            minimum=0.0,
            maximum=60.0,
            default=1.0,
        )
        max_delay = cls._bounded_float(
            values.get("provider_retry_max_delay_seconds", 8.0),
            minimum=base_delay,
            maximum=300.0,
            default=max(8.0, base_delay),
        )
        return cls(
            max_retries=max_retries,
            base_delay_seconds=base_delay,
            max_delay_seconds=max_delay,
        )

    @staticmethod
    def _bounded_int(value: Any, *, minimum: int, maximum: int, default: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(parsed, maximum))

    @staticmethod
    def _bounded_float(
        value: Any,
        *,
        minimum: float,
        maximum: float,
        default: float,
    ) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(parsed, maximum))

    def decide(
        self,
        classification: FailureClassification,
        *,
        completed_attempts: int,
    ) -> RetryDecision:
        attempts = max(1, int(completed_attempts))
        retry_number = attempts
        max_attempts = self.max_retries + 1
        retryable = bool(classification.retryable)
        should_retry = retryable and retry_number <= self.max_retries

        if should_retry:
            retry_after = classification.retry_after_seconds
            if retry_after is not None:
                delay = max(0.0, min(float(retry_after), self.max_delay_seconds))
                reason = "retryable provider failure; honoring Retry-After within policy cap"
            else:
                delay = min(
                    self.base_delay_seconds * (2 ** max(0, retry_number - 1)),
                    self.max_delay_seconds,
                )
                reason = "retryable provider failure; applying deterministic exponential backoff"
        else:
            delay = 0.0
            reason = (
                "provider failure is not retryable"
                if not retryable
                else "provider retry budget is exhausted"
            )

        return RetryDecision(
            should_retry=should_retry,
            completed_attempts=attempts,
            retry_number=retry_number,
            max_retries=self.max_retries,
            max_attempts=max_attempts,
            delay_seconds=delay,
            waiting_status=WorkflowStatus.WAITING_PROVIDER.value,
            exhausted_status=(
                WorkflowStatus.BLOCKED_PROVIDER.value
                if classification.category is FailureCategory.PROVIDER_TRANSIENT
                else classification.workflow_status
            ),
            reason=reason,
        )
