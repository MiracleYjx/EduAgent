"""Each actual export has a single business owner and immutable identity."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.models.base import Base, UUIDPrimaryKeyMixin


class ExportFile(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "export_files"
    __table_args__ = (
        CheckConstraint(
            "(CASE WHEN course_id IS NULL THEN 0 ELSE 1 END + "
            "CASE WHEN exam_id IS NULL THEN 0 ELSE 1 END + "
            "CASE WHEN submission_id IS NULL THEN 0 ELSE 1 END) = 1",
            name="ck_export_single_owner",
        ),
        CheckConstraint(
            "audience IN ('teacher_only', 'submission_owner') AND "
            "(audience <> 'submission_owner' OR submission_id IS NOT NULL)",
            name="ck_export_audience",
        ),
        CheckConstraint(
            "status IN ('writing', 'ready', 'failed')", name="ck_export_status",
        ),
        CheckConstraint(
            "(status = 'writing' AND completed_at IS NULL AND error IS NULL) OR "
            "(status = 'ready' AND completed_at IS NOT NULL AND error IS NULL "
            "AND storage_path IS NOT NULL AND file_metadata IS NOT NULL) OR "
            "(status = 'failed' AND completed_at IS NOT NULL AND error IS NOT NULL)",
            name="ck_export_lifecycle",
        ),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at",
            name="ck_export_times",
        ),
        CheckConstraint(
            "file_metadata IS NULL OR jsonb_typeof(file_metadata) = 'object'",
            name="ck_export_metadata_object",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "error IS NULL OR jsonb_typeof(error) = 'object'",
            name="ck_export_error_object",
        ).ddl_if(dialect="postgresql"),
    )

    course_id: Mapped[UUID | None] = mapped_column(ForeignKey("courses.id", ondelete="RESTRICT"), index=True)
    exam_id: Mapped[UUID | None] = mapped_column(ForeignKey("exams.id", ondelete="RESTRICT"), index=True)
    submission_id: Mapped[UUID | None] = mapped_column(ForeignKey("submissions.id", ondelete="RESTRICT"), index=True)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    audience: Mapped[str] = mapped_column(String(32), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_path: Mapped[str | None] = mapped_column(String(1024))
    file_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="writing")
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
