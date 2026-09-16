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
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.api.grading import get_grading_task_service
from backend.app.core.app import create_app
from backend.app.core.database import Base, get_db
from backend.app.domain.enums import (
    AnswerStatus,
    QuestionStatus,
    QuestionType,
    SubmissionStatus,
    UserRole,
)
from backend.app.models import (
    Answer,
    ExamResult,
    GradingResult,
    Question,
    Submission,
    User,
    WorkflowRun,
)
from backend.app.schemas.ai import GradingResult as GradingResultPayload
from backend.app.schemas.grading import GradingTaskStatus, QuestionResultDTO
from backend.app.services.auth_service import create_access_token
from backend.app.services.grading.confidence_policy import ConfidenceDecision
from backend.app.services.grading.grading_repository import DatabaseGradingRepository
from backend.app.services.grading.grading_task_service import (
    DatabaseGradingSubmissionReader,
    DefaultScoringPipeline,
    GradingTargetAnswer,
    GradingTaskService,
    InlineGradingTaskExecutor,
    NotConfiguredGradingRepository,
    SubmissionSnapshot,
)
from backend.app.services.grading.subjective_grader import ProviderNotReadyError
from backend.app.services.grading.subjective_pipeline import build_subjective_scorer
from tests.support.grading_doubles import (
    InMemoryGradingRepository,
    NonCallableSubmissionReader,
    RecordingExecutor,
    StubSubmissionReader,
    make_task,
)
from tests.support.subjective_grading_doubles import (
    StubEmbeddingProvider,
    StubReranker,
    StubRetriever,
    StubScoringProvider,
    make_chunk,
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
    """未配置结果存储时拒绝返回虚构任务，三个端点均返回 503。

    TCR（2026-09-16，T056）：生产装配已改用真实仓储，因此这里**显式注入**未接通存储来
    固定该语义；生产路径的就绪判断由 ``DatabaseGradingRepository.ensure_ready`` 的测试覆盖，
    不再依赖本机数据库是否已迁移。
    """

    repository = NotConfiguredGradingRepository()
    service = GradingTaskService(
        repository=repository,
        reader=NonCallableSubmissionReader(),
        executor=RecordingExecutor(),
    )
    client = client_factory(service)
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


# --------------------------------------------------------------------------- #
# B01：任务查询与单题结果查询必须校验教师课程归属
# --------------------------------------------------------------------------- #


def _real_service(
    session: Session,
    repository: InMemoryGradingRepository | None = None,
) -> tuple[GradingTaskService, InMemoryGradingRepository, RecordingExecutor]:
    """用真实快照读取器（内存 SQLite）构造任务服务。"""

    resolved = repository if repository is not None else InMemoryGradingRepository()
    recorder = RecordingExecutor()
    service = GradingTaskService(
        repository=resolved,
        reader=DatabaseGradingSubmissionReader(session=session),
        executor=recorder,
        clock=lambda: datetime(2026, 9, 16, 12, 0, tzinfo=UTC),
    )
    return service, resolved, recorder


def _other_teacher(session: Session) -> User:
    """创建不属于本课程的其他教师。"""

    return add_user(
        session,
        UserRole.TEACHER,
        username="other",
        email="other@example.com",
    )


def test_task_query_denies_teacher_from_other_course(
    session: Session, scenario, client_factory
) -> None:
    """TCR（2026-09-16，B01）：任务查询必须校验课程归属；旧实现只用
    VIEW_GRADING_RESULTS 守位，其他课程教师可直接读到任务详情。

    修复前预期：越权教师得到 200（越权可读）；修复后：403。
    """

    service, repository, _ = _real_service(session)
    submission_id = str(scenario["submission"].id)
    repository.save_task(make_task("task-1", submission_id))
    client = client_factory(service)

    allowed = client.get(
        "/api/grading/tasks/task-1",
        headers=headers(scenario["teacher"], UserRole.TEACHER),
    )
    denied = client.get(
        "/api/grading/tasks/task-1",
        headers=headers(_other_teacher(session), UserRole.TEACHER),
    )

    assert allowed.status_code == 200
    assert denied.status_code == 403


def test_trigger_without_configured_store_never_reads_business_data(
    scenario, client_factory
) -> None:
    """TCR（2026-09-16，B04）：默认未配置结果存储时，触发必须在读取业务答卷、
    创建任务与调用执行器之前返回 503 GRADING_STORE_NOT_READY。

    修复前预期：先读业务库（读取器报错并泄漏 500）；修复后：503 且零副作用。
    """

    reader = NonCallableSubmissionReader()
    recorder = RecordingExecutor()
    service = GradingTaskService(
        repository=NotConfiguredGradingRepository(),
        reader=reader,
        executor=recorder,
    )
    client = client_factory(service)
    submission_id = str(scenario["submission"].id)

    response = client.post(
        f"/api/grading/submissions/{submission_id}/trigger",
        headers=headers(scenario["teacher"], UserRole.TEACHER),
        json={"regrade": False},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["error_code"] == "GRADING_STORE_NOT_READY"
    assert reader.teacher_load_calls == 0
    assert reader.load_calls == 0
    assert recorder.executed == []


def test_single_result_query_denies_teacher_from_other_course(
    session: Session, scenario, client_factory
) -> None:
    """TCR（2026-09-16，B01）：单题结果查询同样必须校验课程归属。

    修复前预期：越权教师得到 200（越权可读）；修复后：403。
    """

    service, repository, _ = _real_service(session)
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
            grading_status="Accepted",
            review_status="Not Required",
            validation_status="Validated",
            reason="评分理由。",
        ),
    )
    client = client_factory(service)
    path = f"/api/grading/submissions/{submission.id}/answers/{answer.id}"

    allowed = client.get(path, headers=headers(scenario["teacher"], UserRole.TEACHER))
    denied = client.get(path, headers=headers(_other_teacher(session), UserRole.TEACHER))

    assert allowed.status_code == 200
    assert denied.status_code == 403


# --------------------------------------------------------------------------- #
# B01～B04：真实仓储装配下的落库、任务状态映射与失败收敛
# --------------------------------------------------------------------------- #


@pytest.fixture
def mixed_scenario(session: Session) -> dict[str, object]:
    """一份含一道客观题与一道主观题的已提交答卷。"""

    teacher = add_teacher(session)
    course = add_course(session, teacher)
    objective = Question(
        course_id=course.id,
        created_by=teacher.id,
        type=QuestionType.SINGLE_CHOICE,
        content="下列哪个是不可变类型？",
        options=["tuple", "list"],
        reference_answer="tuple",
        knowledge_points=["数据类型"],
        score=Decimal("10.00"),
        status=QuestionStatus.APPROVED,
    )
    subjective = Question(
        course_id=course.id,
        created_by=teacher.id,
        type=QuestionType.SHORT_ANSWER,
        content="解释变量的作用。",
        reference_answer="变量用于保存数据。",
        scoring_rubric="说明保存和引用数据即可。",
        knowledge_points=["变量"],
        score=Decimal("10.00"),
        status=QuestionStatus.APPROVED,
    )
    session.add_all([objective, subjective])
    session.commit()
    exam = add_published_exam(session, course, teacher, [objective.id, subjective.id])
    student = add_student(session, username="mixed")
    submission = Submission(
        exam_id=exam.id,
        student_id=student.id,
        status=SubmissionStatus.SUBMITTED,
        submitted_at=datetime.now(UTC),
    )
    session.add(submission)
    session.flush()
    objective_answer = Answer(
        submission_id=submission.id,
        question_id=objective.id,
        content="tuple",
        status=AnswerStatus.SUBMITTED,
    )
    subjective_answer = Answer(
        submission_id=submission.id,
        question_id=subjective.id,
        content="变量用于保存数据。",
        status=AnswerStatus.SUBMITTED,
    )
    session.add_all([objective_answer, subjective_answer])
    session.commit()
    return {
        "teacher": teacher,
        "student": student,
        "course": course,
        "exam": exam,
        "submission": submission,
        "objective_answer": objective_answer,
        "subjective_answer": subjective_answer,
    }


def _database_service(
    session: Session,
    *,
    subjective_scorer: object | None = None,
) -> GradingTaskService:
    """用真实仓储、真实快照读取器与真实评分管道构造任务服务。

    仅替换外部依赖（主观题评分器或其 Provider）；持久化与任务状态来自
    :class:`DatabaseGradingRepository`，使用与测试同一个 SQLite 库。
    """

    engine = session.get_bind()
    repository = DatabaseGradingRepository(session_factory=lambda: Session(engine))
    reader = DatabaseGradingSubmissionReader(session_factory=lambda: Session(engine))
    pipeline = DefaultScoringPipeline(subjective_scorer=subjective_scorer)  # type: ignore[arg-type]
    return GradingTaskService(
        repository=repository,
        reader=reader,
        executor=InlineGradingTaskExecutor(
            repository=repository,
            reader=reader,
            pipeline=pipeline,
        ),
    )


def _pending_review_scorer():
    """返回待复核主观题结果的替身评分器（不调用 LLM）。"""

    def score(
        snapshot: SubmissionSnapshot,
        target: GradingTargetAnswer,
    ) -> tuple[GradingResultPayload, ConfidenceDecision]:
        return (
            GradingResultPayload(
                question_type=target.question_type,
                score=6.0,
                max_score=float(target.max_score),
                reason="说明了数据保存作用。",
                correct_points=["保存数据"],
                missing_knowledge_points=["引用数据"],
                knowledge_points=list(target.knowledge_points),
                suggestions=["补充变量引用。"],
                confidence=0.5,
                validation_status="Validated",
                review_status="Pending Review",
                retrieved_context_ids=["chunk-1"],
                answer_id=target.answer_id,
                submission_id=snapshot.submission_id,
            ),
            ConfidenceDecision(
                confidence=0.5,
                threshold=0.8,
                requires_review=True,
                review_status="Pending Review",
                grading_status="Pending Review",
                reason="置信度低于阈值，进入待人工复核。",
            ),
        )

    return score


def _trigger(client: TestClient, scenario: dict[str, object], teacher: User) -> dict:
    """通过真实端点触发阅卷并返回任务状态响应体。"""

    submission = scenario["submission"]
    assert isinstance(submission, Submission)
    response = client.post(
        f"/api/grading/submissions/{submission.id}/trigger",
        headers=headers(teacher, UserRole.TEACHER),
        json={"regrade": False},
    )
    assert response.status_code == 202
    return response.json()


def test_trigger_persists_results_and_reads_them_from_database(
    session: Session, mixed_scenario, client_factory
) -> None:
    """TCR（2026-09-16，T056）：真实仓储装配下触发阅卷后结果确实落库，
    任务状态与单题结果均从数据库读取，且任务状态可持久化查询。
    """

    service = _database_service(
        session, subjective_scorer=_pending_review_scorer()
    )
    client = client_factory(service)
    body = _trigger(client, mixed_scenario, mixed_scenario["teacher"])
    submission = mixed_scenario["submission"]
    objective_answer = mixed_scenario["objective_answer"]
    subjective_answer = mixed_scenario["subjective_answer"]
    assert isinstance(submission, Submission)
    assert isinstance(objective_answer, Answer)
    assert isinstance(subjective_answer, Answer)

    task = client.get(
        f"/api/grading/tasks/{body['task_id']}",
        headers=headers(mixed_scenario["teacher"], UserRole.TEACHER),
    )
    objective = client.get(
        f"/api/grading/submissions/{submission.id}/answers/{objective_answer.id}",
        headers=headers(mixed_scenario["teacher"], UserRole.TEACHER),
    )
    subjective = client.get(
        f"/api/grading/submissions/{submission.id}/answers/{subjective_answer.id}",
        headers=headers(mixed_scenario["teacher"], UserRole.TEACHER),
    )

    assert body["durable"] is True
    assert task.status_code == 200
    assert task.json()["status"] == GradingTaskStatus.COMPLETED.value
    assert task.json()["durable"] is True
    assert task.json()["graded_answer_count"] == 2
    assert task.json()["pending_review_answer_count"] == 1
    assert task.json()["is_final"] is False
    assert Decimal(objective.json()["score"]) == Decimal("10.00")
    assert objective.json()["counted"] is True
    assert subjective.json()["requires_review"] is True
    assert subjective.json()["counted"] is False
    assert subjective.json()["decision"]["threshold"] == 0.8

    with Session(session.get_bind()) as check:
        rows = list(check.scalars(select(GradingResult)))
        assert len(rows) == 2
        exam_result = check.scalars(select(ExamResult)).one()
        assert exam_result.is_final is False
        assert exam_result.final_total_score is None
        assert exam_result.confirmed_subtotal == Decimal("10.00")
        workflow = check.scalars(select(WorkflowRun)).one()
        assert workflow.status.value == "Paused"
        assert workflow.pause_reason is not None
        assert workflow.request_id
        assert workflow.exam_result_id == exam_result.id


def test_subjective_failure_keeps_business_error_code_without_partial_results(
    session: Session, mixed_scenario, client_factory
) -> None:
    """真实主观题链路失败时：任务保留业务错误码、无部分结果、不伪造分数。"""

    engine = session.get_bind()
    scorer = build_subjective_scorer(
        session_factory=lambda: Session(engine),
        settings=build_test_settings(),
        provider=StubScoringProvider(
            error=ProviderNotReadyError("评分 Provider 未就绪。")
        ),
        retriever=StubRetriever([make_chunk()]),
        reranker=StubReranker(),
        embedding_provider=StubEmbeddingProvider(),
    )
    client = client_factory(_database_service(session, subjective_scorer=scorer))
    body = _trigger(client, mixed_scenario, mixed_scenario["teacher"])

    task = client.get(
        f"/api/grading/tasks/{body['task_id']}",
        headers=headers(mixed_scenario["teacher"], UserRole.TEACHER),
    )

    assert task.status_code == 200
    assert task.json()["status"] == GradingTaskStatus.FAILED.value
    assert task.json()["error_code"] == "GRADING_PROVIDER_NOT_READY"
    assert task.json()["retryable"] is False

    submission = mixed_scenario["submission"]
    assert isinstance(submission, Submission)
    with Session(session.get_bind()) as check:
        assert list(check.scalars(select(ExamResult))) == []
        assert list(check.scalars(select(GradingResult))) == []
        statuses = {
            answer.status.value
            for answer in check.scalars(
                select(Answer).where(Answer.submission_id == submission.id)
            )
        }
        assert statuses == {"Failed"}
        assert check.scalars(select(WorkflowRun)).one().status.value == "Failed"
