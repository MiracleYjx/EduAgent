"""T026 Exam Service 单元测试：考试组卷和发布边界。"""

from __future__ import annotations

from collections.abc import Generator
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.core.database import Base
from backend.app.domain.enums import ExamStatus, QuestionStatus, QuestionType, UserRole
from backend.app.models import Course, Role, User
from backend.app.services.auth_service import hash_password
from backend.app.services.course_service import CourseService
from backend.app.services.exam_service import (
    ExamPermissionError,
    ExamService,
    ExamValidationError,
)
from backend.app.services.question_service import QuestionService


@pytest.fixture
def session() -> Generator[Session, None, None]:
    """创建隔离的考试服务测试数据库会话。"""

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session
    engine.dispose()


def add_teacher(
    session: Session,
    *,
    username: str = "teacher",
    email: str = "teacher@example.com",
) -> User:
    """向测试库写入一个教师账号。"""

    teacher = User(
        username=username,
        email=email,
        password_hash=hash_password("正确密码"),
    )
    role = session.scalar(select(Role).where(Role.name == UserRole.TEACHER))
    teacher.roles.append(role or Role(name=UserRole.TEACHER, description="教师"))
    session.add(teacher)
    session.commit()
    session.refresh(teacher)
    return teacher


def add_course(session: Session, teacher: User, name: str = "Python 基础") -> Course:
    """创建由指定教师负责的测试课程。"""

    summary = CourseService(session).create_course(
        name=name,
        created_by=teacher.id,
    )
    course = session.get(Course, UUID(summary.id))
    assert course is not None
    return course


def add_question(
    session: Session,
    course: Course,
    teacher: User,
    *,
    content: str = "解释变量的作用。",
    score: int = 10,
    approved: bool = False,
) -> UUID:
    """创建测试题目，并按需推进到 Approved 状态。"""

    question_service = QuestionService(session)
    summary = question_service.create_question(
        course_id=course.id,
        question_type=QuestionType.SHORT_ANSWER,
        content=content,
        reference_answer="变量用于保存数据。",
        scoring_rubric="说明保存和引用数据即可。",
        score=score,
        created_by=teacher.id,
    )
    if approved:
        question_service.update_question_status(
            summary.id,
            QuestionStatus.PENDING_REVIEW,
            teacher_id=teacher.id,
        )
        summary = question_service.update_question_status(
            summary.id,
            QuestionStatus.APPROVED,
            teacher_id=teacher.id,
        )
    return UUID(summary.id)


def test_exam_service_creates_associates_and_publishes_exam(
    session: Session,
) -> None:
    """教师可以用已审核题目创建考试并完成发布。"""

    teacher = add_teacher(session)
    course = add_course(session, teacher)
    first_question = add_question(session, course, teacher, approved=True)
    second_question = add_question(
        session,
        course,
        teacher,
        content="解释函数的作用。",
        score=5,
        approved=True,
    )
    service = ExamService(session)

    exam = service.create_exam(
        course_id=course.id,
        title="Python 基础测验",
        description="第一章阶段测验。",
        duration_minutes=45,
        question_ids=[first_question],
        created_by=teacher.id,
    )

    assert exam.status is ExamStatus.DRAFT
    assert exam.question_ids == [str(first_question)]
    assert exam.question_count == 1
    assert exam.total_score == Decimal("10.00")

    updated = service.add_questions(
        exam.id,
        [second_question],
        teacher_id=teacher.id,
    )
    assert set(updated.question_ids) == {str(first_question), str(second_question)}
    assert updated.question_count == 2
    assert updated.total_score == Decimal("15.00")

    published = service.publish_exam(exam.id, teacher_id=teacher.id)
    assert published.status is ExamStatus.PUBLISHED
    assert (
        service.get_exam(exam.id, teacher_id=teacher.id).status is ExamStatus.PUBLISHED
    )
    assert [item.id for item in service.list_exams(teacher_id=teacher.id)] == [exam.id]


def test_exam_service_rejects_unapproved_and_cross_course_questions(
    session: Session,
) -> None:
    """未审核题目和其他课程题目不能进入考试。"""

    teacher = add_teacher(session)
    course = add_course(session, teacher)
    other_course = add_course(session, teacher, name="数据结构")
    draft_question = add_question(session, course, teacher)
    other_course_question = add_question(
        session,
        other_course,
        teacher,
        content="解释栈和队列的区别。",
        approved=True,
    )
    service = ExamService(session)

    with pytest.raises(ExamValidationError, match="Approved"):
        service.create_exam(
            course_id=course.id,
            title="非法测验",
            question_ids=[draft_question],
            created_by=teacher.id,
        )

    exam = service.create_exam(
        course_id=course.id,
        title="待组卷测验",
        created_by=teacher.id,
    )
    with pytest.raises(ExamValidationError, match="同一课程"):
        service.add_questions(
            exam.id,
            [other_course_question],
            teacher_id=teacher.id,
        )


def test_exam_service_requires_questions_before_publish(
    session: Session,
) -> None:
    """没有已审核题目的草稿考试不能发布。"""

    teacher = add_teacher(session)
    course = add_course(session, teacher)
    service = ExamService(session)
    exam = service.create_exam(
        course_id=course.id,
        title="空白测验",
        created_by=teacher.id,
    )

    with pytest.raises(ExamValidationError, match="至少需要关联"):
        service.publish_exam(exam.id, teacher_id=teacher.id)


def test_exam_service_enforces_owner_and_freezes_published_exam(
    session: Session,
) -> None:
    """非课程所有者不能组卷，已发布考试不能继续修改题目。"""

    teacher = add_teacher(session)
    another_teacher = add_teacher(
        session,
        username="another-teacher",
        email="another-teacher@example.com",
    )
    course = add_course(session, teacher)
    question_id = add_question(session, course, teacher, approved=True)
    service = ExamService(session)
    exam = service.create_exam(
        course_id=course.id,
        title="权限测验",
        question_ids=[question_id],
        created_by=teacher.id,
    )

    with pytest.raises(ExamPermissionError, match="无权访问"):
        service.publish_exam(exam.id, teacher_id=another_teacher.id)

    service.publish_exam(exam.id, teacher_id=teacher.id)
    with pytest.raises(ExamValidationError, match="已发布"):
        service.add_questions(exam.id, [question_id], teacher_id=teacher.id)
