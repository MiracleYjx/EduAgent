"""T154 question image relationships and G05 formal evidence column."""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_question_assets"
down_revision: str | None = "0014_paper_import"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("questions", sa.Column("image_assessment", postgresql.JSONB(none_as_null=True)))
    op.create_check_constraint("ck_question_image_assessment", "questions", "image_assessment IS NULL OR jsonb_typeof(image_assessment) = 'object'")
    op.create_table(
        "question_assets",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("question_id", sa.Uuid(), sa.ForeignKey("questions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("asset_type", sa.String(16), nullable=False),
        # Imported associations project the original staged registration; only ordinary
        # relationships write these physical columns. Logical locator is validated by service.
        sa.Column("file_path", sa.String(1024)),
        sa.Column("file_metadata", postgresql.JSONB(none_as_null=True)),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("caption", sa.Text()),
        sa.Column("source_page_id", sa.Uuid(), sa.ForeignKey("source_pages.id", ondelete="RESTRICT")),
        sa.Column("region", postgresql.JSONB(none_as_null=True)),
        sa.Column("order_index", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("question_id", "order_index", name="uq_question_assets_order"),
        sa.CheckConstraint("asset_type IN ('figure', 'table', 'diagram')", name="ck_question_asset_type"),
        sa.CheckConstraint("width > 0 AND height > 0", name="ck_question_asset_dimensions"),
        sa.CheckConstraint("order_index IS NULL OR order_index BETWEEN 1 AND 5", name="ck_question_asset_order"),
        sa.CheckConstraint("region IS NULL OR source_page_id IS NOT NULL", name="ck_question_asset_region_page"),
        sa.CheckConstraint("region IS NULL OR jsonb_typeof(region) = 'object'", name="ck_question_asset_region"),
        sa.CheckConstraint("file_metadata IS NULL OR jsonb_typeof(file_metadata) = 'object'", name="ck_question_asset_metadata"),
    )
    op.create_index("ix_question_assets_question_id", "question_assets", ["question_id"])
    op.create_index("ix_question_assets_source_page_id", "question_assets", ["source_page_id"])


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS(SELECT 1 FROM question_assets)")):
        raise RuntimeError("存在正式题图，须人工处置后才能降级。")
    if op.get_bind().scalar(sa.text("SELECT EXISTS(SELECT 1 FROM questions WHERE image_assessment IS NOT NULL)")):
        raise RuntimeError("存在图像核对证据，不能静默丢弃。")
    op.drop_table("question_assets")
    op.drop_constraint("ck_question_image_assessment", "questions", type_="check")
    op.drop_column("questions", "image_assessment")
