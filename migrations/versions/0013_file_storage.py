"""Persist file metadata and actual export ownership.

Revision ID: 0013_file_storage
Revises: 0012_audit_logs
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_file_storage"
down_revision: str | None = "0012_audit_logs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("file_metadata", postgresql.JSONB(none_as_null=True), nullable=True))
    op.create_check_constraint(
        "ck_document_file_metadata_object", "documents",
        "file_metadata IS NULL OR jsonb_typeof(file_metadata) = 'object'",
    )
    op.create_table(
        "export_files",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("course_id", sa.Uuid(), sa.ForeignKey("courses.id", ondelete="RESTRICT")),
        sa.Column("exam_id", sa.Uuid(), sa.ForeignKey("exams.id", ondelete="RESTRICT")),
        sa.Column("submission_id", sa.Uuid(), sa.ForeignKey("submissions.id", ondelete="RESTRICT")),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("audience", sa.String(32), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("storage_path", sa.String(1024)),
        sa.Column("file_metadata", postgresql.JSONB(none_as_null=True)),
        sa.Column("status", sa.String(16), nullable=False, server_default="writing"),
        sa.Column("error", postgresql.JSONB(none_as_null=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "(CASE WHEN course_id IS NULL THEN 0 ELSE 1 END + "
            "CASE WHEN exam_id IS NULL THEN 0 ELSE 1 END + "
            "CASE WHEN submission_id IS NULL THEN 0 ELSE 1 END) = 1",
            name="ck_export_single_owner",
        ),
        sa.CheckConstraint(
            "audience IN ('teacher_only', 'submission_owner') AND "
            "(audience <> 'submission_owner' OR submission_id IS NOT NULL)",
            name="ck_export_audience",
        ),
        sa.CheckConstraint("status IN ('writing', 'ready', 'failed')", name="ck_export_status"),
        sa.CheckConstraint(
            "(status = 'writing' AND completed_at IS NULL AND error IS NULL) OR "
            "(status = 'ready' AND completed_at IS NOT NULL AND error IS NULL "
            "AND storage_path IS NOT NULL AND file_metadata IS NOT NULL) OR "
            "(status = 'failed' AND completed_at IS NOT NULL AND error IS NOT NULL)",
            name="ck_export_lifecycle",
        ),
        sa.CheckConstraint("completed_at IS NULL OR completed_at >= created_at", name="ck_export_times"),
        sa.CheckConstraint(
            "file_metadata IS NULL OR jsonb_typeof(file_metadata) = 'object'",
            name="ck_export_metadata_object",
        ),
        sa.CheckConstraint("error IS NULL OR jsonb_typeof(error) = 'object'", name="ck_export_error_object"),
    )
    for field in ("course_id", "exam_id", "submission_id"):
        op.create_index(f"ix_export_files_{field}", "export_files", [field])


def downgrade() -> None:
    op.drop_table("export_files")
    op.drop_constraint("ck_document_file_metadata_object", "documents", type_="check")
    op.drop_column("documents", "file_metadata")
