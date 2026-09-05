from __future__ import annotations

import asyncio
from functools import wraps
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel, SecretStr

from backend.app.ai.llm.base import BaseLLMProvider
from backend.app.ai.llm.deepseek import DeepSeekProvider
from backend.app.ai.llm.factory import create_llm_provider
from backend.app.core.config import AppSettings
from backend.app.core.retry_policy import ProviderExecutionError, RetryPolicy


class ExampleResult(BaseModel):
    value: int


class FallbackProvider(BaseLLMProvider):
    provider_name = "fallback"

    async def generate_structured(
        self,
        messages: list[dict[str, str]],
        schema: type[BaseModel],
        model: str | None = None,
    ) -> BaseModel:
        del messages, model
        return schema(value=99)


def build_settings() -> AppSettings:
    return AppSettings(
        database_url="postgresql+psycopg://user:password@localhost:5432/eduagent",
        redis_url="redis://localhost:6379/0",
        llm_provider="deepseek",
        deepseek_api_key=SecretStr("unit-test-placeholder"),
        deepseek_base_url="https://api.deepseek.com",
        deepseek_model="deepseek-chat",
        embedding_provider="local",
        rerank_provider="none",
        confidence_threshold=0.8,
    )


def response(content: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def build_client(*results: SimpleNamespace) -> SimpleNamespace:
    completions = SimpleNamespace(create=AsyncMock(side_effect=list(results)))
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


def async_test(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


@async_test
async def test_deepseek_uses_json_mode_and_validates_pydantic_result() -> None:
    client = build_client(response('{"value": 7}'))
    provider = DeepSeekProvider(
        build_settings(),
        client=client,
        retry_policy=RetryPolicy(jitter_seconds=0),
    )

    result = await provider.generate_structured(
        [{"role": "user", "content": "请给出结果"}],
        ExampleResult,
        model="deepseek-test",
    )

    assert result == ExampleResult(value=7)
    request = client.chat.completions.create.call_args.kwargs
    assert request["model"] == "deepseek-test"
    assert request["response_format"] == {"type": "json_object"}
    assert "json" in request["messages"][0]["content"].lower()


@async_test
async def test_deepseek_retries_timeout_twice() -> None:
    client = build_client(TimeoutError(), TimeoutError(), response('{"value": 8}'))
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    provider = DeepSeekProvider(
        build_settings(),
        client=client,
        retry_policy=RetryPolicy(jitter_seconds=0),
        sleep=sleep,
    )

    result = await provider.generate_structured(
        [{"role": "user", "content": "请返回 JSON"}],
        ExampleResult,
    )

    assert result.value == 8
    assert client.chat.completions.create.await_count == 3
    assert delays == [1.0, 2.0]


@async_test
async def test_deepseek_retries_invalid_json_only_once() -> None:
    client = build_client(response("不是 JSON"), response('{"value": 9}'))
    provider = DeepSeekProvider(
        build_settings(),
        client=client,
        retry_policy=RetryPolicy(jitter_seconds=0),
        sleep=_recordless_sleep,
    )

    result = await provider.generate_structured(
        [{"role": "user", "content": "请返回 JSON"}],
        ExampleResult,
    )

    assert result.value == 9
    assert client.chat.completions.create.await_count == 2


@async_test
async def test_deepseek_reports_empty_response_after_retry_exhaustion() -> None:
    client = build_client(response(None), response(""), response("   "))
    provider = DeepSeekProvider(
        build_settings(),
        client=client,
        retry_policy=RetryPolicy(jitter_seconds=0),
        sleep=_recordless_sleep,
    )

    with pytest.raises(ProviderExecutionError) as caught:
        await provider.generate_structured(
            [{"role": "user", "content": "请返回 JSON"}],
            ExampleResult,
        )

    assert caught.value.info.code == "ProviderEmptyResponse"
    assert caught.value.info.attempt_count == 3
    assert caught.value.info.status == "ProviderEmptyResponse"


@async_test
async def test_deepseek_uses_configured_fallback_after_primary_failure() -> None:
    client = build_client(TimeoutError(), TimeoutError(), TimeoutError())
    provider = DeepSeekProvider(
        build_settings(),
        client=client,
        retry_policy=RetryPolicy(jitter_seconds=0),
        fallback_provider=FallbackProvider(),
        sleep=_recordless_sleep,
    )

    result = await provider.generate_structured(
        [{"role": "user", "content": "请返回 JSON"}],
        ExampleResult,
    )

    assert result == ExampleResult(value=99)
    assert client.chat.completions.create.await_count == 3


@async_test
async def test_deepseek_sanitizes_provider_error() -> None:
    secret = "unit-test-placeholder"
    client = build_client(RuntimeError(secret))
    provider = DeepSeekProvider(
        build_settings(),
        client=client,
        retry_policy=RetryPolicy(jitter_seconds=0),
        sleep=_recordless_sleep,
    )

    with pytest.raises(ProviderExecutionError) as caught:
        await provider.generate_structured(
            [{"role": "user", "content": "请返回 JSON"}],
            ExampleResult,
        )

    assert caught.value.info.code == "ProviderFailed"
    assert secret not in str(caught.value)


def test_importing_deepseek_registers_provider_factory() -> None:
    provider = create_llm_provider(build_settings())

    assert isinstance(provider, DeepSeekProvider)


async def _recordless_sleep(delay: float) -> None:
    del delay
