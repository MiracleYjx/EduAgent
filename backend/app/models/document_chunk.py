"""知识片段模型：来源元数据、向量与全文检索字段。

``DocumentChunk`` 是检索的最小单元，只保存“可检索的课程上下文”所需字段：

- ``embedding``：PostgreSQL 使用 ``pgvector`` 向量列，用于语义近邻检索；其他方言
  （单元测试使用的 SQLite）退化为 JSON 文本，让同一套持久化代码可以在两种方言下运行。
- ``search_vector``：PostgreSQL ``tsvector``，用于 ``to_tsvector`` 关键词检索与 GIN 索引。
- ``metadata``：JSON 来源元数据（document_id、course_id、chunk_index、定位信息等）。
  列名保留业务要求的 ``metadata``，ORM 属性名使用 ``chunk_metadata``，避免与
  ``Base.metadata`` 冲突。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator, TypeEngine

from backend.app.core.config import EMBEDDING_DIMENSION_DEFAULT
from backend.app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from sqlalchemy.engine import Dialect

    from backend.app.models.document import Document

#: 向量维度来自统一配置事实源，与 PostgreSQL 的 vector(1024) 迁移保持一致。
EMBEDDING_VECTOR_DIMENSION: Final[int] = EMBEDDING_DIMENSION_DEFAULT


class EmbeddingVector(TypeDecorator[list[float]]):
    """向量列类型：PostgreSQL 使用 pgvector，其他方言退化为 JSON 文本。

    该类型让“写入片段向量”的业务代码与方言无关，同时避免 SQLite 测试库因无法渲染
    ``vector`` 类型而无法建表。
    """

    impl = Text
    cache_ok = True

    def __init__(self, dimension: int = EMBEDDING_VECTOR_DIMENSION) -> None:
        super().__init__()
        self.dimension = dimension

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        """按方言选择底层实现：PostgreSQL 使用 pgvector 向量类型。"""

        if dialect.name == "postgresql":
            return dialect.type_descriptor(Vector(self.dimension))
        return dialect.type_descriptor(Text())

    def process_bind_param(
        self,
        value: Sequence[float] | None,
        dialect: Dialect,
    ) -> list[float] | str | None:
        """写入时统一为浮点序列；非 PostgreSQL 方言序列化为 JSON 文本。"""

        if value is None:
            return None
        vector = [float(item) for item in value]
        if dialect.name == "postgresql":
            return vector
        return json.dumps(vector)

    def process_result_value(
        self,
        value: Any,
        dialect: Dialect,
    ) -> list[float] | None:
        """读取时统一还原为浮点列表。"""

        if value is None:
            return None
        if dialect.name == "postgresql":
            return [float(item) for item in value]
        return [float(item) for item in json.loads(str(value))]


class DocumentChunk(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """属于某个资料的知识片段，携带来源元数据、向量和全文检索字段。"""

    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "chunk_index",
            name="uq_document_chunks_document_index",
        ),
        Index(
            "ix_document_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index(
            "ix_document_chunks_search_vector_gin",
            "search_vector",
            postgresql_using="gin",
        ),
    )

    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    course_id: Mapped[UUID] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    knowledge_base_id: Mapped[UUID] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(
        EmbeddingVector(EMBEDDING_VECTOR_DIMENSION)
    )
    search_vector: Mapped[str | None] = mapped_column(
        TSVECTOR().with_variant(Text(), "sqlite")
    )
    chunk_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSON,
        default=dict,
        nullable=False,
    )
    document: Mapped[Document] = relationship("Document", back_populates="chunks")


__all__ = [
    "EMBEDDING_VECTOR_DIMENSION",
    "DocumentChunk",
    "EmbeddingVector",
]
