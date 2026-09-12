"""T030 教师准备流程集成测试。"""

from __future__ import annotations

from collections.abc import Generator
from secrets import token_urlsafe
from typing import Any
from uuid import UUID

import gradio as gr
import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, selectinload, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core.app import create_app
from backend.app.core.database import Base, get_db
from backend.app.domain.enums import (
    DocumentStatus,
    ExamStatus,
    QuestionStatus,
    UserRole,
)
from backend.app.models import (
    Course,
    Document,
    Exam,
    KnowledgeBase,
    Question,
    Role,
    User,
)
from backend.app.services.auth_service import hash_password
from tests.unit.settings_helpers import build_test_settings

TEST_JWT_SECRET = token_urlsafe(48)
TEST_PASSWORD = token_urlsafe(24)


@pytest.fixture
def session_factory() -> Generator[sessionmaker[Session], None, None]:
    """创建可跨 TestClient 请求共享的隔离 SQLite 会话工厂。"""

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


@pytest.fixture
def teacher_id(session_factory: sessionmaker[Session]) -> UUID:
    """写入具备完整教师权限的测试账号。"""

    with session_factory() as session:
        teacher = User(
            username="integration-teacher",
            email="integration-teacher@example.com",
            password_hash=hash_password(TEST_PASSWORD),
        )
        teacher.roles.append(Role(name=UserRole.TEACHER, description="教师"))
        session.add(teacher)
        session.commit()
        session.refresh(teacher)
        return teacher.id


@pytest.fixture
def client(
    session_factory: sessionmaker[Session],
) -> Generator[TestClient, None, None]:
    """构造使用隔离数据库和测试 JWT 密钥的 API 客户端。"""

    with gr.Blocks() as test_gradio_app:
        gr.Markdown("教师准备流程集成测试")
    application = create_app(
        settings=build_test_settings(JWT_SECRET_KEY=TEST_JWT_SECRET),
        gradio_app=test_gradio_app,
    )

    def override_get_db() -> Generator[Session, None, None]:
        """为每个 API 请求提供独立测试会话。"""

        with session_factory() as session:
            try:
                yield session
            except Exception:
                session.rollback()
                raise

    application.dependency_overrides[get_db] = override_get_db
    with TestClient(application) as test_client:
        yield test_client
    application.dependency_overrides.clear()


def _expect_json(
    response: Response,
    expected_status: int,
    operation: str,
) -> dict[str, Any]:
    """校验 API 状态码，并在失败时输出当前业务步骤。"""

    assert response.status_code == expected_status, (
        f"{operation}失败：HTTP {response.status_code}，响应为 {response.text}"
    )
    body = response.json()
    assert isinstance(body, dict), f"{operation}未返回 JSON 对象。"
    return body


def test_teacher_prepares_course_document_question_and_exam(
    client: TestClient,
    session_factory: sessionmaker[Session],
    teacher_id: UUID,
) -> None:
    """教师可完成课程资料登记、人工题审核和已审核题目组卷。"""

    login = _expect_json(
        client.post(
            "/api/auth/login",
            json={
                "username": "integration-teacher",
                "password": TEST_PASSWORD,
            },
        ),
        200,
        "教师登录",
    )
    headers = {"Authorization": f"Bearer {login['access_token']}"}

    course = _expect_json(
        client.post(
            "/api/courses",
            headers=headers,
            json={
                "name": "Python 教师培训",
                "description": "用于验证教师准备考试的完整流程。",
            },
        ),
        201,
        "创建课程",
    )
    course_id = course["id"]
    assert course["created_by"] == str(teacher_id)

    knowledge_base = _expect_json(
        client.post(
            "/api/knowledge-bases",
            headers=headers,
            json={
                "course_id": course_id,
                "name": "Python 课程资料",
                "description": "保存课程资料元数据。",
            },
        ),
        201,
        "创建知识库",
    )
    knowledge_base_id = knowledge_base["id"]
    assert knowledge_base["course_id"] == course_id

    document = _expect_json(
        client.post(
            f"/api/knowledge-bases/{knowledge_base_id}/documents",
            headers=headers,
            json={
                "original_filename": "第一章.md",
                "storage_path": "uploads/python/第一章.md",
            },
        ),
        201,
        "登记课程资料元数据",
    )
    document_id = document["id"]
    assert document["course_id"] == course_id
    assert document["knowledge_base_id"] == knowledge_base_id
    assert document["uploaded_by"] == str(teacher_id)
    assert document["file_format"] == "md"
    assert document["status"] == DocumentStatus.UPLOADED.value

    question = _expect_json(
        client.post(
            "/api/questions",
            headers=headers,
            json={
                "course_id": course_id,
                "type": "SHORT_ANSWER",
                "content": "请说明 Python 函数的主要作用。",
                "reference_answer": "函数用于封装并复用一段逻辑。",
                "scoring_rubric": "说明封装和复用两个要点。",
                "difficulty": "简单",
                "knowledge_points": ["函数", "代码复用"],
                "score": 10,
            },
        ),
        201,
        "创建人工题目",
    )
    question_id = question["id"]
    assert question["status"] == QuestionStatus.DRAFT.value

    rejected_exam = _expect_json(
        client.post(
            "/api/exams",
            headers=headers,
            json={
                "course_id": course_id,
                "title": "未审核题目测试考试",
                "question_ids": [question_id],
            },
        ),
        422,
        "验证未审核题目不能组卷",
    )
    assert "Approved" in str(rejected_exam["detail"])

    pending_question = _expect_json(
        client.post(
            f"/api/questions/{question_id}/submit-review",
            headers=headers,
        ),
        200,
        "提交题目审核",
    )
    assert pending_question["status"] == QuestionStatus.PENDING_REVIEW.value

    approved_question = _expect_json(
        client.post(
            f"/api/questions/{question_id}/approve",
            headers=headers,
        ),
        200,
        "审核通过题目",
    )
    assert approved_question["status"] == QuestionStatus.APPROVED.value

    exam = _expect_json(
        client.post(
            "/api/exams",
            headers=headers,
            json={
                "course_id": course_id,
                "title": "Python 第一章测验",
                "description": "验证已审核题目可以创建考试。",
                "duration_minutes": 45,
                "question_ids": [question_id],
            },
        ),
        201,
        "使用已审核题目创建考试",
    )
    exam_id = exam["id"]
    assert exam["status"] == ExamStatus.DRAFT.value
    assert exam["question_ids"] == [question_id]
    assert exam["question_count"] == 1

    published_exam = _expect_json(
        client.post(
            f"/api/exams/{exam_id}/publish",
            headers=headers,
        ),
        200,
        "发布考试",
    )
    assert published_exam["status"] == ExamStatus.PUBLISHED.value

    with session_factory() as session:
        persisted_course = session.get(Course, UUID(course_id))
        persisted_knowledge_base = session.get(
            KnowledgeBase,
            UUID(knowledge_base_id),
        )
        persisted_document = session.get(Document, UUID(document_id))
        persisted_question = session.get(Question, UUID(question_id))
        persisted_exam = session.scalar(
            select(Exam)
            .options(selectinload(Exam.questions))
            .where(Exam.id == UUID(exam_id))
        )

        assert persisted_course is not None
        assert persisted_course.created_by == teacher_id
        assert persisted_knowledge_base is not None
        assert persisted_knowledge_base.course_id == persisted_course.id
        assert persisted_document is not None
        assert persisted_document.course_id == persisted_course.id
        assert persisted_document.knowledge_base_id == persisted_knowledge_base.id
        assert persisted_document.status is DocumentStatus.UPLOADED
        assert persisted_question is not None
        assert persisted_question.status is QuestionStatus.APPROVED
        assert persisted_exam is not None
        assert persisted_exam.status is ExamStatus.PUBLISHED
        assert [item.id for item in persisted_exam.questions] == [persisted_question.id]
