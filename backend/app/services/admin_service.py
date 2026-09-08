"""管理员服务：用户管理、角色管理和基础运行状态。"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from backend.app.core.database import (
    DatabaseNotReadyError,
    check_postgres_ready,
    get_engine,
)
from backend.app.core.redis import (
    RedisNotReadyError,
    check_redis_ready,
    get_redis_client,
)
from backend.app.domain.enums import UserRole
from backend.app.domain.permissions import ROLE_DISPLAY_NAMES, normalize_role
from backend.app.models import Role, User
from backend.app.models.associations import user_roles
from backend.app.services.auth_service import hash_password

_ROLE_ORDER = {role: index for index, role in enumerate(UserRole)}


class AdminServiceError(RuntimeError):
    """管理员服务的安全异常基类。"""


class AdminNotFoundError(AdminServiceError):
    """查询的用户或角色不存在时抛出。"""


class AdminConflictError(AdminServiceError):
    """唯一性冲突或受约束资源无法变更时抛出。"""


class AdminValidationError(AdminServiceError):
    """管理员输入不合法时抛出。"""


class AdminUserSummary(BaseModel):
    """面向 API 和 UI 的用户摘要。"""

    model_config = ConfigDict(frozen=True)

    id: str
    username: str
    email: str
    is_active: bool
    roles: tuple[str, ...]
    created_at: datetime
    updated_at: datetime


class AdminRoleSummary(BaseModel):
    """面向 API 和 UI 的角色摘要。"""

    model_config = ConfigDict(frozen=True)

    id: str | None = None
    name: UserRole
    description: str | None = None
    user_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AdminComponentStatus(BaseModel):
    """单个运行组件的健康状态。"""

    model_config = ConfigDict(frozen=True)

    name: str
    healthy: bool
    detail: str = ""


class AdminSystemStatus(BaseModel):
    """管理员可见的基础运行状态快照。"""

    model_config = ConfigDict(frozen=True)

    service_name: str = "backend"
    checked_at: datetime
    overall_healthy: bool
    database: AdminComponentStatus
    redis: AdminComponentStatus
    user_count: int | None = None
    active_user_count: int | None = None
    role_count: int | None = None
    users_by_role: dict[str, int] = Field(default_factory=dict)


def _normalize_text(value: str | None, field_name: str) -> str:
    """统一清理文本输入并拒绝空值。"""

    if not isinstance(value, str) or not value.strip():
        raise AdminValidationError(f"{field_name}不能为空。")
    return value.strip()


def _normalize_roles(
    roles: Iterable[UserRole | str] | UserRole | str,
) -> tuple[UserRole, ...]:
    """将角色输入规范化为稳定排序且去重的角色元组。"""

    if isinstance(roles, (str, UserRole)):
        roles = (roles,)

    try:
        normalized = {normalize_role(role) for role in roles}
    except (TypeError, ValueError) as exc:
        raise AdminValidationError("角色值无效。") from exc
    if not normalized:
        raise AdminValidationError("用户至少需要一个角色。")
    return tuple(sorted(normalized, key=lambda role: _ROLE_ORDER[role]))


def _normalize_user_id(user_id: UUID | str) -> UUID:
    """将用户标识统一转换为 UUID。"""

    try:
        return UUID(str(user_id))
    except (TypeError, ValueError) as exc:
        raise AdminValidationError("用户标识无效。") from exc


class AdminService:
    """封装管理员用户、角色和运行状态相关的基础业务。"""

    def __init__(
        self,
        session: Session,
        *,
        engine: Any | None = None,
        redis_client: Any | None = None,
    ) -> None:
        self.session = session
        self.engine: Engine = cast(Engine, engine or session.get_bind() or get_engine())
        self.redis_client = redis_client if redis_client is not None else get_redis_client()

    def list_users(self) -> list[AdminUserSummary]:
        """列出所有用户及其角色摘要。"""

        try:
            users = self.session.scalars(
                select(User)
                .options(selectinload(User.roles))
                .order_by(User.created_at, User.username)
            ).all()
        except SQLAlchemyError as exc:
            raise AdminServiceError("无法读取用户列表。") from exc
        return [self._user_summary(user) for user in users]

    def get_user(self, user_id: UUID | str) -> AdminUserSummary:
        """按用户标识获取单个用户摘要。"""

        user = self._load_user(user_id)
        return self._user_summary(user)

    def create_user(
        self,
        *,
        username: str,
        email: str,
        password: str,
        roles: Iterable[UserRole | str] | UserRole | str,
        is_active: bool = True,
    ) -> AdminUserSummary:
        """创建一个可初始化登录的用户。"""

        normalized_roles = _normalize_roles(roles)
        user = User(
            username=_normalize_text(username, "用户名"),
            email=_normalize_text(email, "邮箱"),
            password_hash=hash_password(_normalize_text(password, "密码")),
            is_active=is_active,
        )
        user.roles = [self._get_or_create_role(role) for role in normalized_roles]
        self.session.add(user)
        return self._commit_user(user, "创建用户失败。")

    def update_user(
        self,
        user_id: UUID | str,
        *,
        username: str | None = None,
        email: str | None = None,
        password: str | None = None,
        is_active: bool | None = None,
    ) -> AdminUserSummary:
        """更新用户基础资料或登录状态。"""

        user = self._load_user(user_id)
        if all(value is None for value in (username, email, password, is_active)):
            raise AdminValidationError("至少需要提供一个更新字段。")

        if username is not None:
            user.username = _normalize_text(username, "用户名")
        if email is not None:
            user.email = _normalize_text(email, "邮箱")
        if password is not None:
            user.password_hash = hash_password(_normalize_text(password, "密码"))
        if is_active is not None:
            user.is_active = is_active

        return self._commit_user(user, "更新用户失败。")

    def set_user_active(self, user_id: UUID | str, is_active: bool) -> AdminUserSummary:
        """启用或停用指定用户。"""

        return self.update_user(user_id, is_active=is_active)

    def delete_user(self, user_id: UUID | str) -> None:
        """删除一个未被业务数据引用的用户。"""

        user = self._load_user(user_id)
        try:
            self.session.delete(user)
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise AdminConflictError("用户仍被业务数据引用，无法删除。") from exc
        except SQLAlchemyError as exc:
            self.session.rollback()
            raise AdminServiceError("删除用户失败。") from exc

    def list_roles(self) -> list[AdminRoleSummary]:
        """列出系统角色及其使用情况。"""

        counts = self._role_usage_counts()
        try:
            persisted_roles = {
                normalize_role(role.name): role
                for role in self.session.scalars(select(Role)).all()
            }
        except SQLAlchemyError as exc:
            raise AdminServiceError("无法读取角色列表。") from exc

        return [
            self._role_summary(role, persisted_roles.get(role), counts.get(role, 0))
            for role in UserRole
        ]

    def set_user_roles(
        self,
        user_id: UUID | str,
        roles: Iterable[UserRole | str] | UserRole | str,
    ) -> AdminUserSummary:
        """替换用户的全部角色。"""

        normalized_roles = _normalize_roles(roles)
        user = self._load_user(user_id)
        user.roles = [self._get_or_create_role(role) for role in normalized_roles]
        return self._commit_user(user, "更新用户角色失败。")

    def grant_role(
        self,
        user_id: UUID | str,
        role: UserRole | str,
    ) -> AdminUserSummary:
        """为用户新增一个角色。"""

        normalized_role = _normalize_roles(role)[0]
        user = self._load_user(user_id)
        current_roles = {
            normalize_role(existing_role.name)
            for existing_role in user.roles
        }
        if normalized_role not in current_roles:
            user.roles.append(self._get_or_create_role(normalized_role))
        return self._commit_user(user, "更新用户角色失败。")

    def revoke_role(
        self,
        user_id: UUID | str,
        role: UserRole | str,
    ) -> AdminUserSummary:
        """从用户移除一个角色，不能把用户变成无角色状态。"""

        normalized_role = _normalize_roles(role)[0]
        user = self._load_user(user_id)
        remaining_roles = [
            existing_role
            for existing_role in user.roles
            if normalize_role(existing_role.name) != normalized_role
        ]
        if not remaining_roles:
            raise AdminValidationError("用户至少需要一个角色。")
        user.roles = remaining_roles
        return self._commit_user(user, "更新用户角色失败。")

    def ensure_role(self, role: UserRole | str) -> AdminRoleSummary:
        """确保单个角色存在，并返回其摘要。"""

        normalized_role = normalize_role(role)
        role_model = self._get_or_create_role(normalized_role)
        counts = self._role_usage_counts()
        return self._role_summary(normalized_role, role_model, counts[normalized_role])

    def get_system_status(self) -> AdminSystemStatus:
        """返回管理员可以查看的基础运行状态。"""

        checked_at = datetime.now(UTC)
        database_status, user_count, active_user_count, roles = self._database_snapshot()
        redis_status = self._redis_snapshot()
        role_count = len(roles)
        users_by_role = {item.name.value: item.user_count for item in roles}
        overall_healthy = database_status.healthy and redis_status.healthy

        return AdminSystemStatus(
            checked_at=checked_at,
            overall_healthy=overall_healthy,
            database=database_status,
            redis=redis_status,
            user_count=user_count,
            active_user_count=active_user_count,
            role_count=role_count,
            users_by_role=users_by_role,
        )

    def _database_snapshot(
        self,
    ) -> tuple[AdminComponentStatus, int | None, int | None, list[AdminRoleSummary]]:
        """采集数据库连接和统计快照。"""

        try:
            check_postgres_ready(self.engine)
            user_count, active_user_count = self._user_counts()
            roles = self.list_roles()
        except (DatabaseNotReadyError, AdminServiceError) as exc:
            return (
                AdminComponentStatus(name="数据库", healthy=False, detail=str(exc)),
                None,
                None,
                [self._role_summary(role, None, 0) for role in UserRole],
            )

        return (
            AdminComponentStatus(name="数据库", healthy=True, detail="数据库连接正常。"),
            user_count,
            active_user_count,
            roles,
        )

    def _redis_snapshot(self) -> AdminComponentStatus:
        """采集 Redis 连接快照。"""

        try:
            check_redis_ready(self.redis_client)
        except RedisNotReadyError as exc:
            return AdminComponentStatus(name="Redis", healthy=False, detail=str(exc))
        return AdminComponentStatus(name="Redis", healthy=True, detail="Redis 连接正常。")

    def _user_counts(self) -> tuple[int, int]:
        """统计用户总数和启用用户数。"""

        try:
            user_count = int(
                self.session.scalar(select(func.count()).select_from(User)) or 0
            )
            active_user_count = int(
                self.session.scalar(
                    select(func.count()).select_from(User).where(User.is_active.is_(True))
                )
                or 0
            )
        except SQLAlchemyError as exc:
            raise AdminServiceError("无法读取用户统计。") from exc
        return user_count, active_user_count

    def _role_usage_counts(self) -> dict[UserRole, int]:
        """统计每个内置角色的用户数量。"""

        counts = {role: 0 for role in UserRole}
        try:
            rows = self.session.execute(
                select(Role.name, func.count(user_roles.c.user_id))
                .select_from(Role)
                .outerjoin(user_roles, user_roles.c.role_id == Role.id)
                .group_by(Role.name)
            ).all()
        except SQLAlchemyError as exc:
            raise AdminServiceError("无法读取角色统计。") from exc

        for raw_role, total in rows:
            try:
                counts[normalize_role(raw_role)] = int(total)
            except (TypeError, ValueError):
                continue
        return counts

    def _get_or_create_role(self, role: UserRole) -> Role:
        """读取角色，不存在时自动创建内置角色记录。"""

        role_model = self.session.scalar(select(Role).where(Role.name == role))
        if role_model is not None:
            if not role_model.description:
                role_model.description = ROLE_DISPLAY_NAMES[role]
            return role_model

        role_model = Role(name=role, description=ROLE_DISPLAY_NAMES[role])
        self.session.add(role_model)
        self.session.flush()
        return role_model

    def _load_user(self, user_id: UUID | str) -> User:
        """加载目标用户，统一处理不存在和非法标识。"""

        normalized_user_id = _normalize_user_id(user_id)
        try:
            user = self.session.scalar(
                select(User)
                .options(selectinload(User.roles))
                .where(User.id == normalized_user_id)
            )
        except SQLAlchemyError as exc:
            raise AdminServiceError("无法读取用户信息。") from exc
        if user is None:
            raise AdminNotFoundError("用户不存在。")
        return user

    def _commit_user(self, user: User, fallback_message: str) -> AdminUserSummary:
        """提交用户变更并返回安全摘要。"""

        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise AdminConflictError("用户名或邮箱已存在。") from exc
        except SQLAlchemyError as exc:
            self.session.rollback()
            raise AdminServiceError(fallback_message) from exc

        self.session.refresh(user)
        return self._user_summary(user)

    @staticmethod
    def _user_summary(user: User) -> AdminUserSummary:
        """把用户实体转换为不含敏感字段的摘要。"""

        return AdminUserSummary(
            id=str(user.id),
            username=user.username,
            email=user.email,
            is_active=user.is_active,
            roles=tuple(
                role.value
                for role in sorted(
                    {
                        normalize_role(existing_role.name)
                        for existing_role in user.roles
                    },
                    key=lambda item: _ROLE_ORDER[item],
                )
            ),
            created_at=user.created_at,
            updated_at=user.updated_at,
        )

    @staticmethod
    def _role_summary(
        role: UserRole,
        role_model: Role | None,
        user_count: int,
    ) -> AdminRoleSummary:
        """把角色实体转换为角色摘要。"""

        return AdminRoleSummary(
            id=str(role_model.id) if role_model is not None else None,
            name=role,
            description=(
                role_model.description if role_model is not None else ROLE_DISPLAY_NAMES[role]
            ),
            user_count=user_count,
            created_at=role_model.created_at if role_model is not None else None,
            updated_at=role_model.updated_at if role_model is not None else None,
        )


__all__ = [
    "AdminComponentStatus",
    "AdminConflictError",
    "AdminNotFoundError",
    "AdminRoleSummary",
    "AdminService",
    "AdminServiceError",
    "AdminSystemStatus",
    "AdminUserSummary",
    "AdminValidationError",
]
