"""题目生成调用的实际来源元数据。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.models.base import Base

if TYPE_CHECKING:
    from backend.app.models.question import Question


class QuestionGenerationMetadata(Base):
    """一题一份生成记录；未知模型版本保持 NULL。"""

    __tablename__ = "question_generation_metadata"

    question_id: Mapped[UUID] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"), primary_key=True
    )
    request_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    model_version: Mapped[str | None] = mapped_column(String(128))
    provider_name: Mapped[str] = mapped_column(String(64), nullable=False)
    retrieval_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
    )

    question: Mapped[Question] = relationship("Question")


__all__ = ["QuestionGenerationMetadata"]
