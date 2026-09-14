"""BGE 系列本地 Embedding Provider。

BGE 属于本地句向量模型，复用 :class:`LocalEmbeddingProvider` 的加载与编码逻辑，并补充
两点 BGE 专有约定：

- 未配置 ``EMBEDDING_MODEL`` 时使用 BGE 中文大模型作为默认值。
- 检索场景下只为查询添加指令前缀（BGE-M3 不需要前缀），文档侧保持原文。
"""

from __future__ import annotations

from typing import Final

from backend.app.ai.embedding.providers.local import (
    DEFAULT_BATCH_SIZE,
    LocalEmbeddingProvider,
    ModelLoader,
)
from backend.app.core.config import AppSettings

BGE_DEFAULT_MODEL: Final[str] = "BAAI/bge-large-zh-v1.5"
#: BGE v1.5 中文模型建议在检索查询前添加的指令前缀。
BGE_QUERY_PREFIX: Final[str] = "为这个句子生成表示以用于检索相关文章："


def bge_query_prefix(model_name: str) -> str:
    """返回该 BGE 模型应使用的查询前缀；M3 系列不需要前缀。"""

    return "" if "m3" in model_name.lower() else BGE_QUERY_PREFIX


class BgeEmbeddingProvider(LocalEmbeddingProvider):
    """BGE 系列本地 Embedding Provider。"""

    provider_name = "bge"

    def __init__(
        self,
        *,
        model: str = BGE_DEFAULT_MODEL,
        dimension: int | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        model_loader: ModelLoader | None = None,
    ) -> None:
        super().__init__(
            model=model,
            dimension=dimension,
            query_prefix=bge_query_prefix(model),
            batch_size=batch_size,
            model_loader=model_loader,
            provider_name="bge",
        )

    @classmethod
    def from_settings(cls, settings: AppSettings) -> BgeEmbeddingProvider:
        """按运行配置创建 BGE Provider，未配置模型时使用 BGE 默认模型。"""

        model = (settings.embedding_model or "").strip() or BGE_DEFAULT_MODEL
        return cls(model=model, dimension=settings.embedding_dimension)


__all__ = [
    "BGE_DEFAULT_MODEL",
    "BGE_QUERY_PREFIX",
    "BgeEmbeddingProvider",
    "bge_query_prefix",
]
