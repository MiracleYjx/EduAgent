"""题目引用的知识片段快照；资料删除后仍保留生成时依据。"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import (
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.models.base import Base, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from backend.app.models.document_chunk import DocumentChunk
    from backend.app.models.question import Question


class QuestionSourceChunk(UUIDPrimaryKeyMixin, Base):
    """记录题目生成时引用的片段身份、正文和检索证据。"""

    __tablename__ = "question_source_chunks"
    __table_args__ = (
        UniqueConstraint(
            "question_id", "chunk_id", name="uq_question_source_chunks_question_chunk"
        ),
        UniqueConstraint(
            "question_id",
            "source_order",
            name="uq_question_source_chunks_question_order",
        ),
        Index("ix_question_source_chunks_course_question", "course_id", "question_id"),
    )

    question_id: Mapped[UUID] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"), nullable=False
    )
    chunk_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    live_chunk_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("document_chunks.id", ondelete="SET NULL"), nullable=True
    )
    document_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    course_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    source_order: Mapped[int] = mapped_column(Integer, nullable=False)
    content_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    source_file: Mapped[str] = mapped_column(String(255), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    retrieval_rank: Mapped[int | None] = mapped_column(Integer)
    score_kind: Mapped[str | None] = mapped_column(String(32))
    score_value: Mapped[float | None] = mapped_column(Float)

    question: Mapped[Question] = relationship("Question")
    live_chunk: Mapped[DocumentChunk | None] = relationship("DocumentChunk")


__all__ = ["QuestionSourceChunk"]
