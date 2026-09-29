from __future__ import annotations

import asyncio
from functools import wraps
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import BaseModel
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.ai.llm.base import BaseLLMProvider, LLMMessages
from backend.app.ai.llm.deepseek import DeepSeekProvider
from backend.app.ai.llm.factory import create_llm_provider
from backend.app.core.config import AppSettings
from backend.app.core.retry_policy import ProviderExecutionError, RetryPolicy
from backend.app.models import AgentRun
from backend.app.services.trace_service import (
    TraceService,
    bind_trace,
    trace_prompt_version,
)
from tests.unit.settings_helpers import build_test_settings


def async_test(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


@async_test
async def test_provider_trace_records_actual_model_latency_tokens_without_content() -> None:
    calls = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload: dict = {"choices": [{"message": {"content": '{"value": 7}'}}]}
        if calls == 1:
            payload["usage"] = {
                "prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10,
            }
        return httpx.Response(200, json=payload)

    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool)
    AgentRun.__table__.create(engine)
    try:
        async with AsyncOpenAI(
            api_key="private-key", base_url="https://llm.test/v1",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        ) as client:
            provider = DeepSeekProvider(
                build_test_settings(deepseek_model="observed-model"), client=client,
            )
            with bind_trace(
                request_id="trace-request", user_id=None, workflow_id=None,
                service=TraceService(lambda: Session(engine)),
            ), trace_prompt_version("prompt-v2"):
                for _ in range(2):
                    result = await provider.generate_structured(
                        [{"role": "user", "content": "private student answer Authorization secret"}],
                        ExampleResult,
                    )
                    assert result.value == 7
        with Session(engine) as session:
            rows = session.scalars(select(AgentRun).order_by(AgentRun.created_at)).all()
            assert len(rows) == 2
            assert all(row.model == "observed-model" for row in rows)
            assert all(row.prompt_version == "prompt-v2" for row in rows)
            assert all(row.latency_ms is not None and row.latency_ms >= 0 for row in rows)
            assert (rows[0].input_tokens, rows[0].output_tokens, rows[0].total_tokens) == (7, 3, 10)
            assert (rows[1].input_tokens, rows[1].output_tokens, rows[1].total_tokens) == (None, None, None)
            assert "private-key" not in str([row.__dict__ for row in rows])
            assert "student answer" not in str([row.__dict__ for row in rows])
            assert "Authorization" not in str([row.__dict__ for row in rows])
    finally:
        engine.dispose()


@async_test
async def test_deepseek_metadata_matches_instance_and_actual_sdk_request() -> None:
    """P4.2：修改配置对象不应改变已构造 Provider 的调用模型或追踪身份。"""

    import json

    requests: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"value": 7}'}}],
        })

    settings = build_test_settings(deepseek_model="instance-model-v2")
    async with AsyncOpenAI(
        api_key="test-only-key", base_url="https://llm.test/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    ) as client:
        provider = DeepSeekProvider(settings, client=client)
        settings.deepseek_model = "configuration-changed-after-construction"
        result = await provider.generate_structured(
            [{"role": "system", "content": "test-prompt-v1\nReturn JSON"}], ExampleResult,
        )
        metadata = provider.describe(prompt_version="test-prompt-v1")
    assert result.value == 7
    assert requests[0]["model"] == metadata["model"] == "instance-model-v2"
    assert metadata["provider"] == "deepseek"
    assert metadata["prompt_version"] == "test-prompt-v1"
    assert metadata["call_path"] == (
        "backend.app.ai.llm.deepseek.DeepSeekProvider.generate_structured"
    )
    assert "test-only-key" not in str(metadata)
    assert "https://" not in str(metadata)


def test_deepseek_with_unidentified_client_does_not_claim_real_model() -> None:
    provider = DeepSeekProvider(build_settings(), client=build_client())
    metadata = provider.describe()
    assert metadata["provider"] == "unknown"
    assert metadata["model"] == "unknown"


@async_test
async def test_deepseek_fallback_metadata_does_not_guess_final_provider() -> None:
    async with AsyncOpenAI(api_key="test-only-key") as client:
        provider = DeepSeekProvider(
            build_settings(), client=client, fallback_provider=FallbackProvider(),
        )
        assert provider.describe()["provider"] == "unknown"
        assert provider.describe()["model"] == "unknown"


@async_test
async def test_deepseek_empty_instance_model_is_unknown() -> None:
    async with AsyncOpenAI(api_key="test-only-key") as client:
        provider = DeepSeekProvider(build_settings(), client=client)
        provider._model = "   "
        assert provider.describe()["model"] == "unknown"


class ExampleResult(BaseModel):
    value: int


class FallbackProvider(BaseLLMProvider):
    provider_name = "fallback"

    async def generate_structured(
        self,
        messages: LLMMessages,
        schema: type[BaseModel],
        model: str | None = None,
    ) -> BaseModel:
        del messages, model
        return schema(value=99)


def build_settings() -> AppSettings:
    return build_test_settings(deepseek_api_key="unit-test-placeholder")


def response(content: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def build_client(*results: SimpleNamespace | BaseException) -> SimpleNamespace:
    completions = SimpleNamespace(create=AsyncMock(side_effect=list(results)))
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


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

    result = cast(
        ExampleResult,
        await provider.generate_structured(
            [{"role": "user", "content": "请返回 JSON"}],
            ExampleResult,
        ),
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

    result = cast(
        ExampleResult,
        await provider.generate_structured(
            [{"role": "user", "content": "请返回 JSON"}],
            ExampleResult,
        ),
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
