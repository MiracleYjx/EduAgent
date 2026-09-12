"""考试与被分配学生之间的持久化关联。"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.models.base import Base, UUIDPrimaryKeyMixin


class ExamParticipant(UUIDPrimaryKeyMixin, Base):
    """记录考试分配；没有任何分配的考试按兼容策略向全体学生开放。"""

    __tablename__ = "exam_participants"
    __table_args__ = (
        UniqueConstraint("exam_id", "student_id", name="uq_exam_participants_exam_student"),
    )

    exam_id: Mapped[UUID] = mapped_column(
        ForeignKey("exams.id", ondelete="CASCADE"), nullable=False
    )
    student_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
    )


__all__ = ["ExamParticipant"]
