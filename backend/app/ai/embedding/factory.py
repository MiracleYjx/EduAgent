"""可注册、可替换的 Embedding Provider 工厂。

业务检索层只依赖本模块与 :class:`BaseEmbeddingProvider`，通过 ``EMBEDDING_PROVIDER``
选择具体实现。工厂遵守两条硬约束：

- 只返回已注册且处于就绪状态的 Provider；未就绪或缺少必要配置时抛出
  :class:`EmbeddingProviderNotReadyError`，不返回伪造或半可用实例。
- 未注册的 Provider 直接失败，并列出当前可用 Provider。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import lru_cache

from backend.app.ai.embedding.base import (
    BaseEmbeddingProvider,
    EmbeddingProviderNotReadyError,
)
from backend.app.ai.errors import AIConfigurationError
from backend.app.core.config import AppSettings, get_settings

type EmbeddingProviderBuilder = Callable[[AppSettings], BaseEmbeddingProvider]


class EmbeddingProviderFactoryError(RuntimeError):
    """Embedding Provider 工厂相关错误的基类。"""


class UnsupportedEmbeddingProviderError(EmbeddingProviderFactoryError):
    """配置的 Provider 没有注册时抛出的异常。"""


class EmbeddingProviderAlreadyRegisteredError(EmbeddingProviderFactoryError):
    """重复注册同名 Provider 时抛出的异常。"""


class EmbeddingProviderFactory:
    """按名称注册并创建 Embedding Provider。"""

    def __init__(
        self,
        providers: Mapping[str, EmbeddingProviderBuilder] | None = None,
    ) -> None:
        self._providers: dict[str, EmbeddingProviderBuilder] = {}
        for name, builder in (providers or {}).items():
            self.register(name, builder)

    @staticmethod
    def _normalize_name(name: str) -> str:
        normalized = name.strip().lower()
        if not normalized:
            raise ValueError("Embedding Provider 名称不能为空。")
        return normalized

    def register(
        self,
        name: str,
        builder: EmbeddingProviderBuilder,
        *,
        replace: bool = False,
    ) -> EmbeddingProviderFactory:
        """注册一个由运行配置构造 Provider 的工厂函数。"""

        normalized_name = self._normalize_name(name)
        if not callable(builder):
            raise TypeError("Embedding Provider 工厂必须是可调用对象。")
        self._register_builder(normalized_name, builder, replace=replace)
        return self

    def _register_builder(
        self,
        name: str,
        builder: EmbeddingProviderBuilder,
        *,
        replace: bool,
    ) -> None:
        if name in self._providers and not replace:
            raise EmbeddingProviderAlreadyRegisteredError(
                f"Embedding Provider 已注册：{name}。"
            )
        self._providers[name] = builder

    def _get_builder(self, name: str) -> EmbeddingProviderBuilder | None:
        return self._providers.get(name)

    def create(self, settings: AppSettings | None = None) -> BaseEmbeddingProvider:
        """根据 EMBEDDING_PROVIDER 配置创建就绪的 Provider 实例。"""

        runtime_settings = settings or get_settings()
        if not runtime_settings.embedding_configured:
            raise AIConfigurationError("Embedding")
        provider_name = self._normalize_name(runtime_settings.embedding_provider)
        builder = self._get_builder(provider_name)
        if builder is None:
            supported = ", ".join(self.supported_providers()) or "无"
            raise UnsupportedEmbeddingProviderError(
                f"未注册的 Embedding Provider：{provider_name}。"
                f"当前可用 Provider：{supported}。"
                "DeepSeek 未提供公开 Embedding API，请选择可用 Provider。"
            )

        provider = builder(runtime_settings)
        if not isinstance(provider, BaseEmbeddingProvider):
            raise TypeError(
                "Embedding Provider 工厂必须返回 BaseEmbeddingProvider 实例。"
            )
        if not provider.is_ready():
            detail = provider.readiness_detail() or "Provider 未满足运行条件。"
            raise EmbeddingProviderNotReadyError(
                f"Embedding Provider {provider_name} 当前未就绪：{detail}",
                provider_name=provider_name,
            )
        return provider

    def readiness(self, settings: AppSettings | None = None) -> tuple[bool, str | None]:
        """返回当前选择的 Provider 是否就绪，供启动检查与界面展示使用。"""

        try:
            self.create(settings)
        except EmbeddingProviderFactoryError as exc:
            return False, str(exc)
        except (EmbeddingProviderNotReadyError, AIConfigurationError) as exc:
            return False, str(exc)
        return True, None

    def supported_providers(self) -> tuple[str, ...]:
        """返回已注册 Provider 名称，便于诊断和展示。"""

        return tuple(sorted(self._providers))


class _RegistryEmbeddingProviderFactory(EmbeddingProviderFactory):
    """默认工厂只读取 Registry，不保留第二份内置 Provider 清单。"""

    def _register_builder(
        self,
        name: str,
        builder: EmbeddingProviderBuilder,
        *,
        replace: bool,
    ) -> None:
        from backend.app.services.settings.provider_registry import (
            DuplicateProviderError,
            get_provider_registry,
        )

        try:
            get_provider_registry().register_legacy(
                "embedding", name, builder, replace=replace
            )
        except DuplicateProviderError:
            raise EmbeddingProviderAlreadyRegisteredError(
                f"Embedding Provider 已注册：{name}。"
            ) from None

    def _get_builder(self, name: str) -> EmbeddingProviderBuilder | None:
        from backend.app.services.settings.provider_registry import (
            get_provider_registry,
        )

        owner = get_provider_registry()
        if not any(entry.adapter_id == name for entry in owner.entries(kind="embedding")):
            return None
        return owner.entry("embedding", name).legacy_factory

    def supported_providers(self) -> tuple[str, ...]:
        from backend.app.services.settings.provider_registry import (
            get_provider_registry,
        )

        return tuple(sorted(
            entry.adapter_id for entry in get_provider_registry().entries(kind="embedding")
        ))


_default_factory = _RegistryEmbeddingProviderFactory()


def get_embedding_provider_factory() -> EmbeddingProviderFactory:
    """返回进程级默认 Provider 工厂。"""

    return _default_factory


def register_embedding_provider(
    name: str,
    builder: EmbeddingProviderBuilder,
    *,
    replace: bool = False,
) -> EmbeddingProviderFactory:
    """向默认工厂注册 Provider，并使已缓存实例失效。"""

    _default_factory.register(name, builder, replace=replace)
    get_embedding_provider.cache_clear()
    return _default_factory


def create_embedding_provider(
    settings: AppSettings | None = None,
) -> BaseEmbeddingProvider:
    """使用默认工厂创建 Provider。"""

    return _default_factory.create(settings)


@lru_cache(maxsize=1)
def get_embedding_provider() -> BaseEmbeddingProvider:
    """返回进程级缓存的默认 Provider 实例。"""

    return create_embedding_provider()


def reset_embedding_provider_cache() -> None:
    """清空缓存的 Provider 实例，仅供测试或进程重启使用。"""

    get_embedding_provider.cache_clear()


__all__ = [
    "EmbeddingProviderAlreadyRegisteredError",
    "EmbeddingProviderBuilder",
    "EmbeddingProviderFactory",
    "EmbeddingProviderFactoryError",
    "UnsupportedEmbeddingProviderError",
    "create_embedding_provider",
    "get_embedding_provider",
    "get_embedding_provider_factory",
    "register_embedding_provider",
    "reset_embedding_provider_cache",
]
