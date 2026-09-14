"""检索层共享契约：模式、过滤条件、结果字段与错误码。

检索契约以 ``.specify/contracts/rag-retrieval.md`` 为准：

- 四种检索模式：``vector_only``、``keyword_only``、``hybrid``、``hybrid_rerank``。
- 每条结果必须保留 ``chunk_id``、``course_id``、``document_id``、``content`` 以及各阶段
  分数与 ``rank``，不允许返回无来源的自由文本。
- 检索不到有效上下文时必须返回空列表或明确的不可用状态，不得编造上下文。

本模块只承载契约与工厂：具体实现由 :mod:`backend.app.ai.retrieval.vector_search` 与
:mod:`backend.app.ai.retrieval.keyword_search` 提供；``hybrid``/``hybrid_rerank`` 属于
T043/T044 范围，当前批次调用时抛出明确错误，不返回伪造候选。
"""

from __future__ import annotations

import importlib
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, ClassVar, Final
from uuid import UUID

from sqlalchemy.orm import Session

#: 查询文本或查询向量不合法；必须先修正输入。
RETRIEVAL_INVALID_INPUT: Final[str] = "RETRIEVAL_INVALID_INPUT"
#: 当前数据库方言不支持该检索模式（例如非 PostgreSQL 的 tsvector）。
RETRIEVAL_UNSUPPORTED_DIALECT: Final[str] = "RETRIEVAL_UNSUPPORTED_DIALECT"
#: 该检索模式尚未实现（当前批次只提供 vector/keyword）。
RETRIEVAL_MODE_NOT_IMPLEMENTED: Final[str] = "RETRIEVAL_MODE_NOT_IMPLEMENTED"

#: 检索失败码对应的可读提示。
RETRIEVAL_ERROR_MESSAGES: Final[Mapping[str, str]] = MappingProxyType(
    {
        RETRIEVAL_INVALID_INPUT: "检索输入不合法，请检查查询内容或过滤条件。",
        RETRIEVAL_UNSUPPORTED_DIALECT: "当前数据库不支持该检索模式，请检查数据库配置。",
        RETRIEVAL_MODE_NOT_IMPLEMENTED: "该检索模式尚未实现，不能返回未经融合或重排的结果。",
    }
)

#: 默认与上限 Top-K，避免单次检索返回过多片段。
DEFAULT_TOP_K: Final[int] = 5
MAX_TOP_K: Final[int] = 50

#: 候选来源标记：单路召回与两路同时召回。
SOURCE_MODE_VECTOR: Final[str] = "vector"
SOURCE_MODE_KEYWORD: Final[str] = "keyword"
SOURCE_MODE_BOTH: Final[str] = "both"


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    """混合检索需要的完整查询：查询文本 + 已算好的 query embedding。

    检索层不负责调用 Embedding Provider，因此混合检索要求调用方同时传入两种形式。
    """

    text: str
    embedding: tuple[float, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", normalize_query_text(self.text))
        object.__setattr__(
            self,
            "embedding",
            tuple(normalize_query_vector(self.embedding)),
        )


class RetrievalError(RuntimeError):
    """检索失败基类；默认按不可重试的输入或配置问题处理。"""

    error_code: ClassVar[str] = RETRIEVAL_INVALID_INPUT
    retryable: ClassVar[bool] = False

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.error_code}：{detail}")
        self.detail = detail

    @property
    def user_message(self) -> str:
        """返回可直接展示给教师或学生的失败提示。"""

        return RETRIEVAL_ERROR_MESSAGES.get(
            self.error_code, RETRIEVAL_ERROR_MESSAGES[RETRIEVAL_INVALID_INPUT]
        )


class RetrievalInputError(RetrievalError):
    """查询文本、查询向量或过滤条件不合法。"""

    error_code: ClassVar[str] = RETRIEVAL_INVALID_INPUT


class RetrievalUnsupportedDialectError(RetrievalError):
    """当前数据库方言不支持请求的检索模式。"""

    error_code: ClassVar[str] = RETRIEVAL_UNSUPPORTED_DIALECT


class RetrievalModeNotImplementedError(RetrievalError):
    """请求的检索模式尚未实现；不得用其他模式的结果顶替。"""

    error_code: ClassVar[str] = RETRIEVAL_MODE_NOT_IMPLEMENTED


class RetrievalMode(StrEnum):
    """四种检索模式；取值与检索契约和 Benchmark 配置名一致。"""

    VECTOR_ONLY = "vector_only"
    KEYWORD_ONLY = "keyword_only"
    HYBRID = "hybrid"
    HYBRID_RERANK = "hybrid_rerank"


def _coerce_uuid(value: UUID | str, field_name: str) -> UUID:
    """把课程、知识库或资料标识规范化为 UUID。"""

    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value).strip())
    except (AttributeError, TypeError, ValueError) as exc:
        raise RetrievalInputError(f"{field_name}无效。") from exc


def _coerce_filter_values(
    values: Sequence[UUID | str] | None,
    field_name: str,
) -> tuple[UUID, ...]:
    """规范化过滤条件中的标识集合，空值表示不过滤。"""

    if values is None:
        return ()
    if isinstance(values, (str, bytes, UUID)):
        raise RetrievalInputError(f"{field_name}必须是标识序列。")
    return tuple(_coerce_uuid(value, field_name) for value in values)


@dataclass(frozen=True, slots=True)
class RetrievalFilters:
    """检索范围过滤条件；空集合表示不限制该维度。"""

    course_ids: tuple[UUID, ...] = ()
    knowledge_base_ids: tuple[UUID, ...] = ()
    document_ids: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "course_ids",
            _coerce_filter_values(self.course_ids, "课程标识"),
        )
        object.__setattr__(
            self,
            "knowledge_base_ids",
            _coerce_filter_values(self.knowledge_base_ids, "知识库标识"),
        )
        object.__setattr__(
            self,
            "document_ids",
            _coerce_filter_values(self.document_ids, "资料标识"),
        )

    @property
    def is_empty(self) -> bool:
        """返回是否未设置任何过滤条件。"""

        return not (self.course_ids or self.knowledge_base_ids or self.document_ids)

    def describe(self) -> str:
        """返回可安全展示的过滤范围描述。"""

        if self.is_empty:
            return "课程范围：未限制。"
        return (
            f"课程范围：{len(self.course_ids)} 个课程，"
            f"{len(self.knowledge_base_ids)} 个知识库，"
            f"{len(self.document_ids)} 份资料。"
        )


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """一条检索候选；字段与检索契约的必需结果字段一一对应。"""

    chunk_id: str
    course_id: str
    document_id: str
    content: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    semantic_score: float | None = None
    keyword_score: float | None = None
    fusion_score: float | None = None
    rerank_score: float | None = None
    rank: int = 0
    #: 候选来源标记：vector / keyword / both，便于诊断融合行为。
    source_mode: str | None = None

    @property
    def score(self) -> float:
        """返回当前阶段用于排序的分数：优先重排、再融合、再单路分数。"""

        for candidate in (
            self.rerank_score,
            self.fusion_score,
            self.semantic_score,
            self.keyword_score,
        ):
            if candidate is not None:
                return candidate
        return 0.0

    def as_dict(self) -> dict[str, Any]:
        """返回可直接交给 Agent、UI 或 Benchmark 的字典。"""

        return {
            "chunk_id": self.chunk_id,
            "course_id": self.course_id,
            "document_id": self.document_id,
            "content": self.content,
            "metadata": dict(self.metadata),
            "semantic_score": self.semantic_score,
            "keyword_score": self.keyword_score,
            "fusion_score": self.fusion_score,
            "rerank_score": self.rerank_score,
            "rank": self.rank,
            "source_mode": self.source_mode,
            "score": self.score,
        }

    @classmethod
    def from_document_chunk(
        cls,
        chunk: Any,
        *,
        rank: int = 0,
        semantic_score: float | None = None,
        keyword_score: float | None = None,
        source_mode: str | None = None,
    ) -> RetrievedChunk:
        """由 ``DocumentChunk`` 实体构造候选，保留全部来源信息。"""

        metadata = getattr(chunk, "chunk_metadata", None) or {}
        return cls(
            chunk_id=str(chunk.id),
            course_id=str(chunk.course_id),
            document_id=str(chunk.document_id),
            content=str(chunk.content),
            metadata=dict(metadata),
            semantic_score=semantic_score,
            keyword_score=keyword_score,
            rank=rank,
            source_mode=source_mode,
        )


def normalize_mode(mode: RetrievalMode | str) -> RetrievalMode:
    """把模式名称或取值规范化为 :class:`RetrievalMode`。"""

    if isinstance(mode, RetrievalMode):
        return mode
    if not isinstance(mode, str):
        raise RetrievalInputError("检索模式无效。")
    candidate = mode.strip()
    for item in RetrievalMode:
        if candidate.lower() in {item.value.lower(), item.name.lower()}:
            return item
    raise RetrievalInputError(f"未知的检索模式：{candidate or '（空）'}。")


def normalize_top_k(top_k: int = DEFAULT_TOP_K) -> int:
    """校验 Top-K 范围，避免非法或过大的候选数量。"""

    if isinstance(top_k, bool) or not isinstance(top_k, int):
        raise RetrievalInputError("top_k 必须是整数。")
    if top_k <= 0:
        raise RetrievalInputError("top_k 必须大于 0。")
    if top_k > MAX_TOP_K:
        raise RetrievalInputError(f"top_k 不能超过 {MAX_TOP_K}。")
    return top_k


def normalize_query_vector(query: object) -> list[float]:
    """校验查询向量，返回浮点列表。"""

    if isinstance(query, (str, bytes)) or not isinstance(query, Sequence):
        raise RetrievalInputError("语义检索需要 query embedding 向量，而不是文本。")
    try:
        vector = [float(value) for value in query]
    except (TypeError, ValueError) as exc:
        raise RetrievalInputError("query embedding 必须是数值序列。") from exc
    if not vector:
        raise RetrievalInputError("query embedding 不能为空。")
    return vector


def normalize_query_text(query: object) -> str:
    """校验关键词查询文本，拒绝空查询和非文本输入。"""

    if not isinstance(query, str):
        raise RetrievalInputError("关键词检索需要查询文本，而不是向量。")
    normalized = query.strip()
    if not normalized:
        raise RetrievalInputError("关键词查询文本不能为空。")
    return normalized


def resolve_filters(filters: RetrievalFilters | None) -> RetrievalFilters:
    """规范化过滤条件；未提供时表示不限制范围。"""

    if filters is None:
        return RetrievalFilters()
    if isinstance(filters, RetrievalFilters):
        return filters
    if isinstance(filters, Mapping):
        return RetrievalFilters(
            course_ids=tuple(filters.get("course_ids", ()) or ()),
            knowledge_base_ids=tuple(filters.get("knowledge_base_ids", ()) or ()),
            document_ids=tuple(filters.get("document_ids", ()) or ()),
        )
    raise RetrievalInputError("过滤条件无效。")


def resolve_dialect_name(session: Session) -> str:
    """返回当前会话使用的数据库方言名称。"""

    bind = session.get_bind()
    if bind is None:
        raise RetrievalInputError("检索会话未绑定数据库引擎。")
    return bind.dialect.name


class BaseRetriever(ABC):
    """检索实现的最小契约：单模式检索，输入查询并返回带来源的候选。"""

    #: 该实现负责的检索模式。
    mode: ClassVar[RetrievalMode]

    @abstractmethod
    def search(
        self,
        session: Session,
        query: str | Sequence[float],
        *,
        top_k: int = DEFAULT_TOP_K,
        filters: RetrievalFilters | None = None,
    ) -> list[RetrievedChunk]:
        """执行检索；无匹配时返回空列表。"""

        raise NotImplementedError


#: 内置实现按需导入，避免包初始化时的循环依赖。
_BUILTIN_RETRIEVERS: Final[Mapping[RetrievalMode, tuple[str, str]]] = MappingProxyType(
    {
        RetrievalMode.VECTOR_ONLY: (
            "backend.app.ai.retrieval.vector_search",
            "VectorSearchRetriever",
        ),
        RetrievalMode.KEYWORD_ONLY: (
            "backend.app.ai.retrieval.keyword_search",
            "KeywordSearchRetriever",
        ),
        RetrievalMode.HYBRID: (
            "backend.app.ai.retrieval.hybrid_search",
            "HybridSearchRetriever",
        ),
        RetrievalMode.HYBRID_RERANK: (
            "backend.app.ai.retrieval.reranker",
            "HybridRerankRetriever",
        ),
    }
)


def get_retriever(mode: RetrievalMode | str, **kwargs: Any) -> BaseRetriever:
    """按模式返回检索实现；未实现或未就绪时抛出明确错误。"""

    normalized = normalize_mode(mode)
    builtin = _BUILTIN_RETRIEVERS.get(normalized)
    if builtin is None:
        raise RetrievalModeNotImplementedError(
            f"检索模式 {normalized.value} 尚未实现，不得返回未融合或未重排的结果。"
        )
    module_name, class_name = builtin
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:  # 实现文件缺失时不得静默降级。
        raise RetrievalModeNotImplementedError(
            f"检索模式 {normalized.value} 的实现尚未就绪。"
        ) from exc
    retriever_class: type[BaseRetriever] = getattr(module, class_name)
    return retriever_class(**kwargs)


__all__ = [
    "DEFAULT_TOP_K",
    "MAX_TOP_K",
    "RETRIEVAL_ERROR_MESSAGES",
    "RETRIEVAL_INVALID_INPUT",
    "RETRIEVAL_MODE_NOT_IMPLEMENTED",
    "RETRIEVAL_UNSUPPORTED_DIALECT",
    "SOURCE_MODE_BOTH",
    "SOURCE_MODE_KEYWORD",
    "SOURCE_MODE_VECTOR",
    "BaseRetriever",
    "RetrievalError",
    "RetrievalFilters",
    "RetrievalInputError",
    "RetrievalMode",
    "RetrievalModeNotImplementedError",
    "RetrievalQuery",
    "RetrievalUnsupportedDialectError",
    "RetrievedChunk",
    "get_retriever",
    "normalize_mode",
    "normalize_query_text",
    "normalize_query_vector",
    "normalize_top_k",
    "resolve_dialect_name",
    "resolve_filters",
]
