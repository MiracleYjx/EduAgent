"""题库题目持久化模型。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    Text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from backend.app.domain.enums import QuestionSourceType, QuestionStatus, QuestionType
from backend.app.models.associations import exam_questions
from backend.app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, enum_type

if TYPE_CHECKING:
    from backend.app.models.course import Course
    from backend.app.models.exam import Exam
    from backend.app.models.extracted_question import ExtractedQuestion
    from backend.app.models.question_asset import QuestionAsset
    from backend.app.models.user import User


class Question(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """人工或 AI 生成、需经过审核后才能用于考试的题目。"""

    __tablename__ = "questions"
    __table_args__ = (Index("ix_questions_course_status", "course_id", "status"), CheckConstraint("image_assessment IS NULL OR jsonb_typeof(image_assessment) = 'object'", name="ck_question_image_assessment").ddl_if(dialect="postgresql"))

    course_id: Mapped[UUID] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type: Mapped[QuestionType] = mapped_column(
        enum_type(QuestionType, "question_type"), nullable=False, index=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    options: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON)
    order_preserved: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    reference_answer: Mapped[str | None] = mapped_column(Text)
    scoring_rubric: Mapped[str | None] = mapped_column(Text)
    image_assessment: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql"))
    assets: Mapped[list[QuestionAsset]] = relationship("QuestionAsset", back_populates="question", order_by="QuestionAsset.order_index", passive_deletes="all")
    analysis: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[QuestionSourceType | None] = mapped_column(enum_type(QuestionSourceType, "question_source_type", length=32))
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    imported_extracted_question: Mapped[ExtractedQuestion | None] = relationship("ExtractedQuestion", back_populates="question", passive_deletes="all")
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


    @validates("image_assessment")
    def valid_image_assessment(self, key: str, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        from backend.app.schemas.image_assessment import ImageAssessment
        return ImageAssessment.model_validate(value).model_dump(mode="json")


__all__ = ["Question"]
