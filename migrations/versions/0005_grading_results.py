"""新增单题评分结果与整卷结果表

Revision ID: 0005_grading_results
Revises: 0004_document_chunks
Create Date: 2026-09-16

本迁移只创建本 revision 负责的对象：``grading_results``、``exam_results``
两张表及其索引、唯一约束、CHECK 约束与来源外键。

枚举以当时字面值固定记录（``native_enum=False`` + 显式 CHECK），不导入会变化的应用枚举类；
``downgrade()`` 只撤销本 revision 创建的表与索引，不触碰其它 revision 的对象。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_grading_results"
down_revision: str | None = "0004_document_chunks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 题型取值（当时的 QuestionType 字面值）。
QUESTION_TYPE_VALUES: tuple[str, ...] = (
    "SINGLE_CHOICE",
    "MULTIPLE_CHOICE",
    "TRUE_FALSE",
    "FILL_BLANK",
    "SHORT_ANSWER",
    "ESSAY",
)
#: 结构化校验状态取值（当时的 ValidationStatus 字面值）。
VALIDATION_STATUS_VALUES: tuple[str, ...] = ("Pending", "Validated", "Failed")
#: 人工复核状态取值（当时的 ReviewStatus 字面值）。
REVIEW_STATUS_VALUES: tuple[str, ...] = (
    "Not Required",
    "Pending Review",
    "Confirmed",
    "Modified",
    "Re-grade",
    "Final",
)
#: 单题评分状态取值（当时的 GradingStatus 字面值）。
GRADING_STATUS_VALUES: tuple[str, ...] = (
    "Pending",
    "Validated",
    "Accepted",
    "Pending Review",
    "Final",
    "Failed",
)
#: 整卷结果状态取值（当时的 ExamResultStatus 字面值）。
EXAM_RESULT_STATUS_VALUES: tuple[str, ...] = (
    "Pending",
    "Pending Review",
    "Final",
    "Failed",
)

#: 决策快照列组：必须同时为空或同时存在。
DECISION_SNAPSHOT_COLUMNS: tuple[str, ...] = (
    "decision_confidence",
    "decision_threshold",
    "decision_requires_review",
    "decision_review_status",
    "decision_grading_status",
    "decision_reason",
)

_DECISION_SNAPSHOT_ALL_ABSENT = " AND ".join(
    f"{column} IS NULL" for column in DECISION_SNAPSHOT_COLUMNS
)
_DECISION_SNAPSHOT_ALL_PRESENT = " AND ".join(
    f"{column} IS NOT NULL" for column in DECISION_SNAPSHOT_COLUMNS
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
    """创建单题评分结果表与整卷结果表及其索引。"""

    op.create_table(
        "grading_results",
        sa.Column("answer_id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column(
            "question_type",
            _enum_type(QUESTION_TYPE_VALUES, "question_type"),
            nullable=False,
        ),
        sa.Column("score", sa.Numeric(precision=8, scale=2), nullable=False),
        sa.Column("max_score", sa.Numeric(precision=8, scale=2), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("correct_points", sa.JSON(), nullable=False),
        sa.Column("missing_knowledge_points", sa.JSON(), nullable=False),
        sa.Column("knowledge_points", sa.JSON(), nullable=False),
        sa.Column("suggestions", sa.JSON(), nullable=False),
        sa.Column("retrieved_context_ids", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Float(precision=53), nullable=False),
        sa.Column(
            "validation_status",
            _enum_type(VALIDATION_STATUS_VALUES, "grading_validation_status"),
            nullable=False,
        ),
        sa.Column(
            "review_status",
            _enum_type(REVIEW_STATUS_VALUES, "grading_review_status"),
            nullable=False,
        ),
        sa.Column("decision_confidence", sa.Float(precision=53), nullable=True),
        sa.Column("decision_threshold", sa.Float(precision=53), nullable=True),
        sa.Column("decision_requires_review", sa.Boolean(), nullable=True),
        sa.Column(
            "decision_review_status",
            _enum_type(REVIEW_STATUS_VALUES, "grading_decision_review_status"),
            nullable=True,
        ),
        sa.Column(
            "decision_grading_status",
            _enum_type(GRADING_STATUS_VALUES, "grading_decision_grading_status"),
            nullable=True,
        ),
        sa.Column("decision_reason", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(["answer_id"], ["answers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["submission_id"], ["submissions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("answer_id", name="uq_grading_results_answer"),
        _enum_check("question_type", QUESTION_TYPE_VALUES, "question_type"),
        _enum_check(
            "validation_status",
            VALIDATION_STATUS_VALUES,
            "grading_validation_status",
        ),
        _enum_check("review_status", REVIEW_STATUS_VALUES, "grading_review_status"),
        _enum_check(
            "decision_review_status",
            REVIEW_STATUS_VALUES,
            "grading_decision_review_status",
        ),
        _enum_check(
            "decision_grading_status",
            GRADING_STATUS_VALUES,
            "grading_decision_grading_status",
        ),
        sa.CheckConstraint(
            "score >= 0", name="ck_grading_results_score_non_negative"
        ),
        sa.CheckConstraint(
            "max_score > 0", name="ck_grading_results_max_score_positive"
        ),
        sa.CheckConstraint("score <= max_score", name="ck_grading_results_score_range"),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_grading_results_confidence_range",
        ),
        sa.CheckConstraint(
            "decision_confidence IS NULL "
            "OR (decision_confidence >= 0 AND decision_confidence <= 1)",
            name="ck_grading_results_decision_confidence_range",
        ),
        sa.CheckConstraint(
            "decision_threshold IS NULL "
            "OR (decision_threshold >= 0 AND decision_threshold <= 1)",
            name="ck_grading_results_decision_threshold_range",
        ),
        sa.CheckConstraint(
            f"({_DECISION_SNAPSHOT_ALL_ABSENT}) OR ({_DECISION_SNAPSHOT_ALL_PRESENT})",
            name="ck_grading_results_decision_snapshot",
        ),
    )
    op.create_index(
        op.f("ix_grading_results_submission_id"),
        "grading_results",
        ["submission_id"],
        unique=False,
    )
    op.create_index(
        "ix_grading_results_submission_review_status",
        "grading_results",
        ["submission_id", "review_status"],
        unique=False,
    )
    op.create_table(
        "exam_results",
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("exam_id", sa.Uuid(), nullable=False),
        sa.Column("student_id", sa.Uuid(), nullable=False),
        sa.Column(
            "result_status",
            _enum_type(EXAM_RESULT_STATUS_VALUES, "exam_result_status"),
            nullable=False,
        ),
        sa.Column("is_final", sa.Boolean(), nullable=False),
        sa.Column(
            "final_total_score", sa.Numeric(precision=10, scale=2), nullable=True
        ),
        sa.Column(
            "confirmed_subtotal", sa.Numeric(precision=10, scale=2), nullable=False
        ),
        sa.Column("total_max_score", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("aggregated_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.ForeignKeyConstraint(["exam_id"], ["exams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["student_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("submission_id", name="uq_exam_results_submission"),
        _enum_check(
            "result_status", EXAM_RESULT_STATUS_VALUES, "exam_result_status"
        ),
        sa.CheckConstraint(
            "total_max_score > 0", name="ck_exam_results_total_max_score_positive"
        ),
        sa.CheckConstraint(
            "confirmed_subtotal >= 0",
            name="ck_exam_results_confirmed_subtotal_non_negative",
        ),
        sa.CheckConstraint(
            "final_total_score IS NULL OR final_total_score >= 0",
            name="ck_exam_results_final_total_score_non_negative",
        ),
        sa.CheckConstraint(
            "(is_final IS TRUE AND result_status = 'Final' "
            "AND final_total_score IS NOT NULL) "
            "OR (is_final IS FALSE AND result_status <> 'Final' "
            "AND final_total_score IS NULL)",
            name="ck_exam_results_final_state",
        ),
    )
    op.create_index(
        "ix_exam_results_exam_student",
        "exam_results",
        ["exam_id", "student_id"],
        unique=False,
    )


def downgrade() -> None:
    """只撤销本 revision 创建的表与索引。"""

    op.drop_index("ix_exam_results_exam_student", table_name="exam_results")
    op.drop_table("exam_results")
    op.drop_index(
        "ix_grading_results_submission_review_status", table_name="grading_results"
    )
    op.drop_index(
        op.f("ix_grading_results_submission_id"), table_name="grading_results"
    )
    op.drop_table("grading_results")
