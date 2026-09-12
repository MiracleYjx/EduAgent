"""考试分配范围与学生提交边界契约测试。"""

from collections.abc import Generator
from datetime import timedelta
from uuid import UUID

import gradio as gr
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.core.app import create_app
from backend.app.core.database import Base, get_db
from backend.app.domain.enums import SubmissionStatus, UserRole
from backend.app.models import Exam, ExamParticipant, Submission, User
from backend.app.services.auth_service import create_access_token
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
def client(session: Session) -> Generator[TestClient, None, None]:
    """保留真实认证、API 与服务，只替换数据库连接。"""

    with gr.Blocks() as ui:
        gr.Markdown("考试范围契约测试")
    app = create_app(settings=build_test_settings(), gradio_app=ui)

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


def test_assigned_exam_is_hidden_and_cannot_be_started_by_other_student(
    client: TestClient, session: Session, scenario
) -> None:
    """学生 A 无法列出、查看、开始或直接提交只分配给 B 的考试。"""

    exam, student_a, student_b, question_id = scenario
    session.add(ExamParticipant(exam_id=exam.id, student_id=student_b.id))
    session.commit()
    for path in ("/api/submissions/exams", "/api/submissions/available-exams"):
        response = client.get(path, headers=headers(student_a))
        assert response.status_code == 200
        assert response.json() == []
        allowed = client.get(path, headers=headers(student_b))
        assert allowed.status_code == 200
        assert [item["id"] for item in allowed.json()] == [str(exam.id)]
    assert client.get(f"/api/submissions/exams/{exam.id}", headers=headers(student_a)).status_code == 403
    for path in (f"/api/submissions/exams/{exam.id}/start", f"/api/exams/{exam.id}/submissions"):
        assert client.post(path, headers=headers(student_a)).status_code == 403
    assert client.post(
        f"/api/exams/{exam.id}/submit", headers=headers(student_a),
        json={"answers": {str(question_id): "变量用于保存数据。"}},
    ).status_code == 403
    assert client.post(f"/api/exams/{exam.id}/submissions", headers=headers(student_b)).status_code == 201


def test_unassigned_exam_remains_open_and_can_be_submitted(
    client: TestClient, scenario
) -> None:
    """没有分配记录的历史考试对两名学生开放，均可完成作答提交。"""

    exam, student_a, student_b, question_id = scenario
    for student in (student_a, student_b):
        response = client.get("/api/submissions/exams", headers=headers(student))
        assert response.status_code == 200
        assert [item["id"] for item in response.json()] == [str(exam.id)]
        submission = client.post(f"/api/exams/{exam.id}/submissions", headers=headers(student))
        assert submission.status_code == 201
        submitted = client.post(
            f"/api/submissions/{submission.json()['id']}/submit",
            headers=headers(student),
            json={"answers": {str(question_id): "变量用于保存数据。"}},
        )
        assert submitted.status_code == 200
        assert submitted.json()["status"] == "Submitted"


def test_existing_draft_cannot_submit_after_assignment_changes(
    client: TestClient, session: Session, scenario
) -> None:
    """新增限制后，已有草稿不能绕过资格检查，失败不会改变草稿状态。"""

    exam, student_a, student_b, question_id = scenario
    started = client.post(f"/api/exams/{exam.id}/submissions", headers=headers(student_a))
    assert started.status_code == 201
    submission_id = started.json()["id"]
    session.add(ExamParticipant(exam_id=exam.id, student_id=student_b.id))
    session.commit()
    assert client.post(f"/api/exams/{exam.id}/submissions", headers=headers(student_a)).status_code == 403
    denied = client.post(
        f"/api/submissions/{submission_id}/submit", headers=headers(student_a),
        json={"answers": {str(question_id): "变量用于保存数据。"}},
    )
    assert denied.status_code == 403
    submission = session.get(Submission, UUID(submission_id))
    assert submission is not None
    assert submission.status is SubmissionStatus.DRAFT
    assert submission.submitted_at is None


def test_assignment_persists_timestamp_and_rejects_duplicates(session: Session, scenario) -> None:
    """分配时间可持久化，同一考试和学生只能分配一次。"""

    exam, student_a, _, _ = scenario
    assignment = ExamParticipant(exam_id=exam.id, student_id=student_a.id)
    session.add(assignment)
    session.commit()
    assert assignment.assigned_at is not None
    session.add(ExamParticipant(exam_id=exam.id, student_id=student_a.id))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
