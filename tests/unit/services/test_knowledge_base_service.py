"""T022 知识库服务单元测试：知识库、文档元数据和处理状态。"""

from __future__ import annotations

from collections.abc import Generator
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.core.database import Base
from backend.app.domain.enums import DocumentStatus, UserRole
from backend.app.models import Course, KnowledgeBase, Role, User
from backend.app.services.auth_service import hash_password
from backend.app.services.course_service import CourseService
from backend.app.services.knowledge_base_service import (
    DocumentValidationError,
    KnowledgeBaseConflictError,
    KnowledgeBasePermissionError,
    KnowledgeBaseService,
)


@pytest.fixture
def session() -> Generator[Session, None, None]:
    """创建隔离的知识库服务测试数据库会话。"""

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session
    engine.dispose()


def add_teacher(
    session: Session,
    *,
    username: str = "teacher",
    email: str = "teacher@example.com",
) -> User:
    """向测试库写入一个教师账号。"""

    teacher = User(
        username=username,
        email=email,
        password_hash=hash_password("正确密码"),
    )
    role = session.scalar(select(Role).where(Role.name == UserRole.TEACHER))
    teacher.roles.append(role or Role(name=UserRole.TEACHER, description="教师"))
    session.add(teacher)
    session.commit()
    session.refresh(teacher)
    return teacher


def add_course(session: Session, teacher: User, name: str = "Python 基础"):
    """通过课程服务创建测试课程。"""

    return CourseService(session).create_course(
        name=name,
        created_by=teacher.id,
    )


def test_knowledge_base_and_document_metadata_lifecycle(session: Session) -> None:
    """知识库和文档元数据应能创建、查询并持久化处理状态。"""

    teacher = add_teacher(session)
    course = add_course(session, teacher)
    service = KnowledgeBaseService(session)

    knowledge_base = service.create_knowledge_base(
        course_id=course.id,
        name="课程资料",
        description="用于课程问答和出题。",
        teacher_id=teacher.id,
    )
    assert knowledge_base.course_id == course.id
    assert knowledge_base.name == "课程资料"
    assert knowledge_base.document_count == 0

    document = service.upload_document(
        course_id=course.id,
        knowledge_base_id=knowledge_base.id,
        uploaded_by=teacher.id,
        original_filename="第一章.MD",
        storage_path="uploads/第一章.md",
    )
    assert document.course_id == course.id
    assert document.knowledge_base_id == knowledge_base.id
    assert document.uploaded_by == str(teacher.id)
    assert document.file_format == "md"
    assert document.status is DocumentStatus.UPLOADED

    parsing = service.update_document_status(
        document.id,
        DocumentStatus.PARSING,
        teacher_id=teacher.id,
    )
    embedding = service.update_document_status(
        document.id,
        DocumentStatus.EMBEDDING,
        teacher_id=teacher.id,
    )
    ready = service.update_document_status(
        document.id,
        DocumentStatus.READY,
        teacher_id=teacher.id,
    )

    assert parsing.status is DocumentStatus.PARSING
    assert embedding.status is DocumentStatus.EMBEDDING
    assert ready.status is DocumentStatus.READY
    assert service.get_document(document.id).status is DocumentStatus.READY
    assert len(service.list_documents(knowledge_base_id=knowledge_base.id)) == 1
    assert service.get_knowledge_base(knowledge_base.id).document_count == 1


def test_document_failure_and_retry_persist_safe_state(session: Session) -> None:
    """文档失败原因和可重试标记应持久化，重试后应回到解析状态。"""

    teacher = add_teacher(session)
    course = add_course(session, teacher)
    service = KnowledgeBaseService(session)
    knowledge_base = service.create_knowledge_base(
        course_id=course.id,
        name="课程资料",
    )
    document = service.upload_document(
        course_id=course.id,
        knowledge_base_id=knowledge_base.id,
        uploaded_by=teacher.id,
        original_filename="第一章.txt",
    )

    failed = service.mark_document_failed(
        document.id,
        error_code="DOCUMENT_PARSE_FAILED",
        error_message="资料解析失败，请检查文件内容后重新上传。",
        retryable=True,
    )
    assert failed.status is DocumentStatus.FAILED
    assert failed.error_code == "DOCUMENT_PARSE_FAILED"
    assert failed.error_message == "资料解析失败，请检查文件内容后重新上传。"
    assert failed.retryable is True

    retried = service.retry_document(document.id)
    assert retried.status is DocumentStatus.PARSING
    assert retried.error_code is None
    assert retried.error_message is None
    assert retried.retryable is False


def test_knowledge_base_binding_and_document_scope_are_enforced(
    session: Session,
) -> None:
    """知识库可以绑定到同一教师的其他课程，但文档必须保持课程一致。"""

    teacher = add_teacher(session)
    first_course = add_course(session, teacher, "第一门课程")
    second_course = add_course(session, teacher, "第二门课程")
    service = KnowledgeBaseService(session)
    knowledge_base = service.create_knowledge_base(
        course_id=first_course.id,
        name="待绑定资料",
    )

    rebound = service.bind_to_course(
        knowledge_base.id,
        second_course.id,
        teacher_id=teacher.id,
    )
    assert rebound.course_id == second_course.id

    with pytest.raises(KnowledgeBaseConflictError, match="知识库不属于指定课程"):
        service.upload_document(
            course_id=first_course.id,
            knowledge_base_id=knowledge_base.id,
            uploaded_by=teacher.id,
            original_filename="lesson.txt",
        )

    document = service.upload_document(
        course_id=second_course.id,
        knowledge_base_id=knowledge_base.id,
        uploaded_by=teacher.id,
        original_filename="lesson.txt",
    )
    assert document.course_id == second_course.id
    assert session.get(Course, UUID(second_course.id)) is not None
    assert session.get(KnowledgeBase, UUID(knowledge_base.id)) is not None


def test_knowledge_base_rejects_unsupported_format_and_wrong_owner(
    session: Session,
) -> None:
    """不支持的格式和非课程所有者操作都应被拒绝。"""

    teacher = add_teacher(session)
    another_teacher = add_teacher(
        session,
        username="another-teacher",
        email="another-teacher@example.com",
    )
    course = add_course(session, teacher)
    service = KnowledgeBaseService(session)

    with pytest.raises(KnowledgeBasePermissionError, match="无权访问该课程"):
        service.create_knowledge_base(
            course_id=course.id,
            name="越权资料",
            teacher_id=another_teacher.id,
        )

    knowledge_base = service.create_knowledge_base(
        course_id=course.id,
        name="课程资料",
    )
    with pytest.raises(
        DocumentValidationError, match="当前仅支持 PDF、TXT 和 Markdown"
    ):
        service.upload_document(
            course_id=course.id,
            knowledge_base_id=knowledge_base.id,
            uploaded_by=teacher.id,
            original_filename="课程资料.docx",
        )

    document = service.upload_document(
        course_id=course.id,
        knowledge_base_id=knowledge_base.id,
        uploaded_by=teacher.id,
        original_filename="课程资料.txt",
    )
    assert service.list_documents(teacher_id=another_teacher.id) == []
    assert service.get_document(document.id, teacher_id=teacher.id).id == document.id
