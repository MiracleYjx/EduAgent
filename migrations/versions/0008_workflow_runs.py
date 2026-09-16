"""新增阅卷工作流运行表

Revision ID: 0008_workflow_runs
Revises: 0007_review_records
Create Date: 2026-09-16

本迁移只创建 ``workflow_runs`` 表及其索引、约束与来源外键。

枚举以当时字面值固定记录（``native_enum=False`` + 显式 CHECK），不导入会变化的应用枚举类；
``downgrade()`` 只撤销本 revision 创建的表与索引。答卷删除级联运行记录；
当前答案与整卷结果指针在被引用行删除时清空（``SET NULL``），保留运行历史。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_workflow_runs"
down_revision: str | None = "0007_review_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 工作流状态取值（当时的 WorkflowStatus 字面值）。
WORKFLOW_STATUS_VALUES: tuple[str, ...] = (
    "Queued",
    "Running",
    "Paused",
    "Failed",
    "Completed",
)


def _enum_type(values: tuple[str, ...], name: str) -> sa.Enum:
    """构造记录固定字面值的非原生枚举类型（长度与模型的自动推导一致）。"""

    return sa.Enum(
        *values,
        name=name,
        native_enum=False,
        create_constraint=False,
        length=max(len(value) for value in values),
    )


def _enum_check(column: str, values: tuple[str, ...], name: str) -> sa.CheckConstraint:
    """构造与枚举取值一致的显式 CHECK 约束。"""

    literals = ", ".join(f"'{value}'" for value in values)
    return sa.CheckConstraint(f"{column} IN ({literals})", name=name)


def upgrade() -> None:
    """创建阅卷工作流运行表及其索引。"""

    op.create_table(
        "workflow_runs",
        sa.Column("workflow_id", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("current_node", sa.String(length=64), nullable=True),
        sa.Column("current_answer_id", sa.Uuid(), nullable=True),
        sa.Column(
            "status",
            _enum_type(WORKFLOW_STATUS_VALUES, "workflow_status"),
            nullable=False,
        ),
        sa.Column("checkpoint", sa.JSON(), nullable=True),
        sa.Column("pause_reason", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("resumable", sa.Boolean(), nullable=False),
        sa.Column("exam_result_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"], ["submissions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["current_answer_id"], ["answers.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["exam_result_id"], ["exam_results.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id", name="uq_workflow_runs_workflow_id"),
        _enum_check("status", WORKFLOW_STATUS_VALUES, "workflow_status"),
        sa.CheckConstraint(
            "retry_count >= 0",
            name="ck_workflow_runs_retry_count_non_negative",
        ),
    )
    op.create_index(
        op.f("ix_workflow_runs_request_id"),
        "workflow_runs",
        ["request_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_workflow_runs_submission_id"),
        "workflow_runs",
        ["submission_id"],
        unique=False,
    )


def downgrade() -> None:
    """只撤销本 revision 创建的表与索引。"""

    op.drop_index(op.f("ix_workflow_runs_submission_id"), table_name="workflow_runs")
    op.drop_index(op.f("ix_workflow_runs_request_id"), table_name="workflow_runs")
    op.drop_table("workflow_runs")
