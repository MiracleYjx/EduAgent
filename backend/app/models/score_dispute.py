"""A dispute accepts an explicit current grading round, never a replacement score."""

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ScoreDispute(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "score_disputes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["disputed_attempt_id", "answer_id"],
            ["grade_attempts.id", "grade_attempts.answer_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["accepted_attempt_id", "answer_id"],
            ["grade_attempts.id", "grade_attempts.answer_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "state IN ('pending','awaiting_acceptance','resolved')",
            name="ck_dispute_state",
        ),
        CheckConstraint(
            "(state='resolved' AND accepted_attempt_id IS NOT NULL) OR (state<>'resolved' AND accepted_attempt_id IS NULL)",
            name="ck_dispute_acceptance",
        ),
    )
    answer_id: Mapped[UUID] = mapped_column(
        ForeignKey("practice_answers.id", ondelete="RESTRICT")
    )
    disputed_attempt_id: Mapped[UUID] = mapped_column(Uuid)
    state: Mapped[str] = mapped_column(String(32), server_default="pending")
    reason: Mapped[str]
    accepted_attempt_id: Mapped[UUID | None] = mapped_column(Uuid)
