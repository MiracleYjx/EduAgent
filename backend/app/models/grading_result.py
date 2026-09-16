"""单题评分结果持久化模型。

``GradingResult`` 保存一次自动阅卷对单题的正式产出，并在主观题上额外保留**当次**
置信度决策快照：

- ``confidence``：原评分置信度，独立保留。不使用决策字段替代，也不按当前配置重算历史结论。
- ``decision_*``：本次决策实际使用的置信度、阈值、是否复核、复核/评分状态与原因。
  六列必须全空或全非空，保证读回后能完整复原
  :class:`backend.app.schemas.grading.ConfidenceDecisionDTO`。
- 列表字段（正确要点、缺失知识点、知识点、学习建议、检索片段标识）保留原始顺序与重复，
  不做去重或排序。

缺结果的题目不会写入 0 分占位行：没有评分行本身即表示“尚未收到结果”，
避免把“缺失”伪装成“零分”。
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.domain.enums import (
    GradingStatus,
    QuestionType,
    ReviewStatus,
    ValidationStatus,
)
from backend.app.models.base import (
    Base,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    enum_type,
)

#: 决策快照列组：必须同时为空或同时存在，避免留下无法复原的半截决策。
DECISION_SNAPSHOT_COLUMNS = (
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


class GradingResult(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """单道题的评分结果：得分、理由、知识点、置信度与当次决策快照。"""

    __tablename__ = "grading_results"
    __table_args__ = (
        UniqueConstraint("answer_id", name="uq_grading_results_answer"),
        CheckConstraint("score >= 0", name="ck_grading_results_score_non_negative"),
        CheckConstraint("max_score > 0", name="ck_grading_results_max_score_positive"),
        CheckConstraint("score <= max_score", name="ck_grading_results_score_range"),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_grading_results_confidence_range",
        ),
        CheckConstraint(
            "decision_confidence IS NULL "
            "OR (decision_confidence >= 0 AND decision_confidence <= 1)",
            name="ck_grading_results_decision_confidence_range",
        ),
        CheckConstraint(
            "decision_threshold IS NULL "
            "OR (decision_threshold >= 0 AND decision_threshold <= 1)",
            name="ck_grading_results_decision_threshold_range",
        ),
        CheckConstraint(
            f"({_DECISION_SNAPSHOT_ALL_ABSENT}) OR ({_DECISION_SNAPSHOT_ALL_PRESENT})",
            name="ck_grading_results_decision_snapshot",
        ),
        Index(
            "ix_grading_results_submission_review_status",
            "submission_id",
            "review_status",
        ),
    )

    answer_id: Mapped[UUID] = mapped_column(
        ForeignKey("answers.id", ondelete="CASCADE"), nullable=False
    )
    submission_id: Mapped[UUID] = mapped_column(
        ForeignKey("submissions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question_type: Mapped[QuestionType] = mapped_column(
        enum_type(QuestionType, "question_type"), nullable=False
    )
    score: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    max_score: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    correct_points: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    missing_knowledge_points: Mapped[list[str]] = mapped_column(
        JSON, default=list, nullable=False
    )
    knowledge_points: Mapped[list[str]] = mapped_column(
        JSON, default=list, nullable=False
    )
    suggestions: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    retrieved_context_ids: Mapped[list[str]] = mapped_column(
        JSON, default=list, nullable=False
    )
    confidence: Mapped[float] = mapped_column(Float(53), nullable=False)
    validation_status: Mapped[ValidationStatus] = mapped_column(
        enum_type(ValidationStatus, "grading_validation_status"),
        default=ValidationStatus.PENDING,
        nullable=False,
    )
    review_status: Mapped[ReviewStatus] = mapped_column(
        enum_type(ReviewStatus, "grading_review_status"),
        default=ReviewStatus.NOT_REQUIRED,
        nullable=False,
    )
    decision_confidence: Mapped[float | None] = mapped_column(Float(53))
    decision_threshold: Mapped[float | None] = mapped_column(Float(53))
    decision_requires_review: Mapped[bool | None] = mapped_column(Boolean)
    decision_review_status: Mapped[ReviewStatus | None] = mapped_column(
        enum_type(ReviewStatus, "grading_decision_review_status")
    )
    decision_grading_status: Mapped[GradingStatus | None] = mapped_column(
        enum_type(GradingStatus, "grading_decision_grading_status")
    )
    decision_reason: Mapped[str | None] = mapped_column(Text)


__all__ = ["DECISION_SNAPSHOT_COLUMNS", "GradingResult"]
