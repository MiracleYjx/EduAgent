"""Stable course chapters whose current directory was confirmed by a teacher."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Chapter(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "chapters"
    __table_args__ = (
        CheckConstraint(
            "jsonb_typeof(sections) = 'array'", name="ck_chapters_sections_array"
        ).ddl_if(dialect="postgresql"),
        Index("ix_chapters_course_id", "course_id"),
    )

    course_id: Mapped[UUID] = mapped_column(
        ForeignKey("courses.id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    sections: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=list
    )
    confirmed_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


__all__ = ["Chapter"]
