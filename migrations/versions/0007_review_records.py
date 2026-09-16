"""新增教师复核记录表

Revision ID: 0007_review_records
Revises: 0006_diagnosis_reports
Create Date: 2026-09-16

本迁移只创建 ``review_records`` 表及其索引、CHECK 约束与来源外键。

枚举以当时字面值固定记录（``native_enum=False`` + 显式 CHECK），不导入会变化的应用枚举类；
``downgrade()`` 只撤销本 revision 创建的表与索引。表级 CHECK 只允许
``Confirmed``/``Modified``/``Re-grade`` 三类教师操作，并保证原始分数与复核后分数非负；
``final_knowledge_points`` 为空时保存为 SQL NULL（``none_as_null=True``）。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_review_records"
down_revision: str | None = "0006_diagnosis_reports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 复核状态取值（当时的 ReviewStatus 字面值）。
REVIEW_STATUS_VALUES: tuple[str, ...] = (
    "Not Required",
    "Pending Review",
    "Confirmed",
    "Modified",
    "Re-grade",
    "Final",
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
    """创建教师复核记录表及其索引。"""

    op.create_table(
        "review_records",
        sa.Column("grading_result_id", sa.Uuid(), nullable=False),
        sa.Column("reviewer_id", sa.Uuid(), nullable=False),
        sa.Column(
            "decision",
            _enum_type(REVIEW_STATUS_VALUES, "review_decision"),
            nullable=False,
        ),
        sa.Column("original_score", sa.Numeric(precision=8, scale=2), nullable=False),
        sa.Column("original_reason", sa.Text(), nullable=False),
        sa.Column("original_knowledge_points", sa.JSON(), nullable=False),
        sa.Column("final_score", sa.Numeric(precision=8, scale=2), nullable=True),
        sa.Column("final_reason", sa.Text(), nullable=True),
        sa.Column("final_knowledge_points", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
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
            ["grading_result_id"], ["grading_results.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["reviewer_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        _enum_check("decision", REVIEW_STATUS_VALUES, "review_decision"),
        sa.CheckConstraint(
            "decision IN ('Confirmed', 'Modified', 'Re-grade')",
            name="ck_review_records_decision_operable",
        ),
        sa.CheckConstraint(
            "original_score >= 0",
            name="ck_review_records_original_score_non_negative",
        ),
        sa.CheckConstraint(
            "final_score IS NULL OR final_score >= 0",
            name="ck_review_records_final_score_non_negative",
        ),
    )
    op.create_index(
        op.f("ix_review_records_grading_result_id"),
        "review_records",
        ["grading_result_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_review_records_reviewer_id"),
        "review_records",
        ["reviewer_id"],
        unique=False,
    )


def downgrade() -> None:
    """只撤销本 revision 创建的表与索引。"""

    op.drop_index(op.f("ix_review_records_reviewer_id"), table_name="review_records")
    op.drop_index(
        op.f("ix_review_records_grading_result_id"), table_name="review_records"
    )
    op.drop_table("review_records")
