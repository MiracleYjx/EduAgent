"""T161 stable course chapters and nullable confirmed chunk locations."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019_chapter_scope"
down_revision: str | None = "0018_options_json"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chapters",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "course_id",
            sa.Uuid(),
            sa.ForeignKey("courses.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("sections", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column(
            "confirmed_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(sections) = 'array'", name="ck_chapters_sections_array"
        ),
    )
    op.create_index("ix_chapters_course_id", "chapters", ["course_id"])
    op.add_column("document_chunks", sa.Column("chapter_id", sa.Uuid(), nullable=True))
    op.add_column(
        "document_chunks", sa.Column("section_order", sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        "fk_document_chunks_chapter_id_chapters",
        "document_chunks",
        "chapters",
        ["chapter_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_document_chunks_section_chapter",
        "document_chunks",
        "section_order IS NULL OR (section_order > 0 AND chapter_id IS NOT NULL)",
    )
    op.create_index(
        "ix_document_chunks_course_chapter_section",
        "document_chunks",
        ["course_id", "chapter_id", "section_order"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM chapters) OR EXISTS(SELECT 1 FROM document_chunks WHERE chapter_id IS NOT NULL OR section_order IS NOT NULL)"
        )
    ):
        raise RuntimeError("Confirmed chapter data must be migrated before downgrade.")
    op.drop_index(
        "ix_document_chunks_course_chapter_section", table_name="document_chunks"
    )
    op.drop_constraint(
        "ck_document_chunks_section_chapter", "document_chunks", type_="check"
    )
    op.drop_constraint(
        "fk_document_chunks_chapter_id_chapters", "document_chunks", type_="foreignkey"
    )
    op.drop_column("document_chunks", "section_order")
    op.drop_column("document_chunks", "chapter_id")
    op.drop_index("ix_chapters_course_id", table_name="chapters")
    op.drop_table("chapters")
