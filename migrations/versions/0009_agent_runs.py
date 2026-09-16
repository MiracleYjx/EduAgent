"""新增 Agent 运行追踪表

Revision ID: 0009_agent_runs
Revises: 0008_workflow_runs
Create Date: 2026-09-16

本迁移只创建 ``agent_runs`` 表及其索引、CHECK 约束与来源外键。

追踪状态使用当时固定的 Trace 字面值 ``success``/``failure``/``pending_review``（字符串列 + 显式
CHECK），不复用工作流枚举；结构化输出校验状态按当时的 ValidationStatus 字面值固定记录。
``downgrade()`` 只撤销本 revision 创建的表与索引。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_agent_runs"
down_revision: str | None = "0008_workflow_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Trace 状态取值（plan §7 的结构化事件约定）。
TRACE_STATUS_VALUES: tuple[str, ...] = ("success", "failure", "pending_review")
#: 结构化输出校验状态取值（当时的 ValidationStatus 字面值）。
VALIDATION_STATUS_VALUES: tuple[str, ...] = ("Pending", "Validated", "Failed")


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
    """创建 Agent 运行追踪表及其索引。"""

    op.create_table(
        "agent_runs",
        sa.Column("agent_type", sa.String(length=64), nullable=False),
        sa.Column("workflow_id", sa.String(length=64), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("input_summary", sa.Text(), nullable=True),
        sa.Column("output_summary", sa.Text(), nullable=True),
        sa.Column(
            "validation_status",
            _enum_type(VALIDATION_STATUS_VALUES, "agent_validation_status"),
            nullable=True,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("prompt_version", sa.String(length=64), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("error_retryable", sa.Boolean(), nullable=True),
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
            ["workflow_id"], ["workflow_runs.workflow_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        _enum_check(
            "validation_status",
            VALIDATION_STATUS_VALUES,
            "agent_validation_status",
        ),
        _enum_check("status", TRACE_STATUS_VALUES, "ck_agent_runs_status_trace_value"),
        sa.CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="ck_agent_runs_latency_non_negative",
        ),
        sa.CheckConstraint(
            "(input_tokens IS NULL OR input_tokens >= 0) "
            "AND (output_tokens IS NULL OR output_tokens >= 0) "
            "AND (total_tokens IS NULL OR total_tokens >= 0)",
            name="ck_agent_runs_token_counts_non_negative",
        ),
    )
    op.create_index(
        op.f("ix_agent_runs_workflow_id"), "agent_runs", ["workflow_id"], unique=False
    )
    op.create_index(
        op.f("ix_agent_runs_request_id"), "agent_runs", ["request_id"], unique=False
    )
    op.create_index(op.f("ix_agent_runs_user_id"), "agent_runs", ["user_id"], unique=False)


def downgrade() -> None:
    """只撤销本 revision 创建的表与索引。"""

    op.drop_index(op.f("ix_agent_runs_user_id"), table_name="agent_runs")
    op.drop_index(op.f("ix_agent_runs_request_id"), table_name="agent_runs")
    op.drop_index(op.f("ix_agent_runs_workflow_id"), table_name="agent_runs")
    op.drop_table("agent_runs")
