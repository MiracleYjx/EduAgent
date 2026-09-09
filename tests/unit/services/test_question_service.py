"""T024 Question Service 单元测试：人工题目管理和审核状态转换。"""

from __future__ import annotations

from collections.abc import Generator
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.core.database import Base
from backend.app.domain.enums import QuestionStatus, QuestionType, UserRole
from backend.app.models import Course, Role, User
from backend.app.services.auth_service import hash_password
from backend.app.services.course_service import CourseService
from backend.app.services.question_service import (
    QuestionNotFoundError,
    QuestionPermissionError,
    QuestionService,
    QuestionValidationError,
)


@pytest.fixture
def session() -> Generator[Session, None, None]:
    """创建隔离的 Question Service 测试数据库会话。"""

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
        description="课程描述",
        created_by=teacher.id,
    )
    course = session.get(Course, UUID(summary.id))
    assert course is not None
    return course


def test_question_service_supports_crud_and_owner_filter(
    session: Session,
) -> None:
    """教师可以管理自己的人工题目，查询不会泄露其他课程题目。"""

    teacher = add_teacher(session)
    another_teacher = add_teacher(
        session,
        username="another-teacher",
        email="another-teacher@example.com",
    )
    course = add_course(session, teacher)
    another_course = add_course(session, another_teacher, name="数据结构")
    service = QuestionService(session)

    created = service.create_question(
        course_id=course.id,
        question_type=QuestionType.SHORT_ANSWER,
        content="解释变量的作用。",
        reference_answer="变量用于保存数据。",
        scoring_rubric="说明保存和引用数据即可。",
        difficulty="简单",
        knowledge_points=["变量", "数据保存"],
        score=10,
        created_by=teacher.id,
    )
    other = service.create_question(
        course_id=another_course.id,
        question_type=QuestionType.TRUE_FALSE,
        content="变量可以保存数据。",
        reference_answer="True",
        score=2,
        created_by=another_teacher.id,
    )

    assert created.status is QuestionStatus.DRAFT
    assert created.score == Decimal("10.00")
    assert created.created_by == str(teacher.id)
    assert [item.id for item in service.list_questions(teacher_id=teacher.id)] == [
        created.id
    ]
    assert service.get_question(created.id, teacher_id=teacher.id).id == created.id

    updated = service.update_question(
        created.id,
        content="解释变量和常量的区别。",
        knowledge_points=["变量", "常量"],
        score=12,
        teacher_id=teacher.id,
    )
    assert updated.content == "解释变量和常量的区别。"
    assert updated.knowledge_points == ["变量", "常量"]
    assert updated.score == Decimal("12.00")

    with pytest.raises(QuestionPermissionError, match="无权访问该题目"):
        service.get_question(created.id, teacher_id=another_teacher.id)

    service.delete_question(other.id, teacher_id=another_teacher.id)
    service.delete_question(created.id, teacher_id=teacher.id)
    with pytest.raises(QuestionNotFoundError, match="题目不存在"):
        service.get_question(created.id)


def test_question_service_persists_review_status_transitions(
    session: Session,
) -> None:
    """题目必须按审核状态机流转，不能绕过待审核直接批准。"""

    teacher = add_teacher(session)
    course = add_course(session, teacher)
    service = QuestionService(session)
    question = service.create_question(
        course_id=course.id,
        question_type=QuestionType.SINGLE_CHOICE,
        content="下列哪项是 Python 的内置类型？",
        options=["列表", "课程"],
        reference_answer="列表",
        score=5,
        created_by=teacher.id,
    )

    with pytest.raises(QuestionValidationError, match="不能从“Draft”变更为“Approved”"):
        service.update_question_status(
            question.id,
            QuestionStatus.APPROVED,
            teacher_id=teacher.id,
        )

    pending = service.update_question_status(
        question.id,
        QuestionStatus.PENDING_REVIEW,
        teacher_id=teacher.id,
    )
    assert pending.status is QuestionStatus.PENDING_REVIEW
    approved = service.update_question_status(
        question.id,
        QuestionStatus.APPROVED,
        teacher_id=teacher.id,
    )
    assert approved.status is QuestionStatus.APPROVED

    with pytest.raises(
        QuestionValidationError, match="不能从“Approved”变更为“Needs Revision”"
    ):
        service.update_question_status(
            question.id,
            QuestionStatus.NEEDS_REVISION,
            teacher_id=teacher.id,
        )

    revised = service.create_question(
        course_id=course.id,
        question_type=QuestionType.SHORT_ANSWER,
        content="解释函数。",
        reference_answer="可复用的代码块。",
        score=5,
        created_by=teacher.id,
    )
    service.update_question_status(
        revised.id,
        QuestionStatus.PENDING_REVIEW,
        teacher_id=teacher.id,
    )
    needs_revision = service.update_question_status(
        revised.id,
        QuestionStatus.NEEDS_REVISION,
        teacher_id=teacher.id,
    )
    assert needs_revision.status is QuestionStatus.NEEDS_REVISION
    resubmitted = service.update_question_status(
        revised.id,
        QuestionStatus.PENDING_REVIEW,
        teacher_id=teacher.id,
    )
    assert resubmitted.status is QuestionStatus.PENDING_REVIEW


def test_question_service_validates_required_fields_and_type_values(
    session: Session,
) -> None:
    """题目服务应拒绝空内容、非法题型、非法分值和无效课程。"""

    teacher = add_teacher(session)
    course = add_course(session, teacher)
    service = QuestionService(session)

    with pytest.raises(QuestionValidationError, match="题目内容不能为空"):
        service.create_question(
            course_id=course.id,
            question_type=QuestionType.SHORT_ANSWER,
            content="  ",
            score=5,
            created_by=teacher.id,
        )

    with pytest.raises(QuestionValidationError, match="题型无效"):
        service.create_question(
            course_id=course.id,
            question_type="UNKNOWN",
            content="题目内容",
            score=5,
            created_by=teacher.id,
        )

    with pytest.raises(QuestionValidationError, match="题目分值必须大于 0"):
        service.create_question(
            course_id=course.id,
            question_type=QuestionType.TRUE_FALSE,
            content="题目内容",
            score=0,
            created_by=teacher.id,
        )

    with pytest.raises(QuestionNotFoundError, match="课程不存在"):
        service.create_question(
            course_id="00000000-0000-0000-0000-000000000000",
            question_type=QuestionType.TRUE_FALSE,
            content="题目内容",
            score=1,
            created_by=teacher.id,
        )


def test_question_service_rejects_conflicting_type_aliases(
    session: Session,
) -> None:
    """题型的兼容别名同时传入时必须保持一致。"""

    teacher = add_teacher(session)
    course = add_course(session, teacher)
    service = QuestionService(session)

    with pytest.raises(QuestionValidationError, match="题型输入不一致"):
        service.create_question(
            course_id=course.id,
            question_type=QuestionType.TRUE_FALSE,
            type=QuestionType.SHORT_ANSWER,
            content="题目内容",
            score=1,
            created_by=teacher.id,
        )
