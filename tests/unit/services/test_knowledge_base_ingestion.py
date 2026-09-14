"""T039 知识库摄取持久化单元测试：真实阶段、片段落库与失败处理。

测试运行在 SQLite 内存库上，Embedding Provider 使用替身（不下载模型、不访问网络），
解析器可使用真实 TXT 解析器或替身注册表，用于稳定覆盖失败路径。
"""

from __future__ import annotations

import hashlib
from collections.abc import Generator, Sequence
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.ai.embedding.base import BaseEmbeddingProvider
from backend.app.ai.ingestion.parsers import (
    DOCUMENT_PARSE_FAILED,
    DocumentParseError,
    DocumentParserRegistry,
    ParsedDocument,
    ParsedSection,
)
from backend.app.ai.ingestion.service import (
    KNOWLEDGE_BASE_EMPTY,
    IngestionService,
    StatusListener,
)
from backend.app.core.database import Base
from backend.app.domain.enums import DocumentStatus, UserRole
from backend.app.models import (
    Course,
    Document,
    DocumentChunk,
    Role,
    User,
)
from backend.app.models.document_chunk import EMBEDDING_VECTOR_DIMENSION
from backend.app.services.auth_service import hash_password
from backend.app.services.course_service import CourseService
from backend.app.services.knowledge_base_service import (
    DocumentValidationError,
    KnowledgeBasePermissionError,
    KnowledgeBaseService,
)

LESSON_TEXT = (
    "第一节 检索增强生成。\n\n"
    "检索增强生成先检索课程资料，再让模型基于检索结果作答。\n\n"
    "第二节 向量检索。\n\n"
    "向量检索使用余弦距离衡量语义相似度。\n"
).encode()


@pytest.fixture
def session() -> Generator[Session, None, None]:
    """创建隔离的摄取测试数据库会话。"""

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session
    engine.dispose()


def add_teacher(session: Session, *, username: str = "teacher") -> User:
    """向测试库写入一个教师账号。"""

    teacher = User(
        username=username,
        email=f"{username}@example.com",
        password_hash=hash_password("正确密码"),
    )
    role = session.scalar(select(Role).where(Role.name == UserRole.TEACHER))
    teacher.roles.append(role or Role(name=UserRole.TEACHER, description="教师"))
    session.add(teacher)
    session.commit()
    session.refresh(teacher)
    return teacher


def add_course(session: Session, teacher: User) -> Course:
    """通过课程服务创建测试课程。"""

    return CourseService(session).create_course(name="Python 基础", created_by=teacher.id)


class StubEmbeddingProvider(BaseEmbeddingProvider):
    """可控的 Embedding 替身：确定性向量、可指定维度与就绪状态。"""

    provider_name = "stub"
    model_name = "stub-1024"

    def __init__(
        self,
        *,
        dimension: int = EMBEDDING_VECTOR_DIMENSION,
        ready: bool = True,
        readiness_detail: str | None = None,
    ) -> None:
        self.dimension = dimension
        self._ready = ready
        self._readiness_detail = readiness_detail
        self.calls: list[list[str]] = []

    def is_ready(self) -> bool:
        return self._ready

    def readiness_detail(self) -> str | None:
        return self._readiness_detail

    async def embed_documents(self, documents: Sequence[str]) -> list[list[float]]:
        normalized = self.ensure_documents(documents)
        self.calls.append(normalized)
        return self.validate_document_vectors(
            normalized, [self._vector(text) for text in normalized]
        )

    async def embed_query(self, query: str) -> list[float]:
        return self._vector(self.ensure_query(query))

    def _vector(self, text: str) -> list[float]:
        """按内容摘要生成确定性向量，避免依赖随机数。"""

        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [digest[index % len(digest)] / 255 for index in range(self.dimension)]


def build_factory(
    *,
    provider: BaseEmbeddingProvider | None = None,
    registry: DocumentParserRegistry | None = None,
):
    """构造注入替身依赖的编排器工厂（保持 Service 层的阶段回调）。"""

    def build(on_transition: StatusListener) -> IngestionService:
        return IngestionService(
            parser_registry=registry,
            embedding_provider=provider,
            on_transition=on_transition,
        )

    return build


def seed_document(
    session: Session,
    *,
    storage_path: str | None = None,
) -> tuple[User, KnowledgeBaseService, str]:
    """创建课程、知识库与待摄取资料，返回教师、服务和文档标识。"""

    teacher = add_teacher(session)
    course = add_course(session, teacher)
    service = KnowledgeBaseService(session)
    knowledge_base = service.create_knowledge_base(
        course_id=course.id,
        name="课程资料",
        teacher_id=teacher.id,
    )
    document = service.upload_document(
        course_id=course.id,
        knowledge_base_id=knowledge_base.id,
        uploaded_by=teacher.id,
        original_filename="第一章.txt",
        storage_path=storage_path,
        teacher_id=teacher.id,
    )
    return teacher, service, document.id


def test_ingest_document_persists_chunks_and_real_stages(session: Session) -> None:
    """成功摄取：真实阶段依次落库，片段带来源元数据与向量写入 DocumentChunk。"""

    teacher, service, document_id = seed_document(session)
    provider = StubEmbeddingProvider()
    recorded: list[DocumentStatus] = []
    original_update = service.update_document_status

    def spy(document_id_value, status, **kwargs):
        recorded.append(DocumentStatus(status))
        return original_update(document_id_value, status, **kwargs)

    service.update_document_status = spy  # type: ignore[method-assign]

    result = service.ingest_document(
        document_id,
        content=LESSON_TEXT,
        teacher_id=teacher.id,
        ingestion_service_factory=build_factory(provider=provider),
    )

    assert recorded == [
        DocumentStatus.PARSING,
        DocumentStatus.CHUNKING,
        DocumentStatus.EMBEDDING,
        DocumentStatus.READY,
    ]
    assert result.status is DocumentStatus.READY
    assert result.chunk_count >= 1
    assert result.error_code is None

    document = session.get(Document, UUID(document_id))
    assert document is not None
    assert document.status is DocumentStatus.READY
    assert document.retryable is False

    chunks = session.scalars(
        select(DocumentChunk)
        .where(DocumentChunk.document_id == document.id)
        .order_by(DocumentChunk.chunk_index)
    ).all()
    assert len(chunks) == result.chunk_count
    for index, chunk in enumerate(chunks):
        assert chunk.chunk_index == index
        assert chunk.content.strip()
        assert chunk.course_id == document.course_id
        assert chunk.knowledge_base_id == document.knowledge_base_id
        assert chunk.chunk_metadata["document_id"] == str(document.id)
        assert chunk.chunk_metadata["course_id"] == str(document.course_id)
        assert chunk.chunk_metadata["chunk_index"] == index
        assert chunk.embedding is not None
        assert len(chunk.embedding) == EMBEDDING_VECTOR_DIMENSION
    # 测试方言不支持 tsvector，保持为空而不是伪造全文检索内容。
    assert all(chunk.search_vector is None for chunk in chunks)


def test_ingest_document_marks_failed_on_parse_error(session: Session) -> None:
    """解析失败：文档置为 Failed 并保留可读原因，且不写入任何片段。"""

    teacher, service, document_id = seed_document(session)
    registry = MagicMock(spec=DocumentParserRegistry)
    registry.parse.side_effect = DocumentParseError(
        DOCUMENT_PARSE_FAILED,
        detail="替身解析失败",
    )

    result = service.ingest_document(
        document_id,
        content=LESSON_TEXT,
        teacher_id=teacher.id,
        ingestion_service_factory=build_factory(
            provider=StubEmbeddingProvider(),
            registry=registry,
        ),
    )

    assert result.status is DocumentStatus.FAILED
    assert result.error_code == DOCUMENT_PARSE_FAILED
    assert result.error_message
    assert result.retryable is True
    assert result.chunk_count == 0
    assert session.scalars(select(DocumentChunk)).all() == []

    document = session.get(Document, UUID(document_id))
    assert document is not None
    assert document.status is DocumentStatus.FAILED
    assert document.error_code == DOCUMENT_PARSE_FAILED
    assert document.error_message
    assert document.retryable is True


def test_ingest_document_empty_knowledge_is_terminal_failure(session: Session) -> None:
    """清洗后无有效片段：判定为 KNOWLEDGE_BASE_EMPTY 终态，且不允许直接重试。"""

    teacher, service, document_id = seed_document(session)
    registry = MagicMock(spec=DocumentParserRegistry)
    registry.parse.return_value = ParsedDocument(
        file_format="txt",
        text="   \n\n   ",
        sections=(ParsedSection(index=1, location="第 1 段", text="   "),),
    )

    result = service.ingest_document(
        document_id,
        content=LESSON_TEXT,
        teacher_id=teacher.id,
        ingestion_service_factory=build_factory(
            provider=StubEmbeddingProvider(),
            registry=registry,
        ),
    )

    assert result.status is DocumentStatus.FAILED
    assert result.error_code == KNOWLEDGE_BASE_EMPTY
    assert result.error_message == "资料未形成有效知识片段，暂不能用于出题或阅卷。"
    assert result.retryable is False
    assert session.scalars(select(DocumentChunk)).all() == []

    with pytest.raises(DocumentValidationError):
        service.retry_document(document_id, teacher_id=teacher.id)


def test_ingest_document_not_ready_provider_is_reported(session: Session) -> None:
    """Provider 未就绪：文档失败并给出未就绪原因，不伪造 Ready 知识。"""

    teacher, service, document_id = seed_document(session)

    result = service.ingest_document(
        document_id,
        content=LESSON_TEXT,
        teacher_id=teacher.id,
        ingestion_service_factory=build_factory(
            provider=StubEmbeddingProvider(
                ready=False,
                readiness_detail="缺少 EMBEDDING_MODEL 配置",
            )
        ),
    )

    assert result.status is DocumentStatus.FAILED
    assert result.error_code == "EMBEDDING_PROVIDER_NOT_READY"
    assert result.retryable is False
    assert session.scalars(select(DocumentChunk)).all() == []


def test_ingest_document_rejects_dimension_mismatch(session: Session) -> None:
    """向量维度与 pgvector 列不一致时失败，避免写入无法检索的片段。"""

    teacher, service, document_id = seed_document(session)

    result = service.ingest_document(
        document_id,
        content=LESSON_TEXT,
        teacher_id=teacher.id,
        ingestion_service_factory=build_factory(
            provider=StubEmbeddingProvider(dimension=8)
        ),
    )

    assert result.status is DocumentStatus.FAILED
    assert result.error_code == "EMBEDDING_DIMENSION_MISMATCH"
    assert result.retryable is False
    assert result.chunk_count == 0
    assert session.scalars(select(DocumentChunk)).all() == []


def test_ingest_document_reads_storage_file(session: Session, tmp_path: Path) -> None:
    """未直接传入字节时从 storage_path 读取真实文件内容。"""

    lesson = tmp_path / "lesson.txt"
    lesson.write_text(LESSON_TEXT.decode("utf-8"), encoding="utf-8")
    teacher, service, document_id = seed_document(session, storage_path=str(lesson))

    result = service.ingest_document(
        document_id,
        teacher_id=teacher.id,
        ingestion_service_factory=build_factory(provider=StubEmbeddingProvider()),
    )

    assert result.status is DocumentStatus.READY
    assert result.chunk_count >= 1


def test_ingest_document_requires_readable_file(session: Session, tmp_path: Path) -> None:
    """存储文件缺失时抛出可读的校验错误，而不是伪造 Ready 状态。"""

    teacher, service, document_id = seed_document(
        session,
        storage_path=str(tmp_path / "missing.txt"),
    )

    with pytest.raises(DocumentValidationError):
        service.ingest_document(
            document_id,
            teacher_id=teacher.id,
            ingestion_service_factory=build_factory(provider=StubEmbeddingProvider()),
        )


def test_ingest_document_replaces_previous_chunks(session: Session) -> None:
    """重复摄取同一资料时应替换旧片段，不残留过期知识也不违反唯一约束。"""

    teacher, service, document_id = seed_document(session)
    factory = build_factory(provider=StubEmbeddingProvider())

    first = service.ingest_document(
        document_id,
        content=LESSON_TEXT,
        teacher_id=teacher.id,
        ingestion_service_factory=factory,
    )
    second = service.ingest_document(
        document_id,
        content=LESSON_TEXT,
        teacher_id=teacher.id,
        ingestion_service_factory=factory,
    )

    assert first.status is DocumentStatus.READY
    assert second.status is DocumentStatus.READY
    chunks = session.scalars(select(DocumentChunk)).all()
    assert len(chunks) == second.chunk_count
    assert [chunk.chunk_index for chunk in chunks] == list(range(second.chunk_count))


def test_ingest_document_requires_course_access(session: Session) -> None:
    """非课程所有者不能触发摄取。"""

    _, service, document_id = seed_document(session)
    intruder = add_teacher(session, username="intruder")

    with pytest.raises(KnowledgeBasePermissionError):
        service.ingest_document(
            document_id,
            content=LESSON_TEXT,
            teacher_id=intruder.id,
            ingestion_service_factory=build_factory(provider=StubEmbeddingProvider()),
        )


def test_list_document_chunks_returns_metadata_for_ui(session: Session) -> None:
    """界面可读取片段摘要：序号、内容、来源元数据与是否有向量。"""

    teacher, service, document_id = seed_document(session)
    service.ingest_document(
        document_id,
        content=LESSON_TEXT,
        teacher_id=teacher.id,
        ingestion_service_factory=build_factory(provider=StubEmbeddingProvider()),
    )

    rows = service.list_document_chunks(document_id, teacher_id=teacher.id)

    assert rows
    assert rows[0]["chunk_index"] == 0
    assert rows[0]["has_embedding"] is True
    assert rows[0]["metadata"]["document_id"] == document_id
