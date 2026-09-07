"""T016 认证服务的失败优先单元测试。"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.core.database import Base
from backend.app.domain.enums import UserRole
from backend.app.models import Role, User
from backend.app.services.auth_service import (
    AuthenticationError,
    AuthService,
    InvalidTokenError,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)

SECRET = "test-jwt-secret-that-is-not-a-production-secret"


@pytest.fixture
def session() -> Generator[Session, None, None]:
    """创建隔离的认证测试数据库会话。"""

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session
    engine.dispose()


def add_user(session: Session, *, active: bool = True) -> User:
    """向测试数据库写入带角色的用户。"""

    user = User(
        username="teacher",
        email="teacher@example.com",
        password_hash=hash_password("正确密码"),
        is_active=active,
    )
    user.roles.append(Role(name=UserRole.TEACHER))
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def test_password_hash_is_salted_and_verifiable() -> None:
    """密码哈希不得保存明文，并且相同密码应能完成校验。"""

    first_hash = hash_password("正确密码")
    second_hash = hash_password("正确密码")

    assert first_hash != second_hash
    assert "正确密码" not in first_hash
    assert verify_password("正确密码", first_hash) is True
    assert verify_password("错误密码", first_hash) is False
    assert verify_password("正确密码", "invalid-hash") is False


def test_access_token_contains_identity_and_expiration() -> None:
    """JWT 应包含身份、角色、类型和有效期声明。"""

    user_id = uuid4()
    token = create_access_token(
        user_id,
        secret_key=SECRET,
        roles=[UserRole.TEACHER],
        expires_delta=timedelta(minutes=15),
    )

    payload = decode_access_token(token, secret_key=SECRET)

    assert payload["sub"] == str(user_id)
    assert payload["roles"] == [UserRole.TEACHER.value]
    assert payload["type"] == "access"
    assert payload["exp"] > payload["iat"]


def test_tampered_and_expired_tokens_are_rejected() -> None:
    """签名不匹配或已过期的 JWT 必须拒绝。"""

    token = create_access_token(
        uuid4(),
        secret_key=SECRET,
        expires_delta=timedelta(minutes=1),
    )
    tampered = f"{token[:-1]}{'a' if token[-1] != 'a' else 'b'}"

    with pytest.raises(InvalidTokenError, match="认证凭证无效"):
        decode_access_token(tampered, secret_key=SECRET)

    expired = create_access_token(
        uuid4(),
        secret_key=SECRET,
        expires_delta=timedelta(seconds=-1),
    )
    with pytest.raises(InvalidTokenError, match="已过期"):
        decode_access_token(expired, secret_key=SECRET)


def test_authenticate_user_accepts_username_or_email(session: Session) -> None:
    """登录可以使用用户名或邮箱，失败时返回统一中文提示。"""

    user = add_user(session)
    service = AuthService(session, secret_key=SECRET)

    assert service.authenticate("teacher", "正确密码").id == user.id
    assert service.authenticate("teacher@example.com", "正确密码").id == user.id

    with pytest.raises(AuthenticationError, match="用户名、邮箱或密码错误"):
        service.authenticate("teacher", "错误密码")


def test_inactive_user_and_current_user_loading(session: Session) -> None:
    """停用用户不能登录，合法 JWT 可以加载当前用户。"""

    user = add_user(session)
    service = AuthService(session, secret_key=SECRET)
    token = service.issue_access_token(user)

    assert service.get_current_user(token).id == user.id

    user.is_active = False
    session.commit()
    with pytest.raises(AuthenticationError, match="账户已停用"):
        service.authenticate("teacher", "正确密码")
    with pytest.raises(AuthenticationError, match="账户已停用"):
        service.get_current_user(token)


def test_token_time_validation_can_use_explicit_clock() -> None:
    """令牌验证应依据 UTC 时间，便于稳定测试过期边界。"""

    issued_at = datetime(2026, 1, 1, tzinfo=UTC)
    token = create_access_token(
        uuid4(),
        secret_key=SECRET,
        expires_delta=timedelta(minutes=5),
        now=issued_at,
    )

    assert decode_access_token(
        token,
        secret_key=SECRET,
        now=issued_at + timedelta(minutes=4),
    )["exp"] > int((issued_at + timedelta(minutes=4)).timestamp())
    with pytest.raises(InvalidTokenError):
        decode_access_token(
            token,
            secret_key=SECRET,
            now=issued_at + timedelta(minutes=6),
        )
