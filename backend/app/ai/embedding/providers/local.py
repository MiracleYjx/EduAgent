"""本地 Hugging Face / sentence-transformers Embedding Provider。

本地 Provider 在首次调用时加载模型，避免导入配置阶段就占用内存；模型依赖缺失时给出
可操作的安装提示，而不是返回无法工作的实例。``model_loader`` 参数支持注入自定义加载器，
便于测试和自定义部署。
"""

from __future__ import annotations

import asyncio
import importlib.util
from collections.abc import Callable, Sequence
from typing import Any, Final

from backend.app.ai.embedding.base import (
    BaseEmbeddingProvider,
    EmbeddingProviderError,
    EmbeddingProviderNotReadyError,
)
from backend.app.core.config import AppSettings

DEFAULT_BATCH_SIZE: Final[int] = 32
LOCAL_DEPENDENCY_HINT: Final[str] = "请安装本地模型依赖：pip install sentence-transformers。"

type ModelLoader = Callable[[str], Any]


def sentence_transformers_available() -> bool:
    """判断本地句向量依赖是否已安装。"""

    return importlib.util.find_spec("sentence_transformers") is not None


def load_sentence_transformer(model_name: str) -> Any:
    """加载本地句向量模型；缺少依赖时抛出明确的未就绪错误。"""

    if not sentence_transformers_available():
        raise EmbeddingProviderNotReadyError(
            f"本地 Embedding 依赖不可用。{LOCAL_DEPENDENCY_HINT}",
            provider_name="local",
        )
    # sentence-transformers 属于可选的本地依赖，未安装时由 is_ready 给出安装提示。
    from sentence_transformers import (  # type: ignore[import-not-found]
        SentenceTransformer,
    )

    return SentenceTransformer(model_name)


class LocalEmbeddingProvider(BaseEmbeddingProvider):
    """基于本地句向量模型的 Embedding Provider。"""

    provider_name = "local"

    def __init__(
        self,
        *,
        model: str,
        dimension: int | None = None,
        query_prefix: str = "",
        batch_size: int = DEFAULT_BATCH_SIZE,
        model_loader: ModelLoader | None = None,
        provider_name: str = "local",
    ) -> None:
        self.provider_name = provider_name
        self.model_name = model
        self.dimension = dimension
        self._query_prefix = query_prefix
        self._batch_size = batch_size
        self._model_loader = model_loader if model_loader is not None else load_sentence_transformer
        self._model: Any | None = None

    @classmethod
    def from_settings(cls, settings: AppSettings) -> LocalEmbeddingProvider:
        """按运行配置创建 Provider；缺少模型标识时明确失败。"""

        model = (settings.embedding_model or "").strip()
        if not model:
            raise EmbeddingProviderNotReadyError(
                f"缺少 EMBEDDING_MODEL 配置，无法确定 {cls.provider_name} Provider 使用的本地模型。",
                provider_name=cls.provider_name,
            )
        return cls(
            model=model,
            dimension=settings.embedding_dimension,
        )

    def is_ready(self) -> bool:
        """注入自定义加载器或已安装本地依赖时视为就绪。"""

        if self._model_loader is not load_sentence_transformer:
            return True
        return sentence_transformers_available()

    def readiness_detail(self) -> str | None:
        """返回未就绪原因与安装建议。"""

        if self.is_ready():
            return None
        return f"当前环境缺少本地 Embedding 依赖。{LOCAL_DEPENDENCY_HINT}"

    async def embed_documents(self, documents: Sequence[str]) -> list[list[float]]:
        normalized = self.ensure_documents(documents)
        model = await self._ensure_model()
        vectors = await self._encode(model, normalized)
        return self.validate_document_vectors(normalized, vectors)

    async def embed_query(self, query: str) -> list[float]:
        validated = self.ensure_query(query)
        model = await self._ensure_model()
        text = f"{self._query_prefix}{validated}" if self._query_prefix else validated
        vectors = await self._encode(model, [text])
        if not vectors:
            raise EmbeddingProviderError(
                "本地模型未返回查询向量。", provider_name=self.provider_name
            )
        return self.validate_vector(vectors[0])

    async def _ensure_model(self) -> Any:
        """首次调用时加载模型，并记录模型自报的向量维度。"""

        if self._model is None:
            self._model = await asyncio.to_thread(self._load_model)
        return self._model

    def _load_model(self) -> Any:
        try:
            model = self._model_loader(self.model_name)
        except EmbeddingProviderError:
            raise
        except Exception as exc:
            raise EmbeddingProviderError(
                f"本地 Embedding 模型加载失败：{type(exc).__name__}。",
                provider_name=self.provider_name,
            ) from exc

        resolved_dimension = getattr(model, "get_sentence_embedding_dimension", None)
        if callable(resolved_dimension):
            reported = resolved_dimension()
            if isinstance(reported, int) and reported > 0:
                self.dimension = reported
        return model

    async def _encode(self, model: Any, texts: list[str]) -> list[list[float]]:
        """在线程中完成编码，避免阻塞事件循环。"""

        try:
            encoded = await asyncio.to_thread(
                model.encode,
                texts,
                batch_size=self._batch_size,
                normalize_embeddings=True,
                convert_to_numpy=True,
            )
        except EmbeddingProviderError:
            raise
        except Exception as exc:
            raise EmbeddingProviderError(
                f"本地 Embedding 计算失败：{type(exc).__name__}。",
                provider_name=self.provider_name,
            ) from exc
        return [[float(value) for value in vector] for vector in encoded]


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "LOCAL_DEPENDENCY_HINT",
    "LocalEmbeddingProvider",
    "ModelLoader",
    "load_sentence_transformer",
    "sentence_transformers_available",
]
