"""PostgreSQL ``tsvector + GIN`` 关键词检索。

关键词检索覆盖语义检索容易遗漏的术语、专有名词、编号与精确词形：

- 输入是查询文本、Top-K 与课程/知识库/资料过滤条件；本模块**不触发 Embedding 调用**。
- 使用 ``plainto_tsquery``（默认）或 ``websearch_to_tsquery`` 构造查询，``search_vector @@
  query`` 命中 ``ix_document_chunks_search_vector_gin`` GIN 索引。
- 排序分数使用 ``ts_rank``，按分数降序返回；无命中时返回空列表。

中文教学资料使用 ``simple`` 配置，避免英文词干化破坏术语；需要注意 PostgreSQL 默认分词器
按空白与标点切分，连续中文文本会形成长 token，因此关键词命中的前提是资料本身已分词
（例：术语之间带空格）或后续接入中文分词扩展（如 zhparser）。这一能力边界不得被掩盖：
命中不到时返回空列表，不伪造结果。非 PostgreSQL 方言没有 ``tsvector`` 维护能力，此时
抛出明确的不支持错误。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar, Final, Literal, cast

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from backend.app.ai.retrieval.base import (
    DEFAULT_TOP_K,
    BaseRetriever,
    RetrievalFilters,
    RetrievalInputError,
    RetrievalMode,
    RetrievalUnsupportedDialectError,
    RetrievedChunk,
    normalize_query_text,
    normalize_top_k,
    resolve_dialect_name,
    resolve_filters,
)
from backend.app.models import DocumentChunk

POSTGRES_DIALECT: Final[str] = "postgresql"

#: 支持的 tsquery 构造方式：``plainto`` 适合自然查询，``websearch`` 支持引号与布尔词。
KeywordQueryBuilder = Literal["plainto", "websearch"]

#: 默认文本检索配置；中文资料不做英文词干化。
DEFAULT_TS_CONFIG: Final[str] = "simple"


class KeywordSearchRetriever(BaseRetriever):
    """基于 PostgreSQL tsvector 的关键词检索实现。"""

    mode: ClassVar[RetrievalMode] = RetrievalMode.KEYWORD_ONLY

    def __init__(
        self,
        *,
        query_builder: KeywordQueryBuilder = "plainto",
        ts_config: str = DEFAULT_TS_CONFIG,
    ) -> None:
        if query_builder not in {"plainto", "websearch"}:
            raise RetrievalInputError(
                f"不支持的 tsquery 构造方式：{query_builder}；可选 plainto 或 websearch。"
            )
        self.query_builder = query_builder
        self.ts_config = ts_config

    def search(
        self,
        session: Session,
        query: str | Sequence[float],
        *,
        top_k: int = DEFAULT_TOP_K,
        filters: RetrievalFilters | None = None,
    ) -> list[RetrievedChunk]:
        """按查询文本检索命中的 Top-K 知识片段。"""

        # 先校验输入本身，再判断方言，保证错误原因与数据库无关且稳定。
        text_query = normalize_query_text(query)
        limit = normalize_top_k(top_k)
        scope = resolve_filters(filters)
        if resolve_dialect_name(session) != POSTGRES_DIALECT:
            raise RetrievalUnsupportedDialectError(
                "关键词检索依赖 PostgreSQL tsvector 与 GIN 索引，当前数据库方言不支持。"
            )

        tsquery = self._build_tsquery(text_query)
        rank = cast(
            "ColumnElement[float]",
            func.ts_rank(DocumentChunk.search_vector, tsquery),
        ).label("rank")
        statement = (
            self._apply_filters(
                select(DocumentChunk, rank).where(
                    DocumentChunk.search_vector.is_not(None),
                    DocumentChunk.search_vector.op("@@")(tsquery),
                ),
                scope,
            )
            # 分数降序；同分时按分块序号保证确定性。
            .order_by(rank.desc(), DocumentChunk.chunk_index)
            .limit(limit)
        )
        rows = session.execute(statement).all()
        return [
            RetrievedChunk.from_document_chunk(
                chunk,
                rank=index,
                keyword_score=float(score),
            )
            for index, (chunk, score) in enumerate(rows)
        ]

    def _build_tsquery(self, text_query: str) -> ColumnElement[Any]:
        """按配置构造 tsquery 表达式。"""

        builder = (
            func.plainto_tsquery
            if self.query_builder == "plainto"
            else func.websearch_to_tsquery
        )
        return cast("ColumnElement[Any]", builder(self.ts_config, text_query))

    @staticmethod
    def _apply_filters(
        statement: Select[Any],
        scope: RetrievalFilters,
    ) -> Select[Any]:
        """按课程、知识库与资料范围收窄检索范围。"""

        if scope.course_ids:
            statement = statement.where(DocumentChunk.course_id.in_(scope.course_ids))
        if scope.knowledge_base_ids:
            statement = statement.where(
                DocumentChunk.knowledge_base_id.in_(scope.knowledge_base_ids)
            )
        if scope.document_ids:
            statement = statement.where(
                DocumentChunk.document_id.in_(scope.document_ids)
            )
        return statement


__all__ = [
    "DEFAULT_TS_CONFIG",
    "POSTGRES_DIALECT",
    "KeywordQueryBuilder",
    "KeywordSearchRetriever",
]
