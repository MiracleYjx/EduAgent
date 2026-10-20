"""Persist structured subjective-grading feedback without rewriting fixed input."""

from alembic import op

revision = "0031_personal_grading_feedback"
down_revision = "0030_personal_duplicates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE grade_attempts ADD COLUMN feedback JSONB"
    )


def downgrade() -> None:
    raise RuntimeError("Personal grading history requires an explicit preservation plan")
