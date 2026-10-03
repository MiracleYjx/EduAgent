"""考试持久化模型。"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from backend.app.domain.enums import ExamStatus
from backend.app.models.associations import exam_questions
from backend.app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, enum_type

if TYPE_CHECKING:
    from backend.app.models.course import Course
    from backend.app.models.exam_question import ExamQuestion
    from backend.app.models.question import Question
    from backend.app.models.submission import Submission
    from backend.app.models.user import User


class Exam(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """教师从课程题库组织的考试。"""

    __tablename__ = "exams"
    __table_args__ = (
        CheckConstraint(
            "assembly_constraints IS NULL OR jsonb_typeof(assembly_constraints) = 'object'",
            name="ck_exams_assembly_constraints_shape",
        ).ddl_if(dialect="postgresql"),
    )

    assembly_constraints: Mapped[dict[str, Any] | None] = mapped_column(
        JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")
    )

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
    exam_question_links: Mapped[list[ExamQuestion]] = relationship(
        "ExamQuestion",
        back_populates="exam",
        cascade="all, delete-orphan",
        order_by="ExamQuestion.order_index",
        passive_deletes=True,
    )
    # Existing readers retain query/eager-load support; all writes use the association.
    questions: Mapped[list[Question]] = relationship(
        "Question",
        secondary=exam_questions,
        back_populates="exams",
        order_by=exam_questions.c.order_index,
        viewonly=True,
    )
    submissions: Mapped[list[Submission]] = relationship(
        "Submission", back_populates="exam", cascade="all, delete-orphan"
    )

    @validates("assembly_constraints")
    def valid_assembly_constraints(
        self, key: str, value: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        from backend.app.schemas.exam_assembly import AssemblyConstraints

        return AssemblyConstraints.model_validate(value).model_dump(mode="json")


__all__ = ["Exam"]
