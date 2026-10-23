"""Persist execution identity for explicit, fenced orphan coordination."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0035_operation_executor"
down_revision = "0034_material_index_operation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("operation_runs", sa.Column("execution_id", sa.Uuid(), nullable=True))
    op.add_column("operation_runs", sa.Column("executor_identity", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM operation_runs WHERE execution_id IS NOT NULL)")):
        raise RuntimeError("Execution history prevents downgrade; retain the original evidence")
    op.drop_column("operation_runs", "executor_identity")
    op.drop_column("operation_runs", "execution_id")
