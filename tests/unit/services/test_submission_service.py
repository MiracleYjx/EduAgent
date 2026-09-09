"""T028 Submission Service 单元测试：学生考试、答案和答卷状态边界。"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.core.database import Base
from backend.app.domain.enums import (
    AnswerStatus,
    ExamStatus,
    QuestionStatus,
    QuestionType,
    SubmissionStatus,
    UserRole,
)
from backend.app.models import Course, Exam, Role, User
from backend.app.services.auth_service import hash_password
from backend.app.services.course_service import CourseService
from backend.app.services.exam_service import ExamService
from backend.app.services.question_service import QuestionService
from backend.app.services.submission_service import (
    SubmissionConflictError,
    SubmissionNotAvailableError,
    SubmissionPermissionError,
    SubmissionService,
    SubmissionValidationError,
)


@pytest.fixture
def session() -> Generator[Session, None, None]:
    """创建隔离的答卷服务测试数据库会话。"""

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session
    engine.dispose()


def add_user(
    session: Session,
    role_name: UserRole,
    *,
    username: str,
    email: str,
) -> User:
    """向测试库写入带指定角色的账号。"""

    user = User(
        username=username,
        email=email,
        password_hash=hash_password("正确密码"),
    )
    role = session.scalar(select(Role).where(Role.name == role_name))
    user.roles.append(role or Role(name=role_name, description=role_name.value))
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def add_teacher(session: Session) -> User:
    """创建测试教师。"""

    return add_user(
        session,
        UserRole.TEACHER,
        username="teacher",
        email="teacher@example.com",
    )


def add_student(session: Session, *, username: str = "student") -> User:
    """创建测试学生。"""

    return add_user(
        session,
        UserRole.STUDENT,
        username=username,
        email=f"{username}@example.com",
    )


def add_course(session: Session, teacher: User) -> Course:
    """创建测试课程。"""

    summary = CourseService(session).create_course(
        name="Python 基础",
        created_by=teacher.id,
    )
    course = session.get(Course, UUID(summary.id))
    assert course is not None
    return course


def add_approved_question(
    session: Session,
    course: Course,
    teacher: User,
    *,
    content: str,
) -> UUID:
    """创建并审核一道可用于考试的题目。"""

    question_service = QuestionService(session)
    summary = question_service.create_question(
        course_id=course.id,
        question_type=QuestionType.SHORT_ANSWER,
        content=content,
        reference_answer="变量用于保存数据。",
        scoring_rubric="说明核心概念即可。",
        score=10,
        created_by=teacher.id,
    )
    question_service.update_question_status(
        summary.id,
        QuestionStatus.PENDING_REVIEW,
        teacher_id=teacher.id,
    )
    approved = question_service.update_question_status(
        summary.id,
        QuestionStatus.APPROVED,
        teacher_id=teacher.id,
    )
    return UUID(approved.id)


def add_published_exam(
    session: Session,
    course: Course,
    teacher: User,
    question_ids: list[UUID],
    *,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
) -> Exam:
    """创建并发布测试考试。"""

    exam_service = ExamService(session)
    summary = exam_service.create_exam(
        course_id=course.id,
        title="Python 阶段测验",
        question_ids=question_ids,
        starts_at=starts_at,
        ends_at=ends_at,
        created_by=teacher.id,
    )
    exam_service.publish_exam(summary.id, teacher_id=teacher.id)
    exam = session.get(Exam, UUID(summary.id))
    assert exam is not None
    return exam


def test_submission_service_lists_only_currently_available_exams(
    session: Session,
) -> None:
    """学生只能看到已发布且处于开放时间窗的考试。"""

    teacher = add_teacher(session)
    student = add_student(session)
    course = add_course(session, teacher)
    question_id = add_approved_question(
        session,
        course,
        teacher,
        content="解释变量的作用。",
    )
    moment = datetime(2026, 9, 9, 10, 0, tzinfo=UTC)
    available = add_published_exam(session, course, teacher, [question_id])
    add_published_exam(
        session,
        course,
        teacher,
        [question_id],
        starts_at=moment + timedelta(minutes=1),
    )
    add_published_exam(
        session,
        course,
        teacher,
        [question_id],
        ends_at=moment,
    )
    draft_summary = ExamService(session).create_exam(
        course_id=course.id,
        title="草稿测验",
        question_ids=[question_id],
        created_by=teacher.id,
    )

    exams = SubmissionService(session).list_available_exams(
        student_id=student.id,
        now=moment,
    )

    assert [item.id for item in exams] == [str(available.id)]
    assert draft_summary.status is ExamStatus.DRAFT

    with pytest.raises(SubmissionPermissionError, match="只有学生账号"):
        SubmissionService(session).list_available_exams(
            student_id=teacher.id,
            now=moment,
        )


def test_submission_service_creates_reuses_draft_and_persists_answers(
    session: Session,
) -> None:
    """重复打开考试时复用草稿，并按题目幂等保存答案。"""

    teacher = add_teacher(session)
    student = add_student(session)
    course = add_course(session, teacher)
    first_question = add_approved_question(
        session,
        course,
        teacher,
        content="解释变量的作用。",
    )
    second_question = add_approved_question(
        session,
        course,
        teacher,
        content="解释函数的作用。",
    )
    exam = add_published_exam(
        session,
        course,
        teacher,
        [first_question, second_question],
    )
    service = SubmissionService(session)

    created = service.create_submission(exam.id, student.id)
    reopened = service.create_submission(exam.id, student.id)
    first_answer = service.save_answer(
        created.id,
        first_question,
        "保存数据。",
        student_id=student.id,
    )
    updated_answer = service.save_answer(
        created.id,
        first_question,
        "保存并引用数据。",
        student_id=student.id,
    )
    batch = service.save_answers(
        created.id,
        {second_question: "组织可复用逻辑。"},
        student_id=student.id,
    )

    assert created.status is SubmissionStatus.DRAFT
    assert reopened.id == created.id
    assert first_answer.id == updated_answer.id
    assert updated_answer.content == "保存并引用数据。"
    assert [answer.question_id for answer in batch] == [str(second_question)]
    assert service.get_submission(created.id, student.id).answer_count == 2


def test_submission_service_rejects_incomplete_or_foreign_answers(
    session: Session,
) -> None:
    """缺少答案或提交不属于考试的题目时，草稿状态必须保持不变。"""

    teacher = add_teacher(session)
    student = add_student(session)
    course = add_course(session, teacher)
    first_question = add_approved_question(
        session,
        course,
        teacher,
        content="第一道题。",
    )
    second_question = add_approved_question(
        session,
        course,
        teacher,
        content="第二道题。",
    )
    foreign_question = add_approved_question(
        session,
        course,
        teacher,
        content="不属于当前答卷的题目。",
    )
    exam = add_published_exam(
        session,
        course,
        teacher,
        [first_question, second_question],
    )
    service = SubmissionService(session)
    submission = service.create_submission(exam.id, student.id)
    service.save_answer(submission.id, first_question, "第一题答案。")

    with pytest.raises(SubmissionValidationError, match="必须完成全部题目"):
        service.submit_submission(submission.id, student_id=student.id)
    with pytest.raises(SubmissionValidationError, match="不属于当前考试"):
        service.save_answer(submission.id, foreign_question, "越权答案。")

    unchanged = service.get_submission(submission.id, student.id)
    assert unchanged.status is SubmissionStatus.DRAFT
    assert unchanged.submitted_at is None

    service.save_answer(submission.id, second_question, "第二题答案。")
    submitted = service.submit_submission(
        submission.id,
        student_id=student.id,
        now=datetime(2026, 9, 9, 12, 0, tzinfo=UTC),
    )
    assert submitted.status is SubmissionStatus.SUBMITTED
    assert submitted.is_complete
    assert all(answer.status is AnswerStatus.SUBMITTED for answer in submitted.answers)


def test_submission_service_protects_duplicate_submission_and_freezes_answers(
    session: Session,
) -> None:
    """提交后不能重复提交、覆盖答案或让其他学生读取答卷。"""

    teacher = add_teacher(session)
    student = add_student(session)
    another_student = add_student(session, username="another-student")
    course = add_course(session, teacher)
    question_id = add_approved_question(
        session,
        course,
        teacher,
        content="解释封装。",
    )
    exam = add_published_exam(session, course, teacher, [question_id])
    service = SubmissionService(session)
    submission = service.create_submission(exam.id, student.id)
    service.save_answer(submission.id, question_id, "隐藏实现细节。")
    submitted = service.submit_submission(submission.id, student_id=student.id)

    with pytest.raises(SubmissionConflictError, match="不能重复提交") as duplicate:
        service.create_submission(exam.id, student.id)
    assert duplicate.value.existing_submission is not None
    assert duplicate.value.existing_submission.status is SubmissionStatus.SUBMITTED

    with pytest.raises(SubmissionConflictError, match="不能修改答案"):
        service.save_answer(submission.id, question_id, "覆盖答案。", student.id)
    with pytest.raises(SubmissionConflictError, match="不能重复提交"):
        service.submit_submission(submission.id, student_id=student.id)
    with pytest.raises(SubmissionPermissionError, match="其他学生"):
        service.get_submission(submission.id, student_id=another_student.id)

    persisted = service.get_submission(submission.id, student.id)
    assert persisted.status is SubmissionStatus.SUBMITTED
    assert persisted.answers[0].content == "隐藏实现细节。"
    assert submitted.submitted_at is not None


def test_submission_service_transitions_submission_and_answer_states(
    session: Session,
) -> None:
    """答卷和单题答案只能沿规定状态机向前推进。"""

    teacher = add_teacher(session)
    student = add_student(session)
    course = add_course(session, teacher)
    question_id = add_approved_question(
        session,
        course,
        teacher,
        content="解释模块。",
    )
    exam = add_published_exam(session, course, teacher, [question_id])
    service = SubmissionService(session)
    submission = service.create_submission(exam.id, student.id)
    answer = service.save_answer(submission.id, question_id, "模块用于组织代码。")
    submitted = service.submit_submission(submission.id, student.id)
    graded_answer = service.update_answer_status(
        answer.id,
        AnswerStatus.GRADING,
        student_id=student.id,
    )
    graded_answer = service.update_answer_status(
        graded_answer.id,
        AnswerStatus.GRADED,
        student_id=student.id,
    )
    graded = service.update_submission_status(
        submitted.id,
        SubmissionStatus.GRADED,
        student_id=student.id,
    )
    reviewed = service.update_submission_status(
        graded.id,
        SubmissionStatus.REVIEWED,
        student_id=student.id,
    )

    assert graded_answer.status is AnswerStatus.GRADED
    assert graded.status is SubmissionStatus.GRADED
    assert graded.graded_at is not None
    assert reviewed.status is SubmissionStatus.REVIEWED
    assert reviewed.reviewed_at is not None

    with pytest.raises(
        SubmissionValidationError, match="不能从“Reviewed”变更为“Draft”"
    ):
        service.update_submission_status(
            reviewed.id,
            SubmissionStatus.DRAFT,
            student_id=student.id,
        )


def test_submission_service_rejects_exam_outside_open_window(
    session: Session,
) -> None:
    """考试未开始或已结束时不能创建答卷。"""

    teacher = add_teacher(session)
    student = add_student(session)
    course = add_course(session, teacher)
    question_id = add_approved_question(
        session,
        course,
        teacher,
        content="解释异常处理。",
    )
    starts_at = datetime(2026, 9, 9, 11, 0, tzinfo=UTC)
    exam = add_published_exam(
        session,
        course,
        teacher,
        [question_id],
        starts_at=starts_at,
    )

    with pytest.raises(SubmissionNotAvailableError, match="尚未到开放时间"):
        SubmissionService(session).create_submission(
            exam.id,
            student.id,
            now=datetime(2026, 9, 9, 10, 0, tzinfo=UTC),
        )
