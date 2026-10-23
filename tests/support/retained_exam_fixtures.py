"""考试分配范围与学生提交边界契约测试。"""

from collections.abc import Generator
from datetime import timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.core.database import Base, get_db
from backend.app.domain.enums import UserRole
from backend.app.models import Exam, User
from backend.app.services.auth_service import create_access_token
from tests.support.application_lifecycle import bind_startup_to_session
from tests.support.retained_component_app import component_app as create_app
from tests.unit.services.test_submission_service import (
    add_approved_question,
    add_course,
    add_published_exam,
    add_student,
    add_teacher,
)
from tests.unit.settings_helpers import build_test_settings


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
def client(session: Session, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    """保留真实认证、API 与服务，只替换数据库连接。"""

    bind_startup_to_session(monkeypatch, session)
    app = create_app(settings=build_test_settings(), routers=('exams', 'submissions'))

    def database() -> Generator[Session, None, None]:
        yield session

    app.dependency_overrides[get_db] = database
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def scenario(session: Session) -> tuple[Exam, User, User, UUID]:
    """创建一场已发布考试及两名有效学生。"""

    teacher = add_teacher(session)
    course = add_course(session, teacher)
    question_id = add_approved_question(session, course, teacher, content="解释变量。")
    exam = add_published_exam(session, course, teacher, [question_id])
    return exam, add_student(session, username="a"), add_student(session, username="b"), question_id


def headers(user: User) -> dict[str, str]:
    """使用测试配置签发学生凭证。"""

    token = create_access_token(
        user.id,
        secret_key=build_test_settings().JWT_SECRET_KEY,
        roles=[UserRole.STUDENT],
        expires_delta=timedelta(minutes=60),
    )
    return {"Authorization": f"Bearer {token}"}
