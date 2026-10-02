"""Preserve option JSON order and record unrecoverable legacy object ordering."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0018_options_json"
down_revision = "0017_extracted_order"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in ("extracted_questions", "questions"):
        previous = next(
            c["type"] for c in inspector.get_columns(table) if c["name"] == "options"
        )
        op.add_column(
            table,
            sa.Column(
                "order_preserved",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
        )
        if isinstance(previous, postgresql.JSONB):
            bind.execute(
                sa.text(
                    f"UPDATE {table} SET order_preserved=false "
                    "WHERE jsonb_typeof(options)='object' AND options <> '{}'::jsonb"
                )
            )
        op.alter_column(
            table,
            "options",
            existing_type=previous,
            type_=sa.JSON(),
            existing_nullable=True,
            postgresql_using="options::json",
        )
    # A JSON target cannot restore the unknown input order of an old JSONB source.
    bind.execute(
        sa.text(
            "UPDATE questions SET order_preserved=false WHERE id IN "
            "(SELECT question_id FROM extracted_questions WHERE NOT order_preserved)"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM extracted_questions WHERE NOT order_preserved "
            "OR (json_typeof(options)='object' AND options::jsonb <> '{}'::jsonb)) "
            "OR EXISTS (SELECT 1 FROM questions WHERE NOT order_preserved)"
        )
    ):
        raise RuntimeError(
            "存在选项顺序或历史丢序标记，不能静默丢失；请先导出并显式处置后降级。"
        )
    # Canonical 0002 questions.options was already JSON; keep it JSON.
    op.alter_column(
        "extracted_questions",
        "options",
        existing_type=sa.JSON(),
        type_=postgresql.JSONB(none_as_null=True),
        existing_nullable=True,
        postgresql_using="options::jsonb",
    )
    op.drop_column("questions", "order_preserved")
    op.drop_column("extracted_questions", "order_preserved")
