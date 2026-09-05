import asyncio
from collections.abc import Sequence
from typing import Any

import pytest
from pydantic import BaseModel, SecretStr

from backend.app.ai.llm.base import BaseLLMProvider
from backend.app.ai.llm.factory import (
    LLMProviderFactory,
    ProviderAlreadyRegisteredError,
    UnsupportedLLMProviderError,
)
from backend.app.core.config import AppSettings


class ExampleResult(BaseModel):
    value: int


class StubProvider(BaseLLMProvider):
    provider_name = "deepseek"

    async def generate_structured(
        self,
        messages: Sequence[dict[str, str]],
        schema: type[BaseModel],
        model: str | None = None,
    ) -> BaseModel:
        del messages, model
        return schema(value=7)


def build_settings(provider: str = "deepseek") -> AppSettings:
    return AppSettings(
        database_url="postgresql+psycopg://user:password@localhost:5432/eduagent",
        redis_url="redis://localhost:6379/0",
        llm_provider=provider,
        deepseek_api_key=SecretStr("test-key"),
        deepseek_base_url="https://api.deepseek.com",
        deepseek_model="deepseek-chat",
        embedding_provider="local",
        rerank_provider="none",
        confidence_threshold=0.8,
    )


def test_base_provider_exposes_structured_output_contract() -> None:
    provider = StubProvider()

    result = asyncio.run(
        provider.generate_structured(
            [{"role": "user", "content": "请返回结构化结果"}],
            ExampleResult,
        )
    )

    assert isinstance(result, ExampleResult)
    assert result.value == 7


def test_factory_creates_registered_provider_from_runtime_configuration() -> None:
    captured: dict[str, Any] = {}

    def build_provider(settings: AppSettings) -> BaseLLMProvider:
        captured["settings"] = settings
        return StubProvider()

    factory = LLMProviderFactory()
    factory.register("deepseek", build_provider)

    provider = factory.create(build_settings())

    assert isinstance(provider, StubProvider)
    assert captured["settings"].llm_provider == "deepseek"
    assert factory.supported_providers() == ("deepseek",)


def test_factory_rejects_unregistered_provider_with_safe_message() -> None:
    factory = LLMProviderFactory()

    with pytest.raises(UnsupportedLLMProviderError, match="未注册的 LLM Provider"):
        factory.create(build_settings("openai_compatible"))


def test_factory_rejects_duplicate_provider_registration() -> None:
    factory = LLMProviderFactory()
    factory.register("deepseek", lambda settings: StubProvider())

    with pytest.raises(ProviderAlreadyRegisteredError, match="已注册"):
        factory.register("deepseek", lambda settings: StubProvider())


def test_factory_rejects_builder_that_does_not_return_provider() -> None:
    factory = LLMProviderFactory()
    factory.register("deepseek", lambda settings: settings)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="必须返回 BaseLLMProvider"):
        factory.create(build_settings())
