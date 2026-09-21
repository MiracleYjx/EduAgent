"""与具体模型无关的 LLM Provider 抽象契约。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, TypedDict

from pydantic import BaseModel

type LLMMessage = Mapping[str, Any]
type LLMMessages = Sequence[LLMMessage]


class LLMProviderMetadata(TypedDict):
    """本次调用的脱敏来源；unknown 不得由全局配置补齐。"""

    provider: str
    model: str
    prompt_version: str
    call_path: str


def _identity(value: object) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else "unknown"


def _describe_instance(
    provider: object, *, provider_name: str = "unknown", prompt_version: str | None = None,
) -> LLMProviderMetadata:
    """仅描述可观察的实例信息；方法入口不等同于远端模型调用成功。"""

    method = getattr(provider, "generate_structured", None)
    module = getattr(method, "__module__", None)
    name = getattr(method, "__qualname__", None)
    return {
        "provider": _identity(provider_name),
        "model": _identity(
            getattr(provider, "model_name", None) or getattr(provider, "model", None)
        ),
        "prompt_version": _identity(prompt_version),
        "call_path": f"{module}.{name}" if module and name else "unknown",
    }


def describe_llm_provider(
    provider: object, *, prompt_version: str | None = None,
) -> LLMProviderMetadata:
    """消费 BaseLLMProvider 接口；既有 duck-typed 注入对象不猜 Provider 身份。"""

    if isinstance(provider, BaseLLMProvider):
        return provider.describe(prompt_version=prompt_version)
    return _describe_instance(provider, prompt_version=prompt_version)


class BaseLLMProvider(ABC):
    """所有 LLM Provider 共同遵守的最小接口。"""

    provider_name: ClassVar[str] = "unknown"

    def describe(self, *, prompt_version: str | None = None) -> LLMProviderMetadata:
        """描述实例默认调用，不读取配置；提示版本由实际构造消息的调用方提供。

        call_path 为绑定的 generate_structured 方法入口，不记录 URL/凭据；此接口
        不声称远端请求成功。替身应显式声明 provider_name="stub"，未声明则 unknown。
        """

        return _describe_instance(
            self, provider_name=self.provider_name, prompt_version=prompt_version,
        )

    @abstractmethod
    async def generate_structured(
        self,
        messages: LLMMessages,
        schema: type[BaseModel],
        model: str | None = None,
    ) -> BaseModel:
        """根据消息生成并返回经过校验的结构化结果。"""

        raise NotImplementedError


__all__ = [
    "BaseLLMProvider", "LLMMessage", "LLMMessages", "LLMProviderMetadata",
    "describe_llm_provider",
]
