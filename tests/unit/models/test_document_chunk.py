"""T038 DocumentChunk 模型单元测试：表结构、方言适配、持久化与约束。

测试全部运行在 SQLite 内存库上：向量列通过 ``EmbeddingVector`` 退化为 JSON 文本，
因此不需要 PostgreSQL 即可验证业务持久化逻辑；PostgreSQL 侧的 ``vector(1024)``、
HNSW 与 GIN 索引由迁移与 `alembic check` 验证。
"""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.core.database import Base
from backend.app.domain.enums import UserRole
from backend.app.models import (
    Course,
    Document,
    DocumentChunk,
    KnowledgeBase,
    Role,
    User,
)
from backend.app.models.document_chunk import (
    EMBEDDING_VECTOR_DIMENSION,
    EmbeddingVector,
)

DOCUMENT_INDEXES = {
    "ix_document_chunks_course_id",
    "ix_document_chunks_document_id",
    "ix_document_chunks_knowledge_base_id",
    "ix_document_chunks_embedding_hnsw",
    "ix_document_chunks_search_vector_gin",
}


def _seed_document(session: Session) -> Document:
    """创建课程、知识库与资料，作为知识片段的来源关系。"""

    teacher = User(
        username="teacher",
        email="teacher@example.com",
        password_hash="hashed-password",
    )
    teacher.roles.append(Role(name=UserRole.TEACHER))
    course = Course(name="Python 基础", creator=teacher)
    knowledge_base = KnowledgeBase(name="课程资料", course=course)
    document = Document(
        original_filename="lesson.md",
        file_format="md",
        knowledge_base=knowledge_base,
        course=course,
        uploader=teacher,
    )
    session.add(document)
    session.commit()
    return document


def test_document_chunk_table_and_retrieval_indexes_are_registered() -> None:
    """表、来源外键索引与 HNSW/GIN 检索索引都应注册到统一元数据。"""

    table = Base.metadata.tables["document_chunks"]

    assert [column.name for column in table.columns] == [
        "document_id",
        "course_id",
        "knowledge_base_id",
        "chunk_index",
        "content",
        "embedding",
        "search_vector",
        "metadata",
        "id",
        "created_at",
        "updated_at",
    ]
    assert {index.name for index in table.indexes} == DOCUMENT_INDEXES
    hnsw = next(i for i in table.indexes if i.name.endswith("_hnsw"))
    assert hnsw.dialect_options["postgresql"]["using"] == "hnsw"
    assert hnsw.dialect_options["postgresql"]["ops"] == {
        "embedding": "vector_cosine_ops",
    }
    gin = next(i for i in table.indexes if i.name.endswith("_gin"))
    assert gin.dialect_options["postgresql"]["using"] == "gin"
    assert table.c.metadata.name == "metadata"


def test_embedding_vector_type_uses_pgvector_on_postgresql() -> None:
    """PostgreSQL 方言必须渲染为 pgvector 向量列，其他方言退化为文本。"""

    vector_type = EmbeddingVector(EMBEDDING_VECTOR_DIMENSION)
    postgres_impl = vector_type.load_dialect_impl(postgresql.dialect())
    sqlite_impl = vector_type.load_dialect_impl(create_engine("sqlite://").dialect)

    assert postgres_impl.__class__.__name__ == "VECTOR"
    assert postgres_impl.dim == EMBEDDING_VECTOR_DIMENSION
    assert sqlite_impl.__class__.__name__ == "Text"


def test_document_chunk_persists_metadata_and_embedding_in_sqlite() -> None:
    """片段内容、来源元数据与向量在测试方言下都应可写入并读回。"""

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        document = _seed_document(session)
        chunk = DocumentChunk(
            document_id=document.id,
            course_id=document.course_id,
            knowledge_base_id=document.knowledge_base_id,
            chunk_index=0,
            content="检索增强生成结合了检索与生成。",
            embedding=[0.25, 0.5, 0.75],
            chunk_metadata={
                "document_id": str(document.id),
                "course_id": str(document.course_id),
                "chunk_index": 0,
                "location": "第 1 段",
            },
        )
        session.add(chunk)
        session.commit()
        session.expire_all()

        stored = session.get(DocumentChunk, chunk.id)

    assert stored is not None
    assert stored.chunk_index == 0
    assert stored.embedding == [0.25, 0.5, 0.75]
    assert stored.chunk_metadata["chunk_index"] == 0
    assert stored.chunk_metadata["location"] == "第 1 段"
    # 测试方言没有 tsvector 维护能力，保持为空而不是伪造全文索引内容。
    assert stored.search_vector is None
    assert isinstance(stored.id, UUID)


def test_document_chunk_rejects_duplicate_document_index() -> None:
    """同一资料内的分块序号必须唯一。"""

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        document = _seed_document(session)
        for _ in range(2):
            session.add(
                DocumentChunk(
                    document_id=document.id,
                    course_id=document.course_id,
                    knowledge_base_id=document.knowledge_base_id,
                    chunk_index=0,
                    content="重复片段",
                    chunk_metadata={},
                )
            )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_document_chunk_is_removed_with_document() -> None:
    """删除资料时其知识片段必须级联删除，不残留可检索内容。"""

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        document = _seed_document(session)
        session.add(
            DocumentChunk(
                document_id=document.id,
                course_id=document.course_id,
                knowledge_base_id=document.knowledge_base_id,
                chunk_index=0,
                content="待级联删除的片段",
                chunk_metadata={},
            )
        )
        session.commit()

        session.delete(document)
        session.commit()

        remaining = session.query(DocumentChunk).count()
        assert remaining == 0
        assert "document_chunks" in inspect(engine).get_table_names()
