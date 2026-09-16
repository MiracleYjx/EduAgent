"""新增学生诊断报告表

Revision ID: 0006_diagnosis_reports
Revises: 0005_grading_results
Create Date: 2026-09-16

本迁移只创建 ``diagnosis_reports`` 表及其索引、CHECK 约束与来源外键。

枚举以当时字面值固定记录（``native_enum=False`` + 显式 CHECK），不导入会变化的应用枚举类；
``downgrade()`` 只撤销本 revision 创建的表与索引。状态一致性 CHECK 固化
``Ready``（无 error_code、有 generated_at）、``Failed``（有 error_code、有 generated_at）
与 ``Stale``（有 generated_at）三种可落库形态，``Not Ready`` 不落库。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_diagnosis_reports"
down_revision: str | None = "0005_grading_results"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 诊断状态取值（当时的 DiagnosisStatus 字面值，含不落库的 Not Ready）。
DIAGNOSIS_STATUS_VALUES: tuple[str, ...] = (
    "Ready",
    "Not Ready",
    "Failed",
    "Stale",
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
    """创建学生诊断报告表及其索引。"""

    op.create_table(
        "diagnosis_reports",
        sa.Column("exam_result_id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("student_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            _enum_type(DIAGNOSIS_STATUS_VALUES, "diagnosis_status"),
            nullable=False,
        ),
        sa.Column("mastery_by_knowledge_point", sa.JSON(), nullable=False),
        sa.Column("weak_knowledge_points", sa.JSON(), nullable=False),
        sa.Column("error_reasons", sa.JSON(), nullable=False),
        sa.Column("learning_suggestions", sa.JSON(), nullable=False),
        sa.Column("insufficient_evidence_answer_ids", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=True),
        sa.Column("source_code", sa.String(length=64), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "source_exam_result_updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
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
            ["exam_result_id"], ["exam_results.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"], ["submissions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["student_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        _enum_check("status", DIAGNOSIS_STATUS_VALUES, "diagnosis_status"),
        sa.CheckConstraint(
            "attempt_count IS NULL OR attempt_count >= 0",
            name="ck_diagnosis_reports_attempt_count_non_negative",
        ),
        sa.CheckConstraint(
            "(status = 'Ready' AND error_code IS NULL AND generated_at IS NOT NULL) "
            "OR (status = 'Failed' AND error_code IS NOT NULL "
            "AND generated_at IS NOT NULL) "
            "OR (status = 'Stale' AND generated_at IS NOT NULL)",
            name="ck_diagnosis_reports_status_consistency",
        ),
    )
    op.create_index(
        op.f("ix_diagnosis_reports_exam_result_id"),
        "diagnosis_reports",
        ["exam_result_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_diagnosis_reports_submission_id"),
        "diagnosis_reports",
        ["submission_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_diagnosis_reports_student_id"),
        "diagnosis_reports",
        ["student_id"],
        unique=False,
    )
    op.create_index(
        "ix_diagnosis_reports_student_generated",
        "diagnosis_reports",
        ["student_id", "generated_at"],
        unique=False,
    )


def downgrade() -> None:
    """只撤销本 revision 创建的表与索引。"""

    op.drop_index(
        "ix_diagnosis_reports_student_generated", table_name="diagnosis_reports"
    )
    op.drop_index(
        op.f("ix_diagnosis_reports_student_id"), table_name="diagnosis_reports"
    )
    op.drop_index(
        op.f("ix_diagnosis_reports_submission_id"), table_name="diagnosis_reports"
    )
    op.drop_index(
        op.f("ix_diagnosis_reports_exam_result_id"), table_name="diagnosis_reports"
    )
    op.drop_table("diagnosis_reports")
