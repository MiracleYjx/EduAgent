"""T017 认证与 RBAC 契约测试：覆盖登录、无效令牌与三角色边界。"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.core.database import Base
from backend.app.domain.enums import UserRole
from backend.app.domain.permissions import (
    PERMISSION_DISPLAY_NAMES,
    ROLE_DISPLAY_NAMES,
    Permission,
    PermissionDeniedError,
    has_permission,
    require_permission,
)
from backend.app.models import Role, User
from backend.app.services.auth_service import (
    AuthenticationError,
    AuthService,
    InvalidTokenError,
    create_access_token,
    hash_password,
)

SECRET = "test-jwt-secret-that-is-not-a-production-secret"


@pytest.fixture
def session() -> Generator[Session, None, None]:
    """为认证契约测试提供隔离的 SQLite 会话。"""

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session
    engine.dispose()


def seed_user(
    session: Session,
    *,
    username: str,
    email: str,
    role: UserRole,
    password: str = "正确密码",
    active: bool = True,
) -> User:
    """创建带角色的测试用户，用于登录和令牌契约验证。"""

    user = User(
        username=username,
        email=email,
        password_hash=hash_password(password),
        is_active=active,
    )
    user.roles.append(Role(name=role))
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def test_login_contract_accepts_username_or_email_and_rejects_bad_credentials(
    session: Session,
) -> None:
    """登录契约要求用户名和邮箱都可用，错误凭证必须被拒绝。"""

    user = seed_user(
        session,
        username="teacher",
        email="teacher@example.com",
        role=UserRole.TEACHER,
    )
    service = AuthService(session, secret_key=SECRET)

    assert service.authenticate("teacher", "正确密码").id == user.id
    assert service.authenticate("teacher@example.com", "正确密码").id == user.id

    for identifier, password in [("teacher", "错误密码"), ("unknown", "正确密码")]:
        with pytest.raises(AuthenticationError, match="用户名、邮箱或密码错误"):
            service.authenticate(identifier, password)


def test_token_contract_rejects_tampering_expiration_and_disabled_users(
    session: Session,
) -> None:
    """无效令牌、过期令牌和停用账户都必须失败，且错误要安全。"""

    user = seed_user(
        session,
        username="teacher",
        email="teacher@example.com",
        role=UserRole.TEACHER,
    )
    service = AuthService(session, secret_key=SECRET)
    issued_token = service.issue_access_token(user)

    tampered_token = f"{issued_token[:-1]}{'a' if issued_token[-1] != 'a' else 'b'}"
    with pytest.raises(InvalidTokenError, match="认证凭证无效"):
        service.get_current_user(tampered_token)

    expired_token = create_access_token(
        user.id,
        secret_key=SECRET,
        roles=[UserRole.TEACHER],
        expires_delta=timedelta(seconds=-1),
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    with pytest.raises(InvalidTokenError, match="已过期"):
        service.get_current_user(expired_token)

    user.is_active = False
    session.commit()
    with pytest.raises(AuthenticationError, match="账户已停用"):
        service.get_current_user(issued_token)


@pytest.mark.parametrize(
    ("role", "allowed_permission", "denied_permission"),
    [
        (
            UserRole.TEACHER,
            Permission.REVIEW_LOW_CONFIDENCE_GRADING,
            Permission.MANAGE_USERS,
        ),
        (
            UserRole.STUDENT,
            Permission.SUBMIT_EXAMS,
            Permission.REVIEW_LOW_CONFIDENCE_GRADING,
        ),
        (
            UserRole.ADMIN,
            Permission.MANAGE_USERS,
            Permission.CREATE_EXAMS,
        ),
    ],
)
def test_rbac_contract_distinguishes_teacher_student_and_admin_boundaries(
    role: UserRole,
    allowed_permission: Permission,
    denied_permission: Permission,
) -> None:
    """三种角色的权限边界必须清晰，越权请求要返回中文拒绝提示。"""

    assert has_permission(role, allowed_permission)
    assert not has_permission(role, denied_permission)
    require_permission(role, allowed_permission)

    expected_message = f"{ROLE_DISPLAY_NAMES[role]}无权执行“{PERMISSION_DISPLAY_NAMES[denied_permission]}”。"
    with pytest.raises(PermissionDeniedError, match=expected_message):
        require_permission(role, denied_permission)
