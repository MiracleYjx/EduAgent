"""角色持久化模型。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base
from backend.app.domain.enums import UserRole
from backend.app.models.associations import user_roles
from backend.app.models.base import TimestampMixin, UUIDPrimaryKeyMixin, enum_type

if TYPE_CHECKING:
    from backend.app.models.user import User


class Role(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """系统支持的教师、学生和管理员角色。"""

    __tablename__ = "roles"

    name: Mapped[UserRole] = mapped_column(
        enum_type(UserRole, "user_role"), unique=True, nullable=False
    )
    description: Mapped[str | None] = mapped_column()
    users: Mapped[list[User]] = relationship(
        "User", secondary=user_roles, back_populates="roles"
    )


__all__ = ["Role"]
