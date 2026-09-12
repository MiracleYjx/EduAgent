"""认证服务：密码凭证、JWT 会话令牌和当前用户加载。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from backend.app.core.config import get_settings
from backend.app.domain.enums import UserRole
from backend.app.domain.permissions import ROLE_DISPLAY_NAMES
from backend.app.models import Role, User

_PASSWORD_ALGORITHM = "scrypt"
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_HASH_BYTES = 32
_MAX_PASSWORD_LENGTH = 1024
_JWT_ALGORITHM = "HS256"
_JWT_TYPE = "access"
_DEFAULT_ACCESS_TOKEN_LIFETIME = timedelta(minutes=60)

# 这些标识仅用于本地开发模式，便于上线前按前缀检索并清理。
_DEV_MODE_ACCOUNT_SPECS: tuple[tuple[UserRole, str, str], ...] = (
    (UserRole.ADMIN, "dev_admin", "dev_admin@eduagent.local"),
    (UserRole.TEACHER, "dev_teacher", "dev_teacher@eduagent.local"),
    (UserRole.STUDENT, "dev_student", "dev_student@eduagent.local"),
)
_DEV_MODE_TRUE_VALUES = frozenset({"1", "true", "t", "yes", "y", "on"})
_DEV_MODE_FALSE_VALUES = frozenset({"0", "false", "f", "no", "n", "off", ""})


class AuthenticationError(RuntimeError):
    """认证失败或认证配置无效时抛出的安全异常。"""


class InvalidTokenError(AuthenticationError):
    """JWT 格式、签名或有效期校验失败。"""


def _coerce_dev_mode(value: Any) -> bool:
    """将开发模式配置转换为严格布尔值。"""

    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _DEV_MODE_TRUE_VALUES:
            return True
        if normalized in _DEV_MODE_FALSE_VALUES:
            return False
    raise ValueError("DEV_MODE 配置值无效，请使用 true 或 false。")


def _resolve_dev_mode(
    dev_mode: bool | str | int | None,
    settings: Any | None,
) -> bool:
    """按显式参数、运行配置或环境变量解析开发模式开关。"""

    if settings is not None:
        configured = getattr(settings, "DEV_MODE", None)
        if configured is None:
            configured = getattr(settings, "dev_mode", None)
        if configured is not None:
            return _coerce_dev_mode(configured)
    if dev_mode is not None:
        return _coerce_dev_mode(dev_mode)
    return _coerce_dev_mode(os.getenv("DEV_MODE", "false"))


def _ensure_dev_mode_role(session: Session, role: UserRole) -> Role:
    """读取或创建一个内置角色记录。"""

    role_model = session.scalar(select(Role).where(Role.name == role))
    if role_model is not None:
        if not role_model.description:
            role_model.description = ROLE_DISPLAY_NAMES[role]
        return role_model

    role_model = Role(name=role, description=ROLE_DISPLAY_NAMES[role])
    session.add(role_model)
    session.flush()
    return role_model


def _ensure_dev_mode_account(
    session: Session,
    role: UserRole,
    username: str,
    email: str,
) -> User:
    """幂等准备单个开发账号，不覆盖标识冲突的真实账号。"""

    matches = session.scalars(
        select(User)
        .options(selectinload(User.roles))
        .where(or_(User.username == username, User.email == email))
    ).all()
    unique_matches = {str(user.id): user for user in matches}
    if len(unique_matches) > 1:
        raise AuthenticationError(f"开发模式账号标识冲突：{username}。")

    user = next(iter(unique_matches.values()), None)
    if user is not None:
        if user.username != username or user.email != email:
            raise AuthenticationError(f"开发模式账号标识已被占用：{username}。")
    else:
        # 只在创建时生成一次随机密码；数据库中永远只保存哈希。
        user = User(
            username=username,
            email=email,
            password_hash=hash_password(secrets.token_urlsafe(32)),
            is_active=True,
        )
        session.add(user)

    user.is_active = True
    role_model = _ensure_dev_mode_role(session, role)
    user.roles = [role_model]
    session.flush()
    return user


def ensure_dev_mode_accounts(
    session: Session,
    dev_mode: bool | str | int | None = None,
    *,
    settings: Any | None = None,
) -> dict[UserRole, User]:
    """仅在开发模式开启时幂等创建或复用三个预设测试账号。

    返回值按角色映射到用户实体，供 Gradio 使用 ``issue_access_token``
    签发标准 JWT。关闭开发模式时不执行任何数据库查询或写入。
    """

    if not _resolve_dev_mode(dev_mode, settings):
        return {}

    accounts: dict[UserRole, User] = {}
    try:
        for role, username, email in _DEV_MODE_ACCOUNT_SPECS:
            accounts[role] = _ensure_dev_mode_account(
                session,
                role,
                username,
                email,
            )
        session.commit()
        for account in accounts.values():
            session.refresh(account)
    except SQLAlchemyError:
        session.rollback()
        raise
    return accounts


def _validate_password(password: str) -> str:
    """校验密码输入，避免空凭证和过大的哈希计算请求。"""

    if not isinstance(password, str) or not password:
        raise ValueError("密码不能为空。")
    if len(password) > _MAX_PASSWORD_LENGTH:
        raise ValueError("密码长度不能超过 1024 个字符。")
    return password


def _derive_password_key(
    password: str, salt: bytes, *, n: int, r: int, p: int
) -> bytes:
    """使用受控参数计算密码派生密钥。"""

    return hashlib.scrypt(
        _validate_password(password).encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=_HASH_BYTES,
    )


def hash_password(password: str) -> str:
    """使用随机盐生成不可逆密码哈希，不保存密码明文。"""

    salt = secrets.token_bytes(_SALT_BYTES)
    derived_key = _derive_password_key(
        password,
        salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
    )
    encoded_salt = _urlsafe_encode(salt)
    encoded_key = _urlsafe_encode(derived_key)
    parameters = f"ln={_SCRYPT_N.bit_length() - 1},r={_SCRYPT_R},p={_SCRYPT_P}"
    return f"{_PASSWORD_ALGORITHM}${parameters}${encoded_salt}${encoded_key}"


def verify_password(password: str, encoded_hash: str) -> bool:
    """校验密码哈希；格式异常按校验失败处理，不向调用方暴露细节。"""

    if not isinstance(encoded_hash, str):
        return False

    try:
        algorithm, parameter_text, encoded_salt, encoded_key = encoded_hash.split(
            "$", 3
        )
        if algorithm != _PASSWORD_ALGORITHM:
            return False
        parameters = dict(item.split("=", 1) for item in parameter_text.split(","))
        log_n = int(parameters["ln"])
        r = int(parameters["r"])
        p = int(parameters["p"])
        salt = _urlsafe_decode(encoded_salt)
        expected_key = _urlsafe_decode(encoded_key)
        if not 10 <= log_n <= 20 or not 1 <= r <= 32 or not 1 <= p <= 8:
            return False
        if len(salt) < 8 or len(expected_key) != _HASH_BYTES:
            return False
        n = 2**log_n
        actual_key = _derive_password_key(password, salt, n=n, r=r, p=p)
    except (KeyError, TypeError, ValueError, binascii.Error):
        return False

    return hmac.compare_digest(actual_key, expected_key)


def _urlsafe_encode(value: bytes) -> str:
    """编码 JWT 或密码字段使用的无填充 Base64URL。"""

    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _urlsafe_decode(value: str) -> bytes:
    """解码无填充 Base64URL，并拒绝空值。"""

    if not value:
        raise ValueError("编码值不能为空。")
    padding = "=" * (-len(value) % 4)
    decoded = base64.b64decode(
        value + padding,
        altchars=b"-_",
        validate=True,
    )
    # 拒绝末尾填充位被篡改但仍能解码成相同字节的非规范编码。
    if _urlsafe_encode(decoded) != value.rstrip("="):
        raise ValueError("Base64URL 编码不规范。")
    return decoded


def _resolve_secret_key(secret_key: str | SecretStr | None) -> bytes:
    """解析显式传入的密钥，缺省时读取经过校验的类型化配置。"""

    if isinstance(secret_key, SecretStr):
        value = secret_key.get_secret_value()
    elif secret_key is None:
        value = get_settings().JWT_SECRET_KEY.get_secret_value()
    else:
        value = secret_key
    if not isinstance(value, str) or not value.strip():
        raise AuthenticationError("JWT 密钥未配置，请设置 JWT_SECRET_KEY。")
    return value.encode("utf-8")


def _as_utc(moment: datetime | None) -> datetime:
    """将时间统一为带时区的 UTC 时间。"""

    if moment is None:
        return datetime.now(UTC)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _normalize_subject(subject: UUID | str) -> str:
    """规范化 JWT 主体标识，避免签发空身份令牌。"""

    value = str(subject).strip()
    if not value:
        raise ValueError("JWT 主体标识不能为空。")
    return value


def _normalize_roles(roles: Iterable[UserRole | str]) -> list[str]:
    """规范化令牌中的角色值，保持与领域枚举一致。"""

    normalized: list[str] = []
    for role in roles:
        candidate = str(role).strip()
        for supported_role in UserRole:
            if candidate in {supported_role.name, supported_role.value}:
                normalized.append(supported_role.value)
                break
        else:
            raise ValueError(f"未知角色：{candidate}")
    return list(dict.fromkeys(normalized))


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    """使用稳定 JSON 编码生成待签名字节。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def create_access_token(
    subject: UUID | str | Mapping[str, Any],
    secret_key: str | SecretStr | None = None,
    *,
    roles: Iterable[UserRole | str] = (),
    expires_delta: timedelta | None = None,
    now: datetime | None = None,
) -> str:
    """创建 HS256 JWT 访问令牌。"""

    if isinstance(subject, Mapping):
        claims = dict(subject)
        raw_subject = claims.pop("sub", None)
        if raw_subject is None:
            raise ValueError("JWT 主体标识不能为空。")
        requested_roles = claims.pop("roles", roles)
    else:
        claims = {}
        raw_subject = subject
        requested_roles = roles

    issued_at = _as_utc(now)
    lifetime = expires_delta
    if lifetime is None:
        lifetime = (
            timedelta(minutes=get_settings().JWT_EXPIRE_MINUTES)
            if secret_key is None
            else _DEFAULT_ACCESS_TOKEN_LIFETIME
        )
    expires_at = issued_at + lifetime
    payload: dict[str, Any] = {
        **claims,
        "sub": _normalize_subject(raw_subject),
        "roles": _normalize_roles(requested_roles),
        "type": _JWT_TYPE,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": secrets.token_urlsafe(16),
    }
    header = {"alg": _JWT_ALGORITHM, "typ": "JWT"}
    encoded_header = _urlsafe_encode(_json_bytes(header))
    encoded_payload = _urlsafe_encode(_json_bytes(payload))
    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    signature = hmac.new(
        _resolve_secret_key(secret_key), signing_input, hashlib.sha256
    ).digest()
    return f"{encoded_header}.{encoded_payload}.{_urlsafe_encode(signature)}"


def _invalid_token() -> InvalidTokenError:
    """返回统一的令牌错误，避免把解析细节暴露给调用方。"""

    return InvalidTokenError("认证凭证无效。")


def decode_access_token(
    token: str,
    secret_key: str | SecretStr | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """验证并解析 HS256 JWT 访问令牌。"""

    if not isinstance(token, str):
        raise _invalid_token()
    parts = token.split(".")
    if len(parts) != 3:
        raise _invalid_token()

    try:
        encoded_header, encoded_payload, encoded_signature = parts
        header = json.loads(_urlsafe_decode(encoded_header))
        payload = json.loads(_urlsafe_decode(encoded_payload))
        signature = _urlsafe_decode(encoded_signature)
        if not isinstance(header, dict) or header.get("alg") != _JWT_ALGORITHM:
            raise _invalid_token()
        if header.get("typ") != "JWT" or not isinstance(payload, dict):
            raise _invalid_token()
        signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
        expected_signature = hmac.new(
            _resolve_secret_key(secret_key), signing_input, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(signature, expected_signature):
            raise _invalid_token()
        subject = payload.get("sub")
        issued_at = payload.get("iat")
        expires_at = payload.get("exp")
        if (
            not isinstance(subject, str)
            or not subject.strip()
            or payload.get("type") != _JWT_TYPE
            or isinstance(issued_at, bool)
            or not isinstance(issued_at, int)
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, int)
        ):
            raise _invalid_token()
        roles = payload.get("roles", [])
        if not isinstance(roles, list) or not all(
            isinstance(role, str) for role in roles
        ):
            raise _invalid_token()
    except InvalidTokenError:
        raise
    except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError):
        raise _invalid_token() from None

    current_timestamp = int(_as_utc(now).timestamp())
    if expires_at <= current_timestamp:
        raise InvalidTokenError("认证凭证已过期。")
    return payload


class AuthService:
    """面向数据库会话的认证业务服务。"""

    def __init__(
        self,
        session: Session,
        secret_key: str | SecretStr | None = None,
        *,
        access_token_lifetime: timedelta | None = None,
    ) -> None:
        self.session = session
        self.secret_key = secret_key
        self.access_token_lifetime = access_token_lifetime

    def authenticate(self, identifier: str, password: str) -> User:
        """使用用户名或邮箱校验用户密码并返回当前用户。"""

        normalized_identifier = identifier.strip()
        user = self.session.scalar(
            select(User).where(
                or_(
                    User.username == normalized_identifier,
                    User.email == normalized_identifier,
                )
            )
        )
        if user is None:
            raise AuthenticationError("用户名、邮箱或密码错误。")
        if not user.is_active:
            raise AuthenticationError("账户已停用。")
        if not verify_password(password, user.password_hash):
            raise AuthenticationError("用户名、邮箱或密码错误。")
        return user

    def issue_access_token(self, user: User) -> str:
        """为已加载的用户签发访问令牌。"""

        return create_access_token(
            user.id,
            self.secret_key,
            roles=[role.name for role in user.roles],
            expires_delta=self.access_token_lifetime,
        )

    def get_current_user(self, token: str) -> User:
        """验证访问令牌并从数据库加载当前用户。"""

        payload = decode_access_token(token, self.secret_key)
        try:
            user_id = UUID(payload["sub"])
        except (KeyError, TypeError, ValueError):
            raise _invalid_token() from None
        user = self.session.get(User, user_id)
        if user is None:
            raise InvalidTokenError("认证凭证对应的用户不存在。")
        if not user.is_active:
            raise AuthenticationError("账户已停用。")
        return user


def authenticate_user(session: Session, identifier: str, password: str) -> User:
    """使用环境中的 JWT 配置完成用户名/邮箱认证。"""

    return AuthService(session).authenticate(identifier, password)


def load_current_user(session: Session, token: str) -> User:
    """使用环境中的 JWT 配置加载当前用户。"""

    return AuthService(session).get_current_user(token)


get_password_hash = hash_password


__all__ = [
    "AuthService",
    "AuthenticationError",
    "InvalidTokenError",
    "authenticate_user",
    "create_access_token",
    "decode_access_token",
    "ensure_dev_mode_accounts",
    "get_password_hash",
    "hash_password",
    "load_current_user",
    "verify_password",
]
