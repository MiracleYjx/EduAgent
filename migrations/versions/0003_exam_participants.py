"""新增考试学生分配表，保留无分配考试的历史开放语义。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_exam_participants"
down_revision: str | None = "7c00941eb6e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建分配记录、外键、唯一约束和学生查询索引。"""

    op.create_table(
        "exam_participants",
        sa.Column("exam_id", sa.Uuid(), nullable=False),
        sa.Column("student_id", sa.Uuid(), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["exam_id"], ["exams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["student_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("exam_id", "student_id", name="uq_exam_participants_exam_student"),
    )
    op.create_index("ix_exam_participants_student_id", "exam_participants", ["student_id"])


def downgrade() -> None:
    """仅移除本次新增的考试分配表。"""

    op.drop_index("ix_exam_participants_student_id", table_name="exam_participants")
    op.drop_table("exam_participants")
