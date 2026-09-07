"""题库题目持久化模型。"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import JSON, ForeignKey, Index, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.domain.enums import QuestionStatus, QuestionType
from backend.app.models.associations import exam_questions
from backend.app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, enum_type

if TYPE_CHECKING:
    from backend.app.models.course import Course
    from backend.app.models.exam import Exam
    from backend.app.models.user import User


class Question(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """人工或 AI 生成、需经过审核后才能用于考试的题目。"""

    __tablename__ = "questions"
    __table_args__ = (Index("ix_questions_course_status", "course_id", "status"),)

    course_id: Mapped[UUID] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type: Mapped[QuestionType] = mapped_column(
        enum_type(QuestionType, "question_type"), nullable=False, index=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    options: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON)
    reference_answer: Mapped[str | None] = mapped_column(Text)
    scoring_rubric: Mapped[str | None] = mapped_column(Text)
    difficulty: Mapped[str | None] = mapped_column(Text)
    knowledge_points: Mapped[list[str]] = mapped_column(
        JSON, default=list, nullable=False
    )
    score: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    status: Mapped[QuestionStatus] = mapped_column(
        enum_type(QuestionStatus, "question_status"),
        default=QuestionStatus.DRAFT,
        nullable=False,
        index=True,
    )
    created_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    course: Mapped[Course] = relationship("Course", back_populates="questions")
    creator: Mapped[User] = relationship(
        "User", back_populates="created_questions", foreign_keys=[created_by]
    )
    exams: Mapped[list[Exam]] = relationship(
        "Exam", secondary=exam_questions, back_populates="questions"
    )


__all__ = ["Question"]
