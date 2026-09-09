"""T022 课程服务单元测试：课程元数据和课程归属管理。"""

from __future__ import annotations

from collections.abc import Generator
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.core.database import Base
from backend.app.domain.enums import UserRole
from backend.app.models import Course, Role, User
from backend.app.services.auth_service import hash_password
from backend.app.services.course_service import (
    CourseNotFoundError,
    CoursePermissionError,
    CourseService,
    CourseValidationError,
)


@pytest.fixture
def session() -> Generator[Session, None, None]:
    """创建隔离的课程服务测试数据库会话。"""

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


def test_course_service_supports_crud_and_owner_filter(session: Session) -> None:
    """教师可以管理自己的课程，查询结果不会泄露其他教师的课程。"""

    teacher = add_teacher(session)
    another_teacher = add_teacher(
        session,
        username="another-teacher",
        email="another-teacher@example.com",
    )
    service = CourseService(session)

    created = service.create_course(
        name="Python 基础",
        description="变量、函数和模块。",
        created_by=teacher.id,
    )

    assert created.name == "Python 基础"
    assert created.description == "变量、函数和模块。"
    assert created.created_by == str(teacher.id)
    assert [item.id for item in service.list_courses(teacher_id=teacher.id)] == [
        created.id
    ]

    updated = service.update_course(
        created.id,
        name="Python 基础进阶",
        description="面向教师培训的课程。",
        teacher_id=teacher.id,
    )
    assert updated.name == "Python 基础进阶"
    assert updated.description == "面向教师培训的课程。"

    with pytest.raises(CoursePermissionError, match="无权访问该课程"):
        service.get_course(created.id, teacher_id=another_teacher.id)

    service.delete_course(created.id, teacher_id=teacher.id)
    with pytest.raises(CourseNotFoundError, match="课程不存在"):
        service.get_course(created.id)


def test_course_service_validates_input_and_missing_course(
    session: Session,
) -> None:
    """课程服务应拒绝空名称、无效标识和不存在的课程。"""

    teacher = add_teacher(session)
    service = CourseService(session)

    with pytest.raises(CourseValidationError, match="课程名称不能为空"):
        service.create_course(name="  ", created_by=teacher.id)

    with pytest.raises(CourseValidationError, match="课程标识无效"):
        service.get_course("不是 UUID")

    with pytest.raises(CourseNotFoundError, match="课程不存在"):
        service.update_course(
            "00000000-0000-0000-0000-000000000000",
            name="新课程",
        )


def test_course_service_can_clear_description(session: Session) -> None:
    """课程描述传入空白文本时应被规范化为空值。"""

    teacher = add_teacher(session)
    service = CourseService(session)
    created = service.create_course(
        name="待整理课程",
        description="临时描述",
        created_by=teacher.id,
    )

    updated = service.update_course(created.id, description="   ")

    assert updated.description is None
    persisted = session.get(Course, UUID(created.id))
    assert persisted is not None
    assert persisted.description is None
