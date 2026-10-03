"""T165: real parent/derived question relationships, separate from teaching sources."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.models.base import Base, UUIDPrimaryKeyMixin


class QuestionSourcePaper(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "question_source_papers"
    __table_args__ = (
        UniqueConstraint(
            "derived_question_id",
            "source_question_id",
            name="uq_question_source_papers_pair",
        ),
        CheckConstraint(
            "derived_question_id != source_question_id",
            name="ck_question_source_papers_not_self",
        ),
        CheckConstraint(
            "adaptation_type IN ('rewrite', 'translate', 'extend')",
            name="ck_question_source_papers_type",
        ),
        Index("ix_question_source_papers_source_question_id", "source_question_id"),
    )
    derived_question_id: Mapped[UUID] = mapped_column(
        ForeignKey("questions.id", ondelete="RESTRICT"), nullable=False
    )
    source_question_id: Mapped[UUID] = mapped_column(
        ForeignKey("questions.id", ondelete="RESTRICT"), nullable=False
    )
    adaptation_type: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
    )
