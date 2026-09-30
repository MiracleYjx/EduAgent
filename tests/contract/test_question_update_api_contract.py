"""I01 / T133 题目编辑 API 契约测试。

TCR（2026-09-30）：现有契约测试没有验证通过 PATCH 绕过 Approved 的 UI 编辑限制。
新增真实 JWT、路由、服务及隔离数据库验证：六个受限字段返回 409 和结构化中文错误、
请求无副作用；难度/知识点编辑；退回修订后编辑且必须重新提交审核。
沿用 pytest、TestClient 和现有 SQLite 会话，不 mock 题目服务，不修改全局测试配置。
仅验证请求合同，采用认证契约测试相同的请求级客户端；启动恢复由既有独立测试负责。
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from uuid import UUID

import gradio as gr
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.core.app import create_app
from backend.app.core.database import Base, get_db
from backend.app.domain.enums import QuestionStatus, QuestionType, UserRole
from backend.app.services.auth_service import create_access_token
from backend.app.services.question_service import QuestionService
from tests.unit.services.test_question_service import add_course, add_teacher
from tests.unit.settings_helpers import build_test_settings


@pytest.fixture
def session() -> Generator[Session, None, None]:
    """沿用现有契约测试的隔离 SQLite 会话。"""

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
def client(session: Session) -> Generator[TestClient, None, None]:
    """挂载最小 UI，使用真实题目路由和隔离数据库。"""

    with gr.Blocks() as ui:
        gr.Markdown("I01 契约测试")
    app = create_app(settings=build_test_settings(), gradio_app=ui)

    def database() -> Generator[Session, None, None]:
        yield session

    app.dependency_overrides[get_db] = database
    test_client = TestClient(app)
    try:
        yield test_client
    finally:
        test_client.close()


@pytest.fixture
def approved_question(session: Session) -> tuple[str, dict[str, str]]:
    """建立已审核题目，并签发课程负责教师的 JWT。"""

    teacher = add_teacher(session)
    course = add_course(session, teacher)
    service = QuestionService(session)
    question = service.create_question(
        course_id=course.id,
        question_type=QuestionType.SINGLE_CHOICE,
        content="下列哪项是 Python 的内置类型？",
        options=["列表", "课程"],
        reference_answer="列表",
        scoring_rubric="选择列表得 5 分。",
        difficulty="简单",
        knowledge_points=["内置类型"],
        score=5,
        created_by=teacher.id,
    )
    service.update_question_status(
        question.id, QuestionStatus.PENDING_REVIEW, teacher_id=teacher.id
    )
    service.update_question_status(
        question.id, QuestionStatus.APPROVED, teacher_id=teacher.id
    )
    token = create_access_token(
        teacher.id,
        secret_key=build_test_settings().JWT_SECRET_KEY,
        roles=[UserRole.TEACHER],
    )
    return question.id, {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize(
    "changes",
    [
        {"content": "替换题干"},
        {"options": None},
        {"reference_answer": None},
        {"scoring_rubric": None},
        {"type": QuestionType.TRUE_FALSE.value},
        {"score": 10},
    ],
    ids=["content", "options", "reference_answer", "scoring_rubric", "type", "score"],
)
def test_approved_content_patch_returns_structured_conflict(
    client: TestClient,
    session: Session,
    approved_question: tuple[str, dict[str, str]],
    changes: dict[str, Any],
) -> None:
    """通过 API 修改已审核题目被拒绝，返回错误码、中文说明和当前状态。"""

    question_id, headers = approved_question
    teacher_id = UUID(QuestionService(session).get_question(question_id).created_by)
    before = QuestionService(session).get_question(question_id, teacher_id=teacher_id)
    response = client.patch(
        f"/api/questions/{question_id}",
        headers=headers,
        json=changes | {"difficulty": "困难"},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "QUESTION_APPROVED_IMMUTABLE"
    assert detail["current_status"] == "Approved"
    assert "退回修订" in detail["message"]
    assert not session.dirty
    with Session(session.get_bind()) as observer:
        assert QuestionService(observer).get_question(question_id) == before


def test_approved_metadata_patch_is_allowed(
    client: TestClient,
    approved_question: tuple[str, dict[str, str]],
) -> None:
    """API 只修改难度和知识点时，题目保持 Approved。"""

    question_id, headers = approved_question
    response = client.patch(
        f"/api/questions/{question_id}",
        headers=headers,
        json={"difficulty": "困难", "knowledge_points": ["容器", "内置类型"]},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "Approved"
    assert response.json()["difficulty"] == "困难"
    assert response.json()["knowledge_points"] == ["容器", "内置类型"]


def test_revision_edit_and_reapproval_use_existing_endpoints(
    client: TestClient,
    approved_question: tuple[str, dict[str, str]],
) -> None:
    """先退回、编辑、提交审核、批准；修订状态不能直接批准。"""

    question_id, headers = approved_question
    base = f"/api/questions/{question_id}"
    returned = client.post(f"{base}/needs-revision", headers=headers)
    assert returned.status_code == 200
    assert returned.json()["status"] == "Needs Revision"
    edited = client.patch(base, headers=headers, json={"content": "修订题干"})
    assert edited.status_code == 200
    assert edited.json()["content"] == "修订题干"
    assert edited.json()["status"] == "Needs Revision"
    assert client.post(f"{base}/approve", headers=headers).status_code == 422
    submitted = client.post(f"{base}/submit-review", headers=headers)
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "Pending Review"
    approved = client.post(f"{base}/approve", headers=headers)
    assert approved.status_code == 200
    assert approved.json()["status"] == "Approved"
    assert (
        client.patch(base, headers=headers, json={"content": "再次改题"}).status_code
        == 409
    )
