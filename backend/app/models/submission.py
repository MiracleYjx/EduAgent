"""考试答卷持久化模型。"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.domain.enums import SubmissionStatus
from backend.app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, enum_type

if TYPE_CHECKING:
    from backend.app.models.answer import Answer
    from backend.app.models.exam import Exam
    from backend.app.models.user import User


class Submission(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """学生针对一次考试的答卷和评分生命周期。"""

    __tablename__ = "submissions"

    exam_id: Mapped[UUID] = mapped_column(
        ForeignKey("exams.id", ondelete="CASCADE"), nullable=False, index=True
    )
    student_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[SubmissionStatus] = mapped_column(
        enum_type(SubmissionStatus, "submission_status"),
        default=SubmissionStatus.DRAFT,
        nullable=False,
        index=True,
    )
    submitted_at: Mapped[datetime | None]
    graded_at: Mapped[datetime | None]
    reviewed_at: Mapped[datetime | None]
    exam: Mapped[Exam] = relationship("Exam", back_populates="submissions")
    student: Mapped[User] = relationship(
        "User", back_populates="submissions", foreign_keys=[student_id]
    )
    answers: Mapped[list[Answer]] = relationship(
        "Answer", back_populates="submission", cascade="all, delete-orphan"
    )


__all__ = ["Submission"]
