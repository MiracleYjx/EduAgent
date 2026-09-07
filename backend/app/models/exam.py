"""考试持久化模型。"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.domain.enums import ExamStatus
from backend.app.models.associations import exam_questions
from backend.app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, enum_type

if TYPE_CHECKING:
    from backend.app.models.course import Course
    from backend.app.models.question import Question
    from backend.app.models.submission import Submission
    from backend.app.models.user import User


class Exam(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """教师从课程题库组织的考试。"""

    __tablename__ = "exams"

    course_id: Mapped[UUID] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ExamStatus] = mapped_column(
        enum_type(ExamStatus, "exam_status"),
        default=ExamStatus.DRAFT,
        nullable=False,
        index=True,
    )
    duration_minutes: Mapped[int | None] = mapped_column(Integer)
    starts_at: Mapped[datetime | None]
    ends_at: Mapped[datetime | None]
    course: Mapped[Course] = relationship("Course", back_populates="exams")
    creator: Mapped[User] = relationship(
        "User", back_populates="created_exams", foreign_keys=[created_by]
    )
    questions: Mapped[list[Question]] = relationship(
        "Question", secondary=exam_questions, back_populates="exams"
    )
    submissions: Mapped[list[Submission]] = relationship(
        "Submission", back_populates="exam", cascade="all, delete-orphan"
    )


__all__ = ["Exam"]
