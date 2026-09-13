"""与具体模型无关的 Embedding Provider 抽象契约。

业务检索层只依赖本模块的抽象接口和配置，不得直接引用云端 Embedding API、本地
Hugging Face 模型、BGE 或其他具体 SDK。契约要求：

- ``embed_documents()`` 返回的向量数量必须与输入文档数量一致。
- ``embed_query()`` 返回的向量维度必须与知识片段 ``embedding`` 的维度一致。
- 同一 Provider 配置下，文档向量与查询向量必须来自兼容的模型和版本。
- Provider 报错、超时、空输入和维度不匹配都必须返回可识别的失败状态。
- 每次索引或查询都能通过 :meth:`BaseEmbeddingProvider.describe` 记录 Provider 标识与
  模型版本，以支持 Benchmark 回归。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import ClassVar, Final

#: 与资料摄取失败表一致的 Embedding 失败码。
EMBEDDING_FAILED: Final[str] = "EMBEDDING_FAILED"
#: 空输入、非法输入等必须先修正输入的错误码。
EMBEDDING_INVALID_INPUT: Final[str] = "EMBEDDING_INVALID_INPUT"
#: 向量数量或维度不符合契约的错误码。
EMBEDDING_DIMENSION_MISMATCH: Final[str] = "EMBEDDING_DIMENSION_MISMATCH"
#: Provider 在当前环境未就绪（缺少依赖或配置）的错误码。
EMBEDDING_PROVIDER_NOT_READY: Final[str] = "EMBEDDING_PROVIDER_NOT_READY"


class EmbeddingProviderError(RuntimeError):
    """Embedding Provider 失败基类；默认按可重试错误处理。"""

    error_code: ClassVar[str] = EMBEDDING_FAILED
    retryable: ClassVar[bool] = True

    def __init__(self, detail: str, *, provider_name: str = "unknown") -> None:
        super().__init__(f"{self.error_code}：{detail}")
        # detail 只保存可安全展示的技术原因，不得包含文档或查询原文。
        self.detail = detail
        self.provider_name = provider_name


class EmbeddingInputError(EmbeddingProviderError):
    """空输入或非法输入；必须先修正输入，不得无意义重试。"""

    error_code: ClassVar[str] = EMBEDDING_INVALID_INPUT
    retryable: ClassVar[bool] = False


class EmbeddingDimensionError(EmbeddingProviderError):
    """向量数量或维度与契约不一致；属于模型配置或实现问题。"""

    error_code: ClassVar[str] = EMBEDDING_DIMENSION_MISMATCH
    retryable: ClassVar[bool] = False


class EmbeddingProviderNotReadyError(EmbeddingProviderError):
    """Provider 在当前环境未就绪（缺少依赖或配置）。

    工厂必须在创建阶段抛出本错误，不得返回任何伪造或半可用实例。
    """

    error_code: ClassVar[str] = EMBEDDING_PROVIDER_NOT_READY
    retryable: ClassVar[bool] = False


class BaseEmbeddingProvider(ABC):
    """所有 Embedding Provider 共同遵守的最小接口。"""

    #: Provider 标识；同一实现可服务多个已注册名称，因此默认值允许被实例覆盖。
    provider_name: str = "unknown"
    #: 模型标识与版本；云端或本地 Provider 会按运行配置覆盖。
    model_name: str = "unknown"
    #: 向量维度；未知时为 ``None``，由具体实现或首次调用确定。
    dimension: int | None = None

    @abstractmethod
    async def embed_documents(self, documents: Sequence[str]) -> list[list[float]]:
        """把知识文档批量转换为向量；返回数量必须与输入数量一致。"""

        raise NotImplementedError

    @abstractmethod
    async def embed_query(self, query: str) -> list[float]:
        """把检索查询转换为向量；维度必须与文档向量一致。"""

        raise NotImplementedError

    def describe(self) -> dict[str, str | int | None]:
        """返回用于溯源和 Benchmark 记录的 Provider 标识。"""

        return {
            "provider": self.provider_name,
            "model": self.model_name,
            "dimension": self.dimension,
        }

    def is_ready(self) -> bool:
        """返回 Provider 是否具备执行 Embedding 的必要条件。"""

        return True

    def readiness_detail(self) -> str | None:
        """返回未就绪的具体原因与可操作建议；就绪时返回 ``None``。"""

        return None

    def ensure_documents(self, documents: Sequence[str]) -> list[str]:
        """校验文档输入并返回规范化文本列表。

        空输入、单个字符串和全空文档都属于可识别的输入失败，不接受静默成功。
        """

        if isinstance(documents, (str, bytes)):
            raise EmbeddingInputError(
                "embed_documents 需要文本序列，而不是单个字符串。",
                provider_name=self.provider_name,
            )
        normalized = list(documents)
        if not normalized:
            raise EmbeddingInputError(
                "embed_documents 的输入不能为空。", provider_name=self.provider_name
            )
        for document in normalized:
            if not isinstance(document, str):
                raise EmbeddingInputError(
                    "embed_documents 只接受字符串文档。", provider_name=self.provider_name
                )
        if all(not document.strip() for document in normalized):
            raise EmbeddingInputError(
                "embed_documents 不接受全部为空的文档。", provider_name=self.provider_name
            )
        return normalized

    def ensure_query(self, query: str) -> str:
        """校验查询输入；空查询和非法类型直接失败。"""

        if not isinstance(query, str):
            raise EmbeddingInputError(
                "embed_query 只接受字符串查询。", provider_name=self.provider_name
            )
        if not query.strip():
            raise EmbeddingInputError(
                "embed_query 不接受空查询。", provider_name=self.provider_name
            )
        return query

    def validate_vector(self, vector: Sequence[float]) -> list[float]:
        """校验单条向量非空，并在声明维度时校验维度一致。"""

        values = list(vector)
        if not values:
            raise EmbeddingDimensionError(
                "返回的向量不能为空。", provider_name=self.provider_name
            )
        if self.dimension is not None and len(values) != self.dimension:
            raise EmbeddingDimensionError(
                f"向量维度 {len(values)} 与声明的 {self.dimension} 不一致。",
                provider_name=self.provider_name,
            )
        return values

    def validate_document_vectors(
        self,
        documents: Sequence[str],
        vectors: Sequence[Sequence[float]],
    ) -> list[list[float]]:
        """校验批量向量数量与维度，返回上层可直接消费的列表。"""

        if len(vectors) != len(documents):
            raise EmbeddingDimensionError(
                f"文档向量数量 {len(vectors)} 与输入文档数量 {len(documents)} 不一致。",
                provider_name=self.provider_name,
            )
        return [self.validate_vector(vector) for vector in vectors]


__all__ = [
    "EMBEDDING_DIMENSION_MISMATCH",
    "EMBEDDING_FAILED",
    "EMBEDDING_INVALID_INPUT",
    "EMBEDDING_PROVIDER_NOT_READY",
    "BaseEmbeddingProvider",
    "EmbeddingDimensionError",
    "EmbeddingInputError",
    "EmbeddingProviderError",
    "EmbeddingProviderNotReadyError",
]
