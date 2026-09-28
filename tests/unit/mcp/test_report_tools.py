"""T082 report tools through the authenticated in-app tool boundary."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.api.results import ResultsQueryService
from backend.app.core.database import Base
from backend.app.domain.enums import AnswerStatus, SubmissionStatus, UserRole
from backend.app.domain.permissions import Permission
from backend.app.mcp.registry import ToolRegistry
from backend.app.mcp.server import MCPToolServer
from backend.app.mcp.tools.report_tools import register_report_tools
from backend.app.models import Answer, Submission, User
from backend.app.services.auth_service import AuthService
from backend.app.services.course_service import CourseService
from backend.app.services.grading.diagnosis_report_store import DiagnosisReportStore
from backend.app.services.grading.grading_repository import DatabaseGradingRepository
from tests.contract.test_results_api_contract import (
    _t057_final_result,
    _t057_ready_report,
)
from tests.unit.services.test_submission_service import (
    add_approved_question,
    add_course,
    add_published_exam,
    add_student,
    add_teacher,
    add_user,
)

SECRET = "test-report-jwt-secret-not-for-production"


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as active:
        yield active
    engine.dispose()


@pytest.fixture
def setup(session: Session) -> dict[str, Any]:
    teacher = add_teacher(session)
    course = add_course(session, teacher)
    question_id = add_approved_question(session, course, teacher, content="报告测试题")
    exam = add_published_exam(session, course, teacher, [question_id])
    student = add_student(session, username="report-student")
    other_student = add_student(session, username="report-other")
    submissions = {}
    for name, learner in (("a", student), ("b", other_student)):
        submission = Submission(
            exam_id=exam.id,
            student_id=learner.id,
            status=SubmissionStatus.SUBMITTED,
            submitted_at=datetime.now(UTC),
        )
        session.add(submission)
        submissions[name] = submission
    session.commit()
    engine = session.get_bind()
    repository = DatabaseGradingRepository(session_factory=lambda: Session(engine))
    store = DiagnosisReportStore(session_factory=lambda: Session(engine))
    service = ResultsQueryService(
        repository=repository,
        session_factory=lambda: Session(engine),
        diagnosis_store=store,
    )
    registry = ToolRegistry()
    register_report_tools(registry, results_service=service)
    server = MCPToolServer(secret_key=SECRET, registry=registry)
    return {
        "teacher": teacher,
        "course": course,
        "exam": exam,
        "student": student,
        "other_student": other_student,
        "submissions": submissions,
        "repository": repository,
        "store": store,
        "server": server,
    }


def _token(session: Session, user: User) -> str:
    return AuthService(session, secret_key=SECRET).issue_access_token(user)


def _call(session: Session, setup: dict[str, Any], user: User, name: str, **args: Any):
    return setup["server"].call_tool(
        name, args, token=_token(session, user), session=session
    )


def _finalize(
    session: Session, setup: dict[str, Any], *, error_reason: str | None = None
) -> None:
    # Use the same persisted result and diagnosis read models as the public results API.
    submission = setup["submissions"]["a"]
    question_id = setup["exam"].questions[0].id
    session.add(
        Answer(
            submission_id=submission.id,
            question_id=question_id,
            content="已回答",
            status=AnswerStatus.SUBMITTED,
        )
    )
    session.commit()
    result, _ = _t057_final_result(session, {"submissions": setup["submissions"]})
    setup["repository"].save_exam_result(result)
    report = _t057_ready_report(result)
    if error_reason is not None:
        report = report.model_copy(update={"error_reasons": [error_reason]})
    setup["store"].save(report)


def test_registry_keeps_single_permission_and_accepts_either_view_permission(
    session: Session, setup: dict[str, Any]
) -> None:
    teacher = setup["teacher"]
    student = setup["student"]
    teacher_tools = setup["server"].list_tools(
        token=_token(session, teacher), session=session
    )
    student_tools = setup["server"].list_tools(
        token=_token(session, student), session=session
    )
    assert {item["name"] for item in teacher_tools.data["tools"]} == {
        "get_exam_result",
        "get_student_profile",
        "create_report",
    }
    assert {item["name"] for item in student_tools.data["tools"]} == {
        "get_exam_result",
        "get_student_profile",
    }
    assert (
        setup["server"].registry.get("create_report").permission
        == Permission.VIEW_EXAM_RESULTS
    )


def test_exam_result_teacher_all_student_own_and_no_internal_ids(
    session: Session, setup: dict[str, Any]
) -> None:
    _finalize(session, setup)
    teacher, student, exam = setup["teacher"], setup["student"], setup["exam"]
    teacher_result = _call(
        session, setup, teacher, "get_exam_result", exam_id=str(exam.id)
    )
    assert teacher_result.ok
    assert teacher_result.data["count"] == 2
    assert teacher_result.data["results"][0]["total_score"] == "8.00"
    assert teacher_result.data["summary"]["final_count"] == 1
    assert teacher_result.data["summary"]["average_of_final_scores"] == "8.00"
    assert str(setup["submissions"]["a"].id) not in str(teacher_result.data)
    student_result = _call(
        session, setup, student, "get_exam_result", exam_id=str(exam.id)
    )
    assert student_result.ok and student_result.data["count"] == 1
    assert "summary" not in student_result.data
    assert student_result.data["results"][0]["total_score"] == "8.00"


def test_exam_result_denies_foreign_course_and_student_identity(
    session: Session, setup: dict[str, Any]
) -> None:
    foreign = add_user(
        session,
        UserRole.TEACHER,
        username="foreign-report",
        email="foreign-report@example.com",
    )
    exam_id = str(setup["exam"].id)
    assert (
        _call(session, setup, foreign, "get_exam_result", exam_id=exam_id).error.code
        == "COURSE_FORBIDDEN"
    )
    denied = _call(
        session,
        setup,
        setup["student"],
        "get_exam_result",
        exam_id=exam_id,
        student_id=str(setup["other_student"].id),
    )
    assert denied.error.code == "STUDENT_FORBIDDEN"
    assert (
        setup["server"]
        .call_tool("get_exam_result", {"exam_id": exam_id}, token=None, session=session)
        .error.code
        == "TOOL_UNAUTHENTICATED"
    )


def test_profile_is_per_submission_and_teacher_needs_owned_course(
    session: Session, setup: dict[str, Any]
) -> None:
    _finalize(session, setup)
    teacher, student, course = setup["teacher"], setup["student"], setup["course"]
    profile = _call(
        session,
        setup,
        teacher,
        "get_student_profile",
        student_id=str(student.id),
        course_id=str(course.id),
    )
    assert profile.ok
    assert profile.data["scope"] == "按答卷聚合，非跨考试画像"
    assert profile.data["count"] == 1
    assert profile.data["diagnoses"][0]["status"] == "Ready"
    assert profile.data["diagnoses"][0]["learning_suggestions"]
    assert (
        _call(
            session, setup, teacher, "get_student_profile", student_id=str(student.id)
        ).error.code
        == "COURSE_ID_REQUIRED"
    )
    foreign_course = CourseService(session).create_course(
        "另一门课", teacher_id=student.id
    )
    assert (
        _call(
            session,
            setup,
            teacher,
            "get_student_profile",
            student_id=str(student.id),
            course_id=foreign_course.id,
        ).error.code
        == "COURSE_FORBIDDEN"
    )


def test_profile_student_own_and_missing_diagnosis_state(
    session: Session, setup: dict[str, Any]
) -> None:
    student = setup["student"]
    result = _call(
        session, setup, student, "get_student_profile", student_id=str(student.id)
    )
    assert result.ok and result.data["diagnoses"][0]["status"] == "Not Ready"
    assert result.data["diagnoses"][0]["message"]
    denied = _call(
        session,
        setup,
        student,
        "get_student_profile",
        student_id=str(setup["other_student"].id),
    )
    assert denied.error.code == "STUDENT_FORBIDDEN"


def test_report_uses_persisted_data_and_requires_course_for_student(
    session: Session, setup: dict[str, Any]
) -> None:
    _finalize(session, setup)
    teacher, student, exam, course = (
        setup["teacher"],
        setup["student"],
        setup["exam"],
        setup["course"],
    )
    missing = _call(
        session,
        setup,
        teacher,
        "create_report",
        report_type="student",
        student_id=str(student.id),
    )
    assert missing.error.code == "COURSE_ID_REQUIRED"
    exam_report = _call(
        session,
        setup,
        teacher,
        "create_report",
        report_type="exam",
        exam_id=str(exam.id),
    )
    assert exam_report.ok and exam_report.data["summary"]["final_count"] == 1
    assert exam_report.data["diagnoses"][0]["status"] == "Ready"
    student_report = _call(
        session,
        setup,
        teacher,
        "create_report",
        report_type="student",
        student_id=str(student.id),
        course_id=str(course.id),
    )
    assert (
        student_report.ok and student_report.data["diagnoses"][0]["status"] == "Ready"
    )
    assert student_report.data["results"][0]["total_score"] == "8.00"
    assert (
        _call(
            session,
            setup,
            student,
            "create_report",
            report_type="exam",
            exam_id=str(exam.id),
        ).error.code
        == "TOOL_FORBIDDEN"
    )


def test_report_rejects_foreign_course_and_redacts_long_internal_text(
    session: Session, setup: dict[str, Any]
) -> None:
    internal_id = str(setup["submissions"]["a"].id)
    _finalize(session, setup, error_reason=internal_id + "X" * 700)
    profile = _call(
        session,
        setup,
        setup["teacher"],
        "get_student_profile",
        student_id=str(setup["student"].id),
        course_id=str(setup["course"].id),
    )
    reason = profile.data["diagnoses"][0]["error_reasons"][0]
    assert internal_id not in reason and len(reason) <= 501
    foreign = add_user(
        session,
        UserRole.TEACHER,
        username="foreign-report-2",
        email="foreign-report-2@example.com",
    )
    denied = _call(
        session,
        setup,
        foreign,
        "create_report",
        report_type="student",
        student_id=str(setup["student"].id),
        course_id=str(setup["course"].id),
    )
    assert denied.error.code == "COURSE_FORBIDDEN"
