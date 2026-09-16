"""整卷评分结果持久化模型。

``ExamResult`` 保存一份答卷的**当次汇总事实**，而不是可随时重算的推导值：

- ``total_max_score``、``confirmed_subtotal``、``final_total_score``：汇总当时的整卷满分、
  已确认小计与最终总分。题库允许修改题目分值
  （``backend/app/services/question_service.py`` 的 ``_update``），因此整卷满分必须落库，
  不得按当前题目定义重建。
- ``aggregated_at``：本次汇总时间，也是诊断报告判断“是否过期”的来源时间。
- ``result_status`` 复用读模型枚举 :class:`backend.app.schemas.grading.ExamResultStatus`，
  与 ``SubmissionStatus``/``GradingStatus``/``ReviewStatus`` 分离，避免把“任务执行完成”
  当作“成绩已最终确认”。``is_final``、``result_status`` 与 ``final_total_score``
  由表级 CHECK 强制三态一致。

不落库的推导字段：题序来自考试题目顺序；单题 ``grading_status``/``counted``/
``effective_score``/``requires_review``、各类计数与
``missing``/``failed``/``not_validated_answer_ids`` 由评分行与考试题目集合重建。
``grading_results`` 只读关系用于按答卷读取逐题结果，不提供写回路径。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, foreign, mapped_column, relationship

from backend.app.models.base import (
    Base,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    enum_type,
)
from backend.app.models.grading_result import GradingResult
from backend.app.schemas.grading import ExamResultStatus


class ExamResult(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一份答卷的整卷结果：状态、最终成绩与当次汇总事实。"""

    __tablename__ = "exam_results"
    __table_args__ = (
        UniqueConstraint("submission_id", name="uq_exam_results_submission"),
        CheckConstraint(
            "total_max_score > 0", name="ck_exam_results_total_max_score_positive"
        ),
        CheckConstraint(
            "confirmed_subtotal >= 0",
            name="ck_exam_results_confirmed_subtotal_non_negative",
        ),
        CheckConstraint(
            "final_total_score IS NULL OR final_total_score >= 0",
            name="ck_exam_results_final_total_score_non_negative",
        ),
        CheckConstraint(
            "(is_final IS TRUE AND result_status = 'Final' "
            "AND final_total_score IS NOT NULL) "
            "OR (is_final IS FALSE AND result_status <> 'Final' "
            "AND final_total_score IS NULL)",
            name="ck_exam_results_final_state",
        ),
        Index("ix_exam_results_exam_student", "exam_id", "student_id"),
    )

    submission_id: Mapped[UUID] = mapped_column(
        ForeignKey("submissions.id", ondelete="CASCADE"), nullable=False
    )
    exam_id: Mapped[UUID] = mapped_column(
        ForeignKey("exams.id", ondelete="CASCADE"), nullable=False
    )
    student_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    result_status: Mapped[ExamResultStatus] = mapped_column(
        enum_type(ExamResultStatus, "exam_result_status"), nullable=False
    )
    is_final: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    final_total_score: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    confirmed_subtotal: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    total_max_score: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    aggregated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    grading_results: Mapped[list[GradingResult]] = relationship(
        GradingResult,
        primaryjoin=lambda: ExamResult.submission_id
        == foreign(GradingResult.submission_id),
        viewonly=True,
    )


__all__ = ["ExamResult"]
