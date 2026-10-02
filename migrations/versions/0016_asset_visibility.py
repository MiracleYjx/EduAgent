"""T154: explicit student display grant, default closed."""
import sqlalchemy as sa
from alembic import op

revision = "0016_asset_visibility"
down_revision = "0015_question_assets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("question_assets", sa.Column("student_visible", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM question_assets WHERE student_visible)")):
        raise RuntimeError("存在学生展示授权，请先显式关闭后再降级，不能静默丢失已确认开关。")
    op.drop_column("question_assets", "student_visible")
