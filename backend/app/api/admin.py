"""管理员管理、角色管理和运行状态 API。"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.security import require_permission
from backend.app.domain.permissions import Permission
from backend.app.models import User
from backend.app.services.admin_service import (
    AdminConflictError,
    AdminNotFoundError,
    AdminRoleSummary,
    AdminService,
    AdminServiceError,
    AdminSystemStatus,
    AdminUserSummary,
    AdminValidationError,
)

router = APIRouter(prefix="/api/admin", tags=["管理员"])


class AdminUserCreateRequest(BaseModel):
    """管理员创建用户时使用的请求体。"""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64, description="登录用户名。")
    email: str = Field(min_length=1, max_length=255, description="用户邮箱。")
    password: str = Field(min_length=1, max_length=1024, description="初始密码。")
    roles: list[str] = Field(min_length=1, max_length=3, description="用户角色。")
    is_active: bool = Field(default=True, description="是否立即启用。")

    @field_validator("username", "email", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        """清理用户标识文本并拒绝空白输入。"""

        if not isinstance(value, str) or not value.strip():
            raise ValueError("用户名和邮箱不能为空。")
        return value.strip()

    @field_validator("roles")
    @classmethod
    def validate_roles(cls, roles: list[str]) -> list[str]:
        """清理角色值并去除重复项。"""

        normalized = [role.strip() for role in roles if isinstance(role, str)]
        normalized = list(dict.fromkeys(role for role in normalized if role))
        if not normalized:
            raise ValueError("用户至少需要一个角色。")
        return normalized


class AdminUserUpdateRequest(BaseModel):
    """管理员更新用户基础资料时使用的请求体。"""

    model_config = ConfigDict(extra="forbid")

    username: str | None = Field(default=None, min_length=1, max_length=64)
    email: str | None = Field(default=None, min_length=1, max_length=255)
    password: str | None = Field(default=None, min_length=1, max_length=1024)
    is_active: bool | None = None

    @field_validator("username", "email", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: Any) -> str | None:
        """清理可选文本字段，空白值按未提供处理。"""

        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("用户名或邮箱不能为空。")
        return value.strip()


class AdminActiveRequest(BaseModel):
    """管理员修改用户启用状态时使用的请求体。"""

    model_config = ConfigDict(extra="forbid")

    is_active: bool = Field(description="是否启用用户。")


class AdminRoleAssignmentRequest(BaseModel):
    """管理员替换用户角色时使用的请求体。"""

    model_config = ConfigDict(extra="forbid")

    roles: list[str] = Field(min_length=1, max_length=3, description="完整角色列表。")

    @field_validator("roles")
    @classmethod
    def normalize_roles(cls, roles: list[str]) -> list[str]:
        """清理角色值并拒绝空角色列表。"""

        normalized = list(
            dict.fromkeys(role.strip() for role in roles if isinstance(role, str))
        )
        if not normalized:
            raise ValueError("用户至少需要一个角色。")
        return normalized


class AdminRoleRequest(BaseModel):
    """管理员确保角色记录存在时使用的请求体。"""

    model_config = ConfigDict(extra="forbid")

    role: str = Field(min_length=1, description="角色名称。")

    @field_validator("role")
    @classmethod
    def normalize_role_text(cls, role: str) -> str:
        """清理角色名称。"""

        normalized = role.strip()
        if not normalized:
            raise ValueError("角色名称不能为空。")
        return normalized


def get_admin_service(session: Annotated[Session, Depends(get_db)]) -> AdminService:
    """创建使用当前请求数据库会话的管理员服务。"""

    return AdminService(session)


AdminServiceDependency = Annotated[AdminService, Depends(get_admin_service)]
AdminUserManager = Annotated[
    User,
    Depends(require_permission(Permission.MANAGE_USERS)),
]
AdminRoleManager = Annotated[
    User,
    Depends(require_permission(Permission.MANAGE_ROLES)),
]
AdminStatusViewer = Annotated[
    User,
    Depends(require_permission(Permission.VIEW_SYSTEM_STATUS)),
]


def _admin_http_exception(error: BaseException) -> HTTPException:
    """把管理员服务异常转换为统一的中文 HTTP 错误。"""

    if isinstance(error, AdminNotFoundError):
        code = status.HTTP_404_NOT_FOUND
    elif isinstance(error, AdminConflictError):
        code = status.HTTP_409_CONFLICT
    elif isinstance(error, (AdminValidationError, ValueError)):
        code = status.HTTP_422_UNPROCESSABLE_ENTITY
    else:
        code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HTTPException(
        status_code=code, detail=str(error) or "管理员操作失败，请稍后重试。"
    )


@router.get("/users", response_model=list[AdminUserSummary])
def list_users(
    _admin: AdminUserManager,
    service: AdminServiceDependency,
) -> list[AdminUserSummary]:
    """列出用户及其角色摘要。"""

    try:
        return service.list_users()
    except AdminServiceError as exc:
        raise _admin_http_exception(exc) from None


@router.get("/users/{user_id}", response_model=AdminUserSummary)
def get_user(
    user_id: UUID,
    _admin: AdminUserManager,
    service: AdminServiceDependency,
) -> AdminUserSummary:
    """读取指定用户的安全摘要。"""

    try:
        return service.get_user(user_id)
    except AdminServiceError as exc:
        raise _admin_http_exception(exc) from None


@router.post(
    "/users",
    response_model=AdminUserSummary,
    status_code=status.HTTP_201_CREATED,
)
def create_user(
    payload: AdminUserCreateRequest,
    _admin: AdminUserManager,
    service: AdminServiceDependency,
) -> AdminUserSummary:
    """创建用户并返回不含密码的摘要。"""

    try:
        return service.create_user(
            username=payload.username,
            email=payload.email,
            password=payload.password,
            roles=payload.roles,
            is_active=payload.is_active,
        )
    except (AdminServiceError, ValueError) as exc:
        raise _admin_http_exception(exc) from None


@router.patch("/users/{user_id}", response_model=AdminUserSummary)
def update_user(
    user_id: UUID,
    payload: AdminUserUpdateRequest,
    _admin: AdminUserManager,
    service: AdminServiceDependency,
) -> AdminUserSummary:
    """更新用户资料或启用状态。"""

    try:
        return service.update_user(
            user_id,
            username=payload.username,
            email=payload.email,
            password=payload.password,
            is_active=payload.is_active,
        )
    except (AdminServiceError, ValueError) as exc:
        raise _admin_http_exception(exc) from None


@router.post("/users/{user_id}/active", response_model=AdminUserSummary)
def set_user_active(
    user_id: UUID,
    payload: AdminActiveRequest,
    _admin: AdminUserManager,
    service: AdminServiceDependency,
) -> AdminUserSummary:
    """启用或停用指定用户。"""

    try:
        return service.set_user_active(user_id, payload.is_active)
    except (AdminServiceError, ValueError) as exc:
        raise _admin_http_exception(exc) from None


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: UUID,
    _admin: AdminUserManager,
    service: AdminServiceDependency,
) -> Response:
    """删除用户；被业务数据引用时返回冲突提示。"""

    try:
        service.delete_user(user_id)
    except (AdminServiceError, ValueError) as exc:
        raise _admin_http_exception(exc) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/roles", response_model=list[AdminRoleSummary])
def list_roles(
    _admin: AdminRoleManager,
    service: AdminServiceDependency,
) -> list[AdminRoleSummary]:
    """列出内置角色及其用户数量。"""

    try:
        return service.list_roles()
    except AdminServiceError as exc:
        raise _admin_http_exception(exc) from None


@router.post("/roles", response_model=AdminRoleSummary)
def ensure_role(
    payload: AdminRoleRequest,
    _admin: AdminRoleManager,
    service: AdminServiceDependency,
) -> AdminRoleSummary:
    """确保指定的内置角色存在。"""

    try:
        return service.ensure_role(payload.role)
    except (AdminServiceError, ValueError) as exc:
        raise _admin_http_exception(exc) from None


@router.put("/users/{user_id}/roles", response_model=AdminUserSummary)
def set_user_roles(
    user_id: UUID,
    payload: AdminRoleAssignmentRequest,
    _admin: AdminRoleManager,
    service: AdminServiceDependency,
) -> AdminUserSummary:
    """替换指定用户的完整角色集合。"""

    try:
        return service.set_user_roles(user_id, payload.roles)
    except (AdminServiceError, ValueError) as exc:
        raise _admin_http_exception(exc) from None


@router.post("/users/{user_id}/roles/{role}", response_model=AdminUserSummary)
def grant_role(
    user_id: UUID,
    role: str,
    _admin: AdminRoleManager,
    service: AdminServiceDependency,
) -> AdminUserSummary:
    """为用户授予一个角色。"""

    try:
        return service.grant_role(user_id, role)
    except (AdminServiceError, ValueError) as exc:
        raise _admin_http_exception(exc) from None


@router.delete("/users/{user_id}/roles/{role}", response_model=AdminUserSummary)
def revoke_role(
    user_id: UUID,
    role: str,
    _admin: AdminRoleManager,
    service: AdminServiceDependency,
) -> AdminUserSummary:
    """撤销用户的一个角色，但不能撤销其最后一个角色。"""

    try:
        return service.revoke_role(user_id, role)
    except (AdminServiceError, ValueError) as exc:
        raise _admin_http_exception(exc) from None


@router.get("/status", response_model=AdminSystemStatus)
@router.get("/system/status", response_model=AdminSystemStatus)
def get_system_status(
    _admin: AdminStatusViewer,
    service: AdminServiceDependency,
) -> AdminSystemStatus:
    """返回数据库、Redis 和用户统计运行状态。"""

    try:
        return service.get_system_status()
    except AdminServiceError as exc:
        raise _admin_http_exception(exc) from None


__all__ = [
    "AdminActiveRequest",
    "AdminRoleAssignmentRequest",
    "AdminRoleRequest",
    "AdminUserCreateRequest",
    "AdminUserUpdateRequest",
    "get_admin_service",
    "router",
]
