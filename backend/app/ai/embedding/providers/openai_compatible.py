"""云端 OpenAI 兼容 Embedding Provider。

通过 OpenAI 兼容的 ``/embeddings`` 接口生成向量，可接入云端 Embedding API 或任何保持
该协议的服务。缺少 ``EMBEDDING_MODEL`` 或 ``EMBEDDING_API_KEY`` 时，``from_settings``
直接抛出 :class:`EmbeddingProviderNotReadyError`，不会返回无法调用的实例。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from openai import AsyncOpenAI, OpenAIError
from openai.types import CreateEmbeddingResponse

from backend.app.ai.embedding.base import (
    BaseEmbeddingProvider,
    EmbeddingProviderError,
    EmbeddingProviderNotReadyError,
)
from backend.app.core.config import AppSettings

DEFAULT_BATCH_SIZE: Final[int] = 64


class OpenAICompatibleEmbeddingProvider(BaseEmbeddingProvider):
    """基于 OpenAI 兼容协议的云端 Embedding Provider。"""

    provider_name = "openai_compatible"

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str | None = None,
        dimension: int | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self.model_name = model
        self.dimension = dimension
        self._batch_size = batch_size
        # client 允许注入，便于测试与复用连接池。
        self._client = (
            client if client is not None else AsyncOpenAI(api_key=api_key, base_url=base_url)
        )

    @classmethod
    def from_settings(cls, settings: AppSettings) -> OpenAICompatibleEmbeddingProvider:
        """按运行配置创建 Provider；缺少必要配置时明确失败。"""

        model = (settings.embedding_model or "").strip()
        api_key = (
            settings.embedding_api_key.get_secret_value().strip()
            if settings.embedding_api_key is not None
            else ""
        )
        missing = [
            name
            for name, value in (("EMBEDDING_MODEL", model), ("EMBEDDING_API_KEY", api_key))
            if not value
        ]
        if missing:
            raise EmbeddingProviderNotReadyError(
                f"缺少 {', '.join(missing)} 配置，无法创建 {cls.provider_name} Provider。",
                provider_name=cls.provider_name,
            )
        return cls(
            model=model,
            api_key=api_key,
            base_url=str(settings.embedding_base_url) if settings.embedding_base_url else None,
            dimension=settings.embedding_dimension,
        )

    def is_ready(self) -> bool:
        """存在模型标识与客户端即视为可调用；连接错误在调用时统一收敛。"""

        return bool(self.model_name.strip()) and self._client is not None

    async def embed_documents(self, documents: Sequence[str]) -> list[list[float]]:
        normalized = self.ensure_documents(documents)
        vectors: list[list[float]] = []
        for start in range(0, len(normalized), self._batch_size):
            batch = normalized[start : start + self._batch_size]
            response = await self._create_embeddings(batch)
            vectors.extend(list(item.embedding) for item in response.data)
        return self.validate_document_vectors(normalized, vectors)

    async def embed_query(self, query: str) -> list[float]:
        validated = self.ensure_query(query)
        response = await self._create_embeddings([validated])
        if not response.data:
            raise EmbeddingProviderError(
                "云端 Embedding 未返回查询向量。", provider_name=self.provider_name
            )
        return self.validate_vector(list(response.data[0].embedding))

    async def _create_embeddings(self, inputs: list[str]) -> CreateEmbeddingResponse:
        """调用云端接口，并把 SDK 异常映射为统一的 Embedding 失败状态。"""

        try:
            return await self._client.embeddings.create(
                model=self.model_name, input=list(inputs)
            )
        except (OpenAIError, TimeoutError, OSError) as exc:
            # 只保留异常类型，避免把响应正文或密钥写入错误信息。
            raise EmbeddingProviderError(
                f"云端 Embedding 调用失败：{type(exc).__name__}。",
                provider_name=self.provider_name,
            ) from exc


__all__ = ["DEFAULT_BATCH_SIZE", "OpenAICompatibleEmbeddingProvider"]
