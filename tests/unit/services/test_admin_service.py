"""T020 管理员服务单元测试：用户管理、角色管理和系统运行状态。"""

from __future__ import annotations

from collections.abc import Generator

import pytest
from redis.exceptions import RedisError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.core.database import Base
from backend.app.domain.enums import UserRole
from backend.app.models import Role, User
from backend.app.services.admin_service import (
    AdminConflictError,
    AdminNotFoundError,
    AdminService,
    AdminValidationError,
)
from backend.app.services.auth_service import hash_password


class DummyRedis:
    """用于单元测试的轻量 Redis 组件。"""

    def __init__(self, *, healthy: bool = True) -> None:
        self.healthy = healthy

    def ping(self) -> bool:
        if not self.healthy:
            raise RedisError("offline")
        return True


@pytest.fixture
def session() -> Generator[Session, None, None]:
    """创建隔离的管理员服务测试数据库会话。"""

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
    *,
    username: str = "teacher",
    email: str = "teacher@example.com",
    password: str = "正确密码",
    active: bool = True,
    roles: tuple[UserRole, ...] = (UserRole.TEACHER,),
) -> User:
    """向测试库中写入一个可复用的用户。"""

    user = User(
        username=username,
        email=email,
        password_hash=hash_password(password),
        is_active=active,
    )
    user.roles.extend(Role(name=role, description=role.value) for role in roles)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def build_service(session: Session, *, healthy_redis: bool = True) -> AdminService:
    """构造带可控 Redis 组件的管理员服务。"""

    return AdminService(
        session,
        engine=session.get_bind(),
        redis_client=DummyRedis(healthy=healthy_redis),
    )


def test_create_user_set_roles_and_get_user_summary(session: Session) -> None:
    """管理员可以创建用户、替换角色并读取脱敏摘要。"""

    service = build_service(session)

    created = service.create_user(
        username="admin-teacher",
        email="admin-teacher@example.com",
        password="初始密码",
        roles=[UserRole.TEACHER, UserRole.ADMIN],
    )

    assert created.username == "admin-teacher"
    assert created.roles == ("Teacher", "Admin")
    assert created.is_active is True

    updated = service.update_user(created.id, email="new@example.com", is_active=False)
    assert updated.email == "new@example.com"
    assert updated.is_active is False

    user = service.get_user(created.id)
    assert user.id == created.id
    assert user.username == "admin-teacher"


def test_role_operations_grant_revoke_and_list_roles(session: Session) -> None:
    """管理员可以查看角色、补齐角色并调整用户角色。"""

    service = build_service(session)
    user = add_user(
        session,
        username="student",
        email="student@example.com",
        roles=(UserRole.STUDENT,),
    )

    role = service.ensure_role(UserRole.ADMIN)
    assert role.name == UserRole.ADMIN
    assert role.description == "管理员"

    roles_before = service.list_roles()
    assert [item.name for item in roles_before] == [
        UserRole.TEACHER,
        UserRole.STUDENT,
        UserRole.ADMIN,
    ]

    promoted = service.grant_role(user.id, UserRole.ADMIN)
    assert promoted.roles == ("Student", "Admin")

    replaced = service.set_user_roles(user.id, [UserRole.TEACHER])
    assert replaced.roles == ("Teacher",)

    with pytest.raises(AdminValidationError, match="用户至少需要一个角色"):
        service.revoke_role(user.id, UserRole.TEACHER)


def test_duplicate_and_missing_user_operations_raise_safe_errors(
    session: Session,
) -> None:
    """重复用户和缺失用户都必须返回可理解的安全异常。"""

    service = build_service(session)
    user = add_user(session)

    with pytest.raises(AdminConflictError, match="用户名或邮箱已存在"):
        service.create_user(
            username=user.username,
            email="another@example.com",
            password="初始密码",
            roles=UserRole.TEACHER,
        )

    with pytest.raises(AdminNotFoundError, match="用户不存在"):
        service.get_user("00000000-0000-0000-0000-000000000000")

    with pytest.raises(AdminValidationError, match="用户标识无效"):
        service.set_user_active("not-a-uuid", True)


def test_get_system_status_reports_database_and_redis_health(
    session: Session,
) -> None:
    """系统状态应汇总数据库、Redis 和用户/角色统计。"""

    add_user(session)
    service = build_service(session)

    status = service.get_system_status()

    assert status.service_name == "backend"
    assert status.overall_healthy is True
    assert status.database.healthy is True
    assert status.redis.healthy is True
    assert status.user_count == 1
    assert status.active_user_count == 1
    assert status.role_count == 3
    assert status.users_by_role == {"Teacher": 1, "Student": 0, "Admin": 0}


def test_get_system_status_handles_redis_failure(session: Session) -> None:
    """Redis 故障时系统状态仍应返回安全的降级快照。"""

    add_user(session)
    service = build_service(session, healthy_redis=False)

    status = service.get_system_status()

    assert status.overall_healthy is False
    assert status.redis.healthy is False
    assert "Redis" in status.redis.detail or "redis" in status.redis.detail.lower()
