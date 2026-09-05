"""可注册、可替换的 LLM Provider 工厂。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import lru_cache

from backend.app.core.config import AppSettings, get_settings

from .base import BaseLLMProvider

type ProviderBuilder = Callable[[AppSettings], BaseLLMProvider]


class LLMProviderError(RuntimeError):
    """LLM Provider 工厂相关错误的基类。"""


class UnsupportedLLMProviderError(LLMProviderError):
    """当配置的 Provider 没有注册时抛出的异常。"""


class ProviderAlreadyRegisteredError(LLMProviderError):
    """当重复注册同名 Provider 时抛出的异常。"""


class LLMProviderFactory:
    """按名称注册并创建 LLM Provider。"""

    def __init__(
        self,
        providers: Mapping[str, ProviderBuilder] | None = None,
    ) -> None:
        self._providers: dict[str, ProviderBuilder] = {}
        for name, builder in (providers or {}).items():
            self.register(name, builder)

    @staticmethod
    def _normalize_name(name: str) -> str:
        normalized = name.strip().lower()
        if not normalized:
            raise ValueError("LLM Provider 名称不能为空。")
        return normalized

    def register(
        self,
        name: str,
        builder: ProviderBuilder,
        *,
        replace: bool = False,
    ) -> LLMProviderFactory:
        """注册一个由运行配置构造 Provider 的工厂函数。"""

        normalized_name = self._normalize_name(name)
        if not callable(builder):
            raise TypeError("LLM Provider 工厂必须是可调用对象。")
        if normalized_name in self._providers and not replace:
            raise ProviderAlreadyRegisteredError(
                f"LLM Provider 已注册：{normalized_name}。"
            )
        self._providers[normalized_name] = builder
        return self

    def create(self, settings: AppSettings | None = None) -> BaseLLMProvider:
        """根据 LLM_PROVIDER 配置创建 Provider 实例。"""

        runtime_settings = settings or get_settings()
        provider_name = self._normalize_name(runtime_settings.llm_provider)
        builder = self._providers.get(provider_name)
        if builder is None:
            supported = ", ".join(self.supported_providers()) or "无"
            raise UnsupportedLLMProviderError(
                f"未注册的 LLM Provider：{provider_name}。"
                f"当前可用 Provider：{supported}。"
            )

        provider = builder(runtime_settings)
        if not isinstance(provider, BaseLLMProvider):
            raise TypeError("LLM Provider 工厂必须返回 BaseLLMProvider 实例。")
        return provider

    def supported_providers(self) -> tuple[str, ...]:
        """返回已注册 Provider 名称，便于诊断和展示。"""

        return tuple(sorted(self._providers))


_default_factory = LLMProviderFactory()


def get_llm_provider_factory() -> LLMProviderFactory:
    """返回进程级默认 Provider 工厂。"""

    return _default_factory


def register_llm_provider(
    name: str,
    builder: ProviderBuilder,
    *,
    replace: bool = False,
) -> LLMProviderFactory:
    """向默认工厂注册 Provider，并使已缓存实例失效。"""

    _default_factory.register(name, builder, replace=replace)
    get_llm_provider.cache_clear()
    return _default_factory


def create_llm_provider(
    settings: AppSettings | None = None,
) -> BaseLLMProvider:
    """使用默认工厂创建 Provider。"""

    return _default_factory.create(settings)


@lru_cache(maxsize=1)
def get_llm_provider() -> BaseLLMProvider:
    """返回进程级缓存的默认 Provider 实例。"""

    return create_llm_provider()


def reset_llm_provider_cache() -> None:
    """清空缓存的 Provider 实例，仅供测试或进程重启使用。"""

    get_llm_provider.cache_clear()


register_provider = register_llm_provider


__all__ = [
    "LLMProviderError",
    "LLMProviderFactory",
    "ProviderAlreadyRegisteredError",
    "ProviderBuilder",
    "UnsupportedLLMProviderError",
    "create_llm_provider",
    "get_llm_provider",
    "get_llm_provider_factory",
    "register_llm_provider",
    "register_provider",
    "reset_llm_provider_cache",
]
