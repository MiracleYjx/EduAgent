"""T177 retain the actual scoring association; historical results stay unknown."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_grading_exam_question"
down_revision: str | None = "0022_exam_question"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "grading_results", sa.Column("exam_question_id", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        "fk_grading_results_exam_question",
        "grading_results",
        "exam_questions",
        ["exam_question_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_grading_results_exam_question_id", "grading_results", ["exam_question_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_grading_results_exam_question_id", table_name="grading_results")
    op.drop_constraint(
        "fk_grading_results_exam_question", "grading_results", type_="foreignkey"
    )
    op.drop_column("grading_results", "exam_question_id")
