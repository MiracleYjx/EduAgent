"""T163 durable semantic reports; old questions retain unknown validation."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020_content_validation"
down_revision: str | None = "0019_chapter_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "questions",
        sa.Column(
            "validation_revision", sa.BigInteger(), server_default="0", nullable=False
        ),
    )
    op.create_check_constraint(
        "ck_question_validation_revision", "questions", "validation_revision >= 0"
    )
    op.create_table(
        "question_validation_results",
        sa.Column("id", sa.Uuid(), nullable=False, primary_key=True),
        sa.Column(
            "question_id",
            sa.Uuid(),
            sa.ForeignKey("questions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("input_revision", sa.BigInteger(), nullable=False),
        sa.Column("run_no", sa.BigInteger(), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("input_refs", postgresql.JSONB(), nullable=False),
        sa.Column("checks", postgresql.JSONB(), nullable=True),
        sa.Column("issues", postgresql.JSONB(), nullable=True),
        sa.Column("error", postgresql.JSONB(), nullable=True),
        sa.Column("executor_kind", sa.String(16), nullable=False),
        sa.Column("executor_name", sa.String(64), nullable=False),
        sa.Column(
            "requested_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "agent_run_id",
            sa.Uuid(),
            sa.ForeignKey("agent_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("provenance", postgresql.JSONB(), nullable=False),
        sa.Column(
            "manual_dispositions",
            postgresql.JSONB(),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("question_id", "run_no", name="uq_question_validation_run"),
        sa.CheckConstraint(
            "input_revision >= 0 AND run_no > 0", name="ck_question_validation_counters"
        ),
        sa.CheckConstraint(
            "outcome IN ('running', 'passed', 'failed', 'technical_error')",
            name="ck_question_validation_outcome",
        ),
        sa.CheckConstraint(
            "executor_kind IN ('service', 'agent')",
            name="ck_question_validation_executor",
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at",
            name="ck_question_validation_times",
        ),
        sa.CheckConstraint(
            "(outcome = 'running' AND completed_at IS NULL AND checks IS NULL AND issues IS NULL AND error IS NULL) OR (outcome IN ('passed', 'failed') AND completed_at IS NOT NULL AND checks IS NOT NULL AND issues IS NOT NULL AND error IS NULL) OR (outcome = 'technical_error' AND completed_at IS NOT NULL AND error IS NOT NULL AND checks IS NULL AND issues IS NULL)",
            name="ck_question_validation_result_state",
        ),
        *[
            sa.CheckConstraint(
                f"jsonb_typeof({field}) = 'object'",
                name=f"ck_question_validation_{field}",
            )
            for field in ("input_refs", "provenance")
        ],
        sa.CheckConstraint(
            "jsonb_typeof(manual_dispositions) = 'array'",
            name="ck_question_validation_dispositions",
        ),
        *[
            sa.CheckConstraint(
                f"{field} IS NULL OR jsonb_typeof({field}) = '{kind}'",
                name=f"ck_question_validation_{field}",
            )
            for field, kind in (
                ("checks", "array"),
                ("issues", "array"),
                ("error", "object"),
            )
        ],
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM question_validation_results) OR EXISTS(SELECT 1 FROM questions WHERE validation_revision <> 0)"
        )
    ):
        raise RuntimeError(
            "Content-validation evidence must be preserved before downgrade."
        )
    op.drop_table("question_validation_results")
    op.drop_constraint("ck_question_validation_revision", "questions", type_="check")
    op.drop_column("questions", "validation_revision")
