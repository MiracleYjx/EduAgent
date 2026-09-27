"""题目多轮退回时的教师修改意见。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.models.base import Base, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from backend.app.models.question import Question
    from backend.app.models.user import User


class QuestionRevisionComment(UUIDPrimaryKeyMixin, Base):
    """一条不可空且不超过 2000 字的退回意见。"""

    __tablename__ = "question_revision_comments"
    __table_args__ = (
        CheckConstraint(
            "length(trim(comment)) > 0 AND length(comment) <= 2000",
            name="ck_question_revision_comments_content_length",
        ),
        Index(
            "ix_question_revision_comments_question_commented_at",
            "question_id",
            "commented_at",
        ),
    )

    question_id: Mapped[UUID] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"), nullable=False
    )
    comment: Mapped[str] = mapped_column(Text, nullable=False)
    commented_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    commented_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
    )

    question: Mapped[Question] = relationship("Question")
    commenter: Mapped[User] = relationship("User")


__all__ = ["QuestionRevisionComment"]
