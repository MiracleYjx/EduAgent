"""认证依赖、Bearer 令牌处理和 RBAC 守卫。"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import SecretStr
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware

from backend.app.core.database import get_db
from backend.app.domain.enums import UserRole
from backend.app.domain.permissions import (
    PERMISSION_DISPLAY_NAMES,
    ROLE_DISPLAY_NAMES,
    Permission,
    PermissionDeniedError,
    normalize_permission,
    normalize_role,
)
from backend.app.domain.permissions import (
    require_permission as check_permission,
)
from backend.app.models import User
from backend.app.services.auth_service import (
    AuthenticationError,
    AuthService,
    InvalidTokenError,
)

bearer_scheme = HTTPBearer(
    auto_error=False,
    description="登录后返回的 Bearer JWT 访问令牌。",
)
# 保留 OAuth2 命名，便于后续端点按标准 Depends/Security 方式接入。
oauth2_scheme = bearer_scheme


def _coerce_secret(value: Any) -> str | SecretStr | None:
    """把配置中的密钥转换为安全输入，空值统一视为未配置。"""

    if isinstance(value, SecretStr):
        return value if value.get_secret_value().strip() else None
    if isinstance(value, str):
        return value if value.strip() else None
    return None


def get_jwt_secret_key(request: Request) -> str | SecretStr | None:
    """从应用状态、运行配置或环境变量读取 JWT 密钥。"""

    app_state_key = _coerce_secret(getattr(request.app.state, "jwt_secret_key", None))
    if app_state_key is not None:
        return app_state_key

    settings = getattr(request.app.state, "settings", None)
    for field_name in ("jwt_secret_key", "JWT_SECRET_KEY"):
        settings_key = _coerce_secret(getattr(settings, field_name, None))
        if settings_key is not None:
            return settings_key

    return _coerce_secret(os.getenv("JWT_SECRET_KEY"))


def get_auth_service(
    session: Annotated[Session, Depends(get_db)],
    secret_key: Annotated[str | SecretStr | None, Depends(get_jwt_secret_key)],
) -> AuthService:
    """创建使用当前请求数据库会话和 JWT 配置的认证服务。"""

    return AuthService(session, secret_key=secret_key)


def authentication_http_exception(exc: AuthenticationError) -> HTTPException:
    """把认证服务异常转换为不泄露内部细节的 HTTP 响应。"""

    message = str(exc).strip()
    if "JWT 密钥未配置" in message:
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="认证服务暂时不可用，请联系管理员。",
        )

    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=message or "认证凭证无效。",
        headers={"WWW-Authenticate": "Bearer"},
    )


def extract_bearer_token(authorization: str | None) -> str | None:
    """从 Authorization 请求头提取严格格式的 Bearer 令牌。"""

    parts = (authorization or "").strip().split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1]


class AuthenticationMiddleware(BaseHTTPMiddleware):
    """把请求中的令牌放入 request.state，认证依赖负责最终校验。"""

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Any:
        """保存令牌上下文，不拦截公开端点或重复查询数据库。"""

        token = extract_bearer_token(request.headers.get("Authorization"))
        request.state.bearer_token = token
        request.state.auth_token = token
        return await call_next(request)


AuthMiddleware = AuthenticationMiddleware


def get_current_user(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Security(bearer_scheme)
    ],
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> User:
    """解析 Bearer JWT 并加载仍处于启用状态的当前用户。"""

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _authentication_error("请提供 Bearer 认证凭证。")

    try:
        return auth_service.get_current_user(credentials.credentials)
    except InvalidTokenError as exc:
        raise authentication_http_exception(exc) from None
    except AuthenticationError as exc:
        raise authentication_http_exception(exc) from None


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_current_active_user(user: CurrentUser) -> User:
    """提供显式的启用用户依赖，供业务端点复用。"""

    if not user.is_active:
        raise _authentication_error("账户已停用。")
    return user


def _authentication_error(detail: str) -> HTTPException:
    """构造统一的中文未认证响应。"""

    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_user_roles(user: User) -> frozenset[UserRole]:
    """读取并规范化用户角色，未知值不会获得任何权限。"""

    raw_roles: Any = getattr(user, "roles", ())
    if isinstance(raw_roles, (str, UserRole)):
        raw_roles = (raw_roles,)

    normalized: set[UserRole] = set()
    for role in raw_roles or ():
        candidate = getattr(role, "name", role)
        try:
            normalized.add(normalize_role(candidate))
        except (TypeError, ValueError):
            # 角色数据异常时按无权限处理，避免意外放行。
            continue
    return frozenset(normalized)


def get_current_roles(user: CurrentUser) -> tuple[UserRole, ...]:
    """返回当前用户的稳定排序角色列表。"""

    return tuple(sorted(get_user_roles(user), key=lambda role: role.value))


CurrentRoles = Annotated[tuple[UserRole, ...], Depends(get_current_roles)]


def _normalize_role_collection(
    roles: Iterable[UserRole | str] | UserRole | str,
) -> frozenset[UserRole]:
    """兼容单角色、角色序列和角色枚举输入。"""

    if isinstance(roles, (str, UserRole)):
        roles = (roles,)
    normalized = frozenset(normalize_role(role) for role in roles)
    if not normalized:
        raise ValueError("角色守卫至少需要一个允许角色。")
    return normalized


class RoleGuard:
    """只允许至少拥有一个指定角色的当前用户通过。"""

    def __init__(
        self,
        allowed_roles: Iterable[UserRole | str] | UserRole | str,
    ) -> None:
        self.allowed_roles = _normalize_role_collection(allowed_roles)

    def __call__(self, user: CurrentUser) -> User:
        """执行角色检查并返回当前用户。"""

        if not self.allowed_roles.intersection(get_user_roles(user)):
            names = "、".join(
                ROLE_DISPLAY_NAMES[role]
                for role in sorted(self.allowed_roles, key=lambda item: item.value)
            )
            raise _authorization_error(f"当前用户无权访问此资源，需要角色：{names}。")
        return user


RoleChecker = RoleGuard


def require_roles(*allowed_roles: UserRole | str) -> RoleGuard:
    """创建允许任一指定角色的 FastAPI 依赖。"""

    if len(allowed_roles) == 1 and not isinstance(allowed_roles[0], (str, UserRole)):
        return RoleGuard(allowed_roles[0])
    return RoleGuard(allowed_roles)


def require_role(role: UserRole | str) -> RoleGuard:
    """创建单角色 FastAPI 依赖。"""

    return RoleGuard(role)


class PermissionGuard:
    """根据领域权限映射执行 FastAPI 权限检查。"""

    def __init__(self, permission: Permission | str) -> None:
        self.permission = normalize_permission(permission)

    def __call__(self, user: CurrentUser) -> User:
        """检查当前用户权限并返回当前用户。"""

        roles = get_user_roles(user)
        try:
            check_permission(roles, self.permission)
        except PermissionDeniedError as exc:
            detail = str(exc)
            if not detail:
                detail = (
                    f"当前用户无权执行“"
                    f"{PERMISSION_DISPLAY_NAMES[self.permission]}”。"
                )
            raise _authorization_error(detail) from exc
        except ValueError as exc:
            raise _authorization_error("权限配置无效。") from exc
        return user


PermissionChecker = PermissionGuard


def require_permission(permission: Permission | str) -> PermissionGuard:
    """创建基于领域权限的 FastAPI 依赖。"""

    return PermissionGuard(permission)


permission_required = require_permission


def require_any_permission(
    permissions: Iterable[Permission | str],
) -> Callable[..., User]:
    """创建允许权限列表中任一权限通过的 FastAPI 依赖。"""

    normalized_permissions = tuple(normalize_permission(item) for item in permissions)
    if not normalized_permissions:
        raise ValueError("权限守卫至少需要一个权限。")

    def dependency(user: CurrentUser) -> User:
        """检查当前用户是否拥有任一指定权限。"""

        roles = get_user_roles(user)
        for permission in normalized_permissions:
            try:
                check_permission(roles, permission)
            except PermissionDeniedError:
                continue
            return user

        detail = (
            f"当前用户无权执行“"
            f"{PERMISSION_DISPLAY_NAMES[normalized_permissions[0]]}”。"
        )
        raise _authorization_error(detail)

    return dependency


def _authorization_error(detail: str) -> HTTPException:
    """构造统一的中文越权响应。"""

    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


__all__ = [
    "AuthMiddleware",
    "AuthenticationMiddleware",
    "CurrentRoles",
    "CurrentUser",
    "PermissionChecker",
    "PermissionGuard",
    "RoleChecker",
    "RoleGuard",
    "authentication_http_exception",
    "bearer_scheme",
    "extract_bearer_token",
    "get_auth_service",
    "get_current_active_user",
    "get_current_roles",
    "get_current_user",
    "get_jwt_secret_key",
    "get_user_roles",
    "oauth2_scheme",
    "permission_required",
    "require_any_permission",
    "require_permission",
    "require_role",
    "require_roles",
]
