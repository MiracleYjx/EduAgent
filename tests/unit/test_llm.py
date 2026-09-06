import asyncio
from typing import Any, cast

import pytest
from pydantic import BaseModel

from backend.app.ai.llm.base import BaseLLMProvider, LLMMessages
from backend.app.ai.llm.factory import (
    LLMProviderFactory,
    ProviderAlreadyRegisteredError,
    UnsupportedLLMProviderError,
)
from backend.app.core.config import AppSettings
from tests.unit.settings_helpers import build_test_settings


class ExampleResult(BaseModel):
    value: int


class StubProvider(BaseLLMProvider):
    provider_name = "deepseek"

    async def generate_structured(
        self,
        messages: LLMMessages,
        schema: type[BaseModel],
        model: str | None = None,
    ) -> BaseModel:
        del messages, model
        return schema(value=7)


def build_settings(provider: str = "deepseek") -> AppSettings:
    return build_test_settings(llm_provider=provider)


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
    factory.register("deepseek", lambda settings: cast(BaseLLMProvider, settings))

    with pytest.raises(TypeError, match="必须返回 BaseLLMProvider"):
        factory.create(build_settings())
