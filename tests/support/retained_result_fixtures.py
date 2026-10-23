"""T057 结果与诊断读模型 API 契约测试：授权边界、空态与不伪装零值。

TCR（2026-09-16，T057 / B05、B06）：新增学生成绩/错题/诊断/知识点与学生本人反馈、
教师考试结果与摘要端点，需要固化“学生只能读自己的”“教师只能读本人课程”“无结果时不伪装
成 0 分或 0 人”“待复核不计入正式错题”等合同。测试注入内存结果存储替身，证明接口合同与
授权边界；真实成绩来源仍依赖 T060。

本文件只断言对外契约。

TCR（2026-09-17，M03）：摘要此前把草稿计入已提交数。新增草稿与已提交生命周期
混合场景，验证 Submitted/Graded/Reviewed 均计入统计，Draft 不影响提交数、平均分和
未就绪标志；同时锁定结果列表仍保留草稿原有空态，不改变列表展示合同。

TCR（2026-09-17，M05）：教师详情此前漏掉结果仓储就绪检查，诊断仓储错误也会落为 500。
新增真实仓储缺表场景，分别验证未迁移结果存储与诊断存储返回 503，并保留来源错误码；
失败响应不得用空成绩或 Not Ready 空态伪装成功。
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.api.results import ResultsQueryService, get_results_query_service
from backend.app.core.database import Base, get_db
from backend.app.domain.enums import (
    AnswerStatus,
    QuestionType,
    SubmissionStatus,
    UserRole,
)
from backend.app.models import (
    Answer,
    Submission,
    User,
)
from backend.app.schemas.grading import (
    ConfidenceDecisionDTO,
    DiagnosisReportDTO,
    DiagnosisStatus,
    ExamResultDTO,
    ExamResultStatus,
    MasteryByKnowledgePointDTO,
    QuestionResultDTO,
)
from backend.app.services.auth_service import create_access_token
from backend.app.services.grading.diagnosis_report_store import (
    DiagnosisReportStore,
)
from backend.app.services.grading.grading_repository import DatabaseGradingRepository
from tests.support.application_lifecycle import bind_startup_to_session
from tests.support.grading_doubles import InMemoryGradingRepository
from tests.support.retained_component_app import component_app as create_app
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
    """创建教师、课程、题目、考试与两份已提交答卷。"""

    teacher = add_teacher(session)
    course = add_course(session, teacher)
    question_id = add_approved_question(session, course, teacher, content="解释变量。")
    exam = add_published_exam(session, course, teacher, [question_id])
    student_a = add_student(session, username="a")
    student_b = add_student(session, username="b")
    submissions: dict[str, Submission] = {}
    for name, student in (("a", student_a), ("b", student_b)):
        submission = Submission(
            exam_id=exam.id,
            student_id=student.id,
            status=SubmissionStatus.SUBMITTED,
            submitted_at=datetime.now(UTC),
        )
        session.add(submission)
        session.flush()
        session.add(
            Answer(
                submission_id=submission.id,
                question_id=question_id,
                content="变量用于保存数据。",
                status=AnswerStatus.SUBMITTED,
            )
        )
        submissions[name] = submission
    session.commit()
    return {
        "teacher": teacher,
        "student_a": student_a,
        "student_b": student_b,
        "admin": add_user(
            session, UserRole.ADMIN, username="admin", email="admin@example.com"
        ),
        "exam": exam,
        "submissions": submissions,
    }


@pytest.fixture
def client_factory(session: Session, monkeypatch: pytest.MonkeyPatch) -> Generator[object, None, None]:
    """按需装配查询服务替身的 TestClient 工厂。"""

    bind_startup_to_session(monkeypatch, session)

    with ExitStack() as stack:

        def factory(service: ResultsQueryService | None = None) -> TestClient:
            app = create_app(settings=build_test_settings(), routers=('results',))

            def database() -> Generator[Session, None, None]:
                yield session

            app.dependency_overrides[get_db] = database
            if service is not None:
                app.dependency_overrides[get_results_query_service] = lambda: service
            return stack.enter_context(TestClient(app))

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


def _service(
    session: Session,
    repository: InMemoryGradingRepository | None = None,
) -> ResultsQueryService:
    """构造注入内存结果存储的查询服务。"""

    return ResultsQueryService(
        repository=repository or InMemoryGradingRepository(),
        session=session,
    )


def _store_final_result(repository: InMemoryGradingRepository, submission: Submission) -> None:
    """写入一份最终整卷结果。"""

    repository.save_exam_result(
        ExamResultDTO(
            submission_id=str(submission.id),
            exam_id=str(submission.exam_id),
            student_id=str(submission.student_id),
            result_status=ExamResultStatus.FINAL,
            is_final=True,
            final_total_score=Decimal("8.00"),
            confirmed_subtotal=Decimal("8.00"),
            confirmed_subtotal_label="已确认部分小计。",
            total_max_score=Decimal("10.00"),
            expected_answer_count=1,
            graded_answer_count=1,
            counted_answer_count=1,
            pending_review_answer_count=0,
            items=[
                QuestionResultDTO(
                    order=1,
                    answer_id="answer-1",
                    question_id="question-1",
                    question_type=QuestionType.SHORT_ANSWER,
                    max_score=Decimal("10.00"),
                    score=Decimal("8.00"),
                    effective_score=Decimal("8.00"),
                    counted=True,
                    grading_status="Accepted",
                    review_status="Not Required",
                    validation_status="Validated",
                    reason="评分理由。",
                )
            ],
            aggregated_at=datetime(2026, 9, 16, 12, 0, tzinfo=UTC),
        )
    )


















# --------------------------------------------------------------------------- #
# T057：真实仓储读取成绩 + 只读诊断（含过期判定）
# --------------------------------------------------------------------------- #


def _t057_service(
    session: Session,
) -> tuple[ResultsQueryService, DatabaseGradingRepository, DiagnosisReportStore]:
    """用真实仓储与真实诊断存储构造读模型服务。"""

    engine = session.get_bind()
    repository = DatabaseGradingRepository(session_factory=lambda: Session(engine))
    store = DiagnosisReportStore(session_factory=lambda: Session(engine))
    return (
        ResultsQueryService(
            repository=repository,
            session_factory=lambda: Session(engine),
            diagnosis_store=store,
        ),
        repository,
        store,
    )


def _t057_final_result(
    session: Session, scenario
) -> tuple[ExamResultDTO, str]:
    """构造一份真实答卷对应的最终整卷结果 DTO。"""

    submission = scenario["submissions"]["a"]
    answer = session.scalars(
        select(Answer).where(Answer.submission_id == submission.id)
    ).one()
    return (
        ExamResultDTO(
            submission_id=str(submission.id),
            exam_id=str(submission.exam_id),
            student_id=str(submission.student_id),
            result_status=ExamResultStatus.FINAL,
            is_final=True,
            final_total_score=Decimal("8.00"),
            confirmed_subtotal=Decimal("8.00"),
            confirmed_subtotal_label="已确认部分小计。",
            total_max_score=Decimal("10.00"),
            expected_answer_count=1,
            graded_answer_count=1,
            counted_answer_count=1,
            pending_review_answer_count=0,
            items=[
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
                    confidence=0.9,
                    submission_id=str(submission.id),
                    decision=ConfidenceDecisionDTO(
                        confidence=0.9,
                        threshold=0.8,
                        requires_review=False,
                        review_status="Not Required",
                        grading_status="Accepted",
                        reason="置信度不低于阈值，自动接受。",
                    ),
                )
            ],
            aggregated_at=datetime(2026, 9, 16, 12, 0, tzinfo=UTC),
        ),
        str(answer.id),
    )


def _t057_ready_report(exam_result: ExamResultDTO) -> DiagnosisReportDTO:
    """构造 Ready 诊断报告。"""

    return DiagnosisReportDTO(
        exam_result_id=f"exam-result:{exam_result.submission_id}",
        submission_id=exam_result.submission_id,
        student_id=exam_result.student_id,
        status=DiagnosisStatus.READY,
        mastery_by_knowledge_point=[
            MasteryByKnowledgePointDTO(
                knowledge_point="变量",
                answered_count=1,
                correct_count=0,
                awarded_score=Decimal("8.00"),
                max_score=Decimal("10.00"),
                mastery=Decimal("0.80"),
            )
        ],
        weak_knowledge_points=[],
        error_reasons=["question-1：未得满分。"],
        learning_suggestions=["复习变量作用域。"],
        generated_at=datetime(2026, 9, 16, 12, 0, tzinfo=UTC),
        source_exam_result_updated_at=exam_result.aggregated_at,
    )










@pytest.fixture
def t184_pg_session():
    """TCR §39: real isolated PostgreSQL; no SQLite acceptance substitution."""
    from sqlalchemy.orm import Session

    from tests.postgres_helpers import isolated_postgres_engine

    with isolated_postgres_engine() as engine, Session(engine) as session:
        yield session


@pytest.fixture
def t184_client_factory(t184_pg_session, monkeypatch):
    yield from client_factory.__wrapped__(t184_pg_session, monkeypatch)
