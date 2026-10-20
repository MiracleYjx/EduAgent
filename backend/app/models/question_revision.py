"""Revision content is the sole personal question content source."""

from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Numeric,
    String,
    Table,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

CONTENT_JSON = JSON(none_as_null=True).with_variant(
    JSONB(none_as_null=True), "postgresql"
)


class QuestionRevision(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "question_revisions"
    __table_args__ = (
        UniqueConstraint("question_id", "revision"),
        UniqueConstraint("id", "question_id"),
        UniqueConstraint("id", "subject_id"),
        ForeignKeyConstraint(
            ["question_id", "subject_id"],
            ["questions.id", "questions.subject_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "revision > 0 AND default_score > 0", name="ck_revision_positive"
        ),
        CheckConstraint(
            "grounding IN ('uploaded_material','general_knowledge')",
            name="ck_revision_grounding",
        ),
        CheckConstraint(
            "admission_state IN ('incomplete','needs_validation','eligible','needs_correction')",
            name="ck_revision_admission",
        ),
    )
    question_id: Mapped[UUID] = mapped_column(Uuid)
    subject_id: Mapped[UUID] = mapped_column(Uuid)
    revision: Mapped[int]
    question_type: Mapped[str] = mapped_column(String(32))
    stem_blocks: Mapped[list[dict[str, Any]]] = mapped_column(
        CONTENT_JSON, nullable=False
    )
    options: Mapped[list[dict[str, Any]]] = mapped_column(
        CONTENT_JSON, server_default="[]"
    )
    reference_answer: Mapped[str | None]
    scoring_rubric: Mapped[str | None]
    explanation_blocks: Mapped[list[dict[str, Any]]] = mapped_column(
        CONTENT_JSON, server_default="[]"
    )
    grounding: Mapped[str] = mapped_column(String(32))
    grounding_refs: Mapped[dict[str, Any]] = mapped_column(
        CONTENT_JSON, server_default="{}"
    )
    admission_state: Mapped[str] = mapped_column(
        String(32), server_default="needs_validation"
    )
    difficulty: Mapped[str | None] = mapped_column(String(32))
    default_score: Mapped[Decimal] = mapped_column(Numeric(8, 2), server_default="10")


question_revision_knowledge_points = Table(
    "question_revision_knowledge_points",
    Base.metadata,
    Column("revision_id", Uuid, primary_key=True),
    Column("knowledge_point_id", Uuid, primary_key=True),
    Column("subject_id", Uuid, nullable=False),
    ForeignKeyConstraint(
        ["revision_id", "subject_id"],
        ["question_revisions.id", "question_revisions.subject_id"],
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["knowledge_point_id", "subject_id"],
        ["knowledge_points.id", "knowledge_points.subject_id"],
        ondelete="RESTRICT",
    ),
)
question_revision_assets = Table(
    "question_revision_assets",
    Base.metadata,
    Column(
        "revision_id",
        Uuid,
        ForeignKey("question_revisions.id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    Column(
        "asset_id",
        Uuid,
        ForeignKey("question_assets.id", ondelete="RESTRICT"),
        primary_key=True,
    ),
)
