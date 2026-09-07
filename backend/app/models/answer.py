"""单题答案持久化模型。"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import JSON, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.domain.enums import AnswerStatus
from backend.app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, enum_type

if TYPE_CHECKING:
    from backend.app.models.question import Question
    from backend.app.models.submission import Submission


class Answer(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """学生针对单道题的答案及处理状态。"""

    __tablename__ = "answers"
    __table_args__ = (
        UniqueConstraint(
            "submission_id", "question_id", name="uq_answers_submission_question"
        ),
    )

    submission_id: Mapped[UUID] = mapped_column(
        ForeignKey("submissions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question_id: Mapped[UUID] = mapped_column(
        ForeignKey("questions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    content: Mapped[str | list[str] | dict[str, str] | None] = mapped_column(JSON)
    status: Mapped[AnswerStatus] = mapped_column(
        enum_type(AnswerStatus, "answer_status"),
        default=AnswerStatus.DRAFT,
        nullable=False,
        index=True,
    )
    submission: Mapped[Submission] = relationship(
        "Submission", back_populates="answers"
    )
    question: Mapped[Question] = relationship("Question")


__all__ = ["Answer"]
