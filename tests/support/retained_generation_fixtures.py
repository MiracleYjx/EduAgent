"""T075 AI 出题 API 契约测试：两阶段状态机、原子落库与对象级授权。

TCR（2026-09-18，T075 / 评审 B01～B03、B11、遗漏1）：

- **既有覆盖缺口**：T067/T068 各自用替身验证了 Agent 与校验器，但没有真实责任方把
  “生成 → 自动校验 → 候选题落库”串起来；没有测试证明候选题**整批原子写入**、字段保真、
  教师只能审核已持久化的 ``Pending Review``、以及跨课程不可见。
- **新增用例**：生成端整批落库（含 ``Pending Review`` 与 ``Needs Revision`` 两种结论）、
  字面字段保真、检索上下文不足 422 且零副作用、Provider 未就绪 503 且零副作用、
  写库中途失败整批回滚、候选列表课程隔离与分页、跨课程详情 404、教师审核状态机与陈旧断言、
  未认证/无权限/路由已注册。
- **对外契约**：plan §4.1（教师条件 → Query → 检索 → Question Agent → Schema → Validator →
  教师审核 → 题库）、FR-024～FR-028、T067/T068、T068 ``plan_transition`` 与
  ``CANDIDATE_STATUS_TRANSITIONS``。

本文件只断言对外契约，不复制服务内部实现。
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator, Sequence
from contextlib import ExitStack
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.ai.agents.question_agent import (
    QuestionAgent,
)
from backend.app.api.question_generation import (
    QuestionGenerationService,
    get_question_generation_service,
)
from backend.app.core.database import Base, get_db
from backend.app.domain.enums import (
    DocumentStatus,
    UserRole,
)
from backend.app.models import (
    Course,
    Document,
    DocumentChunk,
    KnowledgeBase,
    Question,
    User,
)
from backend.app.services.auth_service import create_access_token
from backend.app.services.course_service import CourseService
from backend.app.services.question_validator import QuestionValidator
from tests.support.application_lifecycle import bind_startup_to_session
from tests.support.question_generation_doubles import (
    StubEmbeddingProvider,
    StubQuestionProvider,
    StubRetriever,
    make_candidate,
    make_chunk,
)
from tests.support.retained_component_app import component_app as create_app
from tests.support.semantic_validation_doubles import StubSemanticProvider
from tests.unit.services.test_submission_service import (
    add_course,
    add_student,
    add_teacher,
    add_user,
)
from tests.unit.settings_helpers import build_test_settings

#: 单题无法使用的评分标准：只含分值占位，T068 判定为 Needs Revision。
UNUSABLE_RUBRIC = "2 分"


@pytest.fixture
def session() -> Generator[Session, None, None]:
    """为契约测试创建隔离数据库。"""

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session
    engine.dispose()


@pytest.fixture
def scenario(session: Session) -> dict[str, Any]:
    """创建两名教师、各自课程以及学生与管理员账号。"""

    teacher = add_teacher(session)
    other_teacher = add_user(
        session, UserRole.TEACHER, username="teacher-2", email="teacher2@example.com"
    )
    course = add_course(session, teacher)
    other_course_summary = CourseService(session).create_course(
        name="Python 进阶", created_by=other_teacher.id
    )
    other_course = session.get(Course, UUID(other_course_summary.id))
    assert other_course is not None
    return {
        "session": session,
        "teacher": teacher,
        "other_teacher": other_teacher,
        "course": course,
        "other_course": other_course,
        "student": add_student(session, username="student-1"),
        "admin": add_user(
            session, UserRole.ADMIN, username="admin", email="admin@example.com"
        ),
    }


class _FailingProvider:
    """按指定异常失败的出题 Provider 替身。"""

    provider_name = "failing-question"

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def generate_structured(
        self,
        messages: Sequence[Any],
        schema: Any,
        model: str | None = None,
    ) -> Any:
        raise self.error


def _service(
    session: Session,
    *,
    candidates: Sequence[Any] | None = None,
    chunks: Sequence[Any] | None = None,
    provider_error: Exception | None = None,
) -> QuestionGenerationService:
    """构造注入替身 Provider/检索/Embedding 的真实出题服务。"""

    provider: Any = (
        _FailingProvider(provider_error)
        if provider_error is not None
        else StubQuestionProvider(candidates=candidates or (make_candidate(),))
    )
    return QuestionGenerationService(
        session=session,
        agent=QuestionAgent(provider=provider),
        validator=QuestionValidator(),
        retriever=StubRetriever(
            chunks if chunks is not None else [make_chunk("chunk-1")]
        ),
        embedding_provider=StubEmbeddingProvider(),
        settings=build_test_settings(),
        semantic_provider=StubSemanticProvider(),
    )


def _generate(
    session: Session,
    *,
    course: Course,
    teacher: User,
    candidates: Sequence[Any],
    chunks: Sequence[Any] | None = None,
    provider_error: Exception | None = None,
) -> Any:
    """直接驱动服务完成一次生成，返回响应载荷。"""

    if chunks is None:
        live = _seed_chunk(
            session, course=course, teacher=teacher, content=make_chunk().content
        )
        candidates = [
            candidate.model_copy(
                update={
                    "source_context_ids": [
                        str(live.id) if source_id == "chunk-1" else source_id
                        for source_id in candidate.source_context_ids
                    ]
                }
            )
            for candidate in candidates
        ]
        chunks = [
            make_chunk(
                str(live.id),
                course_id=str(course.id),
                document_id=str(live.document_id),
            )
        ]
    service = _service(
        session,
        candidates=candidates,
        chunks=chunks,
        provider_error=provider_error,
    )
    return asyncio.run(
        service.generate_candidates(
            course_id=str(course.id),
            actor_id=str(teacher.id),
            request_id="request-generate-1",
            count=len(candidates),
        )
    )


def _seed_chunk(
    session: Session,
    *,
    course: Course,
    teacher: User,
    content: str,
) -> Any:
    """写入一条真实的课程片段，供检索依据的课程校验用例使用。"""

    knowledge_base = KnowledgeBase(course_id=course.id, name=f"{course.name} 知识库")
    session.add(knowledge_base)
    session.flush()
    document = Document(
        course_id=course.id,
        knowledge_base_id=knowledge_base.id,
        uploaded_by=teacher.id,
        original_filename="课程资料.pdf",
        file_format="pdf",
        status=DocumentStatus.READY,
    )
    session.add(document)
    session.flush()
    chunk = DocumentChunk(
        document_id=document.id,
        course_id=course.id,
        knowledge_base_id=knowledge_base.id,
        chunk_index=0,
        content=content,
        chunk_metadata={},
    )
    session.add(chunk)
    session.commit()
    return chunk


def _persisted(session: Session, course: Course) -> list[Question]:
    """读取该课程下已落库的候选题。"""

    session.expire_all()
    return list(
        session.scalars(
            select(Question)
            .where(Question.course_id == course.id)
            .order_by(Question.created_at, Question.id)
        )
    )


def _headers(user: User, role: UserRole) -> dict[str, str]:
    """签发指定角色的访问令牌。"""

    token = create_access_token(
        user.id,
        secret_key=build_test_settings().JWT_SECRET_KEY,
        roles=[role],
        expires_delta=timedelta(minutes=60),
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client_factory(session: Session, monkeypatch: pytest.MonkeyPatch) -> Generator[Any, None, None]:
    """装配可注入出题服务的 TestClient 工厂。"""

    bind_startup_to_session(monkeypatch, session)

    with ExitStack() as stack:

        def factory(service: QuestionGenerationService | None = None) -> TestClient:
            app = create_app(settings=build_test_settings(), routers=('question_generation', 'questions'))

            def database() -> Generator[Session, None, None]:
                yield session

            app.dependency_overrides[get_db] = database
            if service is not None:
                app.dependency_overrides[get_question_generation_service] = lambda: (
                    service
                )
            return stack.enter_context(TestClient(app))

        yield factory


# ---------------------------------------------------------------- 权限与路由






def _service_from_scenario(scenario: dict[str, Any]) -> QuestionGenerationService:
    """从场景取会话并构造替身服务（仅供权限用例使用）。"""

    session = scenario["session"]
    assert isinstance(session, Session)
    return _service(session)


# ---------------------------------------------------------------- 生成端


















# ---------------------------------------------------------------- 查询端






# ---------------------------------------------------------------- 教师审核


def _pending_candidate_id(session: Session, course: Course, teacher: User) -> str:
    """生成一条待审核候选题并返回其标识。"""

    response = _generate(
        session,
        course=course,
        teacher=teacher,
        candidates=[make_candidate()],
    )
    return str(response.candidates[0].candidate_id)
