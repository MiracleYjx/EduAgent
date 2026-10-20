"""Stable course chapters whose current directory was confirmed by a teacher."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Chapter(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "chapters"
    __table_args__ = (
        ForeignKeyConstraint(["parent_id", "subject_id"], ["chapters.id", "chapters.subject_id"], name="fk_chapter_parent"),
        UniqueConstraint("id", "subject_id"),
        CheckConstraint("(subject_id IS NULL AND course_id IS NOT NULL AND confirmed_by IS NOT NULL) OR (subject_id IS NOT NULL AND course_id IS NULL AND confirmed_by IS NULL)", name="ck_chapter_domain"),
        CheckConstraint(
            "jsonb_typeof(sections) = 'array'", name="ck_chapters_sections_array"
        ).ddl_if(dialect="postgresql"),
        Index("ix_chapters_course_id", "course_id"),
    )

    subject_id: Mapped[UUID | None] = mapped_column(ForeignKey("subjects.id", ondelete="RESTRICT"), nullable=True)
    parent_id: Mapped[UUID | None] = mapped_column(Uuid)

    course_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("courses.id", ondelete="RESTRICT"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    sections: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=list
    )
    confirmed_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


__all__ = ["Chapter"]
