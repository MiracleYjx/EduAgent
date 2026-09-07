"""课程资料元数据模型。"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.domain.enums import DocumentStatus
from backend.app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, enum_type

if TYPE_CHECKING:
    from backend.app.models.course import Course
    from backend.app.models.knowledge_base import KnowledgeBase
    from backend.app.models.user import User


class Document(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """记录上传文件来源和摄取处理状态的资料实体。"""

    __tablename__ = "documents"

    course_id: Mapped[UUID] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    knowledge_base_id: Mapped[UUID] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    uploaded_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_format: Mapped[str] = mapped_column(String(32), nullable=False)
    storage_path: Mapped[str | None] = mapped_column(String(1024))
    status: Mapped[DocumentStatus] = mapped_column(
        enum_type(DocumentStatus, "document_status"),
        default=DocumentStatus.UPLOADED,
        nullable=False,
        index=True,
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    retryable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    course: Mapped[Course] = relationship("Course", back_populates="documents")
    knowledge_base: Mapped[KnowledgeBase] = relationship(
        "KnowledgeBase", back_populates="documents"
    )
    uploader: Mapped[User] = relationship(
        "User", back_populates="uploaded_documents", foreign_keys=[uploaded_by]
    )


__all__ = ["Document"]
