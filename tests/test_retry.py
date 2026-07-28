"""M15 AC: finite retry w/ backoff for idempotent calls; budget exhaustion
re-raises instead of retrying forever."""

from __future__ import annotations

import pytest

from vodp.infra.retry import RetryBudget, call_with_retry, is_retryable_status


class _RetryableError(Exception):
    pass


def test_is_retryable_status():
    assert is_retryable_status(429) is True
    assert is_retryable_status(500) is True
    assert is_retryable_status(404) is False
    assert is_retryable_status(None) is False


def test_call_with_retry_succeeds_after_transient_failures():
    calls = {"n": 0}
    sleeps = []

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _RetryableError("boom")
        return "ok"

    budget = RetryBudget(max_attempts=5, base_delay=1.0)
    result = call_with_retry(
        flaky, budget=budget, is_retryable=lambda e: isinstance(e, _RetryableError),
        sleep=sleeps.append,
    )
    assert result == "ok"
    assert calls["n"] == 3
    assert len(sleeps) == 2  # 2 retries before success


def test_call_with_retry_reraises_when_budget_exhausted():
    def always_fails():
        raise _RetryableError("boom")

    budget = RetryBudget(max_attempts=2, base_delay=0.01)
    with pytest.raises(_RetryableError):
        call_with_retry(
            always_fails, budget=budget, is_retryable=lambda e: True, sleep=lambda s: None,
        )


def test_call_with_retry_never_retries_non_retryable():
    calls = {"n": 0}

    def fails_once():
        calls["n"] += 1
        raise ValueError("permanent")

    budget = RetryBudget(max_attempts=5)
    with pytest.raises(ValueError):
        call_with_retry(
            fails_once, budget=budget, is_retryable=lambda e: False, sleep=lambda s: None,
        )
    assert calls["n"] == 1
