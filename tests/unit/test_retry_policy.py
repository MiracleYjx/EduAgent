from __future__ import annotations

import asyncio
from functools import wraps

import pytest

from backend.app.core.retry_policy import (
    ProviderCallError,
    ProviderExecutionError,
    RetryPolicy,
)


def async_test(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


@async_test
async def test_retry_policy_uses_backoff_and_stops_after_two_retries() -> None:
    calls = 0
    delays: list[float] = []

    async def operation() -> str:
        nonlocal calls
        calls += 1
        raise ProviderCallError(
            "ProviderTimeout",
            "Provider 请求超时。",
            retryable=True,
        )

    async def sleep(delay: float) -> None:
        delays.append(delay)

    with pytest.raises(ProviderExecutionError) as caught:
        await RetryPolicy(jitter_seconds=0).execute(operation, sleep=sleep)

    assert calls == 3
    assert delays == [1.0, 2.0]
    assert caught.value.info.code == "ProviderTimeout"
    assert caught.value.info.attempt_count == 3


@async_test
async def test_retry_policy_caps_retry_after_to_thirty_seconds() -> None:
    delays: list[float] = []

    async def operation() -> None:
        raise ProviderCallError(
            "ProviderRateLimited",
            "Provider 请求受限。",
            retryable=True,
            retry_after=90,
        )

    async def sleep(delay: float) -> None:
        delays.append(delay)

    with pytest.raises(ProviderExecutionError):
        await RetryPolicy(jitter_seconds=0).execute(operation, sleep=sleep)

    assert delays == [30.0, 30.0]


@async_test
async def test_retry_policy_can_use_one_fallback_attempt() -> None:
    primary_calls = 0
    fallback_calls = 0

    async def primary() -> str:
        nonlocal primary_calls
        primary_calls += 1
        raise ProviderCallError(
            "ProviderFailed",
            "主 Provider 调用失败。",
            retryable=False,
        )

    async def fallback() -> str:
        nonlocal fallback_calls
        fallback_calls += 1
        return "备用结果"

    result = await RetryPolicy().execute(
        primary,
        fallback=fallback,
        sleep=_no_sleep,
    )

    assert result == "备用结果"
    assert primary_calls == 1
    assert fallback_calls == 1


@async_test
async def test_retry_policy_keeps_fallback_failure_safe() -> None:
    secret = "unit-test-placeholder"

    async def primary() -> None:
        raise ProviderCallError(
            "ProviderEmptyResponse",
            "Provider 返回为空。",
            retryable=True,
        )

    async def fallback() -> None:
        raise RuntimeError(secret)

    with pytest.raises(ProviderExecutionError) as caught:
        await RetryPolicy(jitter_seconds=0).execute(
            primary,
            fallback=fallback,
            sleep=_no_sleep,
        )

    assert caught.value.info.code == "ProviderFailed"
    assert caught.value.info.attempt_count == 4
    assert secret not in str(caught.value)


async def _no_sleep(delay: float) -> None:
    del delay
