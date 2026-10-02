"""Persistent extracted question order; legacy unknown order stays NULL."""

import sqlalchemy as sa
from alembic import op

revision = "0017_extracted_order"
down_revision = "0016_asset_visibility"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "extracted_questions", sa.Column("order_index", sa.Integer(), nullable=True)
    )
    op.create_check_constraint(
        "ck_extracted_order",
        "extracted_questions",
        "order_index IS NULL OR order_index >= 1",
    )
    op.create_unique_constraint(
        "uq_extracted_import_order",
        "extracted_questions",
        ["paper_import_id", "order_index"],
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM extracted_questions WHERE order_index IS NOT NULL)"
        )
    ):
        raise RuntimeError(
            "存在已确认题序，不能静默丢失；请先导出并显式清除题序后降级。"
        )
    op.drop_constraint(
        "uq_extracted_import_order", "extracted_questions", type_="unique"
    )
    op.drop_constraint("ck_extracted_order", "extracted_questions", type_="check")
    op.drop_column("extracted_questions", "order_index")
