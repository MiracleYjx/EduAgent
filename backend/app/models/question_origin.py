"""Many real sources may refer to one stable question identity."""

from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from backend.app.models.question_revision import CONTENT_JSON


class QuestionOrigin(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "question_origins"
    __table_args__ = (
        ForeignKeyConstraint(
            ["question_id", "subject_id"],
            ["questions.id", "questions.subject_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["document_id", "subject_id"],
            ["documents.id", "documents.subject_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(source_kind='uploaded' AND document_id IS NOT NULL AND generation_run_id IS NULL) OR (source_kind='ai' AND document_id IS NULL AND generation_run_id IS NOT NULL)",
            name="ck_origin_real_source",
        ),
    )
    question_id: Mapped[UUID] = mapped_column(Uuid)
    subject_id: Mapped[UUID] = mapped_column(Uuid)
    source_kind: Mapped[str] = mapped_column(String(32))
    document_id: Mapped[UUID | None] = mapped_column(Uuid)
    generation_run_id: Mapped[UUID | None] = mapped_column(Uuid)
    source_location: Mapped[dict[str, Any]] = mapped_column(
        CONTENT_JSON, nullable=False
    )
    answer_origin: Mapped[str | None] = mapped_column(String(32))
