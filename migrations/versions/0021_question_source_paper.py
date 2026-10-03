"""T165 parent question sources; do not infer historical source paths."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_question_source_paper"
down_revision: str | None = "0020_content_validation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "question_source_papers",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "derived_question_id",
            sa.Uuid(),
            sa.ForeignKey("questions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "source_question_id",
            sa.Uuid(),
            sa.ForeignKey("questions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("adaptation_type", sa.String(16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "derived_question_id",
            "source_question_id",
            name="uq_question_source_papers_pair",
        ),
        sa.CheckConstraint(
            "derived_question_id != source_question_id",
            name="ck_question_source_papers_not_self",
        ),
        sa.CheckConstraint(
            "adaptation_type IN ('rewrite', 'translate', 'extend')",
            name="ck_question_source_papers_type",
        ),
    )
    op.create_index(
        "ix_question_source_papers_source_question_id",
        "question_source_papers",
        ["source_question_id"],
    )


def downgrade() -> None:
    op.drop_table("question_source_papers")
