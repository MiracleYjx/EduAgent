"""pgvector 语义检索。

输入是调用方已经算好的 query embedding、Top-K 与课程/知识库/资料过滤条件；本模块
**不触发任何 Embedding 调用**，只负责在 ``DocumentChunk`` 上做最近邻检索：

- PostgreSQL 路径使用 pgvector 余弦距离运算符 ``<=>`` 排序并截断 Top-K；表达式与
  ``vector_cosine_ops`` 的 HNSW 索引一致，规划器可自动使用 ``ix_document_chunks_embedding_hnsw``。
- ``exact=True`` 时在同一事务内禁用索引扫描，强制顺序扫描精确计算距离，作为
  Benchmark 的“精确近邻”基线。
- 其他方言（单元与契约测试使用的 SQLite）退化为等价的 Python 精确基线，保证同一套
  契约断言可以运行，且不伪造语义检索能力。

无匹配时返回空列表；查询向量不合法或维度不一致时抛出明确的检索输入错误。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import ClassVar, Final, cast

from sqlalchemy import Float, select, text
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from backend.app.ai.retrieval._filters import apply_retrieval_filters
from backend.app.ai.retrieval.base import (
    DEFAULT_TOP_K,
    BaseRetriever,
    RetrievalFilters,
    RetrievalInputError,
    RetrievalMode,
    RetrievedChunk,
    normalize_query_vector,
    normalize_top_k,
    resolve_dialect_name,
    resolve_filters,
)
from backend.app.models import DocumentChunk

POSTGRES_DIALECT: Final[str] = "postgresql"


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """计算两段向量的余弦相似度，结果限制在 ``[-1, 1]``。"""

    if not left or not right:
        raise RetrievalInputError("余弦相似度需要非空向量。")
    if len(left) != len(right):
        raise RetrievalInputError(
            f"查询向量维度 {len(left)} 与知识片段维度 {len(right)} 不一致。"
        )
    dot = sum(float(a) * float(b) for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(float(value) ** 2 for value in left))
    right_norm = math.sqrt(sum(float(value) ** 2 for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))


def similarity_from_cosine_distance(distance: float) -> float:
    """把 pgvector 的余弦距离转换为余弦相似度并限制范围。"""

    return max(-1.0, min(1.0, 1.0 - float(distance)))


def cosine_distance_expression(query_vector: Sequence[float]) -> ColumnElement[float]:
    """返回 pgvector 余弦距离表达式，避免依赖类型适配器的比较器实现。"""

    return cast(
        "ColumnElement[float]",
        DocumentChunk.embedding.op("<=>", return_type=Float)(list(query_vector)),
    )


class VectorSearchRetriever(BaseRetriever):
    """基于 pgvector 的语义检索实现。"""

    mode: ClassVar[RetrievalMode] = RetrievalMode.VECTOR_ONLY

    def __init__(self, *, exact: bool = False) -> None:
        #: 是否强制精确近邻搜索；用于 Benchmark 基线与结果对照。
        self.exact = bool(exact)

    def search(
        self,
        session: Session,
        query: str | Sequence[float],
        *,
        top_k: int = DEFAULT_TOP_K,
        filters: RetrievalFilters | None = None,
    ) -> list[RetrievedChunk]:
        """按查询向量检索最相似的 Top-K 知识片段。"""

        vector = normalize_query_vector(query)
        limit = normalize_top_k(top_k)
        scope = resolve_filters(filters)
        if resolve_dialect_name(session) == POSTGRES_DIALECT:
            return self._search_postgresql(session, vector, limit, scope)
        return self._search_exact(session, vector, limit, scope)

    def _search_postgresql(
        self,
        session: Session,
        vector: list[float],
        limit: int,
        scope: RetrievalFilters,
    ) -> list[RetrievedChunk]:
        """使用 pgvector 运算符检索；``exact=True`` 时禁用索引扫描。"""

        if self.exact:
            # 精确近邻基线：本事务内关闭索引与位图扫描，避免命中 HNSW 近似结果。
            session.execute(text("SET LOCAL enable_indexscan = off"))
            session.execute(text("SET LOCAL enable_bitmapscan = off"))
        distance = cosine_distance_expression(vector).label("distance")
        statement = (
            apply_retrieval_filters(
                select(DocumentChunk, distance).where(DocumentChunk.embedding.is_not(None)),
                scope,
            )
            .order_by(distance)
            .limit(limit)
        )
        rows = session.execute(statement).all()
        return [
            RetrievedChunk.from_document_chunk(
                chunk,
                rank=rank,
                semantic_score=similarity_from_cosine_distance(float(value)),
            )
            for rank, (chunk, value) in enumerate(rows)
        ]

    def _search_exact(
        self,
        session: Session,
        vector: list[float],
        limit: int,
        scope: RetrievalFilters,
    ) -> list[RetrievedChunk]:
        """非 PostgreSQL 方言的精确基线：在 Python 中计算余弦相似度。"""

        statement = apply_retrieval_filters(
            select(DocumentChunk).where(DocumentChunk.embedding.is_not(None)),
            scope,
        )
        chunks = session.scalars(statement).all()
        scored = [
            (chunk, cosine_similarity(vector, list(chunk.embedding or [])))
            for chunk in chunks
        ]
        # 分数降序；同分时按分块序号保证结果确定性。
        scored.sort(key=lambda item: (-item[1], item[0].chunk_index))
        return [
            RetrievedChunk.from_document_chunk(chunk, rank=rank, semantic_score=score)
            for rank, (chunk, score) in enumerate(scored[:limit])
        ]

__all__ = [
    "POSTGRES_DIALECT",
    "VectorSearchRetriever",
    "cosine_distance_expression",
    "cosine_similarity",
    "similarity_from_cosine_distance",
]
