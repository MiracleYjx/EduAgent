"""P2.3 教师结果页生产 loaders 与真实 Gradio 事件装配集成测试。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import gradio as gr
import pytest
from gradio.state_holder import SessionState
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import backend.app.api.results as results_api
from backend.app.api.results import ResultsQueryService
from backend.app.core.database import Base
from backend.app.domain.enums import (
    AnswerStatus,
    ExamStatus,
    GradingStatus,
    QuestionStatus,
    QuestionType,
    ReviewStatus,
    SubmissionStatus,
    UserRole,
    ValidationStatus,
    WorkflowStatus,
)
from backend.app.models import (
    Answer,
    Course,
    Exam,
    Question,
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
    WeakKnowledgePointDTO,
)
from backend.app.services.grading.diagnosis_report_store import DiagnosisReportStore
from backend.app.services.grading.grading_repository import DatabaseGradingRepository
from backend.app.services.workflow_checkpoint import WORKFLOW_RUN_KIND
from backend.app.ui import results_loaders
from backend.app.ui.results_diagnosis import DIAGNOSIS_NOT_READY_MESSAGE
from backend.app.ui.results_view import (
    TeacherResultsView,
    configure_results_loaders,
    create_teacher_results_view,
    review_context_is_complete,
)


@dataclass(frozen=True, slots=True)
class ScenarioIds:
    teacher_id: str
    course_id: str
    exam_id: str
    question_id: str
    final_student_id: str
    final_submission_id: str
    final_answer_id: str
    pending_student_id: str
    pending_submission_id: str
    pending_answer_id: str


@dataclass(frozen=True, slots=True)
class TeacherUiEnv:
    app: gr.Blocks
    view: TeacherResultsView
    state_component: gr.State
    ids: ScenarioIds
    service: ResultsQueryService


def _session_factory(engine: Engine) -> Any:
    return lambda: Session(engine)


def _seed_scenario(engine: Engine) -> ScenarioIds:
    """准备一份最终成绩和一份待复核成绩。"""

    with Session(engine) as session:
        teacher = User(
            username="p23-teacher",
            email="p23-teacher@example.com",
            password_hash="hashed-password",
        )
        final_student = User(
            username="最终学生",
            email="p23-final@example.com",
            password_hash="hashed-password",
        )
        pending_student = User(
            username="待复核学生",
            email="p23-pending@example.com",
            password_hash="hashed-password",
        )
        course = Course(name="P2.3 授权课程", creator=teacher)
        question = Question(
            course=course,
            creator=teacher,
            type=QuestionType.SHORT_ANSWER,
            content="解释变量的作用。",
            reference_answer="变量用于保存数据。",
            scoring_rubric="说明保存和引用数据即可。",
            knowledge_points=["变量"],
            score=Decimal("10.00"),
            status=QuestionStatus.APPROVED,
        )
        exam = Exam(
            course=course,
            creator=teacher,
            title="P2.3 阶段测验",
            questions=[question],
            status=ExamStatus.PUBLISHED,
        )
        final_submission = Submission(
            exam=exam,
            student=final_student,
            status=SubmissionStatus.SUBMITTED,
            submitted_at=datetime.now(UTC),
        )
        pending_submission = Submission(
            exam=exam,
            student=pending_student,
            status=SubmissionStatus.SUBMITTED,
            submitted_at=datetime.now(UTC),
        )
        final_answer = Answer(
            submission=final_submission,
            question=question,
            content="变量用于保存数据。",
            status=AnswerStatus.SUBMITTED,
        )
        pending_answer = Answer(
            submission=pending_submission,
            question=question,
            content="变量是一个名称。",
            status=AnswerStatus.SUBMITTED,
        )
        session.add_all([final_submission, pending_submission])
        session.commit()
        ids = ScenarioIds(
            teacher_id=str(teacher.id),
            course_id=str(course.id),
            exam_id=str(exam.id),
            question_id=str(question.id),
            final_student_id=str(final_student.id),
            final_submission_id=str(final_submission.id),
            final_answer_id=str(final_answer.id),
            pending_student_id=str(pending_student.id),
            pending_submission_id=str(pending_submission.id),
            pending_answer_id=str(pending_answer.id),
        )

    repository = DatabaseGradingRepository(
        session_factory=_session_factory(engine)
    )
    repository.save_exam_result(_exam_result(ids, final=True))
    repository.save_exam_result(_exam_result(ids, final=False))
    with Session(engine) as session, session.begin():
        session.add(
            WorkflowRun(
                workflow_id="workflow-p23-pending",
                request_id="request-p23-pending",
                submission_id=UUID(ids.pending_submission_id),
                current_node="grade_subjective",
                current_answer_id=UUID(ids.pending_answer_id),
                status=WorkflowStatus.PAUSED,
                checkpoint={
                    "kind": WORKFLOW_RUN_KIND,
                    "thread_id": "thread-p23-pending",
                },
                pause_reason="等待教师复核。",
                retry_count=0,
                resumable=True,
            )
        )
    DiagnosisReportStore(session_factory=_session_factory(engine)).save(
        _ready_report(ids)
    )
    return ids


def _exam_result(ids: ScenarioIds, *, final: bool) -> ExamResultDTO:
    submission_id = (
        ids.final_submission_id if final else ids.pending_submission_id
    )
    student_id = ids.final_student_id if final else ids.pending_student_id
    answer_id = ids.final_answer_id if final else ids.pending_answer_id
    decision = ConfidenceDecisionDTO(
        confidence=0.95 if final else 0.4,
        threshold=0.8,
        requires_review=not final,
        review_status=(
            ReviewStatus.NOT_REQUIRED.value
            if final
            else ReviewStatus.PENDING_REVIEW.value
        ),
        grading_status=(
            GradingStatus.ACCEPTED.value
            if final
            else GradingStatus.PENDING_REVIEW.value
        ),
        reason=(
            "置信度不低于阈值，自动接受。"
            if final
            else "置信度低于阈值，进入人工复核。"
        ),
    )
    return ExamResultDTO(
        submission_id=submission_id,
        exam_id=ids.exam_id,
        student_id=student_id,
        result_status=(
            ExamResultStatus.FINAL if final else ExamResultStatus.PENDING_REVIEW
        ),
        is_final=final,
        final_total_score=Decimal("8.00") if final else None,
        confirmed_subtotal=Decimal("8.00") if final else Decimal("0.00"),
        confirmed_subtotal_label="已确认部分小计。",
        total_max_score=Decimal("10.00"),
        expected_answer_count=1,
        graded_answer_count=1,
        counted_answer_count=1 if final else 0,
        pending_review_answer_count=0 if final else 1,
        items=[
            QuestionResultDTO(
                order=1,
                answer_id=answer_id,
                question_id=ids.question_id,
                question_type=QuestionType.SHORT_ANSWER,
                max_score=Decimal("10.00"),
                score=Decimal("8.00") if final else Decimal("6.00"),
                effective_score=Decimal("8.00") if final else None,
                counted=final,
                requires_review=not final,
                grading_status=(
                    GradingStatus.ACCEPTED.value
                    if final
                    else GradingStatus.PENDING_REVIEW.value
                ),
                review_status=(
                    ReviewStatus.NOT_REQUIRED.value
                    if final
                    else ReviewStatus.PENDING_REVIEW.value
                ),
                validation_status=ValidationStatus.VALIDATED.value,
                reason="评分理由。",
                knowledge_points=["变量"],
                missing_knowledge_points=[] if final else ["引用数据"],
                confidence=0.95 if final else 0.4,
                submission_id=submission_id,
                decision=decision,
            )
        ],
        aggregated_at=datetime(2026, 9, 21, 12, 0, tzinfo=UTC),
    )


def _ready_report(ids: ScenarioIds) -> DiagnosisReportDTO:
    return DiagnosisReportDTO(
        exam_result_id=f"exam-result:{ids.final_submission_id}",
        submission_id=ids.final_submission_id,
        student_id=ids.final_student_id,
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
        weak_knowledge_points=[
            WeakKnowledgePointDTO(
                knowledge_point="变量",
                reason="掌握度仍需提升。",
                error_count=1,
                awarded_score=Decimal("8.00"),
                max_score=Decimal("10.00"),
                mastery=Decimal("0.80"),
            )
        ],
        error_reasons=["变量引用说明不完整。"],
        learning_suggestions=["复习变量的定义与引用。"],
        generated_at=datetime(2026, 9, 21, 12, 1, tzinfo=UTC),
        source_exam_result_updated_at=datetime(2026, 9, 21, 12, 0, tzinfo=UTC),
    )


@pytest.fixture(scope="module")
def teacher_ui_env() -> Any:
    """将生产 loader 的数据库工厂指向隔离数据库。"""

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    ids = _seed_scenario(engine)
    sessions = _session_factory(engine)
    patcher = pytest.MonkeyPatch()
    patcher.setattr(results_loaders, "get_session_factory", lambda: sessions)
    patcher.setattr(results_api, "get_session_factory", lambda: sessions)
    results_loaders.configure_production_results_loaders()
    state_value = {
        "access_token": "teacher-token",
        "user_id": ids.teacher_id,
        "username": "p23-teacher",
        "roles": [UserRole.TEACHER.value],
    }
    with gr.Blocks(analytics_enabled=False) as app:
        state_component = gr.State(state_value)
        view = create_teacher_results_view(state_component)
    service = results_api.build_production_results_query_service()
    try:
        yield TeacherUiEnv(
            app=app,
            view=view,
            state_component=state_component,
            ids=ids,
            service=service,
        )
    finally:
        configure_results_loaders()
        patcher.undo()
        engine.dispose()


def _callback(env: TeacherUiEnv, name: str) -> Any:
    callbacks = [
        block_fn
        for block_fn in env.app.fns.values()
        if getattr(block_fn.fn, "__name__", None) == name
    ]
    assert len(callbacks) == 1
    return callbacks[0]


def _state(env: TeacherUiEnv) -> SessionState:
    state = SessionState(env.app)
    state[env.state_component._id] = env.state_component.value
    return state


def _process(
    env: TeacherUiEnv,
    name: str,
    inputs: list[Any],
    state: SessionState,
    *,
    event_data: gr.EventData | None = None,
) -> tuple[Any, dict[str, Any]]:
    callback = _callback(env, name)
    response = asyncio.run(
        env.app.process_api(
            callback,
            inputs,
            state=state,
            event_data=event_data,
        )
    )
    return callback, response


def _panel_state(env: TeacherUiEnv) -> SessionState:
    _prime_filters(env)
    state = _state(env)
    _process(
        env,
        "refresh_teacher_panel",
        [env.ids.course_id, env.ids.exam_id, None],
        state,
    )
    return state


def _prime_filters(env: TeacherUiEnv) -> None:
    """模拟浏览器应用前两级回调返回的下拉 choices。"""

    env.view.course.choices = [("P2.3 授权课程", env.ids.course_id)]
    env.view.exam.choices = [("P2.3 阶段测验", env.ids.exam_id)]


def _select_student(
    env: TeacherUiEnv,
    state: SessionState,
    submission_id: str,
) -> tuple[Any, dict[str, Any]]:
    records = state[env.view.result_records._id]
    index = next(
        position
        for position, record in enumerate(records)
        if record["submission_id"] == submission_id
    )
    event = gr.EventData(
        None,
        {
            "index": (index, 0),
            "value": None,
            "row_value": None,
            "col_value": None,
            "selected": True,
        },
    )
    return _process(
        env,
        "select_teacher_result",
        [None, env.ids.exam_id, None],
        state,
        event_data=event,
    )


def test_course_selection_loads_exams_with_production_loaders(
    teacher_ui_env: TeacherUiEnv,
) -> None:
    """课程选择事件通过生产 loader 返回该课程考试。"""

    state = _state(teacher_ui_env)
    _, course_response = _process(
        teacher_ui_env,
        "refresh_teacher_courses",
        [None],
        state,
    )
    _, exam_response = _process(
        teacher_ui_env,
        "refresh_teacher_exams",
        [teacher_ui_env.ids.course_id, None],
        state,
    )

    assert course_response["data"][0]["value"] == teacher_ui_env.ids.course_id
    assert exam_response["data"][0]["value"] == teacher_ui_env.ids.exam_id
    assert exam_response["data"][0]["choices"] == [
        ["P2.3 阶段测验", teacher_ui_env.ids.exam_id]
    ]


def test_exam_selection_loads_students_with_production_loaders(
    teacher_ui_env: TeacherUiEnv,
) -> None:
    """考试选择事件展示真实学生姓名、总分、状态和待复核数。"""

    _prime_filters(teacher_ui_env)
    state = _state(teacher_ui_env)
    _, response = _process(
        teacher_ui_env,
        "refresh_teacher_panel",
        [teacher_ui_env.ids.course_id, teacher_ui_env.ids.exam_id, None],
        state,
    )
    rows = response["data"][0]["data"]

    assert {row[0] for row in rows} == {"最终学生", "待复核学生"}
    final_row = next(row for row in rows if row[0] == "最终学生")
    pending_row = next(row for row in rows if row[0] == "待复核学生")
    assert final_row[1:] == ["8.00", "✓ 最终成绩", "0"]
    assert pending_row[1:] == ["最终成绩未形成", "⚠ 待人工复核", "1"]


def test_teacher_summary_matches_results_query_service(
    teacher_ui_env: TeacherUiEnv,
) -> None:
    """页面四项统计逐项等于服务端摘要，不按列表重算。"""

    expected = teacher_ui_env.service.get_exam_summary(
        teacher_ui_env.ids.teacher_id,
        teacher_ui_env.ids.exam_id,
    )
    _prime_filters(teacher_ui_env)
    state = _state(teacher_ui_env)
    _, response = _process(
        teacher_ui_env,
        "refresh_teacher_panel",
        [teacher_ui_env.ids.course_id, teacher_ui_env.ids.exam_id, None],
        state,
    )

    assert response["data"][2:6] == [
        str(expected.submitted_count),
        str(expected.final_count),
        str(expected.pending_review_count),
        str(expected.average_of_final_scores),
    ]


def test_selected_student_renders_persisted_diagnosis_and_empty_state(
    teacher_ui_env: TeacherUiEnv,
) -> None:
    """Ready 报告展示四类诊断事实，无报告的待复核成绩显示空态。"""

    state = _panel_state(teacher_ui_env)
    _, ready_response = _select_student(
        teacher_ui_env,
        state,
        teacher_ui_env.ids.final_submission_id,
    )
    diagnosis = ready_response["data"][1]
    mastery = ready_response["data"][2]["data"]
    _, empty_response = _select_student(
        teacher_ui_env,
        state,
        teacher_ui_env.ids.pending_submission_id,
    )

    assert "变量" in diagnosis and "掌握度仍需提升" in diagnosis
    assert "变量引用说明不完整" in diagnosis
    assert "复习变量的定义与引用" in diagnosis
    assert mastery == [["变量", "1", "0", "8.00", "10.00", "0.80"]]
    assert DIAGNOSIS_NOT_READY_MESSAGE in empty_response["data"][1]


def test_pending_result_carries_review_context(
    teacher_ui_env: TeacherUiEnv,
) -> None:
    """待复核学生行携带真实 Workflow、答卷和答案上下文。"""

    state = _panel_state(teacher_ui_env)
    _, response = _select_student(
        teacher_ui_env,
        state,
        teacher_ui_env.ids.pending_submission_id,
    )
    context = state[teacher_ui_env.view.review_context._id]

    assert context["workflow_id"] == "workflow-p23-pending"
    assert context["submission_id"] == teacher_ui_env.ids.pending_submission_id
    assert context["answer_id"] == teacher_ui_env.ids.pending_answer_id
    assert review_context_is_complete(context)
    assert response["data"][5]["interactive"] is True
