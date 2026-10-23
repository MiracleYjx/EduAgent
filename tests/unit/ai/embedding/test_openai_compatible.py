"""OpenAI 兼容 Embedding 请求编码格式回归测试。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from backend.app.ai.embedding.base import (
    EMBEDDING_DIMENSION_MISMATCH,
    EmbeddingDimensionError,
)
from backend.app.ai.embedding.providers.openai_compatible import (
    OpenAICompatibleEmbeddingProvider,
)

MODEL = "BAAI/bge-m3"
DIMENSION = 1024
BASE64_VECTOR = "AAAAAAAAAAA="


class _Item:
    """模拟 OpenAI 兼容响应中的单条向量。"""

    def __init__(self, embedding: Any) -> None:
        self.embedding = embedding


class _Response:
    """模拟 OpenAI 兼容 Embedding 响应。"""

    def __init__(self, vectors: list[Any]) -> None:
        self.data = [_Item(vector) for vector in vectors]


class _Embeddings:
    """记录请求并返回可控向量的假 embeddings 资源。"""

    def __init__(self, *, vectors: list[Any] | None = None) -> None:
        self._vectors = vectors
        self.requests: list[dict[str, Any]] = []

    async def create(
        self,
        *,
        model: str,
        input: list[str],
        encoding_format: str,
    ) -> _Response:
        self.requests.append(
            {
                "model": model,
                "input": list(input),
                "encoding_format": encoding_format,
            }
        )
        vectors = self._vectors
        if vectors is None:
            vectors = [[0.25] * DIMENSION for _ in input]
        return _Response(vectors)


class _Client:
    """只暴露 embeddings 资源的假 OpenAI 兼容客户端。"""

    def __init__(self, embeddings: _Embeddings) -> None:
        self.embeddings = embeddings


def _provider(embeddings: _Embeddings) -> OpenAICompatibleEmbeddingProvider:
    return OpenAICompatibleEmbeddingProvider(
        model=MODEL,
        api_key="test-key",
        dimension=DIMENSION,
        client=_Client(embeddings),  # type: ignore[arg-type]
    )


def test_request_uses_explicit_float_encoding() -> None:
    """请求必须显式覆盖 SDK 的 base64 默认值。"""

    embeddings = _Embeddings()
    provider = _provider(embeddings)

    asyncio.run(provider.embed_documents(["第一段", "第二段"]))

    assert embeddings.requests == [
        {
            "model": MODEL,
            "input": ["第一段", "第二段"],
            "encoding_format": "float",
        }
    ]


def test_float_response_parses_as_1024_dimensions() -> None:
    """浮点数组响应必须保持文档和查询的 1024 维契约。"""

    embeddings = _Embeddings()
    provider = _provider(embeddings)

    vectors = asyncio.run(provider.embed_documents(["课程资料"]))
    query_vector = asyncio.run(provider.embed_query("课程问题"))

    assert len(vectors) == 1
    assert len(vectors[0]) == DIMENSION
    assert all(isinstance(value, float) for value in vectors[0])
    assert len(query_vector) == DIMENSION
    assert all(isinstance(value, float) for value in query_vector)


def test_base64_response_is_rejected_explicitly() -> None:
    """供应商若忽略 float 参数，Base64 字符串不能静默进入索引。"""

    embeddings = _Embeddings(vectors=[BASE64_VECTOR])
    provider = _provider(embeddings)

    with pytest.raises(EmbeddingDimensionError) as captured:
        asyncio.run(provider.embed_query("课程问题"))

    assert captured.value.error_code == EMBEDDING_DIMENSION_MISMATCH
    assert captured.value.retryable is False
    assert str(DIMENSION) in str(captured.value)
