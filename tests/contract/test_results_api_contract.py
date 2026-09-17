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

import gradio as gr
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.api.results import ResultsQueryService, get_results_query_service
from backend.app.core.app import create_app
from backend.app.core.database import Base, get_db
from backend.app.domain.enums import (
    AnswerStatus,
    QuestionType,
    SubmissionStatus,
    UserRole,
)
from backend.app.models import (
    Answer,
    DiagnosisReport,
    ExamResult,
    Submission,
    User,
    WorkflowRun,
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
    DIAGNOSIS_STORE_NOT_READY,
    DiagnosisReportStore,
)
from backend.app.services.grading.grading_repository import DatabaseGradingRepository
from backend.app.services.grading.grading_task_service import (
    NotConfiguredGradingRepository,
)
from tests.support.grading_doubles import InMemoryGradingRepository
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
def client_factory(session: Session) -> Generator[object, None, None]:
    """按需装配查询服务替身的 TestClient 工厂。"""

    with ExitStack() as stack:

        def factory(service: ResultsQueryService | None = None) -> TestClient:
            with gr.Blocks() as ui:
                gr.Markdown("结果契约测试")
            app = create_app(settings=build_test_settings(), gradio_app=ui)

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


def test_student_without_stored_result_sees_explicit_empty_state(
    session: Session, scenario, client_factory
) -> None:
    """尚无成绩时返回明确空态，不用 0 分代替。"""

    student = scenario["student_a"]
    service = _service(session)
    client = client_factory(service)
    student_headers = headers(student, UserRole.STUDENT)
    submission = scenario["submissions"]["a"]

    listed = client.get("/api/results/me/submissions", headers=student_headers)
    detail = client.get(
        f"/api/results/me/submissions/{submission.id}", headers=student_headers
    )
    diagnosis = client.get(
        f"/api/results/me/submissions/{submission.id}/diagnosis",
        headers=student_headers,
    )

    assert listed.status_code == 200
    entry = listed.json()[0]
    assert entry["total_score"] is None
    assert entry["result_status"] is None
    assert entry["not_ready_reason"]
    assert detail.status_code == 200
    assert detail.json()["total_score"] is None
    assert detail.json()["is_final"] is None
    assert detail.json()["items"] == []
    assert diagnosis.status_code == 200
    assert diagnosis.json()["status"] == "Not Ready"
    assert diagnosis.json()["learning_suggestions"] == []


def test_student_result_detail_reports_final_total_and_confirmed_items(
    session: Session, scenario, client_factory
) -> None:
    """已有最终成绩时返回总分与已确认逐题结果。"""

    repository = InMemoryGradingRepository()
    submission = scenario["submissions"]["a"]
    _store_final_result(repository, submission)
    client = client_factory(_service(session, repository))
    student_headers = headers(scenario["student_a"], UserRole.STUDENT)

    detail = client.get(
        f"/api/results/me/submissions/{submission.id}", headers=student_headers
    )

    assert detail.status_code == 200
    body = detail.json()
    assert body["is_final"] is True
    assert body["total_score"] == "8.00"
    assert body["result_status"] == ExamResultStatus.FINAL.value
    assert len(body["items"]) == 1
    assert body["mistake_answer_ids"] == ["answer-1"]


def test_student_cannot_read_another_student_submission(
    session: Session, scenario, client_factory
) -> None:
    """学生读取他人答卷返回 403。"""

    client = client_factory(_service(session))
    other = scenario["submissions"]["b"]
    student_headers = headers(scenario["student_a"], UserRole.STUDENT)

    detail = client.get(
        f"/api/results/me/submissions/{other.id}", headers=student_headers
    )
    diagnosis = client.get(
        f"/api/results/me/submissions/{other.id}/diagnosis", headers=student_headers
    )

    assert detail.status_code == 403
    assert diagnosis.status_code == 403


def test_teacher_results_do_not_fake_zero_average(
    session: Session, scenario, client_factory
) -> None:
    """教师摘要只统计最终成绩；无最终成绩时平均分为空并标注未就绪。"""

    service = _service(session)
    client = client_factory(service)
    teacher_headers = headers(scenario["teacher"], UserRole.TEACHER)
    exam = scenario["exam"]

    listed = client.get(
        f"/api/results/exams/{exam.id}/submissions", headers=teacher_headers
    )
    summary = client.get(f"/api/results/exams/{exam.id}/summary", headers=teacher_headers)

    assert listed.status_code == 200
    assert len(listed.json()) == 2
    assert {item["student_id"] for item in listed.json()} == {
        str(scenario["student_a"].id),
        str(scenario["student_b"].id),
    }
    assert summary.status_code == 200
    body = summary.json()
    assert body["submitted_count"] == 2
    assert body["final_count"] == 0
    assert body["average_of_final_scores"] is None
    assert body["not_ready"] is True
    assert body["not_ready_reason"]


@pytest.mark.parametrize(
    "submitted_status",
    [SubmissionStatus.SUBMITTED, SubmissionStatus.GRADED, SubmissionStatus.REVIEWED],
)
def test_teacher_summary_excludes_drafts_from_submitted_statistics(
    session: Session, scenario, client_factory, submitted_status: SubmissionStatus
) -> None:
    """草稿不计为已提交，也不把已有最终成绩的摘要变成未就绪。"""

    submitted = scenario["submissions"]["a"]
    draft = scenario["submissions"]["b"]
    submitted.status = submitted_status
    draft.status = SubmissionStatus.DRAFT
    draft.submitted_at = None
    session.commit()
    repository = InMemoryGradingRepository()
    _store_final_result(repository, submitted)
    client = client_factory(_service(session, repository))
    teacher_headers = headers(scenario["teacher"], UserRole.TEACHER)
    exam_id = scenario["exam"].id

    summary = client.get(
        f"/api/results/exams/{exam_id}/summary", headers=teacher_headers
    )

    assert summary.status_code == 200
    body = summary.json()
    assert body["submitted_count"] == 1
    assert body["final_count"] == 1
    assert body["pending_review_count"] == 0
    assert body["average_of_final_scores"] == "8.00"
    assert body["not_ready"] is False
    assert body["not_ready_reason"] is None
    listed = client.get(
        f"/api/results/exams/{exam_id}/submissions", headers=teacher_headers
    )
    assert listed.status_code == 200
    assert len(listed.json()) == 2
    draft_entry = next(
        item for item in listed.json() if item["submission_id"] == str(draft.id)
    )
    assert draft_entry["submitted_at"] is None
    assert draft_entry["total_score"] is None
    assert draft_entry["not_ready_reason"]


def test_teacher_summary_averages_only_final_scores(
    session: Session, scenario, client_factory
) -> None:
    """平均分只统计最终成绩，待复核答卷不参与。"""

    repository = InMemoryGradingRepository()
    _store_final_result(repository, scenario["submissions"]["a"])
    client = client_factory(_service(session, repository))
    teacher_headers = headers(scenario["teacher"], UserRole.TEACHER)

    summary = client.get(
        f"/api/results/exams/{scenario['exam'].id}/summary", headers=teacher_headers
    )

    assert summary.status_code == 200
    body = summary.json()
    assert body["final_count"] == 1
    assert body["average_of_final_scores"] == "8.00"
    assert body["not_ready"] is True


def test_result_endpoints_enforce_role_and_course_boundaries(
    session: Session, scenario, client_factory
) -> None:
    """学生与仅 Admin 无教师读权限，跨课程教师返回 403，未认证返回 401。"""

    client = client_factory(_service(session))
    exam = scenario["exam"]
    submission = scenario["submissions"]["a"]
    teacher_paths = (
        f"/api/results/exams/{exam.id}/submissions",
        f"/api/results/exams/{exam.id}/summary",
        f"/api/results/exams/{exam.id}/submissions/{submission.id}",
    )
    student = scenario["student_a"]
    admin = scenario["admin"]

    for path in teacher_paths:
        assert client.get(path, headers=headers(student, UserRole.STUDENT)).status_code == 403
        assert client.get(path, headers=headers(admin, UserRole.ADMIN)).status_code == 403
        assert client.get(path).status_code == 401

    other_teacher = add_user(
        session, UserRole.TEACHER, username="other", email="other@example.com"
    )
    assert (
        client.get(
            teacher_paths[0], headers=headers(other_teacher, UserRole.TEACHER)
        ).status_code
        == 403
    )


def test_results_endpoints_report_store_not_ready(
    session: Session, client_factory, scenario
) -> None:
    """未接通结果存储时返回 503，不返回空成功结果。

    TCR（2026-09-16，T057）：生产装配已改用真实仓储，因此这里**显式注入**未接通的存储
    固定该语义；生产路径的就绪判断由 ``DatabaseGradingRepository.ensure_ready`` 的测试覆盖，
    不再依赖本机数据库是否已迁移。
    """

    service = ResultsQueryService(
        repository=NotConfiguredGradingRepository(),
        session=session,
    )
    client = client_factory(service)
    student_headers = headers(scenario["student_a"], UserRole.STUDENT)
    teacher_headers = headers(scenario["teacher"], UserRole.TEACHER)

    student = client.get("/api/results/me/submissions", headers=student_headers)
    teacher = client.get(
        f"/api/results/exams/{scenario['exam'].id}/summary", headers=teacher_headers
    )

    assert student.status_code == 503
    assert teacher.status_code == 503
    assert student.json()["detail"]["error_code"] == "GRADING_STORE_NOT_READY"


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


def test_teacher_detail_reports_unmigrated_result_store_as_503(
    session: Session, scenario, client_factory
) -> None:
    """教师详情同样执行真实仓储就绪检查，缺迁移表时不返回空成绩。"""

    service, _, _ = _t057_service(session)
    session.commit()
    WorkflowRun.__table__.drop(session.get_bind())
    client = client_factory(service)
    teacher_headers = headers(scenario["teacher"], UserRole.TEACHER)
    exam = scenario["exam"]
    submission = scenario["submissions"]["a"]

    response = client.get(
        f"/api/results/exams/{exam.id}/submissions/{submission.id}",
        headers=teacher_headers,
    )

    assert response.status_code == 503
    assert response.json()["detail"]["error_code"] == "GRADING_STORE_NOT_READY"
    assert "total_score" not in response.json()


def test_diagnosis_store_unavailable_maps_to_503_with_source_code(
    session: Session, scenario, client_factory
) -> None:
    """诊断表不可用时保留诊断来源码并返回 503，不返回 Not Ready 空态。"""

    service, repository, _ = _t057_service(session)
    exam_result, _ = _t057_final_result(session, scenario)
    repository.save_exam_result(exam_result)
    session.commit()
    DiagnosisReport.__table__.drop(session.get_bind())
    client = client_factory(service)
    student_headers = headers(scenario["student_a"], UserRole.STUDENT)

    response = client.get(
        f"/api/results/me/submissions/{exam_result.submission_id}/diagnosis",
        headers=student_headers,
    )

    assert response.status_code == 503
    assert response.json()["detail"]["error_code"] == DIAGNOSIS_STORE_NOT_READY
    assert "status" not in response.json()


def test_student_result_reads_from_database_and_diagnosis_is_read_only(
    session: Session, scenario, client_factory
) -> None:
    """TCR（2026-09-16，T057）：成绩读自 ExamResult/GradingResult；诊断 GET 只读，
    连续调用不新增诊断行、不触发生成。
    """

    service, repository, store = _t057_service(session)
    exam_result, answer_id = _t057_final_result(session, scenario)
    repository.save_exam_result(exam_result)
    store.save(_t057_ready_report(exam_result))
    client = client_factory(service)
    student_headers = headers(scenario["student_a"], UserRole.STUDENT)
    submission_id = exam_result.submission_id

    detail = client.get(
        f"/api/results/me/submissions/{submission_id}", headers=student_headers
    )
    first = client.get(
        f"/api/results/me/submissions/{submission_id}/diagnosis",
        headers=student_headers,
    )
    second = client.get(
        f"/api/results/me/submissions/{submission_id}/diagnosis",
        headers=student_headers,
    )

    assert detail.status_code == 200
    assert Decimal(detail.json()["total_score"]) == Decimal("8.00")
    assert detail.json()["items"][0]["answer_id"] == answer_id
    assert detail.json()["is_final"] is True
    assert first.status_code == 200
    assert first.json()["status"] == "Ready"
    assert first.json()["learning_suggestions"] == ["复习变量作用域。"]
    assert first.json()["mastery_by_knowledge_point"][0]["mastery"] == "0.80"
    assert second.json()["status"] == "Ready"
    with Session(session.get_bind()) as check:
        assert len(list(check.scalars(select(DiagnosisReport)))) == 1


def test_diagnosis_returns_stale_after_new_aggregation(
    session: Session, scenario, client_factory
) -> None:
    """重新汇总后旧诊断为过期，不被旧结果覆盖。"""

    service, repository, store = _t057_service(session)
    exam_result, _ = _t057_final_result(session, scenario)
    repository.save_exam_result(exam_result)
    store.save(_t057_ready_report(exam_result))
    client = client_factory(service)
    student_headers = headers(scenario["student_a"], UserRole.STUDENT)
    submission_id = exam_result.submission_id
    with Session(session.get_bind()) as update, update.begin():
        row = update.scalars(select(ExamResult)).one()
        row.aggregated_at = exam_result.aggregated_at + timedelta(minutes=1)

    response = client.get(
        f"/api/results/me/submissions/{submission_id}/diagnosis",
        headers=student_headers,
    )

    assert response.json()["status"] == "Stale"
    with Session(session.get_bind()) as check:
        assert len(list(check.scalars(select(DiagnosisReport)))) == 1
