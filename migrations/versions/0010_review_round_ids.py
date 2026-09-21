"""新增当前待复核轮次和复核审计轮次；历史数据保持 NULL。

Revision ID: 0010_review_round_ids
Revises: 0009_agent_runs
Create Date: 2026-09-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_review_round_ids"
down_revision: str | None = "0009_agent_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """不设置默认值或伪造历史轮次；旧行自然为 NULL。"""

    op.add_column(
        "grading_results",
        sa.Column("pending_review_round_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "review_records", sa.Column("review_round_id", sa.Uuid(), nullable=True)
    )
    op.create_index(
        "ix_grading_results_pending_review_round_id",
        "grading_results",
        ["pending_review_round_id"],
        unique=False,
    )


def downgrade() -> None:
    """只撤销本次索引与两列；原业务行保留。"""

    op.drop_index(
        "ix_grading_results_pending_review_round_id", table_name="grading_results"
    )
    op.drop_column("review_records", "review_round_id")
    op.drop_column("grading_results", "pending_review_round_id")
