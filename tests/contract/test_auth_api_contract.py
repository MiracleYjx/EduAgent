"""T018 认证 API 契约测试：验证 Bearer 链路和角色守卫。"""

from __future__ import annotations

from collections.abc import Generator
from datetime import timedelta

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.core.app import create_app
from backend.app.core.database import Base, get_db
from backend.app.core.security import require_permission, require_role
from backend.app.domain.enums import UserRole
from backend.app.domain.permissions import Permission
from backend.app.models import Role, User
from backend.app.services.auth_service import create_access_token, hash_password
from tests.unit.settings_helpers import build_test_settings

SECRET = "test-jwt-secret-that-is-not-a-production-secret"
TEACHER_PERMISSION_GUARD = require_permission(Permission.REVIEW_LOW_CONFIDENCE_GRADING)
ADMIN_ROLE_GUARD = require_role(UserRole.ADMIN)
TEACHER_DEPENDENCY = Depends(TEACHER_PERMISSION_GUARD)
ADMIN_DEPENDENCY = Depends(ADMIN_ROLE_GUARD)


@pytest.fixture
def session() -> Generator[Session, None, None]:
    """为 API 契约测试提供可跨 TestClient 线程使用的 SQLite 会话。"""

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
def app(session: Session):
    """构造带测试数据库和测试 JWT 密钥的应用。"""

    application = create_app(settings=build_test_settings())
    application.state.jwt_secret_key = SECRET

    def override_get_db() -> Generator[Session, None, None]:
        """将认证请求指向隔离测试会话。"""

        yield session

    application.dependency_overrides[get_db] = override_get_db
    yield application
    application.dependency_overrides.clear()


def seed_user(
    session: Session,
    *,
    username: str,
    role: UserRole,
    active: bool = True,
) -> User:
    """创建带指定角色的测试用户。"""

    user = User(
        username=username,
        email=f"{username}@example.com",
        password_hash=hash_password("正确密码"),
        is_active=active,
    )
    user.roles.append(Role(name=role))
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def login(client: TestClient, identifier: str) -> str:
    """登录并返回 Bearer 访问令牌。"""

    response = client.post(
        "/api/auth/login",
        json={"username": identifier, "password": "正确密码"},
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def test_login_accepts_json_and_form_and_me_returns_safe_user(
    app,
    session: Session,
) -> None:
    """登录端点应兼容两种常用请求格式，当前用户响应不得含密码。"""

    user = seed_user(session, username="teacher", role=UserRole.TEACHER)
    client = TestClient(app)

    json_login = client.post(
        "/api/auth/login",
        json={"username": "teacher", "password": "正确密码"},
    )
    assert json_login.status_code == 200
    assert json_login.json()["user"]["id"] == str(user.id)
    assert json_login.json()["user"]["roles"] == [UserRole.TEACHER.value]
    assert "password_hash" not in json_login.json()["user"]

    form_login = client.post(
        "/api/auth/login",
        data={"username": "teacher@example.com", "password": "正确密码"},
    )
    assert form_login.status_code == 200

    me_response = client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {json_login.json()['access_token']}"},
    )
    assert me_response.status_code == 200
    assert me_response.json()["username"] == "teacher"


def test_missing_invalid_expired_and_disabled_credentials_are_rejected(
    app,
    session: Session,
) -> None:
    """缺少、失效、过期和停用账户凭证都必须返回安全错误。"""

    user = seed_user(session, username="teacher", role=UserRole.TEACHER)
    client = TestClient(app)

    missing = client.get("/api/auth/me")
    assert missing.status_code == 401
    assert "认证" in missing.json()["detail"]

    invalid = client.get(
        "/api/auth/me",
        headers={"Authorization": "Bearer invalid-token"},
    )
    assert invalid.status_code == 401
    assert "认证" in invalid.json()["detail"]

    expired = create_access_token(
        user.id,
        secret_key=SECRET,
        roles=[UserRole.TEACHER],
        expires_delta=timedelta(seconds=-1),
    )
    expired_response = client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {expired}"},
    )
    assert expired_response.status_code == 401

    user.is_active = False
    session.commit()
    token = create_access_token(user.id, secret_key=SECRET)
    disabled_response = client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert disabled_response.status_code == 401
    assert "停用" in disabled_response.json()["detail"]


@pytest.mark.parametrize(
    ("role", "teacher_status", "admin_status"),
    [
        (UserRole.TEACHER, 200, 403),
        (UserRole.STUDENT, 403, 403),
        (UserRole.ADMIN, 403, 200),
    ],
)
def test_role_guards_enforce_teacher_and_admin_boundaries(
    app,
    session: Session,
    role: UserRole,
    teacher_status: int,
    admin_status: int,
) -> None:
    """Teacher 和 Admin 专属权限不能被其他角色越权使用。"""

    user = seed_user(session, username=role.value.lower(), role=role)

    @app.get("/contract/teacher")
    def teacher_endpoint(
        current_user: User = TEACHER_DEPENDENCY,
    ) -> dict[str, str]:
        """返回教师守卫验证结果。"""

        return {"username": current_user.username}

    @app.get("/contract/admin")
    def admin_endpoint(
        current_user: User = ADMIN_DEPENDENCY,
    ) -> dict[str, str]:
        """返回管理员守卫验证结果。"""

        return {"username": current_user.username}

    client = TestClient(app)
    token = login(client, user.username)
    headers = {"Authorization": f"Bearer {token}"}

    assert (
        client.get("/contract/teacher", headers=headers).status_code == teacher_status
    )
    assert client.get("/contract/admin", headers=headers).status_code == admin_status
