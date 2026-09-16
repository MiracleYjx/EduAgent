"""学生诊断报告持久化模型。

``DiagnosisReport`` 保存基于**最终整卷结果**生成的诊断：

- ``exam_result_id``：必须是由 ``submission_id`` 定位到的真实 ``ExamResult.id``，不得写入
  ``DiagnosisService`` 的 ``exam-result:{submission_id}`` 应用层标识。
  ``uq_exam_results_submission`` 保证一份答卷只有一条当前结果，因此该外键唯一确定来源。
- ``source_exam_result_updated_at``：生成时实际消费的 ``ExamResultDTO.aggregated_at``，
  用于判断报告是否过期；禁止使用 ORM ``updated_at`` 冒充。
- ``status`` 复用 :class:`backend.app.schemas.grading.DiagnosisStatus`，并由表级 CHECK
  固化状态一致性：``Ready`` 不得携带 ``error_code`` 且必须有 ``generated_at``；
  ``Failed`` 必须携带 ``error_code``；``Stale`` 必须保留 ``generated_at``。
  ``Not Ready`` 只在服务响应中返回、不落库——该状态没有最终整卷结果，外键无法满足 NOT NULL。
- ``insufficient_evidence_answer_ids``、``error_code``、``retryable``、``source_code``、
  ``attempt_count`` 如实记录来源信息，未知时保存 NULL，不填假值。
- JSON 内的 Decimal（掌握度、得分）统一编码为定点字符串（两位小数），读回后仍是字符串，
  不用浮点近似值掩盖精度。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.models.base import (
    Base,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    enum_type,
)
from backend.app.models.exam_result import ExamResult
from backend.app.models.submission import Submission
from backend.app.models.user import User
from backend.app.schemas.grading import DiagnosisStatus


class DiagnosisReport(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一份答卷的学生诊断报告：平台计算字段与来源信息。"""

    __tablename__ = "diagnosis_reports"
    __table_args__ = (
        CheckConstraint(
            "(status = 'Ready' AND error_code IS NULL AND generated_at IS NOT NULL) "
            "OR (status = 'Failed' AND error_code IS NOT NULL "
            "AND generated_at IS NOT NULL) "
            "OR (status = 'Stale' AND generated_at IS NOT NULL)",
            name="ck_diagnosis_reports_status_consistency",
        ),
        CheckConstraint(
            "attempt_count IS NULL OR attempt_count >= 0",
            name="ck_diagnosis_reports_attempt_count_non_negative",
        ),
        Index("ix_diagnosis_reports_student_generated", "student_id", "generated_at"),
    )

    exam_result_id: Mapped[UUID] = mapped_column(
        ForeignKey("exam_results.id", ondelete="CASCADE"), nullable=False, index=True
    )
    submission_id: Mapped[UUID] = mapped_column(
        ForeignKey("submissions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    student_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[DiagnosisStatus] = mapped_column(
        enum_type(DiagnosisStatus, "diagnosis_status"), nullable=False
    )
    mastery_by_knowledge_point: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    weak_knowledge_points: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    error_reasons: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    learning_suggestions: Mapped[list[str]] = mapped_column(
        JSON, default=list, nullable=False
    )
    insufficient_evidence_answer_ids: Mapped[list[str]] = mapped_column(
        JSON, default=list, nullable=False
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    retryable: Mapped[bool | None] = mapped_column(Boolean)
    source_code: Mapped[str | None] = mapped_column(String(64))
    attempt_count: Mapped[int | None] = mapped_column(Integer)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_exam_result_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    exam_result: Mapped[ExamResult] = relationship(ExamResult)
    submission: Mapped[Submission] = relationship(Submission)
    student: Mapped[User] = relationship(User)


__all__ = ["DiagnosisReport"]
