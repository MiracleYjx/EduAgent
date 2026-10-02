"""暂存校正原题；页来源仅存 source_page_ids。"""
from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from backend.app.domain.enums import ExtractedBy, ExtractedQuestionStatus, QuestionType
from backend.app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, enum_type

if TYPE_CHECKING:
    from backend.app.models.paper_import import PaperImport
    from backend.app.models.question import Question


def nullable_json():
    return JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")


class ExtractedQuestion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "extracted_questions"
    __table_args__ = (
        UniqueConstraint("paper_import_id", "order_index", name="uq_extracted_import_order"),
        CheckConstraint("order_index IS NULL OR order_index >= 1", name="ck_extracted_order"),
        Index("ix_extracted_questions_import_status", "paper_import_id", "status"),
        Index("ix_extracted_questions_assets", "assets", postgresql_using="gin").ddl_if(dialect="postgresql"),
        CheckConstraint("score IS NULL OR score > 0", name="ck_extracted_question_score"),
        CheckConstraint("extraction_confidence IS NULL OR extraction_confidence BETWEEN 0 AND 1", name="ck_extracted_question_confidence"),
        CheckConstraint("(status = 'Corrected' AND question_id IS NOT NULL) OR (status <> 'Corrected' AND question_id IS NULL)", name="ck_extracted_question_link"),
        CheckConstraint("status <> 'Rejected' OR length(trim(correction_notes)) > 0 AND correction_notes IS NOT NULL", name="ck_extracted_question_rejection"),
        *[CheckConstraint(f"{field} IS NULL OR jsonb_typeof({field}) = 'array'", name=f"ck_extracted_{field}_array").ddl_if(dialect="postgresql") for field in ("source_page_ids", "knowledge_points", "source_regions", "assets")],
        CheckConstraint("assets IS NULL OR jsonb_array_length(assets) <= 5", name="ck_extracted_assets_count").ddl_if(dialect="postgresql"),
        CheckConstraint("image_assessment IS NULL OR jsonb_typeof(image_assessment) = 'object'", name="ck_extracted_image_assessment").ddl_if(dialect="postgresql"),
    )
    paper_import_id: Mapped[UUID] = mapped_column(ForeignKey("paper_imports.id", ondelete="RESTRICT"), nullable=False)
    source_page_ids: Mapped[list[str]] = mapped_column(nullable_json(), default=list, server_default="[]", nullable=False)
    question_type: Mapped[QuestionType | None] = mapped_column(enum_type(QuestionType, "extracted_question_type", length=32))
    content: Mapped[str | None] = mapped_column(Text)
    options: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON(none_as_null=True))
    order_preserved: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    reference_answer: Mapped[str | None] = mapped_column(Text)
    scoring_rubric: Mapped[str | None] = mapped_column(Text)
    score: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    status: Mapped[ExtractedQuestionStatus] = mapped_column(enum_type(ExtractedQuestionStatus, "extracted_question_status", length=32), default=ExtractedQuestionStatus.EXTRACTED, server_default="Extracted", nullable=False)
    correction_notes: Mapped[str | None] = mapped_column(Text)
    extracted_by: Mapped[ExtractedBy] = mapped_column(enum_type(ExtractedBy, "extracted_by", length=16), nullable=False)
    extraction_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    question_id: Mapped[UUID | None] = mapped_column(ForeignKey("questions.id", ondelete="RESTRICT"), unique=True)
    order_index: Mapped[int | None] = mapped_column(Integer)
    question_number: Mapped[str | None] = mapped_column(Text)
    analysis: Mapped[str | None] = mapped_column(Text)
    knowledge_points: Mapped[list[str] | None] = mapped_column(nullable_json())
    source_regions: Mapped[list[dict[str, Any]] | None] = mapped_column(nullable_json())
    assets: Mapped[list[dict[str, Any]] | None] = mapped_column(nullable_json())
    image_assessment: Mapped[dict[str, Any] | None] = mapped_column(nullable_json())
    paper_import: Mapped[PaperImport] = relationship("PaperImport", back_populates="questions")
    question: Mapped[Question | None] = relationship("Question", back_populates="imported_extracted_question")


    @validates("image_assessment")
    def valid_image_assessment(self, key: str, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        from backend.app.schemas.image_assessment import ImageAssessment
        validated = ImageAssessment.model_validate(value)
        if validated.imported_review is not None:
            raise ValueError("暂存题不得建立正式导入核对绑定。")
        return validated.model_dump(mode="json")
