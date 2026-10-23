"""Represent explicitly local material indexing without inventing HTTP sends."""

import sqlalchemy as sa
from alembic import op

revision = "0034_material_index_operation"
down_revision = "0033_personal_cutover"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_operation_requests", "operation_runs", type_="check")
    op.create_check_constraint("ck_operation_requests", "operation_runs",
        "request_budget >= 0 AND actual_requests >= 0 AND actual_requests <= request_budget")


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM operation_runs WHERE request_budget=0)")):
        raise RuntimeError("Local operation history prevents downgrade; history must not be deleted")
    op.drop_constraint("ck_operation_requests", "operation_runs", type_="check")
    op.create_check_constraint("ck_operation_requests", "operation_runs",
        "request_budget > 0 AND actual_requests >= 0 AND actual_requests <= request_budget")
