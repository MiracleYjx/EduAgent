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
    provider_name = "stub"

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


def test_stub_metadata_does_not_inherit_factory_registration_identity() -> None:
    """P4.2 TCR：注册名不是模型身份，真实调用路径只能指向替身方法。"""

    factory = LLMProviderFactory({"deepseek": lambda _: StubProvider()})
    provider = factory.create(build_settings())
    metadata = provider.describe(prompt_version="test-prompt-v1")
    assert metadata == {
        "provider": "stub",
        "model": "unknown",
        "prompt_version": "test-prompt-v1",
        "call_path": f"{StubProvider.__module__}.StubProvider.generate_structured",
    }
    assert "deepseek" not in str(metadata).lower()


@pytest.mark.parametrize("model_name", [None, "", "   "])
def test_empty_provider_model_metadata_is_unknown(model_name: str | None) -> None:
    provider = StubProvider()
    provider.model_name = model_name
    metadata = provider.describe()
    assert metadata["model"] == "unknown"
    assert metadata["prompt_version"] == "unknown"


def test_undeclared_provider_identity_is_unknown() -> None:
    class AnonymousProvider(StubProvider):
        provider_name = ""

    assert AnonymousProvider().describe()["provider"] == "unknown"
