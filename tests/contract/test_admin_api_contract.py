"""T021 管理员 API 契约测试：验证 RBAC、用户/角色管理和运行状态。"""

from __future__ import annotations

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import RedisError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.api.admin import get_admin_service
from backend.app.core.app import create_app
from backend.app.core.database import Base, get_db
from backend.app.domain.enums import UserRole
from backend.app.models import Role, User
from backend.app.services.admin_service import AdminService
from backend.app.services.auth_service import create_access_token, hash_password
from tests.unit.settings_helpers import build_test_settings

SECRET = "test-jwt-secret-that-is-not-a-production-secret"


class DummyRedis:
    """提供可控健康状态的 Redis 测试替身。"""

    def __init__(self, *, healthy: bool = True) -> None:
        self.healthy = healthy

    def ping(self) -> bool:
        """模拟 Redis PING。"""

        if not self.healthy:
            raise RedisError("offline")
        return True


@pytest.fixture
def session() -> Generator[Session, None, None]:
    """创建隔离的管理员 API 测试数据库。"""

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
    """构造带测试数据库、JWT 和 Redis 替身的应用。"""

    application = create_app(settings=build_test_settings(JWT_SECRET_KEY=SECRET))
    application.dependency_overrides[get_db] = lambda: (yield session)
    application.dependency_overrides[get_admin_service] = lambda: AdminService(
        session,
        engine=session.get_bind(),
        redis_client=DummyRedis(),
    )
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
    user.roles.append(Role(name=role, description=role.value))
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def auth_headers(user: User) -> dict[str, str]:
    """生成测试用户的 Bearer 请求头。"""

    token = create_access_token(user.id, secret_key=SECRET, roles=[UserRole.ADMIN])
    return {"Authorization": f"Bearer {token}"}


def test_admin_api_manages_users_roles_and_status(
    app,
    session: Session,
) -> None:
    """管理员可以执行用户、角色和运行状态操作。"""

    admin = seed_user(session, username="admin", role=UserRole.ADMIN)
    client = TestClient(app)
    headers = auth_headers(admin)

    created = client.post(
        "/api/admin/users",
        headers=headers,
        json={
            "username": "student",
            "email": "student@example.com",
            "password": "学生密码",
            "roles": ["Student"],
        },
    )
    assert created.status_code == 201
    created_body = created.json()
    assert created_body["username"] == "student"
    assert created_body["roles"] == ["Student"]
    assert "password_hash" not in created_body

    user_id = created_body["id"]
    role_update = client.put(
        f"/api/admin/users/{user_id}/roles",
        headers=headers,
        json={"roles": ["Teacher", "Admin"]},
    )
    assert role_update.status_code == 200
    assert role_update.json()["roles"] == ["Teacher", "Admin"]

    disabled = client.post(
        f"/api/admin/users/{user_id}/active",
        headers=headers,
        json={"is_active": False},
    )
    assert disabled.status_code == 200
    assert disabled.json()["is_active"] is False

    roles = client.get("/api/admin/roles", headers=headers)
    assert roles.status_code == 200
    assert [item["name"] for item in roles.json()] == ["Teacher", "Student", "Admin"]

    system_status = client.get("/api/admin/status", headers=headers)
    assert system_status.status_code == 200
    assert system_status.json()["overall_healthy"] is True
    assert system_status.json()["database"]["healthy"] is True
    assert system_status.json()["redis"]["healthy"] is True

    deleted = client.delete(f"/api/admin/users/{user_id}", headers=headers)
    assert deleted.status_code == 204


@pytest.mark.parametrize("role", [UserRole.TEACHER, UserRole.STUDENT])
def test_non_admin_cannot_access_admin_api(
    app,
    session: Session,
    role: UserRole,
) -> None:
    """教师和学生访问管理员 API 时必须被拒绝。"""

    user = seed_user(session, username=role.value.lower(), role=role)
    client = TestClient(app)
    token = create_access_token(user.id, secret_key=SECRET, roles=[role])

    response = client.get(
        "/api/admin/users",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert "无权" in response.json()["detail"]


def test_admin_api_rejects_invalid_input_and_missing_user(
    app,
    session: Session,
) -> None:
    """非法输入和不存在用户应返回稳定的中文错误。"""

    admin = seed_user(session, username="admin", role=UserRole.ADMIN)
    client = TestClient(app)
    headers = auth_headers(admin)

    invalid = client.post(
        "/api/admin/users",
        headers=headers,
        json={
            "username": "bad",
            "email": "bad@example.com",
            "password": "密码",
            "roles": [],
        },
    )
    assert invalid.status_code == 422
    assert "角色" in invalid.json()["detail"][0]["msg"]

    missing = client.get(
        "/api/admin/users/00000000-0000-0000-0000-000000000000",
        headers=headers,
    )
    assert missing.status_code == 404
    assert missing.json()["detail"] == "用户不存在。"
