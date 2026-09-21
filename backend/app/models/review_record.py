"""教师复核记录持久化模型。

``ReviewRecord`` 保存教师对单题评分结果实际执行的一次操作及其前后事实：

- ``decision`` 复用 :class:`backend.app.domain.enums.ReviewStatus`，但表级 CHECK 只允许
  本表记录可操作取值 ``Confirmed``/``Modified``/``Re-grade``。``Not Required``、
  ``Pending Review`` 与 ``Final`` 是结果状态而不是教师操作，不得写入本表。
- 前后事实同时落库：``original_score``/``original_reason``/``original_knowledge_points``
  与 ``final_score``/``final_reason``/``final_knowledge_points``。后续评分行更新只改评分行，
  不会改写复核记录中的历史事实。
- ``Re-grade`` 表示请求重新评分：尚无新结论时 ``final_score`` 与 ``final_reason`` 允许为空，
  且不得填 0 冒充结论；0 分是合法的新结论，与空值可区分。
- ``final_knowledge_points`` 使用 ``JSON(none_as_null=True)``，为空时保存为 SQL NULL 而不是
  JSON ``null`` 文本，避免被状态检查误判或掩盖“未修改知识点”与“修改为空”。
- ``comment`` 是操作备注，与理由字段分离；审查时间由 ``created_at`` 提供。
- ``review_round_id`` 保存该操作消费的 Pending Review 轮次；NULL 表示历史/无轮次记录。
- ``reviewer_id`` 外键只证明用户存在；教师身份与资源授权由后续复核服务负责，本批不新增权限业务。
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    Numeric,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.domain.enums import ReviewStatus
from backend.app.models.base import (
    Base,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    enum_type,
)
from backend.app.models.grading_result import GradingResult
from backend.app.models.user import User


class ReviewRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一次教师复核操作：操作类型、修改前后分数/理由/知识点与备注。"""

    __tablename__ = "review_records"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('Confirmed', 'Modified', 'Re-grade')",
            name="ck_review_records_decision_operable",
        ),
        CheckConstraint(
            "original_score >= 0",
            name="ck_review_records_original_score_non_negative",
        ),
        CheckConstraint(
            "final_score IS NULL OR final_score >= 0",
            name="ck_review_records_final_score_non_negative",
        ),
    )

    grading_result_id: Mapped[UUID] = mapped_column(
        ForeignKey("grading_results.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    reviewer_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    decision: Mapped[ReviewStatus] = mapped_column(
        enum_type(ReviewStatus, "review_decision"), nullable=False
    )
    review_round_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    original_score: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    original_reason: Mapped[str] = mapped_column(Text, nullable=False)
    original_knowledge_points: Mapped[list[str]] = mapped_column(
        JSON, default=list, nullable=False
    )
    final_score: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    final_reason: Mapped[str | None] = mapped_column(Text)
    final_knowledge_points: Mapped[list[str] | None] = mapped_column(
        JSON(none_as_null=True)
    )
    comment: Mapped[str | None] = mapped_column(Text)
    grading_result: Mapped[GradingResult] = relationship(GradingResult)
    reviewer: Mapped[User] = relationship(User)


__all__ = ["ReviewRecord"]
