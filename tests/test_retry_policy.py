from __future__ import annotations

from app.retry_policy import RetryPolicy
from app.runtime_failures import FailureCategory, FailureClassification


def _classification(*, retryable=True, retry_after=None):
    return FailureClassification(
        category=FailureCategory.PROVIDER_TRANSIENT,
        workflow_status="WAITING_PROVIDER",
        retryable=retryable,
        consumes_semantic_repair_budget=False,
        reason="test",
        retry_after_seconds=retry_after,
    )


def test_retry_limit_means_retries_after_initial_call():
    policy = RetryPolicy(max_retries=2, base_delay_seconds=1, max_delay_seconds=10)
    first = policy.decide(_classification(), completed_attempts=1)
    second = policy.decide(_classification(), completed_attempts=2)
    third = policy.decide(_classification(), completed_attempts=3)
    assert first.should_retry is True
    assert second.should_retry is True
    assert third.should_retry is False
    assert first.max_attempts == 3
    assert third.exhausted_status == "BLOCKED_PROVIDER"


def test_retry_after_is_honored_but_capped():
    policy = RetryPolicy(max_retries=2, base_delay_seconds=1, max_delay_seconds=5)
    decision = policy.decide(
        _classification(retry_after=30), completed_attempts=1
    )
    assert decision.should_retry is True
    assert decision.delay_seconds == 5


def test_backoff_is_deterministic_without_jitter():
    policy = RetryPolicy(max_retries=3, base_delay_seconds=0.5, max_delay_seconds=10)
    assert policy.decide(_classification(), completed_attempts=1).delay_seconds == 0.5
    assert policy.decide(_classification(), completed_attempts=2).delay_seconds == 1.0
    assert policy.decide(_classification(), completed_attempts=3).delay_seconds == 2.0


def test_nonretryable_failure_never_retries_even_with_budget():
    policy = RetryPolicy(max_retries=5)
    decision = policy.decide(
        _classification(retryable=False), completed_attempts=1
    )
    assert decision.should_retry is False
    assert decision.reason == "provider failure is not retryable"


def test_options_are_bounded_and_zero_retries_is_supported():
    policy = RetryPolicy.from_options(
        {
            "provider_retry_limit": -3,
            "provider_retry_base_delay_seconds": -1,
            "provider_retry_max_delay_seconds": 9999,
        }
    )
    assert policy.max_retries == 0
    assert policy.base_delay_seconds == 0
    assert policy.max_delay_seconds == 300
    assert policy.decide(_classification(), completed_attempts=1).should_retry is False
