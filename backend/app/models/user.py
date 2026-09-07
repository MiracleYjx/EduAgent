"""用户持久化模型。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from backend.app.models.course import Course
    from backend.app.models.document import Document
    from backend.app.models.exam import Exam
    from backend.app.models.question import Question
    from backend.app.models.role import Role
    from backend.app.models.submission import Submission


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """平台用户，可关联教师、学生或管理员角色。"""

    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    roles: Mapped[list[Role]] = relationship(
        "Role", secondary="user_roles", back_populates="users", lazy="selectin"
    )
    courses: Mapped[list[Course]] = relationship(
        "Course", back_populates="creator", foreign_keys="Course.created_by"
    )
    uploaded_documents: Mapped[list[Document]] = relationship(
        "Document", back_populates="uploader", foreign_keys="Document.uploaded_by"
    )
    created_questions: Mapped[list[Question]] = relationship(
        "Question", back_populates="creator", foreign_keys="Question.created_by"
    )
    created_exams: Mapped[list[Exam]] = relationship(
        "Exam", back_populates="creator", foreign_keys="Exam.created_by"
    )
    submissions: Mapped[list[Submission]] = relationship(
        "Submission", back_populates="student", foreign_keys="Submission.student_id"
    )


__all__ = ["User"]
