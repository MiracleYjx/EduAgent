"""知识库元数据模型。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import JSON, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from backend.app.models.course import Course
    from backend.app.models.document import Document


class KnowledgeBase(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """与课程绑定的可检索教学内容集合。"""

    __tablename__ = "knowledge_bases"
    __table_args__ = (
        UniqueConstraint("course_id", "name", name="uq_knowledge_bases_course_name"),
    )

    course_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), nullable=True, index=True
    )
    subject_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("subjects.id", ondelete="RESTRICT"), nullable=True, unique=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    #: Embedding 索引语义身份；历史 NULL 只读映射为 legacy_unknown，禁止回填。
    embedding_identity: Mapped[dict[str, Any] | None] = mapped_column(
        JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql"),
        nullable=True,
    )
    course: Mapped[Course] = relationship("Course", back_populates="knowledge_bases")
    documents: Mapped[list[Document]] = relationship(
        "Document", back_populates="knowledge_base", cascade="all, delete-orphan"
    )


__all__ = ["KnowledgeBase"]
