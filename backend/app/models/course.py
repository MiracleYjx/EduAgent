"""课程持久化模型。"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from backend.app.models.document import Document
    from backend.app.models.exam import Exam
    from backend.app.models.knowledge_base import KnowledgeBase
    from backend.app.models.question import Question
    from backend.app.models.user import User


class Course(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """教师管理的课程及其业务资源聚合根。"""

    __tablename__ = "courses"

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    creator: Mapped[User] = relationship(
        "User", back_populates="courses", foreign_keys=[created_by]
    )
    knowledge_bases: Mapped[list[KnowledgeBase]] = relationship(
        "KnowledgeBase", back_populates="course", cascade="all, delete-orphan"
    )
    documents: Mapped[list[Document]] = relationship(
        "Document", back_populates="course", cascade="all, delete-orphan"
    )
    questions: Mapped[list[Question]] = relationship(
        "Question", back_populates="course", cascade="all, delete-orphan"
    )
    exams: Mapped[list[Exam]] = relationship(
        "Exam", back_populates="course", cascade="all, delete-orphan"
    )


__all__ = ["Course"]
