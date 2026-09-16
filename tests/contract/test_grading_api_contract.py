"""T056 阅卷 API 契约测试：端点字段、权限边界、状态码与未就绪语义。

TCR（2026-09-16，T056 / B04、B05、B06）：新增阅卷触发、任务状态与单题结果端点，需要
固化“只有真实受理任务才返回 202”“重复触发复用已有任务”“教师必须拥有该课程”
“仅 Admin 与未认证不得触发”“结果存储未接通返回 503 而非虚构任务”等合同。测试注入
测试替身装配的应用服务，证明接口合同与权限边界；生产存储与执行仍未接通（见 T060/T064）。

本文件只断言对外契约，不复制服务内部实现。
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import gradio as gr
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.api.grading import get_grading_task_service
from backend.app.core.app import create_app
from backend.app.core.database import Base, get_db
from backend.app.domain.enums import (
    AnswerStatus,
    QuestionType,
    SubmissionStatus,
    UserRole,
)
from backend.app.models import Answer, Question, Submission, User
from backend.app.schemas.grading import GradingTaskStatus, QuestionResultDTO
from backend.app.services.auth_service import create_access_token
from backend.app.services.grading.grading_task_service import (
    GradingTargetAnswer,
    GradingTaskService,
    SubmissionSnapshot,
)
from tests.support.grading_doubles import (
    InMemoryGradingRepository,
    RecordingExecutor,
    StubSubmissionReader,
    make_task,
)
from tests.unit.services.test_submission_service import (
    add_approved_question,
    add_course,
    add_published_exam,
    add_student,
    add_teacher,
    add_user,
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
def scenario(session: Session) -> dict[str, object]:
    """创建教师、课程、题目、考试，以及一份已提交答卷。"""

    teacher = add_teacher(session)
    course = add_course(session, teacher)
    question_id = add_approved_question(session, course, teacher, content="解释变量。")
    exam = add_published_exam(session, course, teacher, [question_id])
    student = add_student(session, username="a")
    submission = Submission(
        exam_id=exam.id,
        student_id=student.id,
        status=SubmissionStatus.SUBMITTED,
        submitted_at=datetime.now(UTC),
    )
    session.add(submission)
    session.flush()
    answer = Answer(
        submission_id=submission.id,
        question_id=question_id,
        content="变量用于保存数据。",
        status=AnswerStatus.SUBMITTED,
    )
    session.add(answer)
    session.commit()
    return {
        "teacher": teacher,
        "student": student,
        "admin": add_user(
            session, UserRole.ADMIN, username="admin", email="admin@example.com"
        ),
        "course": course,
        "exam": exam,
        "question": session.get(Question, question_id),
        "submission": submission,
        "answer": answer,
    }


@pytest.fixture
def client_factory(session: Session) -> Generator[object, None, None]:
    """按需装配应用服务替身的 TestClient 工厂。"""

    with ExitStack() as stack:

        def factory(service: GradingTaskService | None = None) -> TestClient:
            with gr.Blocks() as ui:
                gr.Markdown("阅卷契约测试")
            app = create_app(settings=build_test_settings(), gradio_app=ui)

            def database() -> Generator[Session, None, None]:
                yield session

            app.dependency_overrides[get_db] = database
            if service is not None:
                app.dependency_overrides[get_grading_task_service] = lambda: service
            client = stack.enter_context(TestClient(app))
            return client

        yield factory


def headers(user: User, role: UserRole) -> dict[str, str]:
    """签发指定角色的访问令牌。"""

    token = create_access_token(
        user.id,
        secret_key=build_test_settings().JWT_SECRET_KEY,
        roles=[role],
        expires_delta=timedelta(minutes=60),
    )
    return {"Authorization": f"Bearer {token}"}


def _snapshot(scenario: dict[str, object]) -> SubmissionSnapshot:
    """由数据库实体构造答卷快照。"""

    question = scenario["question"]
    answer = scenario["answer"]
    submission = scenario["submission"]
    assert isinstance(question, Question)
    assert isinstance(answer, Answer)
    assert isinstance(submission, Submission)
    return SubmissionSnapshot(
        submission_id=str(submission.id),
        exam_id=str(submission.exam_id),
        student_id=str(submission.student_id),
        course_id=str(question.course_id),
        status=submission.status.value,
        answers=(
            GradingTargetAnswer(
                order=1,
                answer_id=str(answer.id),
                question_id=str(answer.question_id),
                question_type=QuestionType.SHORT_ANSWER,
                max_score=Decimal("10.00"),
                knowledge_points=tuple(question.knowledge_points or ()),
                content=question.content,
                reference_answer=question.reference_answer,
                scoring_rubric=question.scoring_rubric,
                student_answer=answer.content,
            ),
        ),
    )


def _service(
    scenario: dict[str, object],
) -> tuple[GradingTaskService, InMemoryGradingRepository, RecordingExecutor]:
    """构造注入替身的任务服务。"""

    teacher = scenario["teacher"]
    submission = scenario["submission"]
    assert isinstance(teacher, User)
    assert isinstance(submission, Submission)
    repository = InMemoryGradingRepository()
    reader = StubSubmissionReader(
        {str(submission.id): _snapshot(scenario)},
        allowed_teacher_id=str(teacher.id),
    )
    recorder = RecordingExecutor()
    service = GradingTaskService(
        repository=repository,
        reader=reader,
        executor=recorder,
        clock=lambda: datetime(2026, 9, 16, 12, 0, tzinfo=UTC),
    )
    return service, repository, recorder


def test_trigger_creates_task_and_returns_queued(scenario, client_factory) -> None:
    """触发返回 202 与排队任务标识；未接通生产存储时 durable 为 False。"""

    service, repository, _ = _service(scenario)
    client = client_factory(service)
    submission_id = str(scenario["submission"].id)
    teacher = scenario["teacher"]

    response = client.post(
        f"/api/grading/submissions/{submission_id}/trigger",
        headers=headers(teacher, UserRole.TEACHER),
        json={"regrade": False},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["submission_id"] == submission_id
    assert body["status"] == GradingTaskStatus.QUEUED.value
    assert body["reused"] is False
    assert body["durable"] is False
    assert repository.get_task(body["task_id"]) is not None


def test_trigger_reuses_in_flight_task(scenario, client_factory) -> None:
    """同一答卷已有进行中任务时返回已有任务且不重复调度。"""

    service, repository, recorder = _service(scenario)
    submission_id = str(scenario["submission"].id)
    repository.save_task(
        make_task("task-running", submission_id, status=GradingTaskStatus.RUNNING)
    )
    client = client_factory(service)

    response = client.post(
        f"/api/grading/submissions/{submission_id}/trigger",
        headers=headers(scenario["teacher"], UserRole.TEACHER),
        json={"regrade": False},
    )

    assert response.status_code == 202
    assert response.json()["task_id"] == "task-running"
    assert response.json()["reused"] is True
    assert recorder.scheduled == []


def test_trigger_completed_task_requires_regrade(scenario, client_factory) -> None:
    """已完成任务在未请求重评时返回 409，重评请求可创建新任务。"""

    service, repository, _ = _service(scenario)
    submission_id = str(scenario["submission"].id)
    repository.save_task(
        make_task("task-done", submission_id, status=GradingTaskStatus.COMPLETED)
    )
    client = client_factory(service)
    teacher_headers = headers(scenario["teacher"], UserRole.TEACHER)

    conflict = client.post(
        f"/api/grading/submissions/{submission_id}/trigger",
        headers=teacher_headers,
        json={"regrade": False},
    )
    regraded = client.post(
        f"/api/grading/submissions/{submission_id}/trigger",
        headers=teacher_headers,
        json={"regrade": True},
    )

    assert conflict.status_code == 409
    assert regraded.status_code == 202
    assert regraded.json()["task_id"] != "task-done"


def test_trigger_rejects_draft_submission(scenario, client_factory) -> None:
    """草稿答卷触发阅卷返回 409。"""

    submission = scenario["submission"]
    submission.status = SubmissionStatus.DRAFT
    service, _, _ = _service(scenario)
    client = client_factory(service)

    response = client.post(
        f"/api/grading/submissions/{submission.id}/trigger",
        headers=headers(scenario["teacher"], UserRole.TEACHER),
        json={"regrade": False},
    )

    assert response.status_code == 409


def test_trigger_unknown_submission_returns_not_found(scenario, client_factory) -> None:
    """未知答卷返回 404。"""

    service, _, _ = _service(scenario)
    client = client_factory(service)

    response = client.post(
        "/api/grading/submissions/submission-unknown/trigger",
        headers=headers(scenario["teacher"], UserRole.TEACHER),
        json={"regrade": False},
    )

    assert response.status_code == 404


def test_trigger_enforces_role_boundaries(scenario, client_factory) -> None:
    """学生与仅 Admin 无法触发阅卷，未认证返回 401。"""

    service, _, _ = _service(scenario)
    client = client_factory(service)
    submission_id = str(scenario["submission"].id)
    path = f"/api/grading/submissions/{submission_id}/trigger"

    student = client.post(
        path, headers=headers(scenario["student"], UserRole.STUDENT), json={"regrade": False}
    )
    admin = client.post(
        path, headers=headers(scenario["admin"], UserRole.ADMIN), json={"regrade": False}
    )
    anonymous = client.post(path, json={"regrade": False})

    assert student.status_code == 403
    assert admin.status_code == 403
    assert anonymous.status_code == 401


def test_trigger_denies_cross_course_teacher(
    session: Session, scenario, client_factory
) -> None:
    """非课程所有者教师返回 403。"""

    service, _, _ = _service(scenario)
    client = client_factory(service)
    other_teacher = add_user(
        session, UserRole.TEACHER, username="other", email="other@example.com"
    )
    submission_id = str(scenario["submission"].id)

    response = client.post(
        f"/api/grading/submissions/{submission_id}/trigger",
        headers=headers(other_teacher, UserRole.TEACHER),
        json={"regrade": False},
    )

    assert response.status_code == 403


def test_trigger_rejects_invalid_payload(scenario, client_factory) -> None:
    """regrade 非布尔值返回 422。"""

    service, _, _ = _service(scenario)
    client = client_factory(service)
    submission_id = str(scenario["submission"].id)

    response = client.post(
        f"/api/grading/submissions/{submission_id}/trigger",
        headers=headers(scenario["teacher"], UserRole.TEACHER),
        json={"regrade": "maybe"},
    )

    assert response.status_code == 422


def test_task_status_endpoint_reports_state(scenario, client_factory) -> None:
    """任务状态端点返回任务事实，未知任务返回 404。"""

    service, repository, _ = _service(scenario)
    submission_id = str(scenario["submission"].id)
    repository.save_task(
        make_task("task-1", submission_id, status=GradingTaskStatus.COMPLETED)
    )
    client = client_factory(service)
    teacher_headers = headers(scenario["teacher"], UserRole.TEACHER)

    found = client.get("/api/grading/tasks/task-1", headers=teacher_headers)
    missing = client.get("/api/grading/tasks/task-unknown", headers=teacher_headers)

    assert found.status_code == 200
    body = found.json()
    assert body["task_id"] == "task-1"
    assert body["submission_id"] == submission_id
    assert body["status"] == GradingTaskStatus.COMPLETED.value
    assert body["durable"] is False
    assert missing.status_code == 404


def test_single_result_endpoint_returns_validated_result(
    scenario, client_factory
) -> None:
    """单题结果端点返回结构化结果与待复核标识，缺失时返回 404。"""

    service, repository, _ = _service(scenario)
    submission = scenario["submission"]
    answer = scenario["answer"]
    repository.save_single_result(
        str(submission.id),
        QuestionResultDTO(
            order=1,
            answer_id=str(answer.id),
            question_id=str(answer.question_id),
            question_type=QuestionType.SHORT_ANSWER,
            max_score=Decimal("10.00"),
            score=Decimal("8.00"),
            effective_score=Decimal("8.00"),
            counted=True,
            requires_review=False,
            grading_status="Accepted",
            review_status="Not Required",
            validation_status="Validated",
            reason="评分理由。",
        ),
    )
    client = client_factory(service)
    teacher_headers = headers(scenario["teacher"], UserRole.TEACHER)

    found = client.get(
        f"/api/grading/submissions/{submission.id}/answers/{answer.id}",
        headers=teacher_headers,
    )
    missing = client.get(
        f"/api/grading/submissions/{submission.id}/answers/answer-unknown",
        headers=teacher_headers,
    )

    assert found.status_code == 200
    body = found.json()
    assert body["answer_id"] == str(answer.id)
    assert body["requires_review"] is False
    assert body["validation_status"] == "Validated"
    assert missing.status_code == 404


def test_default_dependency_reports_store_not_ready(scenario, client_factory) -> None:
    """未配置结果存储时拒绝返回虚构任务，三个端点均返回 503。"""

    client = client_factory()
    submission = scenario["submission"]
    answer = scenario["answer"]
    teacher_headers = headers(scenario["teacher"], UserRole.TEACHER)

    triggered = client.post(
        f"/api/grading/submissions/{submission.id}/trigger",
        headers=teacher_headers,
        json={"regrade": False},
    )
    status = client.get("/api/grading/tasks/task-1", headers=teacher_headers)
    result = client.get(
        f"/api/grading/submissions/{submission.id}/answers/{answer.id}",
        headers=teacher_headers,
    )

    assert triggered.status_code == 503
    assert status.status_code == 503
    assert result.status_code == 503
    assert triggered.json()["detail"]["error_code"] == "GRADING_STORE_NOT_READY"
