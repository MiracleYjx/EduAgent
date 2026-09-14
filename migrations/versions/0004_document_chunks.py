"""新增 knowledge chunk 表：pgvector 向量列、全文检索列与检索索引。

包含：

- ``document_chunks`` 表及 ``documents``/``courses``/``knowledge_bases`` 外键级联。
- ``embedding`` 使用 ``vector(1024)``（与 ``EMBEDDING_MODEL`` 维度一致）。
- HNSW 索引（``vector_cosine_ops``，m=16，ef_construction=64）用于语义近邻检索。
- GIN 索引用于 ``tsvector`` 关键词检索。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0004_document_chunks"
down_revision: str | None = "0003_exam_participants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 与 ``EMBEDDING_MODEL``（BAAI/bge-large-zh-v1.5，1024 维）保持一致。
EMBEDDING_VECTOR_DIMENSION = 1024


def upgrade() -> None:
    """创建知识片段表、检索索引与来源外键。"""

    op.create_table(
        "document_chunks",
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("knowledge_base_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_VECTOR_DIMENSION), nullable=True),
        sa.Column("search_vector", postgresql.TSVECTOR(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["course_id"], ["courses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_id",
            "chunk_index",
            name="uq_document_chunks_document_index",
        ),
    )
    op.create_index(
        op.f("ix_document_chunks_document_id"),
        "document_chunks",
        ["document_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_document_chunks_course_id"),
        "document_chunks",
        ["course_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_document_chunks_knowledge_base_id"),
        "document_chunks",
        ["knowledge_base_id"],
        unique=False,
    )
    # HNSW 索引支撑 pgvector 语义近邻检索，余弦距离与归一化向量保持一致。
    op.create_index(
        "ix_document_chunks_embedding_hnsw",
        "document_chunks",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    # GIN 索引支撑 tsvector 关键词检索，覆盖术语、专有名词和编号。
    op.create_index(
        "ix_document_chunks_search_vector_gin",
        "document_chunks",
        ["search_vector"],
        unique=False,
        postgresql_using="gin",
    )


def downgrade() -> None:
    """移除本次新增的检索索引与知识片段表。"""

    op.drop_index("ix_document_chunks_search_vector_gin", table_name="document_chunks")
    op.drop_index("ix_document_chunks_embedding_hnsw", table_name="document_chunks")
    op.drop_index(op.f("ix_document_chunks_knowledge_base_id"), table_name="document_chunks")
    op.drop_index(op.f("ix_document_chunks_course_id"), table_name="document_chunks")
    op.drop_index(op.f("ix_document_chunks_document_id"), table_name="document_chunks")
    op.drop_table("document_chunks")
