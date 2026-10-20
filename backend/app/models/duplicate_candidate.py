"""User decisions bind two actual content revisions; similarity never merges."""
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class DuplicateCandidate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "duplicate_candidates"
    __table_args__ = (
        UniqueConstraint("subject_id", "left_id", "right_id", "left_revision", "right_revision"),
        ForeignKeyConstraint(["left_id", "subject_id"], ["questions.id", "questions.subject_id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["right_id", "subject_id"], ["questions.id", "questions.subject_id"], ondelete="RESTRICT"),
        CheckConstraint("left_id < right_id", name="ck_duplicate_pair_order"),
        CheckConstraint("left_revision > 0 AND right_revision > 0", name="ck_duplicate_revisions"),
        CheckConstraint("decision IN ('pending','same','different')", name="ck_duplicate_decision"),
    )
    subject_id: Mapped[UUID] = mapped_column(Uuid)
    left_id: Mapped[UUID] = mapped_column(Uuid)
    right_id: Mapped[UUID] = mapped_column(Uuid)
    left_revision: Mapped[int]
    right_revision: Mapped[int]
    decision: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
