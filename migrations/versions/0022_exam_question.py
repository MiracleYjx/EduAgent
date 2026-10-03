"""T170 upgrade the existing exam relation without inventing historical facts."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022_exam_question"
down_revision: str | None = "0021_question_source_paper"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "exams",
        sa.Column(
            "assembly_constraints", postgresql.JSONB(none_as_null=True), nullable=True
        ),
    )
    op.create_check_constraint(
        "ck_exams_assembly_constraints_shape",
        "exams",
        "assembly_constraints IS NULL OR jsonb_typeof(assembly_constraints) = 'object'",
    )
    op.add_column("exam_questions", sa.Column("id", sa.Uuid(), nullable=True))
    op.add_column(
        "exam_questions", sa.Column("order_index", sa.Integer(), nullable=True)
    )
    op.add_column("exam_questions", sa.Column("score", sa.Numeric(8, 2), nullable=True))
    op.add_column(
        "exam_questions", sa.Column("base_score", sa.Numeric(8, 2), nullable=True)
    )
    op.add_column(
        "exam_questions",
        sa.Column(
            "published_knowledge_points",
            postgresql.JSONB(none_as_null=True),
            nullable=True,
        ),
    )
    op.add_column(
        "exam_questions",
        sa.Column("scoring_basis", postgresql.JSONB(none_as_null=True), nullable=True),
    )
    # This timestamps the new association identity; it is not a historical approval time.
    op.add_column(
        "exam_questions",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.execute(sa.text("""
        WITH ordered AS (
            SELECT eq.exam_id, eq.question_id,
                   row_number() OVER (
                       PARTITION BY eq.exam_id ORDER BY q.created_at, q.id
                   ) AS position
            FROM exam_questions eq JOIN questions q ON q.id=eq.question_id
        )
        UPDATE exam_questions eq
        SET id=gen_random_uuid(), order_index=ordered.position
        FROM ordered
        WHERE eq.exam_id=ordered.exam_id AND eq.question_id=ordered.question_id
    """))
    old_pk = sa.inspect(op.get_bind()).get_pk_constraint("exam_questions")["name"]
    op.drop_constraint(old_pk, "exam_questions", type_="primary")
    op.alter_column("exam_questions", "id", existing_type=sa.Uuid(), nullable=False)
    op.alter_column(
        "exam_questions", "order_index", existing_type=sa.Integer(), nullable=False
    )
    op.create_primary_key("exam_questions_pkey", "exam_questions", ["id"])
    op.create_unique_constraint(
        "uq_exam_questions_exam_question", "exam_questions", ["exam_id", "question_id"]
    )
    op.create_unique_constraint(
        "uq_exam_questions_exam_order", "exam_questions", ["exam_id", "order_index"]
    )
    op.create_index("ix_exam_questions_question_id", "exam_questions", ["question_id"])
    for name, expression in (
        ("ck_exam_questions_order", "order_index >= 1"),
        (
            "ck_exam_questions_score",
            "score IS NULL OR (score > 0 AND score <= 999999.99)",
        ),
        (
            "ck_exam_questions_base_score",
            "base_score IS NULL OR (base_score > 0 AND base_score <= 999999.99)",
        ),
        (
            "ck_exam_questions_knowledge_shape",
            "published_knowledge_points IS NULL OR jsonb_typeof(published_knowledge_points) = 'array'",
        ),
        (
            "ck_exam_questions_basis_shape",
            "scoring_basis IS NULL OR jsonb_typeof(scoring_basis) = 'object'",
        ),
    ):
        op.create_check_constraint(name, "exam_questions", expression)


def downgrade() -> None:
    bind = op.get_bind()
    has_facts = bind.scalar(sa.text("""
        SELECT EXISTS (
            SELECT 1 FROM exam_questions
            WHERE score IS NOT NULL OR base_score IS NOT NULL
               OR published_knowledge_points IS NOT NULL OR scoring_basis IS NOT NULL
        ) OR EXISTS (SELECT 1 FROM exams WHERE assembly_constraints IS NOT NULL)
        OR EXISTS (
            SELECT 1 FROM (
                SELECT eq.order_index, row_number() OVER (
                    PARTITION BY eq.exam_id ORDER BY q.created_at, q.id
                ) AS old_position
                FROM exam_questions eq JOIN questions q ON q.id=eq.question_id
            ) ordered WHERE order_index != old_position
        )
    """))
    if has_facts:
        raise RuntimeError(
            "已有考试内分值、评分依据、组卷意图或显式题序，降级会丢失真实数据；请先导出并明确处置。"
        )
    for name in (
        "ck_exam_questions_order",
        "ck_exam_questions_score",
        "ck_exam_questions_base_score",
        "ck_exam_questions_knowledge_shape",
        "ck_exam_questions_basis_shape",
    ):
        op.drop_constraint(name, "exam_questions", type_="check")
    op.drop_index("ix_exam_questions_question_id", table_name="exam_questions")
    op.drop_constraint("uq_exam_questions_exam_order", "exam_questions", type_="unique")
    op.drop_constraint(
        "uq_exam_questions_exam_question", "exam_questions", type_="unique"
    )
    op.drop_constraint("exam_questions_pkey", "exam_questions", type_="primary")
    op.create_primary_key(
        "exam_questions_pkey", "exam_questions", ["exam_id", "question_id"]
    )
    for name in (
        "created_at",
        "scoring_basis",
        "published_knowledge_points",
        "base_score",
        "score",
        "order_index",
        "id",
    ):
        op.drop_column("exam_questions", name)
    op.drop_constraint("ck_exams_assembly_constraints_shape", "exams", type_="check")
    op.drop_column("exams", "assembly_constraints")
